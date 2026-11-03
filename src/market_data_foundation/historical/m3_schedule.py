"""M3 planned release calendar and paused-by-default shared strategy ports."""

import calendar
import hashlib
import json
import os
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
        self, state_path, *, history_start, clock, submit=None, submit_report=None
    ):
        self.state_path, self.history_start = Path(state_path), history_start
        self.clock, self.submit = clock, submit
        self.submit_report = submit_report

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
            dict(e, target=value["work"].get(e["key"], {"state": "pending"}))
            for e in value["events"]
        ]
        future = [
            e["release_at"]
            for e in targets
            if e["release_at"] and datetime.fromisoformat(e["release_at"]) > now
        ]
        return {
            "calendar_status": "fresh" if fresh else "stale" if proof else "missing",
            "calendar": proof,
            "targets": targets,
            "next_due": min(future) if future else None,
            "policy": POLICY,
        }

    def due_check(self):
        with self._locked():
            value = self._load()
            if self.status()["calendar_status"] != "fresh":
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
                and value["work"].get(e["key"], {}).get("state") != "complete"
                and value["work"].get(e["key"], {}).get("attempts", 0) < 3
            ]
            if not pending:
                return {"action": "already_checked"}
            event = min(pending, key=lambda e: e["release_at"])
            work = value["work"].get(event["key"], {})
            request_id = work.get("request_id") or str(
                uuid5(
                    NAMESPACE_URL, "mdf:m3:" + event["key"] + ":" + event["release_at"]
                )
            )
            work = dict(
                work,
                request_id=request_id,
                attempts=work.get("attempts", 0) + 1,
                state="running",
                dataset=event["dataset"],
                started_at=self.clock().isoformat(),
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
        result = submit(
            {
                "request_id": request_id,
                "action": "update",
                "start": event["reference_month"],
                "end": event["reference_month"],
            },
            resume=work["attempts"] > 1,
        )
        with self._locked():
            value = self._load()
            value["work"][event["key"]].update(
                state="complete"
                if result["state"] == "complete"
                and result.get("result", {}).get("coverage_complete")
                else "pending",
                result=result,
            )
            self._save(value)
        return {
            "action": "release_update",
            "request_id": request_id,
            "state": result["state"],
        }


class M3AutoStrategy(CpiAutoStrategy):
    def get(self):
        value = super().get()
        path = self.strategy_path.with_name("m3-worker.json")
        if path.exists():
            try:
                process = json.loads(path.read_text())
                value["worker"]["process"] = process
                if process["state"] == "stopped":
                    value["worker"]["state"] = "stopped"
                else:
                    os.kill(process["pid"], 0)
            except ProcessLookupError:
                value["worker"]["state"] = "stopped"
            except OSError, ValueError, KeyError:
                value["worker"]["state"] = "unknown"
        return {**value, "policy": POLICY}
