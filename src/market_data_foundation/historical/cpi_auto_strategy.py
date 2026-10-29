"""Explicitly enabled CPI strategy and worker liveness, without import-time IO."""

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from market_data_foundation.historical.cpi_release_schedule import (
    CpiScheduleError,
    _aware,
    _instant,
    _write_atomic,
    now_utc,
)
from market_data_foundation.historical.cpi_timeseries_source import validate_month

AUTO_POLICY = {
    "release_windows": [
        {"from_seconds": -30, "until_seconds": 0, "interval_seconds": 15},
        {"from_seconds": 0, "until_seconds": 120, "interval_seconds": 15},
        {"from_seconds": 120, "until_seconds": 600, "interval_seconds": 60},
        {"from_seconds": 600, "until_seconds": 1800, "interval_seconds": 300},
        {"from_seconds": 1800, "until_seconds": 7200, "interval_seconds": 1800},
    ],
    "event_max_actual_http": 30,
    "rolling_24h_max_actual_http": 400,
    "early_detection_requires_post_release_check": True,
    "history_priority": "low",
    "history_batch_calendar_years": 20,
}


class CpiAutoStrategy:
    def __init__(self, scheduler, strategy_path, *, clock=now_utc):
        self.scheduler = scheduler
        self.strategy_path = Path(strategy_path).expanduser().resolve()
        self.clock = clock

    @contextmanager
    def _locked(self):
        self.strategy_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            str(self.strategy_path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _read(self):
        if not self.strategy_path.exists():
            return None
        try:
            value = json.loads(self.strategy_path.read_text())
            if value.get("version") != 1 or type(value.get("enabled")) is not bool:
                raise ValueError
            validate_month(value["history_start"])
            return value
        except OSError, ValueError, TypeError, KeyError:
            raise CpiScheduleError("schedule_strategy_state_invalid") from None

    def _save(self, value):
        _write_atomic(self.strategy_path, json.dumps(value, sort_keys=True).encode())

    def _bind_history(self, value):
        created = value is not None and value.get("created", True)
        if self.scheduler.state_path.exists():
            try:
                persisted = json.loads(self.scheduler.state_path.read_text())
                baseline = validate_month(persisted.get("history_start", "1999-01"))
            except OSError, ValueError, TypeError, AttributeError:
                raise CpiScheduleError("schedule_state_invalid") from None
            if created and value["history_start"] != baseline:
                raise CpiScheduleError("schedule_strategy_history_start_mismatch")
            # The existing maintenance file is authoritative, including when a
            # restarted scheduler was constructed with its default baseline.
            self.scheduler.history_start = baseline
            return baseline, True
        baseline = value["history_start"] if created else "1999-01"
        self.scheduler.history_start = baseline
        return baseline, bool(created)

    def _view(self, value):
        baseline, locked = self._bind_history(value)
        snapshot = self.scheduler.status()
        worker = dict((value or {}).get("worker", {}))
        worker.setdefault("last_heartbeat_at", None)
        worker.setdefault("state", "not_started")
        heartbeat = worker["last_heartbeat_at"]
        if heartbeat and _aware(self.clock()) - _instant(heartbeat) > timedelta(
            seconds=120
        ):
            worker["state"] = "stale"
        return {
            "exists": value is not None and value.get("created", True),
            "enabled": (value or {}).get("enabled", False),
            "history_start": baseline,
            "history_start_locked": locked,
            "worker": worker,
            "next_due": snapshot.get("next_due")
            if value and value["enabled"]
            else None,
            "policy": AUTO_POLICY,
        }

    def get(self):
        return self._view(self._read())

    def update(self, *, enabled, history_start):
        if type(enabled) is not bool:
            raise CpiScheduleError("schedule_strategy_enabled_invalid")
        validate_month(history_start)
        with self._locked():
            value = self._read()
            baseline, locked = self._bind_history(value)
            if locked and history_start != baseline:
                error = CpiScheduleError("schedule_history_start_changed")
                error.message = (
                    "沿用已有维护起点；请单独扩展历史，不会重置已有进度或预算。"
                )
                raise error
            value = value or {
                "version": 1,
                "history_start": baseline,
                "created_at": _aware(self.clock()).isoformat(),
            }
            self.scheduler.history_start = history_start
            value.setdefault("created_at", _aware(self.clock()).isoformat())
            value.update(
                history_start=history_start,
                created=True,
                enabled=enabled,
                updated_at=_aware(self.clock()).isoformat(),
            )
            self._save(value)
        return self._view(value)

    def heartbeat(self, state="idle"):
        if state not in {"idle", "running", "paused", "error"}:
            raise CpiScheduleError("schedule_worker_state_invalid")
        with self._locked():
            value = self._read()
            # An uncreated strategy remains uncreated; status reads never invent
            # liveness. A real worker can persist a separate heartbeat envelope.
            if value is None:
                value = {
                    "version": 1,
                    "enabled": False,
                    "history_start": self._bind_history(None)[0],
                    "created": False,
                }
            value["worker"] = {
                "state": state,
                "last_heartbeat_at": _aware(self.clock()).isoformat(),
            }
            self._save(value)
        return self._view(value)

    def tick(self):
        value = self._read()
        if not value or not value["enabled"]:
            return {"action": "paused", **self.heartbeat("paused")}
        if not getattr(self.scheduler, "auto_mode", False):
            raise CpiScheduleError("schedule_auto_mode_required")
        self.heartbeat("running")
        try:
            result = self.scheduler.due_check()
        except Exception:
            self.heartbeat("error")
            raise
        view = self.heartbeat("idle")
        return {"action": result.get("action", "not_due"), **view}
