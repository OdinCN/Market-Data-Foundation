"""M3 planned release calendar and paused-by-default shared strategy ports."""

import calendar
import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from market_data_foundation.historical.cpi_auto_strategy import CpiAutoStrategy
from market_data_foundation.historical.cpi_release_schedule import (
    CpiScheduleError,
    _write_atomic,
)

CALENDAR_URL = "https://www.census.gov/manufacturing/m3/release_schedule.html"
PROFILE = SimpleNamespace(key="m3", earliest_month=None)
POLICY = {
    "release_update": "latest due advance/full report, each independently routed",
    "actual": "calendar is planned time only",
    "automatic_start": False,
    "tick_max_events": 1,
    "calendar_refresh_hours": 6,
    "calendar_cache_hours": 24,
    "max_stage_attempts": 3,
    "retry_min_seconds": 300,
    "material_retry": "same numeric job; material cooldown; at most 3 attempts",
    "history_maintenance": "verify post-release snapshots or refresh history",
    "rolling_24h_max_actual_http": 400,
}


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.rows, self.row, self.cell = [], None, [], None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.rows, self.row = [], []
        elif self.rows is not None and tag in {"td", "th"}:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append(" ".join(" ".join(self.cell).split()))
            self.cell = None
            if len(self.row) == 3:
                self.rows.append(self.row)
                self.row = []
        elif tag == "table" and self.rows is not None:
            self.tables.append(self.rows)
            self.rows = None


def parse_calendar(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= 2_000_000:
        raise CpiScheduleError("m3_calendar_payload_invalid")
    try:
        parser = _Tables()
        parser.feed(raw.decode("utf-8-sig"))
        tables = [t for t in parser.tables if t and t[0][0] == "Survey Month"]
        if (
            len(tables) != 1
            or "8:30 a.m." not in tables[0][0][1]
            or "10:00 a.m." not in tables[0][0][2]
        ):
            raise ValueError
        months = {name: n for n, name in enumerate(calendar.month_name) if name}
        events, seen = [], set()
        for row in tables[0][1:]:
            name, year = row[0].split()
            reference = f"{int(year):04d}-{months[name]:02d}"
            for dataset, hour, minute, text in (
                ("advm3", 8, 30, row[1]),
                ("m3", 10, 0, row[2]),
            ):
                key = dataset + ":" + reference
                if key in seen:
                    raise ValueError
                seen.add(key)
                planned = (
                    None
                    if text.strip().upper() in {"TBA", "TBD", ""}
                    else datetime.strptime(text, "%m/%d/%Y").replace(
                        hour=hour, minute=minute, tzinfo=ZoneInfo("America/New_York")
                    )
                )
                if planned and planned.strftime("%Y-%m") <= reference:
                    raise ValueError
                events.append(
                    {
                        "key": key,
                        "dataset": dataset,
                        "reference_month": reference,
                        "release_at": planned.astimezone(UTC).isoformat()
                        if planned
                        else None,
                        "status": "scheduled" if planned else "TBA",
                        "actual_at": None,
                        "source": CALENDAR_URL,
                    }
                )
        if not events:
            raise ValueError
        return events
    except Exception:
        raise CpiScheduleError("m3_calendar_structure_invalid") from None


class M3Scheduler:
    asset_key, profile, auto_mode = "m3", PROFILE, True

    def __init__(
        self,
        state_path,
        *,
        history_start,
        clock,
        submit=None,
        submit_report=None,
        calendar_fetch=None,
        maintain_history=None,
    ):
        self.state_path, self.history_start = Path(state_path), history_start
        self.clock, self.submit = clock, submit
        self.submit_report = submit_report
        self.calendar_fetch, self.maintain_history = calendar_fetch, maintain_history

    def _validate_asset(self, value, reason="m3_schedule_asset_mismatch"):
        if value.get("asset") != "m3":
            raise CpiScheduleError(reason)

    @contextmanager
    def _locked(self):
        import fcntl

        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.state_path.with_suffix(".lock").open("a+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def _load(self):
        if not self.state_path.exists():
            return {
                "asset": "m3",
                "version": 1,
                "history_start": self.history_start,
                "events": [],
                "work": {},
                "history_work": {},
                "calendar": {},
            }
        try:
            value = json.loads(self.state_path.read_text())
            self._validate_asset(value)
            if (
                value["version"] != 1
                or not isinstance(value["events"], list)
                or not isinstance(value["work"], dict)
            ):
                raise ValueError
            value.setdefault("history_work", {})
            return value
        except Exception:
            raise CpiScheduleError("m3_schedule_state_invalid") from None

    def _save(self, value):
        _write_atomic(self.state_path, json.dumps(value).encode())

    def import_calendar(self, raw, *, acquired_at):
        events = parse_calendar(raw)
        if acquired_at.utcoffset() is None or acquired_at > self.clock():
            raise CpiScheduleError("m3_calendar_time_invalid")
        with self._locked():
            value = self._load()
            value["events"] = events
            value["calendar"] = {
                "source": CALENDAR_URL,
                "acquired_at": acquired_at.isoformat(),
                "available_at": self.clock().isoformat(),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "cache_valid_until": (acquired_at + timedelta(hours=24)).isoformat(),
                "refresh_mode": "retained_official",
                "next_refresh_at": (acquired_at + timedelta(hours=6)).isoformat(),
                "failures": 0,
                "error": None,
            }
            self._save(value)

    def status(self):
        value = self._load()
        proof, now = value["calendar"], self.clock()
        fresh = bool(
            proof.get("cache_valid_until")
            and datetime.fromisoformat(proof["cache_valid_until"]) >= now
        )
        targets = [
            dict(
                e,
                target=self._work_view(
                    value["work"].get(e["key"], {"state": "pending"}), e
                ),
                history=value.get("history_work", {}).get(
                    e["key"], {"state": "pending"}
                ),
            )
            for e in value["events"]
        ]
        future = [
            e["release_at"]
            for e in targets
            if e["release_at"] and datetime.fromisoformat(e["release_at"]) > now
        ]
        return {
            "calendar_status": "degraded_cached_calendar"
            if fresh and proof.get("error")
            else "fresh"
            if fresh
            else "expired"
            if proof
            else "missing",
            "calendar": proof,
            "targets": targets,
            "next_due": min(future) if future else None,
            "policy": POLICY,
        }

    def refresh_calendar(self, *, force=False):
        if self.calendar_fetch is None:
            return self.status()
        now = self.clock()
        with self._locked():
            value = self._load()
            proof = value["calendar"]
            next_at = proof.get("next_refresh_at") or proof.get("cache_valid_until")
            if not force and next_at and datetime.fromisoformat(next_at) > now:
                return self.status()
            proof["last_attempt_at"] = now.isoformat()
            proof["next_refresh_at"] = (now + timedelta(minutes=5)).isoformat()
            self._save(value)
        # HTTP and parsing never run while holding the calendar state lock.
        try:
            raw = self.calendar_fetch()
            parse_calendar(raw)
            acquired = self.clock()
            raw_path = self.state_path.with_name(
                "m3-calendar-" + hashlib.sha256(raw).hexdigest() + ".raw"
            )
            _write_atomic(raw_path, raw)
            self.import_calendar(raw, acquired_at=acquired)
            with self._locked():
                value = self._load()
                value["calendar"].update(
                    refresh_mode="official_http",
                    last_attempt_at=now.isoformat(),
                    raw_path=str(raw_path),
                )
                self._save(value)
        except Exception as error:
            with self._locked():
                value = self._load()
                proof = value["calendar"]
                failures = proof.get("failures", 0) + 1
                delay = (300, 900, 3600, 14400)[min(failures - 1, 3)]
                retry = getattr(error, "retry_at", None)
                next_at = (
                    max(self.clock() + timedelta(seconds=delay), retry)
                    if isinstance(retry, datetime)
                    else self.clock() + timedelta(seconds=delay)
                )
                proof.update(
                    error="m3_calendar_refresh_failed",
                    failures=failures,
                    next_refresh_at=next_at.isoformat(),
                    refresh_mode="cache" if proof.get("acquired_at") else "unavailable",
                )
                self._save(value)
        return self.status()

    def _history_due(self):
        if self.maintain_history is None:
            return {"action": "already_checked"}
        with self._locked():
            value = self._load()
            eligible = [
                e
                for e in value["events"]
                if value["work"].get(e["key"], {}).get("state")
                in {"complete", "source_unavailable"}
            ]
            pending = []
            for event in eligible:
                history = value["history_work"].get(event["key"], {})
                if history.get("state") in {
                    "complete",
                    "source_unavailable",
                    "pending_exhausted",
                }:
                    continue
                if (
                    history.get("next_attempt_at")
                    and datetime.fromisoformat(history["next_attempt_at"])
                    > self.clock()
                ):
                    continue
                pending.append(event)
            if not pending:
                return {"action": "already_checked"}
            event = min(pending, key=lambda e: e["release_at"])
            history = value["history_work"].get(event["key"], {})
            identity = history.get("request_id") or str(
                uuid5(
                    NAMESPACE_URL,
                    "mdf:m3:history:"
                    + event["key"]
                    + ":"
                    + event["release_at"]
                    + ":"
                    + self.history_start,
                )
            )
            history.update(
                request_id=identity,
                state="running",
                attempts=history.get("attempts", 0) + 1,
                last_attempt_at=self.clock().isoformat(),
            )
            value["history_work"][event["key"]] = history
            self._save(value)
        try:
            result = self.maintain_history(event, identity, self.history_start)
            complete = result.get("state") == "complete"
        except Exception:
            result = {
                "state": "unknown",
                "classification": "m3_history_outcome_unknown",
            }
            complete = False
        with self._locked():
            value = self._load()
            history = value["history_work"][event["key"]]
            history.update(
                state="complete"
                if complete
                else "pending_exhausted"
                if history["attempts"] >= 3
                else "awaiting_provider",
                result=result,
                completed_at=self.clock().isoformat() if complete else None,
                next_attempt_at=None
                if complete or history["attempts"] >= 3
                else (self.clock() + timedelta(seconds=300)).isoformat(),
            )
            self._save(value)
        return {
            "action": "history_verified" if complete else "history_pending",
            "request_id": identity,
            "state": history["state"],
        }

    def due_check(self):
        ready = self._ready_check()
        if ready.get("action") == "target_ready":
            return self._execute_target(ready["event"])
        if ready.get("action") == "material_ready":
            return self._execute_target(ready["event"], material_only=True)
        return ready

    def _material_retry(self, work, event=None):
        # Only jobs that explicitly attempted material are eligible. Legacy
        # numeric completions and old history are never turned into backfill.
        from market_data_foundation.historical.m3_release_acquisition import (
            recoverable_material,
        )

        job = work.get("result") or {}
        if work.get("state") not in {
            "complete",
            "source_unavailable",
        } or not recoverable_material(job):
            return None
        material = job["result"]["release_material"]
        old = work.get("material_retry") or {}
        if event:
            selection = (
                material.get("reports", {}).get(event["dataset"], {}).get("selection")
                or {}
            )
            at = (
                datetime.fromisoformat(event["release_at"])
                if event.get("release_at")
                else None
            )
            date = (
                at.astimezone(ZoneInfo("America/New_York")).date().isoformat()
                if at and at.utcoffset() is not None
                else None
            )
            if (
                selection.get("source_date") != date
                or selection.get("reference_month") != event["reference_month"]
                or selection.get("dataset") != event["dataset"]
                or event.get("status") != "scheduled"
                or event.get("calendar_execution_blocked")
                or event.get("cancelled")
            ):
                return dict(
                    state="calendar_changed",
                    attempts=old.get("attempts", 0),
                    next_attempt_at=None,
                    reasons=["m3_material_calendar_changed"],
                    next_step="该期日历身份已变化，需核对原任务与官方期次；不自动绑定另一发布事件",
                    numeric_state=work["state"],
                )
        outcomes = [
            o
            for r in material.get("reports", {}).values()
            if r.get("state") != "retained"
            for o in r.get("material_outcomes", [])
        ]
        attempts = max(
            [1, old.get("attempts", 0)] + [o.get("attempts", 0) for o in outcomes]
        )
        deadlines = [
            datetime.fromisoformat(v)
            for v in [old.get("next_attempt_at")]
            + [o.get("next_attempt_at") for o in outcomes]
            if v
        ]
        return dict(
            state="running"
            if old.get("state") == "running"
            and deadlines
            and max(deadlines) > self.clock()
            else "pending_exhausted"
            if attempts >= 3
            else "awaiting_provider",
            attempts=attempts,
            last_attempt_at=old.get("last_attempt_at")
            or (outcomes[-1].get("attempted_at") if outcomes else None),
            next_attempt_at=None
            if attempts >= 3
            else max(deadlines).isoformat()
            if deadlines
            else (
                datetime.fromisoformat(work["started_at"]) + timedelta(hours=1)
            ).isoformat(),
            reasons=material.get("reasons", []),
            next_step="本期材料尝试已达上限，需核实来源和材料尝试记录；数值已确认，不自动重采"
            if attempts >= 3
            else "等待材料冷却结束后自动补取同一任务的原文；数值已确认，不重复采集",
            numeric_state=work["state"],
        )

    def _work_view(self, work, event):
        retry = self._material_retry(work, event)
        return dict(work, material_retry=retry) if retry else work

    def _ready_check(self):
        self.refresh_calendar()
        with self._locked():
            value = self._load()
            if self.status()["calendar_status"] not in {
                "fresh",
                "degraded_cached_calendar",
            }:
                return {"action": "calendar_unavailable"}
            due = [
                e
                for e in value["events"]
                if e["release_at"]
                and datetime.fromisoformat(e["release_at"]) <= self.clock()
            ]
            if not due:
                return {"action": "not_due"}
            latest = {
                d: max(
                    (e for e in due if e["dataset"] == d),
                    key=lambda e: e["release_at"],
                    default=None,
                )
                for d in ("advm3", "m3")
            }
            pending = [
                e
                for e in latest.values()
                if e
                and value["work"].get(e["key"], {}).get("state")
                not in {"complete", "source_unavailable"}
                and value["work"].get(e["key"], {}).get("attempts", 0) < 3
            ]
            if not pending:
                event = None
            else:
                pending = [
                    e
                    for e in pending
                    if not value["work"].get(e["key"], {}).get("next_attempt_at")
                    or datetime.fromisoformat(
                        value["work"][e["key"]]["next_attempt_at"]
                    )
                    <= self.clock()
                ]
                if not pending:
                    return {"action": "cooldown_wait"}
                event = min(pending, key=lambda e: e["release_at"])
            if event is None:
                # Only current latest due stages, never all historical jobs.
                waiting = False
                for candidate in sorted(
                    (e for e in latest.values() if e), key=lambda e: e["release_at"]
                ):
                    retry = self._material_retry(
                        value["work"].get(candidate["key"], {}), candidate
                    )
                    if retry and retry["state"] == "awaiting_provider":
                        if (
                            datetime.fromisoformat(retry["next_attempt_at"])
                            <= self.clock()
                        ):
                            return {"action": "material_ready", "event": candidate}
                        waiting = True
                if waiting:
                    return {"action": "material_cooldown_wait"}
            else:
                return self._run_target_unlocked_marker(value, event)
        return self._history_due()

    def _run_target_unlocked_marker(self, value, event):
        # Marker only; called under lock, external effect delegated after return.
        return {"action": "target_ready", "event": event}

    def _execute_target(self, event, *, material_only=False):
        with self._locked():
            value = self._load()
            if not any(
                e["key"] == event["key"] and e["release_at"] == event["release_at"]
                for e in value["events"]
            ):
                return {"action": "calendar_changed"}
            work = value["work"].get(event["key"], {})
            if material_only:
                retry = self._material_retry(work, event)
                if (
                    not retry
                    or retry["state"] != "awaiting_provider"
                    or datetime.fromisoformat(retry["next_attempt_at"]) > self.clock()
                ):
                    return {"action": "material_not_due"}
            request_id = work.get("request_id") or str(
                uuid5(
                    NAMESPACE_URL, "mdf:m3:" + event["key"] + ":" + event["release_at"]
                )
            )
            if material_only:
                work = dict(
                    work,
                    material_retry=dict(
                        retry,
                        state="running",
                        attempts=retry["attempts"] + 1,
                        last_attempt_at=self.clock().isoformat(),
                        next_attempt_at=(self.clock() + timedelta(hours=1)).isoformat(),
                    ),
                )
            else:
                work = dict(
                    work,
                    request_id=request_id,
                    attempts=work.get("attempts", 0) + 1,
                    state="running",
                    dataset=event["dataset"],
                    started_at=self.clock().isoformat(),
                    next_attempt_at=(self.clock() + timedelta(minutes=5)).isoformat(),
                )
            value["work"][event["key"]] = work
            self._save(value)
        if self.submit is None and self.submit_report is None:
            raise CpiScheduleError("m3_worker_submit_not_bound")
        submit = (
            self.submit
            if self.submit_report is None
            else lambda command, **kwargs: self.submit_report(
                event["dataset"], command, **kwargs
            )
        )
        try:
            result = submit(
                {
                    "request_id": request_id,
                    "action": "update",
                    "start": event["reference_month"],
                    "end": event["reference_month"],
                },
                resume=material_only or work["attempts"] > 1,
            )
        except Exception:
            result = {
                "state": "unknown",
                "classification": "m3_target_outcome_unknown",
                "result": {},
            }
        retry_at = self.clock() + timedelta(seconds=300)
        source_failure = (result.get("result") or {}).get("source_failure") or {}
        try:
            if source_failure.get("retry_at"):
                retry_at = max(
                    retry_at, datetime.fromisoformat(source_failure["retry_at"])
                )
        except ValueError, TypeError:
            pass
        with self._locked():
            value = self._load()
            if material_only:
                current = value["work"][event["key"]]
                if result.get("state") == "complete":
                    current["result"] = result
                current["material_retry"]["state"] = "awaiting_provider"
                retry = self._material_retry(current)
                current["material_retry"] = retry or dict(
                    state="retained",
                    attempts=current["material_retry"]["attempts"],
                    next_attempt_at=None,
                    reasons=[],
                )
                self._save(value)
                return {
                    "action": "material_retry",
                    "request_id": request_id,
                    "state": current["state"],
                    "material_retry": current["material_retry"],
                }
            value["work"][event["key"]].update(
                state=(
                    "complete"
                    if result.get("result", {}).get("coverage_complete")
                    else "source_unavailable"
                )
                if result["state"] == "complete"
                else "pending_exhausted"
                if value["work"][event["key"]]["attempts"] >= 3
                else "awaiting_provider",
                next_attempt_at=None
                if result["state"] == "complete"
                else retry_at.isoformat(),
                result=result,
            )
            self._save(value)
        return {
            "action": "release_update",
            "request_id": request_id,
            "state": result["state"],
        }


class M3AutoStrategy(CpiAutoStrategy):
    def _view(self, value):
        return {**super()._view(value), "policy": POLICY}

    def get(self):
        return {**super().get(), "policy": POLICY}
