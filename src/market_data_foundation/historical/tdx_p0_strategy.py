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
            schedule="15:10 initial; 16:00 same-day review; Asia/Shanghai",
            calendar_state=self.calendar_state,
            overlap_calendar_days=14,
            review_target="same_trading_day_only",
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
        # A late start catches up the latest evidenced trading day, including a
        # weekend restart. It never expands the late review into a history run.
        eligible = [
            day
            for day, markets in days.items()
            if set(markets) == {0, 1, 2}
            and all(markets.values())
            and datetime.combine(date.fromisoformat(day), time(15, 10), local.tzinfo)
            <= local
        ]
        if not eligible:
            return
        with self.path.with_suffix(".lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            latest = max(eligible)
            day = latest
            cursor = self.path.with_name("strategy-cursor.state")
            previous = (
                json.loads(cursor.read_bytes())["day"] if cursor.exists() else None
            )
            if previous:
                if previous in eligible and previous < latest:
                    next_day = min(d for d in eligible if d > previous)
                    review = self.jobs.path(strategy_slot_id(previous, "review"))
                    day = previous
                    if review.exists() and self.jobs.summary(review.stem)["state"] in {
                        "COMPLETE",
                        "PARTIAL",
                        "INTERRUPTED",
                    }:
                        day = next_day
            if previous != day:
                atomic(cursor, dict(day=day))
            for slot, action in (
                ("universe", "universe"),
                ("update", "update"),
                ("review", "update"),
            ):
                if slot == "review" and local < datetime.combine(
                    date.fromisoformat(day), time(16), local.tzinfo
                ):
                    return
                identity = strategy_slot_id(day, slot)
                if not self.jobs.path(identity).exists():
                    self.jobs.submit(
                        dict(
                            request_id=identity,
                            action=action,
                            end=day,
                            **({"start": day} if slot == "review" else {}),
                        ),
                        origin="periodic",
                    )
                    return
                record = self.jobs.summary(identity)
                if record["state"] != "COMPLETE":
                    # Older failures stay available to resume without blocking
                    # every later trading day. An active run retains its turn.
                    if day < latest and record["state"] in {"PARTIAL", "INTERRUPTED"}:
                        atomic(cursor, dict(day=next_day))
                    return
