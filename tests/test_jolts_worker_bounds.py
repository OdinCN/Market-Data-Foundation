"""Deterministic deadline and zero-source DEV worker checks; no provider I/O."""

import importlib.util
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from market_data_foundation.historical.cpi_release_schedule import CpiScheduleError
from tests.test_jolts_timeseries import AT

spec = importlib.util.spec_from_file_location(
    "jolts_worker", Path("scripts/mdf_jolts_timeseries_worker.py")
)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


@pytest.mark.parametrize("due", [None, AT + timedelta(days=1)])
def test_idle_future_window_needs_no_source(due):
    snapshot = {
        "calendar": {"next_refresh_at": (AT + timedelta(hours=6)).isoformat()},
        "next_due": due.isoformat() if due else None,
    }
    host = SimpleNamespace(scheduler=SimpleNamespace(status=lambda: snapshot))
    worker.source_budget_guard(host, AT)


@pytest.mark.parametrize("calendar_due", [True, False])
def test_zero_source_worker_stops_before_unexpected_due_work(calendar_due):
    snapshot = {
        "calendar": {
            "next_refresh_at": (
                AT if calendar_due else AT + timedelta(hours=6)
            ).isoformat()
        },
        "next_due": None if calendar_due else AT.isoformat(),
    }
    host = SimpleNamespace(scheduler=SimpleNamespace(status=lambda: snapshot))
    with pytest.raises(CpiScheduleError, match="jolts_worker_source_budget_exhausted"):
        worker.source_budget_guard(host, AT)
    with pytest.raises(CpiScheduleError, match="jolts_worker_source_budget_exhausted"):
        worker.deny_upstream()


@pytest.mark.parametrize(
    "deadline", ["2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z", "2099-01-01T00:00:00"]
)
def test_deadline_rejected_before_host_or_credential_access(deadline):
    with pytest.raises(SystemExit) as error:
        worker.main(
            [
                "--runtime",
                "/unused/runtime",
                "--secret",
                "/unused/secret",
                "--package-store",
                "/unused/packages",
                "--state-directory",
                "/unused/state",
                "--environment-id",
                "fixture",
                "--opt-in-jolts-worker",
                "--allow-collect",
                "--deadline",
                deadline,
                "--no-upstream",
            ]
        )
    assert error.value.code == 2
