"""Pure official calendar projections; never schedule work or infer a month."""

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
            "publication_status": "unknown" if unresolved_cancellation else "scheduled",
        }
        for ref in references
        if ref["reference_month"] not in used
    ]
    return result


def publication_events(snapshot, official_origin):
    """Calendar cache supersedes execution snapshots; ambiguity withholds time."""
    calendar = snapshot.get("calendar_publications", [])
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
