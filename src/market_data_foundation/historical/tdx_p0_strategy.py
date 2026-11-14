"Explicit DEV resident executor; persistent enable/pause and deterministic slots."

import fcntl
import json
import threading
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from market_data_foundation.historical.tdx_p0_jobs import atomic, now, strategy_slot_id


class DailyStrategy:
    def __init__(self, jobs, *, directory=None):
        self.jobs = jobs
        directory = directory or jobs.directory
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "strategy.json"
        self.heartbeat = None
        self.stop_event = threading.Event()
        self.thread = None
        self.calendar_state = "NOT_CHECKED"

    def read(self):
        value = (
            json.loads(self.path.read_bytes())
            if self.path.exists()
            else dict(enabled=False)
        )
        return dict(
            value,
            worker_online=bool(self.thread and self.thread.is_alive()),
            heartbeat_at=self.heartbeat,
            schedule="09:00 roster; 15:35 daily bars; Asia/Shanghai",
            calendar_state=self.calendar_state,
            overlap_calendar_days=14,
            review_target=None,
        )

    def set(self, enabled):
        atomic(self.path, dict(enabled=enabled, updated_at=now()))
        return self.read()

    def start(self):
        if self.thread and self.thread.is_alive():
            return

        def loop():
            while not self.stop_event.is_set():
                self.heartbeat = now()
                try:
                    self.tick()
                except Exception:
                    # Jobs retain classified failures; keep heartbeat available.
                    pass
                self.stop_event.wait(30)

        self.thread = threading.Thread(
            target=loop, daemon=True, name="mdf-tdx-strategy"
        )
        self.thread.start()

    def tick(self, at=None):
        at = at or datetime.now(UTC)
        local = at.astimezone(ZoneInfo("Asia/Shanghai"))
        if not self.read()["enabled"]:
            return
        store = self.jobs.store
        if hasattr(store, "strategy_calendar"):
            calendar = store.strategy_calendar(as_of=at)
        else:
            calendar = (
                (store.universe(as_of=at) or {}).get("details", {}).get("calendar", [])
            )
        days = {}
        for c in calendar:
            if datetime.fromisoformat(c["available_at"]) <= at:
                days.setdefault(c["day"], {})[c["market"]] = c["is_open"]
        calendars = days.get(local.date().isoformat(), {})
        self.calendar_state = (
            "UNKNOWN"
            if set(calendars) != {0, 1, 2}
            else "OPEN"
            if all(calendars.values())
            else "CLOSED"
        )
        # A delayed start can catch up a confirmed trading day.
        eligible = [
            day
            for day, markets in days.items()
            if set(markets) == {0, 1, 2}
            and all(markets.values())
            and datetime.combine(date.fromisoformat(day), time(15, 35), local.tzinfo)
            <= local
        ]
        with self.path.with_suffix(".lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            today = local.date().isoformat()
            if self.calendar_state == "OPEN" and time(9) <= local.time() < time(15, 35):
                roster = strategy_slot_id(today, "universe")
                if not self.jobs.path(roster).exists():
                    self.jobs.submit(
                        dict(request_id=roster, action="universe", end=today),
                        origin="periodic",
                    )
                    return
            if not eligible:
                return
            latest = max(eligible)
            day = latest
            cursor = self.path.with_name("strategy-cursor.state")
            previous = (
                json.loads(cursor.read_bytes())["day"] if cursor.exists() else None
            )
            if previous in eligible and previous < latest:
                day = previous
                previous_update = strategy_slot_id(previous, "update")
                if self.jobs.path(previous_update).exists() and self.jobs.summary(
                    previous_update
                )["state"] in {"COMPLETE", "PARTIAL", "INTERRUPTED"}:
                    day = min(d for d in eligible if d > previous)
            if previous != day:
                atomic(cursor, dict(day=day))
            for action in ("universe", "update"):
                identity = strategy_slot_id(day, action)
                if not self.jobs.path(identity).exists():
                    self.jobs.submit(
                        dict(request_id=identity, action=action, end=day),
                        origin="periodic",
                    )
                    return
                record = self.jobs.summary(identity)
                state = record["state"]
                if (
                    action == "universe"
                    and day == local.date().isoformat()
                    and local.time() >= time(15, 35)
                    and state in {"PARTIAL", "INTERRUPTED"}
                    and record.get("resume_count", 0) == 0
                ):
                    self.jobs.resume(identity)
                    return
                if state != "COMPLETE":
                    # A past failure stays resumable without starving later dates.
                    if day < latest and state in {"PARTIAL", "INTERRUPTED"}:
                        atomic(cursor, dict(day=min(d for d in eligible if d > day)))
                    return
