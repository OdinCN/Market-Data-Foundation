"""PCE opt-in and truthful worker liveness, entirely offline."""

from datetime import timedelta
from unittest.mock import Mock

import pytest

from market_data_foundation.historical.bls_timeseries_profile import CPI_PROFILE
from market_data_foundation.historical.pce_auto_strategy import PceAutoStrategy
from market_data_foundation.historical.pce_release_schedule import PceScheduleError
from tests.test_cpi_timeseries_web import no_network  # noqa: F401
from tests.test_pce_release_schedule import NOW, scheduler


def test_not_started_does_not_create_enable_or_collect(tmp_path):
    value = scheduler(tmp_path)
    strategy = PceAutoStrategy(value, tmp_path / "strategy.json", clock=lambda: NOW)
    view = strategy.get()
    assert view["exists"] is False and view["enabled"] is False
    assert view["worker"]["state"] == "not_started"
    assert view["policy"]["provider"] == "BEA"
    assert "history_batch_calendar_years" not in view["policy"]
    assert not strategy.strategy_path.exists()


def test_paused_worker_heartbeat_never_collects(tmp_path):
    value = scheduler(tmp_path)
    value.due_check = Mock(side_effect=AssertionError("collection_forbidden"))
    strategy = PceAutoStrategy(value, tmp_path / "strategy.json", clock=lambda: NOW)
    assert strategy.tick()["action"] == "paused"
    assert strategy.get()["exists"] is False
    assert strategy.get()["worker"]["state"] == "paused"
    value.due_check.assert_not_called()


def test_explicit_enabled_strategy_ticks_only_own_scheduler(tmp_path):
    value = scheduler(tmp_path)
    value.due_check = Mock(return_value={"action": "not_due"})
    strategy = PceAutoStrategy(value, tmp_path / "strategy.json", clock=lambda: NOW)
    strategy.update(enabled=True, history_start="2006-10")
    assert strategy.tick()["enabled"] is True
    value.due_check.assert_called_once()
    strategy.clock = lambda: NOW + timedelta(minutes=3)
    assert strategy.get()["worker"]["state"] == "stale"


def test_other_asset_scheduler_is_rejected(tmp_path):
    with pytest.raises(PceScheduleError, match="schedule_strategy_asset_mismatch"):
        PceAutoStrategy(Mock(profile=CPI_PROFILE), tmp_path / "strategy.json")
