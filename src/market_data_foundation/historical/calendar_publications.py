"""Pure official calendar projections; never schedule work or infer a month."""

from copy import deepcopy
from datetime import datetime
from urllib.parse import urlsplit


def bls_calendar_publications(state):
    calendar = state["calendar"]
    references = [*calendar.get("display_history", [])]
    cached = calendar.get("reference_calendar") or {}
    references += [
        {**release, "reference_month_evidence": {**cached, **release}}
        for release in cached.get("releases", [])
    ]
    events = list(calendar.get("events", {}).values())
    result = []
    used = set()
    unresolved_cancellation = False
    for event in events:
        month = event.get("reference_month")
        evidence = event.get("reference_month_evidence")
        if not month:
            matches = [
                ref
                for ref in references
                if (
                    ref.get("uid") == event["uid"]
                    or (
                        event.get("summary")
                        and event.get("release_at")
                        and ref["release_at"] == event["release_at"]
                    )
                )
            ]
            months = {ref["reference_month"] for ref in matches}
            if len(months) != 1:
                if (
                    not matches
                    and event["status"] == "CANCELLED"
                    and not event.get("release_at")
                ):
                    unresolved_cancellation = True
                for ref in matches:
                    used.add(ref["reference_month"])
                    result.append({**ref, "publication_status": "unknown"})
                continue
            month = next(iter(months))
            evidence = matches[0]["reference_month_evidence"]
        used.add(month)
        result.append(
            {
                "reference_month": month,
                "uid": event["uid"],
                "release_at": event.get("release_at"),
                "publication_status": "cancelled"
                if event["status"] == "CANCELLED"
                else "unknown"
                if event["status"] == "TENTATIVE"
                or event.get("mapping_status") == "release_time_pending"
                else "scheduled",
                "reference_month_evidence": evidence,
                "acquired_at": calendar.get("acquired_at", calendar.get("fetched_at")),
            }
        )
    for row in result:
        identities = {
            candidate.get("uid")
            for candidate in result
            if candidate["reference_month"] == row["reference_month"]
            and candidate.get("uid")
        }
        if len(identities) > 1 and row["publication_status"] != "cancelled":
            row["publication_status"] = "unknown"
    result += [
        {
            **ref,
            "acquired_at": calendar.get("acquired_at")
            if unresolved_cancellation
            else (ref.get("reference_month_evidence") or {}).get(
                "acquired_at", calendar.get("acquired_at")
            ),
            "publication_status": "unknown" if unresolved_cancellation else "scheduled",
        }
        for ref in references
        if ref["reference_month"] not in used
    ]
    for row in result:
        if (
            state["targets"]
            .get(row["reference_month"], {})
            .get("calendar_execution_blocked")
        ):
            row["calendar_execution_blocked"] = True
    return result


def _instant(value):
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return stamp if stamp.utcoffset() is not None else None
    except ValueError, TypeError:
        return None


def _visible(row, cache, cutoff, basis):
    if cutoff is None:
        return True
    evidence = row.get("reference_month_evidence") or {}
    stamps = [
        row.get("acquired_at", cache.get("acquired_at", cache.get("fetched_at"))),
        evidence.get("acquired_at"),
    ]
    if basis == "available":
        stamps += [
            row.get("available_at", cache.get("available_at")),
            evidence.get("available_at"),
            row.get("imported_at", cache.get("imported_at")),
            evidence.get("imported_at"),
        ]
    stamps = [value for value in stamps if value is not None]
    return bool(stamps) and all(
        (stamp := _instant(value)) is not None and stamp <= cutoff for value in stamps
    )


def publication_events(snapshot, official_origin, *, as_of=None, as_of_basis=None):
    """Select a recorded view before projecting; GET never migrates the cache."""
    cutoff = (
        as_of
        if isinstance(as_of, datetime)
        else datetime.fromisoformat(as_of)
        if as_of
        else None
    )
    if cutoff is not None and cutoff.utcoffset() is None:
        return {}
    history = snapshot.get("calendar_publication_history")
    if cutoff is not None and history is not None:
        validate_publication_history(history)
        eligible = [
            (index, record)
            for index, record in enumerate(history)
            if _instant(record["observed_at"]) <= cutoff
            and (
                as_of_basis != "available" or _instant(record["available_at"]) <= cutoff
            )
        ]
        if not eligible:
            return {}
        _, record = max(
            eligible, key=lambda pair: (_instant(pair[1]["observed_at"]), pair[0])
        )
        if record.get("valid_through") and cutoff > _instant(record["valid_through"]):
            # Restored legacy captures prove only their explicitly verified span.
            # Do not fall back to an older unbounded view across an evidence gap.
            return {}
        snapshot = record["projection"]
    cache = snapshot.get("calendar", {})
    calendar = [
        row
        for row in snapshot.get("calendar_publications", [])
        if _visible(row, cache, cutoff, as_of_basis)
    ]
    # A later, cutoff-ineligible calendar must not suppress an eligible target.
    authoritative = {row.get("reference_month") for row in calendar}
    rows = [
        row
        for row in snapshot.get("targets", [])
        if row.get("reference_month") not in authoritative
        or row.get("state") == "calendar_removed"
        or row.get("calendar_cancelled_after_completion")
    ] + calendar
    groups = {}
    for row in rows:
        evidence = row.get("reference_month_evidence") or {}
        if not _visible(row, cache, cutoff, as_of_basis):
            continue
        month = row.get("reference_month")
        source = evidence.get("source") or evidence.get("evidence_url")
        if (
            not month
            or not isinstance(source, str)
            or urlsplit(source).netloc != urlsplit(official_origin).netloc
            or urlsplit(source).scheme != "https"
            or evidence.get("reference_month", month) != month
        ):
            continue
        status = row.get("publication_status", "scheduled")
        if row.get("state") == "calendar_removed" or row.get(
            "calendar_cancelled_after_completion"
        ):
            status = "cancelled"
        if row.get("calendar_execution_blocked") and status != "cancelled":
            status = "unknown"
        if status not in {"scheduled", "published", "cancelled", "unknown"}:
            status = "unknown"
        at = row.get("calendar_release_at", row.get("release_at"))
        try:
            if datetime.fromisoformat(at).utcoffset() is None:
                at = None
        except ValueError, TypeError:
            at = None
        if not at and status not in {"cancelled", "unknown"}:
            continue
        groups.setdefault(month, []).append(
            {
                "reference_month": month,
                "publication_time": at,
                "time": at,
                "status": status,
                "evidence": {
                    key: evidence[key]
                    for key in ("source", "evidence_url", "sha256")
                    if key in evidence
                },
            }
        )
    result = {}
    for month, candidates in groups.items():
        event = dict(candidates[0])
        if any(row["status"] == "cancelled" for row in candidates):
            event["status"] = "cancelled"
        elif (
            any(row["status"] == "unknown" for row in candidates)
            or len(
                {
                    datetime.fromisoformat(row["time"])
                    for row in candidates
                    if row["time"]
                }
            )
            > 1
        ):
            event["status"] = "unknown"
        if event["status"] in {"cancelled", "unknown"}:
            event.update(time=None, publication_time=None)
        result[month] = event
    return result


def publication_projection(snapshot):
    """Freeze calendar evidence, excluding mutable numeric/execution results."""
    keys = {
        "reference_month",
        "reference_month_evidence",
        "release_at",
        "calendar_release_at",
        "calendar_execution_blocked",
        "calendar_cancelled_after_completion",
        "state",
        "acquired_at",
        "available_at",
        "imported_at",
    }
    return deepcopy(
        {
            "calendar": snapshot.get("calendar", {}),
            "calendar_publications": snapshot.get("calendar_publications", []),
            "targets": [
                {key: value for key, value in row.items() if key in keys}
                for row in snapshot.get("targets", [])
            ],
        }
    )


def validate_publication_history(history):
    if not isinstance(history, list):
        raise ValueError("calendar_publication_history_invalid")
    previous = None
    for row in history:
        if (
            not isinstance(row, dict)
            or _instant(row.get("observed_at")) is None
            or _instant(row.get("available_at")) is None
            or _instant(row["available_at"]) < _instant(row["observed_at"])
            or (
                "valid_through" in row
                and (
                    _instant(row["valid_through"]) is None
                    or _instant(row["valid_through"]) < _instant(row["available_at"])
                )
            )
            or not isinstance(row.get("projection"), dict)
            or "calendar_publication_history" in row["projection"]
        ):
            raise ValueError("calendar_publication_history_invalid")
        projection = row["projection"]
        if (
            not isinstance(projection.get("calendar", {}), dict)
            or any(
                not isinstance(projection.get(key, []), list)
                or any(not isinstance(value, dict) for value in projection.get(key, []))
                for key in ("targets", "calendar_publications")
            )
            or (previous is not None and _instant(row["observed_at"]) < previous)
        ):
            raise ValueError("calendar_publication_history_invalid")
        previous = _instant(row["observed_at"])


def record_publication_view(state, snapshot, observed_at, official_origin):
    """Append the view actually assembled now, never backdate a legacy import.

    Raw acquisition/import times remain in the projection and are independently
    checked on reads. Without an older recorded view, old history stays unknown.
    Repeated semantically identical refreshes retain the first complete view.
    """
    stamp = _instant(observed_at)
    if stamp is None:
        raise ValueError("calendar_publication_time_invalid")
    history = state.setdefault("calendar_publication_history", [])
    validate_publication_history(history)
    if history and stamp < _instant(history[-1]["observed_at"]):
        raise ValueError("calendar_publication_clock_reversal")
    projection = publication_projection(snapshot)
    if (
        history
        and "valid_through" not in history[-1]
        and publication_events(history[-1]["projection"], official_origin)
        == publication_events(projection, official_origin)
    ):
        return False
    history.append(
        {
            "observed_at": stamp.isoformat(),
            "available_at": stamp.isoformat(),
            "timing_basis": "recorded_projection_not_official_publication_time",
            "projection": projection,
        }
    )
    return True
