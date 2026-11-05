"""Explicit DEV daily intent, source-aware maintenance slots and durable retries.

Slots are MDF maintenance times, not claims of official publication. All work uses
normal FinancialJobs, including its shared execution lock and retained raw bytes.
"""

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from market_data_foundation.historical.cpi_release_schedule import _write_atomic
from market_data_foundation.web.cpi_timeseries_jobs import WebError

ET = ZoneInfo("America/New_York")
GROUPS = {
    "policy": (("DFEDTARL", "DFEDTARU"), ("09:10", "14:45"), False),
    "overnight": (("EFFR", "SOFR"), ("09:15", "14:45"), True),
    "treasury": (("DGS2", "DGS10", "DFII10", "T10YIE"), ("18:30",), True),
    "credit": (("BAMLC0A0CM", "BAMLH0A0HYM2"), ("10:30", "19:00"), True),
}
LOOKBACK_DAYS = 14
MAX_ATTEMPTS = 3


def instant(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None:
        raise ValueError("aware_time_required")
    return result.astimezone(UTC)


def slots(at, *, future=False):
    local = instant(at).astimezone(ET)
    result = []
    for name, (_, hours, weekdays) in GROUPS.items():
        candidates = []
        for offset in range(8 if future else 1):
            day = local.date() + timedelta(days=offset)
            if weekdays and day.weekday() >= 5:
                continue
            for hour in hours:
                when = datetime.combine(day, time.fromisoformat(hour), ET).astimezone(
                    UTC
                )
                if (when > at) if future else (when <= at):
                    candidates.append(when)
        if candidates:
            when = min(candidates) if future else max(candidates)
            result.append((name, when))
    return sorted(result, key=lambda item: (item[1], item[0]))


class FinancialSchedule:
    def __init__(self, directory, *, clock=lambda: datetime.now(UTC)):
        self.directory = Path(directory)
        if not self.directory.is_absolute() or ".." in self.directory.parts:
            raise ValueError("absolute_financial_state_directory_required")
        self.clock = clock
        self.path = self.directory / "schedule.json"

    def _read(self, path, default):
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return default
        except OSError, ValueError:
            raise WebError("financial_schedule_unavailable", 503) from None

    def read(self):
        value = self._read(self.path, dict(enabled=False, tasks={}, revision=0))
        if (
            type(value.get("enabled")) is not bool
            or type(value.get("tasks")) is not dict
        ):
            raise WebError("financial_schedule_unavailable", 503)
        return value

    def _save(self, value):
        _write_atomic(self.path, json.dumps(value, allow_nan=False).encode())

    @contextmanager
    def lock(self, name):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.directory / name).open("a") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WebError("financial_schedule_busy", 409) from None
            yield

    def enabled(self, value):
        if type(value) is not bool:
            raise WebError("financial_strategy_invalid")
        with self.lock("policy.lock"):
            state = self.read()
            if state["enabled"] != value:
                state.update(
                    enabled=value,
                    updated_at=self.clock().isoformat(),
                    revision=state["revision"] + 1,
                )
                self._save(state)
        return self.get()

    def heartbeat(self, state, *, classification=None):
        _write_atomic(
            self.directory / "worker.json",
            json.dumps(
                dict(
                    state=state,
                    last_heartbeat_at=self.clock().isoformat(),
                    process=dict(pid=os.getpid()),
                    classification=classification,
                )
            ).encode(),
        )

    def get(self):
        state = self.read()
        tasks = sorted(
            state["tasks"].values(), key=lambda task: task["slot"], reverse=True
        )
        now = self.clock()
        waiting = [
            instant(t["retry_at"])
            for t in tasks
            if t["state"] != "complete" and t["attempts"] < MAX_ATTEMPTS
        ]
        next_slots = [at for _, at in slots(now, future=True)]
        due = [
            at
            for name, at in slots(now)
            if f"{name}:{at.isoformat()}" not in state["tasks"]
        ]
        next_at = min([*waiting, *next_slots, *due], default=None)
        successful = [t["completed_at"] for t in tasks if t["state"] == "complete"]
        return dict(
            status="OK",
            enabled=state["enabled"],
            revision=state["revision"],
            worker=self._read(
                self.directory / "worker.json", dict(state="stopped", process=None)
            ),
            last_successful_collection_at=max(successful, default=None),
            next_check_at=max(now, next_at).isoformat()
            if state["enabled"] and next_at
            else None,
            tasks=tasks,
            lookback_days=LOOKBACK_DAYS,
            max_attempts=MAX_ATTEMPTS,
            timezone="America/New_York",
            maintenance_times={k: list(v[1]) for k, v in GROUPS.items()},
            schedule_semantics=(
                "MDF 来源维护检查时间，非官方公布承诺；回看近14日，周末不虚构观测。"
            ),
        )

    def status(self):
        return self.get()

    def tick(self, jobs):
        with self.lock("tick.lock"):
            at = self.clock()
            with self.lock("policy.lock"):
                state = self.read()
                if not state["enabled"]:
                    return dict(state="paused")
                candidates = [
                    (k, t)
                    for k, t in state["tasks"].items()
                    if t["state"] != "complete"
                    and t["attempts"] < MAX_ATTEMPTS
                    and instant(t["retry_at"]) <= at
                ]
                if not candidates:
                    for name, when in slots(at):
                        key = f"{name}:{when.isoformat()}"
                        if key not in state["tasks"]:
                            end = when.astimezone(ET).date()
                            task = dict(
                                group=name,
                                slot=when.isoformat(),
                                attempts=0,
                                state="pending",
                                request_id=str(
                                    uuid5(
                                        NAMESPACE_URL, "mdf-financial-daily-r1:" + key
                                    )
                                ),
                                command=dict(
                                    action="update",
                                    start=(
                                        end - timedelta(days=LOOKBACK_DAYS - 1)
                                    ).isoformat(),
                                    end=end.isoformat(),
                                ),
                            )
                            task["command"]["request_id"] = task["request_id"]
                            candidates = [(key, task)]
                            break
                if not candidates:
                    return dict(state="not_due")
                key, task = candidates[0]
                task.update(
                    state="running",
                    attempts=task["attempts"] + 1,
                    last_attempt_at=at.isoformat(),
                    retry_at=(at + timedelta(minutes=5)).isoformat(),
                )
                state["tasks"][key] = task
                self._save(state)
            # No policy lock during collection: pause stops subsequent triggers and
            # the current normal job finishes without process termination.
            try:
                existing = jobs.journal.read(task["request_id"])
                result = (
                    jobs.resume(task["request_id"])
                    if existing
                    else jobs.submit(
                        task["command"],
                        selected=GROUPS[task["group"]][0],
                        origin="periodic",
                        schedule_slot=key,
                    )
                )
                task.update(
                    state="complete" if result["state"] == "complete" else "failed",
                    job_state=result["state"],
                    provider_requests=result["provider_requests"],
                    failures=[
                        dict(
                            series_id=s["series_id"],
                            classification=s.get("classification"),
                            retry_at=s.get("retry_at"),
                            state=s["state"],
                        )
                        for s in result["steps"]
                        if s["state"] != "complete"
                    ],
                )
                cooling = [
                    instant(s["retry_at"]) for s in result["steps"] if s.get("retry_at")
                ]
                task["retry_at"] = max(
                    [at + timedelta(minutes=5 * 2 ** (task["attempts"] - 1)), *cooling]
                ).isoformat()
                if task["state"] == "complete":
                    task["completed_at"] = self.clock().isoformat()
                    task.pop("classification", None)
                else:
                    task["classification"] = (
                        result.get("classification")
                        or "financial_periodic_partial_failure"
                    )
            except Exception as error:
                task.update(
                    state="failed",
                    classification=getattr(
                        error, "classification", "financial_periodic_outcome_unknown"
                    ),
                )
                if isinstance(error, WebError) and error.status == 409:
                    task["attempts"] -= 1
            with self.lock("policy.lock"):
                state = self.read()
                state["tasks"][key] = task
                self._save(state)
            return task
