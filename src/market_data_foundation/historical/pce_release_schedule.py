"""BEA Personal Income and Outlays calendar and bounded, opt-in collection.

Only explicitly named reference months create collection targets. Calendar
refresh and data collection have separate budgets; no BLS parser or quota is used.
"""

import calendar
import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from market_data_foundation.historical.bls_timeseries_profile import PCE_PROFILE
from market_data_foundation.historical.cpi_release_schedule import (
    CpiScheduleError,
    _aware,
    _instant,
    _write_atomic,
    now_utc,
)
from market_data_foundation.historical.cpi_timeseries import (
    CpiTimeSeriesError,
    month_at,
    month_number,
    month_range,
)
from market_data_foundation.historical.cpi_timeseries_source import (
    CpiSourceError,
    validate_month,
)

CALENDAR_URL = "https://www.bea.gov/news/schedule/full"
ICS_URL = "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"
MAX_CALENDAR_BYTES = 2_000_000
RETRY_SECONDS = (15, 30, 60, 120, 300, 900, 1800, 3600)
CALENDAR_RETRY_SECONDS = (300, 900, 3600, 21600)
PceScheduleError = CpiScheduleError
_MONTHS = {name.lower(): n for n, name in enumerate(calendar.month_name) if name}
_MONTH_PATTERN = "(?:" + "|".join(calendar.month_name[1:]) + ")"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise PceScheduleError("bea_calendar_redirect_rejected")


def fetch_official_calendar():
    """One bounded public request; never loads or transmits an API key."""
    request = Request(ICS_URL, headers={"Accept": "text/calendar"})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=15) as response:
            raw = response.read(MAX_CALENDAR_BYTES + 1)
    except PceScheduleError:
        raise
    except Exception:
        raise PceScheduleError("bea_calendar_fetch_failed") from None
    if not raw or len(raw) > MAX_CALENDAR_BYTES:
        raise PceScheduleError("bea_calendar_payload_invalid")
    return raw


def _reference_months(title):
    # Accept only complete, explicit month/year clauses after the exact release
    # family. Shared trailing years support BEA's two-month release titles.
    match = re.fullmatch(r"Personal Income and Outlays\s*,\s*(.+)", title, re.I)
    if match is None:
        return ()
    clause = match[1].strip()
    shared = re.fullmatch(
        rf"({_MONTH_PATTERN})(?:\s+and\s+({_MONTH_PATTERN}))?\s+(\d{{4}})",
        clause,
        re.I,
    )
    if shared:
        return tuple(
            f"{shared[3]}-{_MONTHS[m.lower()]:02d}" for m in shared.group(1, 2) if m
        )
    explicit = re.fullmatch(
        rf"({_MONTH_PATTERN})\s+(\d{{4}})\s+and\s+"
        rf"({_MONTH_PATTERN})\s+(\d{{4}})",
        clause,
        re.I,
    )
    if explicit:
        return (
            f"{explicit[2]}-{_MONTHS[explicit[1].lower()]:02d}",
            f"{explicit[4]}-{_MONTHS[explicit[3].lower()]:02d}",
        )
    return ()


def _ics_time(key, value):
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    if "TZID=America/New_York" not in key:
        raise PceScheduleError("bea_calendar_timezone_unverified")
    local = datetime.strptime(value, "%Y%m%dT%H%M%S")
    zone = ZoneInfo("America/New_York")
    first, second = local.replace(tzinfo=zone), local.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise PceScheduleError("bea_calendar_local_time_ambiguous")
    if first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != local:
        raise PceScheduleError("bea_calendar_local_time_invalid")
    return first.astimezone(UTC)


def parse_official_ics(raw):
    """Return BEA events, including unmapped events for honest UI visibility."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_CALENDAR_BYTES:
        raise PceScheduleError("bea_calendar_payload_invalid")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        raise PceScheduleError("bea_calendar_payload_invalid") from None
    text = re.sub(r"\r?\n[ \t]", "", text)
    if not text.strip().startswith("BEGIN:VCALENDAR") or not text.strip().endswith(
        "END:VCALENDAR"
    ):
        raise PceScheduleError("bea_calendar_payload_invalid")
    events, active = {}, None
    digest = hashlib.sha256(raw).hexdigest()
    try:
        for line in text.splitlines():
            if line == "BEGIN:VEVENT":
                if active is not None:
                    raise ValueError
                active = {}
            elif line == "END:VEVENT":
                if active is None:
                    raise ValueError
                title = active.get("SUMMARY", "").replace(r"\,", ",")
                if title.lower().startswith("personal income and outlays"):
                    uid = active["UID"]
                    key, value = active["start"]
                    at = _ics_time(key, value)
                    months = _reference_months(title)
                    for month in months:
                        validate_month(month)
                    event = {
                        "uid": uid,
                        "summary": title,
                        "release_at": at.isoformat(),
                        "reference_months": list(months),
                        "status": active.get("STATUS", "CONFIRMED"),
                        "sequence": int(active.get("SEQUENCE", "0")),
                        "sha256": digest,
                        "source": ICS_URL,
                        "classification": None
                        if months
                        else "calendar_reference_month_unverified",
                    }
                    if uid in events and events[uid] != event:
                        raise ValueError
                    events[uid] = event
                active = None
            elif active is not None:
                key, value = line.split(":", 1)
                name = key.split(";", 1)[0]
                if name == "DTSTART":
                    if "start" in active:
                        raise ValueError
                    active["start"] = (key, value)
                elif name in {"SUMMARY", "UID", "STATUS", "SEQUENCE"}:
                    if name in active:
                        raise ValueError
                    active[name] = value
        if active is not None or not events:
            raise ValueError
    except PceScheduleError:
        raise
    except KeyError, ValueError, TypeError:
        raise PceScheduleError("bea_calendar_payload_invalid") from None
    return events


class PceReleaseScheduler:
    """Persist one five-year history batch or release target per worker tick.

    A durable running marker precedes side effects. Interrupted attempts stop
    pending operator review. Target capture requires two fresh persisted series;
    history may complete with explicit source gaps after all returned rows verify.
    """

    profile = PCE_PROFILE
    asset_key = "pce"
    series_ids = PCE_PROFILE.series_ids
    calendar_url = CALENDAR_URL
    ics_url = ICS_URL

    def __init__(
        self,
        service,
        state_path,
        *,
        clock=now_utc,
        calendar_fetcher=fetch_official_calendar,
        history_start=None,
        auto_mode=True,
        max_attempts=10,
    ):
        if getattr(service, "profile", PCE_PROFILE) is not PCE_PROFILE:
            raise PceScheduleError("schedule_service_profile_mismatch")
        self.service, self.clock = service, clock
        self.state_path = Path(state_path).expanduser().resolve()
        self.calendar_fetcher = calendar_fetcher
        now = _aware(clock())
        self.history_start = validate_month(
            history_start or month_at(now.year * 12 + now.month - 1 - 239)
        )
        self.auto_mode = auto_mode
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise PceScheduleError("schedule_max_attempts_invalid")
        self.max_attempts = max_attempts

    def _validate_asset(self, value, classification="schedule_state_asset_mismatch"):
        if value.get("asset") != self.asset_key:
            raise PceScheduleError(classification)

    def _read(self):
        if not self.state_path.exists():
            return {
                "version": 1,
                "asset": self.asset_key,
                "history_start": self.history_start,
                "calendar": {"source": ICS_URL, "events": {}},
                "targets": [],
            }
        try:
            value = json.loads(self.state_path.read_text())
            self._validate_asset(value)
            if value["version"] != 1 or not isinstance(value["targets"], list):
                raise ValueError
            validate_month(value["history_start"])
            return value
        except PceScheduleError:
            raise
        except OSError, ValueError, TypeError, KeyError:
            raise PceScheduleError("schedule_state_invalid") from None

    def _save(self, state):
        state["history_start"] = self.history_start
        _write_atomic(self.state_path, json.dumps(state, sort_keys=True).encode())

    @contextmanager
    def _locked(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            str(self.state_path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _calendar_status(self, state, at):
        value = state["calendar"]
        if value.get("error"):
            return "unavailable"
        fetched = value.get("acquired_at")
        if not fetched:
            return "missing"
        age = at - _instant(fetched)
        if age < timedelta(0):
            return "clock_reversal"
        return "fresh" if age <= timedelta(hours=24) else "stale"

    def status(self):
        state, now = self._read(), _aware(self.clock())
        due = []
        for target in state["targets"]:
            if target.get("calendar_execution_blocked"):
                continue
            for name in ("target", "history"):
                stage = target.get(name, {})
                if stage.get("state") in {"pending", "awaiting_provider"}:
                    due.append(stage.get("next_attempt_at") or target["release_at"])
        return {
            **state,
            "calendar_publications": [
                {
                    "reference_month": month,
                    "release_at": event["release_at"],
                    "publication_status": "cancelled"
                    if event["status"] == "CANCELLED"
                    else "scheduled",
                    "reference_month_evidence": {
                        "reference_month": month,
                        "source": ICS_URL,
                        "sha256": event["sha256"],
                    },
                }
                for event in state["calendar"].get("events", {}).values()
                for month in event["reference_months"]
            ],
            "calendar_status": self._calendar_status(state, now),
            "calendar_coverage": "explicit_reference_months_only",
            "next_due": min(due) if due else None,
            "signals": {"state": "unsupported", "events": {}},
            "provider": "BEA",
        }

    def _accept(self, state, raw, acquired):
        events = parse_official_ics(raw)
        previous_events = state["calendar"].get("events", {})
        if any(
            event["sequence"] < previous_events.get(uid, {}).get("sequence", 0)
            for uid, event in events.items()
        ):
            raise PceScheduleError("bea_calendar_sequence_reversal")
        previous = {t["reference_month"]: t for t in state["targets"]}
        targets, seen = [], set()
        recent = [
            e
            for e in events.values()
            if e["status"] != "CANCELLED"
            and acquired - timedelta(days=7) <= _instant(e["release_at"]) <= acquired
        ]
        latest = max(recent, key=lambda e: e["release_at"], default=None)
        for event in events.values():
            for month in event["reference_months"]:
                if month in seen:
                    raise PceScheduleError("bea_calendar_reference_month_ambiguous")
                seen.add(month)
                old = previous.pop(month, None)
                attempted = old and (
                    old.get("automatic_target_reason")
                    or any(
                        old.get(name, {}).get("attempts", 0)
                        or old.get(name, {}).get("last_attempt_at")
                        or old.get(name, {}).get("state")
                        in {"running", "unknown", "complete"}
                        for name in ("target", "history")
                    )
                )
                future = _instant(event["release_at"]) > acquired
                compensate = (
                    not attempted
                    and not future
                    and event is latest
                    and self._needs_compensation(month)
                )
                if not (attempted or future or compensate):
                    continue
                same = old is not None
                stage = {
                    "state": "pending",
                    "attempts": 0,
                    "next_attempt_at": (
                        _instant(event["release_at"]) - timedelta(seconds=30)
                    ).isoformat(),
                }
                cancelled = event["status"] == "CANCELLED"
                targets.append(
                    {
                        **(old if same else {}),
                        "reference_month": month,
                        "release_at": event["release_at"],
                        "uid": event["uid"],
                        "automatic_target_reason": (old or {}).get(
                            "automatic_target_reason"
                        )
                        or (
                            "future_official_release"
                            if future
                            else "recent_official_release_missing_local"
                            if compensate
                            else "existing_execution_evidence"
                        ),
                        "publication_status": "cancelled" if cancelled else "scheduled",
                        "state": "calendar_removed" if cancelled else "pending",
                        "calendar_execution_blocked": cancelled,
                        "reference_month_evidence": {
                            "source": ICS_URL,
                            "sha256": event["sha256"],
                            "reference_month": month,
                        },
                        "target": old["target"] if same else dict(stage),
                        "auto": old.get("auto", {}) if same else {},
                        "history": old["history"]
                        if same
                        else {
                            **stage,
                            "next_start": self.history_start,
                        },
                    }
                )
        for old in previous.values():
            old.update(state="calendar_removed", calendar_execution_blocked=True)
            targets.append(old)
        state["targets"] = sorted(targets, key=lambda t: t["reference_month"])
        state["automatic_target_policy_version"] = 1
        state["calendar"] = {
            "source": ICS_URL,
            "events": events,
            "acquired_at": acquired.isoformat(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "consecutive_refresh_failures": 0,
            "next_refresh_at": (acquired + timedelta(hours=6)).isoformat(),
        }
        self._save(state)

    def _needs_compensation(self, month):
        try:
            result = self.service.query_range(
                month,
                month,
                series_ids=self.series_ids,
                allow_upstream=False,
                vintage_mode="available",
            )
            if not isinstance(result, dict):
                return False
            held = {
                p["series_id"]
                for p in result.get("points", [])
                if p.get("month") == month
                and p.get("origin") == "local"
                and p.get("availability") == "available"
                and p.get("available_at") is not None
                and p.get("index_value") is not None
            }
            return not set(self.series_ids) <= held
        except Exception:
            return False

    def import_calendar(self, raw, acquired_at):
        acquired = _aware(acquired_at)
        if acquired > _aware(self.clock()):
            raise PceScheduleError("calendar_acquired_at_in_future")
        with self._locked():
            state = self._read()
            previous = state["calendar"].get("acquired_at")
            if previous and acquired < _instant(previous):
                raise PceScheduleError("calendar_import_older_than_current")
            self._accept(state, raw, acquired)
        return self.status()

    def _refresh(self, state, now):
        try:
            if self.calendar_fetcher is None:
                raise PceScheduleError("bea_calendar_fetcher_unavailable")
            self._accept(state, self.calendar_fetcher(), now)
            return True
        except Exception:
            value = state["calendar"]
            failures = value.get("consecutive_refresh_failures", 0) + 1
            value.update(
                error="bea_calendar_unavailable",
                consecutive_refresh_failures=failures,
                next_refresh_at=(
                    now
                    + timedelta(seconds=CALENDAR_RETRY_SECONDS[min(failures - 1, 3)])
                ).isoformat(),
            )
            self._save(state)
            return False

    def refresh_calendar(self):
        with self._locked():
            self._refresh(self._read(), _aware(self.clock()))
        return self.status()

    def due_check(self):
        now = _aware(self.clock())
        if not self.auto_mode:
            raise PceScheduleError("schedule_auto_mode_required")
        with self._locked():
            state = self._read()
            if not state.get("automatic_target_policy_version") and state["targets"]:
                # Migrate the old all-calendar queue before it can replay history.
                elapsed = [
                    t
                    for t in state["targets"]
                    if now - timedelta(days=7) <= _instant(t["release_at"]) <= now
                ]
                latest_at = max((t["release_at"] for t in elapsed), default=None)
                retained = []
                for target in state["targets"]:
                    evidence = any(
                        target.get(n, {}).get("attempts", 0)
                        or target.get(n, {}).get("last_attempt_at")
                        or target.get(n, {}).get("state")
                        in {"running", "unknown", "complete"}
                        for n in ("target", "history")
                    )
                    future = _instant(target["release_at"]) > now
                    compensate = target[
                        "release_at"
                    ] == latest_at and self._needs_compensation(
                        target["reference_month"]
                    )
                    if evidence or future or compensate:
                        if not evidence:
                            target["history"]["next_start"] = self.history_start
                        target["automatic_target_reason"] = (
                            "existing_execution_evidence"
                            if evidence
                            else "future_official_release"
                            if future
                            else "recent_official_release_missing_local"
                        )
                        retained.append(target)
                state["targets"] = retained
                state["automatic_target_policy_version"] = 1
                self._save(state)
            calendar_state = self._calendar_status(state, now)
            if calendar_state == "clock_reversal":
                return {"action": "clock_reversal"}
            refresh_at = state["calendar"].get("next_refresh_at")
            if not refresh_at or _instant(refresh_at) <= now:
                if not self._refresh(state, now):
                    return {"action": "calendar_unavailable"}
            elif calendar_state != "fresh":
                return {"action": "calendar_unavailable"}
            # A restarted process cannot infer that an interrupted write failed.
            for target in state["targets"]:
                for name in ("target", "history"):
                    if target[name]["state"] == "running":
                        target[name].update(
                            state="unknown", classification="execution_interrupted"
                        )
                        self._save(state)
                        return {"action": "execution_interrupted"}
            eligible = [
                t
                for t in state["targets"]
                if not t.get("calendar_execution_blocked")
                and _instant(t["release_at"]) - timedelta(seconds=30) <= now
            ]
            # Newest release capture first; history stays lower priority.
            for target in reversed(eligible):
                if target["target"]["state"] not in {
                    "complete",
                    "unknown",
                    "pending_exhausted",
                }:
                    result = self._run(state, target, "target", now)
                    if result["action"] != "not_due":
                        return result
            for target in reversed(eligible):
                if target["target"]["state"] == "complete" and target["history"][
                    "state"
                ] not in {"complete", "unknown", "pending_exhausted"}:
                    result = self._run(state, target, "history", now)
                    if result["action"] != "not_due":
                        return result
        return {"action": "not_due"}

    def _run(self, state, target, name, now):
        stage = target[name]
        due = stage.get("next_attempt_at")
        if due and _instant(due) > now:
            return {"action": "not_due"}
        # BEA source control remains authoritative across Web and workers.
        previous = state.get("last_data_attempt_at")
        if previous and now - _instant(previous) < timedelta(seconds=3.1):
            return {"action": "backoff"}
        budget = [
            a
            for a in state.get("data_admissions", [])
            if _instant(a["at"]) > now - timedelta(hours=24)
        ]
        if (
            sum(a["reserved_http"] for a in budget) + 2 > 100
            or target.get("reserved_http", 0) + 2 > 30
        ):
            return {"action": "budget_exhausted"}
        month = target["reference_month"]
        start = (
            month if name == "target" else stage.get("next_start", self.history_start)
        )
        if (
            name == "history"
            and not stage.get("attempts", 0)
            and not stage.get("last_attempt_at")
        ):
            # Earlier calendars seeded an unattempted cursor at the release month.
            # Only execution evidence makes a saved cursor resumable progress.
            start = stage["next_start"] = self.history_start
        end = (
            month
            if name == "target"
            else min(month, month_at(month_number(start) + 59))
        )
        if start > end:
            stage.update(state="complete", completed_at=now.isoformat())
            self._save(state)
            return {"action": "history_not_required"}
        stage.update(
            state="running",
            attempts=stage["attempts"] + 1,
            last_attempt_at=now.isoformat(),
            update_start=start,
            update_end=end,
        )
        if now >= _instant(target["release_at"]):
            stage["post_release_attempts"] = stage.get("post_release_attempts", 0) + 1
        state["last_data_attempt_at"] = now.isoformat()
        state["data_admissions"] = budget + [
            {"at": now.isoformat(), "reserved_http": 2}
        ]
        target["reserved_http"] = target.get("reserved_http", 0) + 2
        stage.update(result=None, readback=None, error=None, readback_verified=False)
        if name == "target":
            target["auto"] = stage.copy()
        self._save(state)
        try:
            result = self.service.update_range(
                start, end, series_ids=self.series_ids, include_buffer=name == "history"
            )
            stage["result"] = result
            stage["coverage_complete"] = result.get("refresh_coverage_complete") is True
            stage["source_missing"] = [
                {"series_id": row["series_id"], "month": row["month"]}
                for row in result.get("source_missing", [])
                if row.get("series_id") in self.series_ids
                and row.get("month") in month_range(start, end)
            ]
            count = result.get("provider_attempts")
            if type(count) is int and count >= 0:
                stage["actual_http"] = stage.get("actual_http", 0) + count
            if result.get("persisted") is not True:
                raise PceScheduleError("bea_source_range_incomplete")
            returned = {
                (p["series_id"], p["month"])
                for p in result.get("source_returned_series_months", [])
            }
            stage["buffer_source_missing"] = [
                row
                for row in result.get("source_missing", [])
                if row.get("month", start) < start
            ]
            stage["source_missing"] = [
                row
                for row in stage["source_missing"]
                if (row["series_id"], row["month"]) not in returned
            ]
            read_start = min([start, *(m for _, m in returned)])
            readback = self.service.query_range(
                read_start,
                end,
                series_ids=self.series_ids,
                allow_upstream=False,
                vintage_mode="available",
            )
            stage["readback"] = readback
            expected = {
                (series, period)
                for series in self.series_ids
                for period in month_range(start, end)
            }
            local = {
                (point["series_id"], point["month"]): point
                for point in readback.get("points", [])
                + readback.get("source_unavailable", [])
                if point.get("origin") == "local"
                and (name == "history" or point.get("index_value") is not None)
                and point.get("available_at") is not None
                and point.get("availability")
                in (
                    {"available", "source_unavailable"}
                    if name == "history"
                    else {"available"}
                )
            }
            produced = {
                (point["series_id"], point["month"]): point
                for point in result.get("points", [])
                + result.get("source_unavailable", [])
            }
            source_gaps = {
                (p["series_id"], p["month"]) for p in stage["source_missing"]
            }
            required = expected if name == "target" else returned
            if name == "target" and result.get("refresh_coverage_complete") is not True:
                raise PceScheduleError("bea_source_range_incomplete")
            if (
                not required <= local.keys()
                or (name == "target" and not expected <= returned)
                or (name == "history" and not (expected - local.keys()) <= source_gaps)
                or (name == "target" and not required <= produced.keys())
                or any(
                    key not in local
                    or any(
                        local[key].get(field) != produced[key].get(field)
                        for field in ("index_value", "revision", "raw_sha256")
                    )
                    for key in produced
                )
                or readback.get("provider_requests") != 0
                or readback.get("provider_attempts") != 0
            ):
                stage["coverage_complete"] = False
                raise PceScheduleError("schedule_local_readback_incomplete")
            stage.update(
                state="complete",
                completed_at=now.isoformat(),
                next_attempt_at=None,
                classification=None,
                readback_verified=True,
            )
            stage["readback"]["verified"] = True
            stage["coverage_complete"] = not source_gaps
            if name == "history" and end < month:
                stage.update(
                    state="pending",
                    post_release_attempts=0,
                    next_start=month_at(month_number(end) + 1),
                    next_attempt_at=(now + timedelta(seconds=300)).isoformat(),
                )
            if name == "target":
                if now < _instant(target["release_at"]):
                    stage.update(
                        state="awaiting_provider",
                        classification="pre_release_probe",
                        next_attempt_at=target["release_at"],
                        preprobe_complete=True,
                    )
                target["auto"] = stage.copy()
        except Exception as error:
            classification = "bea_collection_failed"
            if isinstance(
                error, (PceScheduleError, CpiSourceError, CpiTimeSeriesError)
            ):
                classification = error.classification
            count = getattr(error, "attempts", getattr(error, "provider_attempts", 0))
            if type(count) is int and count >= 0:
                stage["actual_http"] = stage.get("actual_http", 0) + count
            stage["error"] = {
                "classification": classification,
                "provider_attempts": count if type(count) is int else 0,
            }
            stopped = stage["attempts"] >= self.max_attempts or getattr(
                error, "http_status", None
            ) in {401, 403}
            stage.update(
                state="pending_exhausted" if stopped else "awaiting_provider",
                classification=classification,
                next_attempt_at=None
                if stopped
                else (
                    now
                    + timedelta(
                        seconds=RETRY_SECONDS[
                            min(
                                max(0, stage.get("post_release_attempts", 1) - 1),
                                len(RETRY_SECONDS) - 1,
                            )
                        ]
                    )
                ).isoformat(),
            )
            retry_at = getattr(error, "retry_at", None)
            if retry_at is not None and not stopped:
                stage["next_attempt_at"] = max(
                    _instant(stage["next_attempt_at"]), _aware(retry_at)
                ).isoformat()
            if now < _instant(target["release_at"]) and not stopped:
                stage["preprobe_complete"] = True
                stage["next_attempt_at"] = max(
                    _instant(stage["next_attempt_at"]), _instant(target["release_at"])
                ).isoformat()
            if name == "target":
                target["auto"] = stage.copy()
        self._save(state)
        return {"action": name + "_" + stage["state"]}
