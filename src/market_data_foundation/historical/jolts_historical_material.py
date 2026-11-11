"""Resolve past JOLTS references from dated official releases, never month offsets.

Called only by explicit material acquisition. Calendar dates authorize archive
URLs; the archive title supplies the reference month. GET never discovers one.
"""

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError
from market_data_foundation.historical.macro_release_material import parse_material
from market_data_foundation.historical.macro_release_records import instant

ICS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
TITLES = {
    "job openings and labor turnover survey",
    "job openings and labor turnover",
}
OUTSIDE = "jolts_material_outside_requested_range"


def targets(snapshot):
    value = snapshot.get("targets", [])
    return (
        [
            {**row, "reference_month": row.get("reference_month") or month}
            for month, row in value.items()
        ]
        if isinstance(value, dict)
        else value
    )


def candidates(snapshot, start, end, now):
    """Unmapped, unambiguous confirmed events only; current vetoes win."""
    calendar = snapshot.get("calendar", {})
    if calendar.get("source") != ICS_URL or unresolved_cancellation(snapshot):
        return []
    events = calendar.get("events", {})
    events = list(events.values()) if isinstance(events, dict) else events
    target_rows = targets(snapshot)
    blocked_refs = {t.get("reference_month") for t in target_rows if vetoed(t)}
    if start == end and start in blocked_refs:
        return []
    by_uid = {}
    for event in events:
        by_uid.setdefault(event.get("uid"), {})[json.dumps(event, sort_keys=True)] = (
            event
        )
    # Exact repetitions are harmless; conflicting versions of the same UID
    # must first be reconciled by the official calendar parser.
    events = [next(iter(rows.values())) for rows in by_uid.values() if len(rows) == 1]
    days = {}
    for event in events:
        at = event.get("release_at")
        if not at:
            continue
        try:
            day = instant(at).astimezone(ZoneInfo("America/New_York")).date()
        except ValueError, TypeError:
            continue
        days.setdefault(day, []).append(event)
    result = []
    for day, rows in days.items():
        # Archive URLs identify a date, not one of several competing UIDs/times.
        if len(rows) != 1:
            continue
        event = rows[0]
        uid, at = event.get("uid"), event["release_at"]
        if (
            not uid
            or event.get("summary", "").casefold() not in TITLES
            or event.get("status") != "CONFIRMED"
            or vetoed(event)
            or instant(at) > now
            or any(
                vetoed(t) and (t.get("ics_uid") == uid or t.get("release_at") == at)
                for t in target_rows
            )
            or (
                event.get("reference_month")
                and not start <= event["reference_month"] <= end
            )
        ):
            continue
        result.append(
            dict(
                source_url=f"https://www.bls.gov/news.release/archives/jolts_{day:%m%d%Y}.htm",
                source_date=day.isoformat(),
                calendar_uid=uid,
                release_at=at,
                expected_reference=event.get("reference_month"),
                blocked_references=sorted(r for r in blocked_refs if r),
                selection_basis="official_calendar_date_and_verified_archive_reference",
            )
        )
    return sorted(result, key=lambda r: r["release_at"], reverse=True)


def vetoed(row):
    return bool(
        row.get("calendar_execution_blocked")
        or row.get("absent_from_latest_calendar")
        or row.get("cancelled")
        or row.get("state") == "calendar_removed"
        or str(row.get("status", row.get("publication_status", ""))).upper()
        in {"CANCELLED", "CANCELED", "WITHDRAWN", "TBA", "TENTATIVE"}
        or any(
            word
            in str(row.get("calendar_validation", ""))
            + str(row.get("classification", ""))
            for word in ("conflict", "removed", "cancel")
        )
    )


def unresolved_cancellation(snapshot):
    events = snapshot.get("calendar", {}).get("events", {})
    events = list(events.values()) if isinstance(events, dict) else events
    return any(
        str(e.get("status", "")).upper() in {"CANCELLED", "CANCELED", "WITHDRAWN"}
        and not e.get("release_at")
        and not e.get("reference_month")
        for e in events
    )


def confirmed_selection(candidate, raw, start, end):
    """The existing strict parser proves national scope, title and archive date."""
    doc = parse_material(raw, candidate["source_url"], "jolts")
    reference = doc["reference_month"]
    if (
        doc["source_version"]["value"] != candidate["source_date"]
        or (
            candidate.get("expected_reference")
            and candidate["expected_reference"] != reference
        )
        or reference in candidate.get("blocked_references", [])
    ):
        raise CpiTimeSeriesError("release_material_calendar_identity_mismatch")
    if not start <= reference <= end:
        return None
    return {
        **candidate,
        "reference_month": reference,
        "reference_months": doc["reference_months"],
    }


def _write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def _locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CpiTimeSeriesError("release_material_acquisition_busy") from None
        yield


def _state(path):
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except ValueError, OSError:
        raise CpiTimeSeriesError("release_material_state_unknown") from None


def original(directory, candidate, fetcher, clock, *, state_path, allow_fetch=True):
    """Bound discovery to the existing three attempts; shared BLS admission stays
    inside the normal fetcher. An interrupted reservation is never refunded.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(candidate["source_url"].encode()).hexdigest()
    body, metadata = directory / (key + ".html"), directory / (key + ".json")
    state_path = Path(state_path)
    from market_data_foundation.historical.bls_timeseries_profile import JOLTS_PROFILE

    ledger_key = "|".join(
        (
            JOLTS_PROFILE.schema,
            "jolts",
            candidate["source_date"],
            candidate["source_url"],
        )
    )
    with (
        _locked(directory / (key + ".lock")),
        _locked(state_path.with_suffix(state_path.suffix + ".lock")),
    ):
        old = _state(metadata)
        ledger = _state(state_path)
        previous = ledger.get(ledger_key, {})
        if old.get("state") == "retained":
            try:
                raw = body.read_bytes()
            except OSError:
                raise CpiTimeSeriesError("jolts_original_integrity_invalid") from None
            if old.get("source_url") != candidate["source_url"] or hashlib.sha256(
                raw
            ).hexdigest() != old.get("sha256"):
                raise CpiTimeSeriesError("jolts_original_integrity_invalid")
            return raw, False, old
        now = instant(clock())
        for record in (old, previous):
            if record.get("attempts", 0) >= 3 or (
                record.get("next_attempt_at")
                and instant(record["next_attempt_at"]) > now
            ):
                raise CpiTimeSeriesError(
                    record.get("reason") or "release_material_cooldown"
                )
        if not allow_fetch:
            raise CpiTimeSeriesError("jolts_material_discovery_limit")
        record = dict(
            source_url=candidate["source_url"],
            attempts=max(old.get("attempts", 0), previous.get("attempts", 0)) + 1,
            state="running",
            attempted_at=now.isoformat(),
            next_attempt_at=(now + timedelta(hours=1)).isoformat(),
        )
        _write(metadata, json.dumps(record).encode())
        ledger[ledger_key] = {**previous, **record}
        _write(state_path, json.dumps(ledger).encode())
        try:
            raw = fetcher(candidate["source_url"], "jolts")
            # Refuse denial pages or a mismatched archive before caching bytes.
            confirmed_selection(candidate, raw, "0001-01", "9999-12")
            _write(body, raw)
            record.update(
                state="retained",
                sha256=hashlib.sha256(raw).hexdigest(),
                obtained_at=instant(clock()).isoformat(),
                next_attempt_at=None,
            )
        except Exception as error:
            reason = getattr(
                error, "classification", "release_material_transport_or_storage_failed"
            )
            record.update(
                state="partial",
                reason=reason,
                next_attempt_at=(
                    now + timedelta(days=1)
                    if "403" in reason
                    else now + timedelta(hours=1)
                ).isoformat(),
            )
            _write(metadata, json.dumps(record).encode())
            ledger[ledger_key] = {**previous, **record}
            _write(state_path, json.dumps(ledger).encode())
            raise
        _write(metadata, json.dumps(record).encode())
        ledger[ledger_key] = {**previous, **record, "state": "original_cached"}
        _write(state_path, json.dumps(ledger).encode())
        return raw, True, record


def retain_original(directory, candidate, raw, *, obtained_at, evidence_locator, clock):
    """Explicit offline transfer of a verified original; never a provider request.

    The source acquisition time stays separate from this local cache admission
    and from the later business-store confirmation/availability.
    """
    confirmed_selection(candidate, raw, "0001-01", "9999-12")
    obtained, now = instant(obtained_at), instant(clock())
    if obtained > now or not evidence_locator:
        raise CpiTimeSeriesError("jolts_original_provenance_invalid")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(candidate["source_url"].encode()).hexdigest()
    with _locked(directory / (key + ".lock")):
        meta = directory / (key + ".json")
        old = _state(meta)
        sha = hashlib.sha256(raw).hexdigest()
        if old.get("state") == "retained":
            if old.get("sha256") != sha:
                raise CpiTimeSeriesError("jolts_original_integrity_invalid")
            return old
        record = dict(
            old,
            state="retained",
            source_url=candidate["source_url"],
            sha256=sha,
            obtained_at=obtained.isoformat(),
            cached_at=now.isoformat(),
            acquisition_method="reused_retained_official_body",
            evidence_locator=evidence_locator,
        )
        _write(directory / (key + ".html"), raw)
        _write(meta, json.dumps(record).encode())
        return record


def capture(
    store, snapshot, start, end, state_path, *, fetcher, batch_ids, selection=None
):
    from market_data_foundation.historical.macro_release_acquisition import (
        MaterialAcquirer,
    )
    from market_data_foundation.historical.macro_release_records import ReleaseRecords

    repo = ReleaseRecords(store)
    eligible = candidates(snapshot, start, end, instant(repo.clock()))
    blocked = {row.get("reference_month") for row in targets(snapshot) if vetoed(row)}
    events = snapshot.get("calendar", {}).get("events", {})
    events = list(events.values()) if isinstance(events, dict) else events
    if selection and (
        unresolved_cancellation(snapshot)
        or selection["reference_month"] in blocked
        or (
            any(e.get("uid") == selection.get("calendar_uid") for e in events)
            and not any(
                e["calendar_uid"] == selection.get("calendar_uid")
                and e["source_date"] == selection["source_date"]
                for e in eligible
            )
        )
    ):
        selection = None
        eligible = []
    rows = (
        [dict(selection, expected_reference=selection["reference_month"])]
        if selection
        else eligible
    )
    base = dict(
        state="partial",
        material_outcomes=[],
        event_ids=[],
        evidence_ids=[],
        numeric_batch_ids=list(batch_ids),
        provider_requests=0,
        transport_calls=0,
        reasons=["no_due_official_material_selected"],
    )
    for candidate in rows:
        calls = []

        def counted(url, family):
            calls.append(url)
            return fetcher(url, family)

        try:
            directory = Path(state_path).with_suffix(".originals")
            key = hashlib.sha256(candidate["source_url"].encode()).hexdigest()
            if not (directory / (key + ".json")).exists():
                # A prior normal acquisition may predate the discovery cache.
                # Reuse its asset-scoped proof, never refetch just to build cache.
                try:
                    known = repo._rows(instant(repo.clock()))
                except CpiTimeSeriesError as error:
                    if error.classification != "release_schema_not_prepared":
                        raise
                    known = []
                for record in known:
                    ref = record["reference_month"]
                    if (
                        record["event_id"] == "bls:jolts:" + candidate["source_date"]
                        and record["source_url"] == candidate["source_url"]
                        and start <= ref <= end
                        and ref not in blocked
                        and record.get("retention", {}).get("acquisition_method")
                        in {
                            "bounded_official_transport",
                            "retained_official_material_cache",
                        }
                    ):
                        result = MaterialAcquirer(state_path).capture(
                            store,
                            dict(candidate, reference_month=ref),
                            batch_ids=batch_ids,
                        )
                        result["provider_requests"] += base["provider_requests"]
                        result["transport_calls"] += base["transport_calls"]
                        result["original_obtained_at"] = record.get(
                            "retention", {}
                        ).get("original_obtained_at", record["obtained_at"])
                        return result
            raw, fetched, provenance = original(
                directory,
                candidate,
                counted,
                repo.clock,
                state_path=state_path,
                allow_fetch=base["provider_requests"] < 3,
            )
            selection = confirmed_selection(candidate, raw, start, end)
            if selection is None:
                base["reasons"] = [OUTSIDE]
                continue
            result = MaterialAcquirer(
                state_path,
                cached_original=raw,
                original_obtained_at=provenance["obtained_at"],
                acquisition_method="retained_official_material_cache",
            ).capture(store, selection, batch_ids=batch_ids)
            result["provider_requests"] = base["provider_requests"] + len(calls)
            result["transport_calls"] = base["transport_calls"] + len(calls)
            result["evidence_origin"] = (
                "current_bounded_official_transport"
                if fetched
                else "retained_official_material_cache"
            )
            result["original_obtained_at"] = provenance["obtained_at"]
            result["original_evidence_locator"] = provenance.get("evidence_locator")
            return result
        except Exception as error:
            base["reasons"] = [
                getattr(error, "classification", "release_material_acquisition_failed")
            ]
            return base
        finally:
            base["provider_requests"] += len(calls)
            base["transport_calls"] += len(calls)
    return base
