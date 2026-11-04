import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from market_data_foundation.historical.worker_observation import (
    observe_worker,
    process_identity,
)
from market_data_foundation.web.macro_runtime_facts import collection_facts
from scripts.mdf_dev_deadline import permitted


def test_deadline_is_absolute_and_invalid_state_denies(tmp_path):
    path = tmp_path / "window.json"
    at = datetime(2026, 11, 4, tzinfo=UTC)
    assert not permitted(path, now=at)
    path.write_text(
        json.dumps(
            {
                "started_at": at.isoformat(),
                "expires_at": (at + timedelta(days=45)).isoformat(),
            }
        )
    )
    assert permitted(path, now=at + timedelta(days=44))
    assert not permitted(path, now=at + timedelta(days=45))
    assert not permitted(path, now=at - timedelta(seconds=1))


def test_dead_pid_and_reused_pid_never_report_running(tmp_path):
    worker = {"state": "stale", "process": {"pid": 999999999, "state": "running"}}
    result = observe_worker(worker, tmp_path / "strategy.json", "m3")
    assert result["state"] == result["process"]["state"] == "stopped"
    assert result["process"]["pid"] is None
    worker["process"] = {
        **process_identity(os.getpid()),
        "start_ticks": "wrong",
        "state": "running",
    }
    result = observe_worker(worker, tmp_path / "strategy.json", "m3")
    assert result["state"] == result["process"]["state"] == "unknown"
    assert result["process"]["pid"] is None


def test_heartbeat_and_unverified_completion_do_not_invent_success():
    @contextmanager
    def service():
        yield SimpleNamespace(
            status=lambda: {"latest_months": {"a": "2026-09", "b": None}}
        )

    records = [
        {"state": "complete", "finished_at": "2026-11-04T00:00:00Z", "result": {}}
    ]
    host = SimpleNamespace(
        profile=SimpleNamespace(key="employment"),
        service=service,
        executions=lambda: {"executions": records},
    )
    facts = collection_facts(host)
    assert facts["last_successful_collection_at"] is None
    assert facts["latest_data_period"] == "2026-09"
    records[0]["result"]["readback"] = {"verified": True}
    assert (
        collection_facts(host)["last_successful_collection_at"]
        == records[0]["finished_at"]
    )
