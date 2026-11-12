"Explicit DEV resident executor; persistent enable/pause and deterministic slots."

import fcntl
import json
import threading
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from market_data_foundation.historical.tdx_p0_jobs import atomic, now


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
            schedule="15:10 Asia/Shanghai; dated exchange calendar required",
            calendar_state=self.calendar_state,
            overlap_calendar_days=14,
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
        if (
            not self.read()["enabled"]
            or local.weekday() >= 5
            or (local.hour, local.minute) < (15, 10)
        ):
            return
        snapshot = self.jobs.store.universe(as_of=at)
        calendars = {
            c["market"]: c["is_open"]
            for c in (snapshot or {}).get("details", {}).get("calendar", [])
            if c["day"] == local.date().isoformat()
            and datetime.fromisoformat(c["available_at"]) <= at
        }
        self.calendar_state = (
            "UNKNOWN"
            if set(calendars) != {0, 1, 2}
            else "OPEN"
            if all(calendars.values())
            else "CLOSED"
        )
        if self.calendar_state != "OPEN":
            return
        with self.path.with_suffix(".lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            day = local.date().isoformat()
            for action in ("universe", "update"):
                identity = str(uuid5(NAMESPACE_URL, "mdf-tdx-p0:" + day + ":" + action))
                if not self.jobs.path(identity).exists():
                    self.jobs.submit(dict(request_id=identity, action=action, end=day))
                    return
                record = self.jobs.read(identity)
                if record["state"] != "COMPLETE":
                    # An incomplete durable run remains visible for explicit resume;
                    # no unbounded autonomous retry or competing worker.
                    return
