"""Offline PPI strategy persistence, bounded requests and shared admission."""

import json
from datetime import timedelta

import pytest
from test_cpi_auto_strategy_scheduler import ProviderError
from test_cpi_release_schedule import Clock, calendar_html
from test_cpi_timeseries_source import WindowTransport
from test_ppi_release_schedule import OCTOBER, make

from market_data_foundation.historical.bls_cpi_direct_live import HttpResult
from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.historical.cpi_auto_strategy import CpiAutoStrategy
from market_data_foundation.historical.cpi_release_schedule import (
    AUTO_HTTP_CAP,
    CpiReleaseScheduler,
    CpiScheduleError,
)
from market_data_foundation.historical.cpi_request_control import CpiRequestControl
from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesService
from market_data_foundation.historical.cpi_timeseries_source import (
    BlsRangeSource,
    CpiSourceError,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    InMemoryTimeSeriesStore,
)
from market_data_foundation.historical.ppi_auto_strategy import PpiAutoStrategy
from market_data_foundation.historical.ppi_release_schedule import PpiReleaseScheduler


def auto_for(scheduler, clock, tmp_path):
    return PpiAutoStrategy(scheduler, tmp_path / "ppi-strategy.json", clock=clock)


def test_pause_enable_restart_and_history_baseline_are_durable(tmp_path):
    scheduler, service, clock = make(tmp_path, auto_mode=True)
    auto = auto_for(scheduler, clock, tmp_path)
    assert auto.get()["history_start"] == "2010-04"
    assert not auto.get()["exists"]
    assert not auto.strategy_path.exists()
    assert auto.tick()["action"] == "paused"
    assert not scheduler.state_path.exists()
    assert service.updates == []
    auto.update(enabled=True, history_start="2010-04")
    assert json.loads(auto.strategy_path.read_text())["asset"] == "ppi"
    assert auto.tick()["action"] == "updated"
    before = scheduler.state_path.read_bytes()
    auto.update(enabled=False, history_start="2010-04")
    assert auto_for(scheduler, clock, tmp_path).tick()["action"] == "paused"
    assert scheduler.state_path.read_bytes() == before
    assert len(service.updates) == 1
    assert json.loads(before)["asset"] == "ppi"


@pytest.mark.parametrize("asset", [None, "cpi"])
def test_ppi_strategy_refuses_unlabelled_and_cpi_files(tmp_path, asset):
    scheduler, service, clock = make(tmp_path, auto_mode=True)
    auto = auto_for(scheduler, clock, tmp_path)
    value = {"version": 1, "enabled": True, "history_start": "2010-04"}
    if asset:
        value["asset"] = asset
    auto.strategy_path.write_text(json.dumps(value))
    before = auto.strategy_path.read_bytes()
    with pytest.raises(CpiScheduleError, match="schedule_strategy_asset_mismatch"):
        auto.tick()
    assert auto.strategy_path.read_bytes() == before
    assert service.updates == []
    assert not scheduler.state_path.exists()


def test_strategy_checks_maintenance_asset_before_binding_history(tmp_path):
    scheduler, _, clock = make(tmp_path, auto_mode=True)
    value = scheduler._read()
    value["asset"] = "cpi"
    value["history_start"] = "1999-01"
    scheduler.state_path.write_text(json.dumps(value))
    with pytest.raises(CpiScheduleError, match="schedule_asset_mismatch"):
        auto_for(scheduler, clock, tmp_path).get()
    assert scheduler.history_start == "2010-04"


def test_enabled_tick_rejects_wrong_maintenance_asset_before_heartbeat_write(tmp_path):
    scheduler, service, clock = make(tmp_path, auto_mode=True)
    auto = auto_for(scheduler, clock, tmp_path)
    auto.update(enabled=True, history_start="2010-04")
    before = auto.strategy_path.read_bytes()
    value = scheduler._read()
    value["asset"] = "cpi"
    scheduler.state_path.write_text(json.dumps(value))
    with pytest.raises(CpiScheduleError, match="schedule_asset_mismatch"):
        auto.tick()
    assert auto.strategy_path.read_bytes() == before
    assert service.updates == []


def test_cpi_legacy_strategy_still_loads_but_rejects_ppi_and_wrong_wrapper(tmp_path):
    scheduler = CpiReleaseScheduler(None, tmp_path / "cpi.json")
    auto = CpiAutoStrategy(scheduler, tmp_path / "cpi-strategy.json")
    value = {"version": 1, "enabled": False, "history_start": "2000-01"}
    auto.strategy_path.write_text(json.dumps(value))
    assert auto.get()["history_start"] == "2000-01"
    value["asset"] = "ppi"
    auto.strategy_path.write_text(json.dumps(value))
    with pytest.raises(CpiScheduleError, match="schedule_strategy_asset_mismatch"):
        auto.get()
    with pytest.raises(CpiScheduleError, match="schedule_strategy_asset_mismatch"):
        PpiAutoStrategy(scheduler, tmp_path / "wrong.json")


def test_early_capture_requires_post_release_check_then_one_history_pass(tmp_path):
    scheduler, service, clock = make(
        tmp_path, auto_mode=True, at="2026-11-13T13:29:30+00:00"
    )
    assert scheduler.due_check()["action"] == "early_detected"
    assert scheduler.status()["targets"][0]["target"]["state"] != "complete"
    clock.advance(30)
    assert scheduler.due_check()["action"] == "updated"
    assert len(service.updates) == 2
    assert scheduler.due_check()["action"] == "not_due"
    clock.advance(7205)
    result = scheduler.due_check()
    assert result["action"] == "history_updated"
    assert service.updates[-1] == ("2010-04", "2026-10", PPI_PROFILE.series_ids)
    assert result["signals"]["state"] == "unsupported"
    assert scheduler.due_check()["action"] == "not_due"
    assert len(service.updates) == 3


def test_event_cap_stops_at_thirty_and_restart_does_not_reset_it(tmp_path):
    scheduler, service, clock = make(tmp_path, auto_mode=True)
    service.available = False
    scheduler.refresh_calendar()
    state = scheduler._read()
    target = state["targets"]["2026-10"]
    scheduler._auto_target_state(target, clock())["actual_http"] = AUTO_HTTP_CAP - 1
    scheduler._save(state)
    result = scheduler.due_check()
    assert result["targets"][0]["auto"]["actual_http"] == 30
    assert result["targets"][0]["auto"]["state"] == "stopped"
    assert len(service.updates) == 1
    restarted, _, _ = make(tmp_path, auto_mode=True)
    restarted.service = service
    restarted.due_check()
    assert len(service.updates) == 1
    assert restarted.status()["targets"][0]["auto"]["actual_http"] == 30


def test_429_honors_retry_after_and_zero_attempt_shared_refusal(tmp_path):
    scheduler, service, clock = make(tmp_path, auto_mode=True)
    retry_at = clock() + timedelta(seconds=900)
    service.error = ProviderError(
        "cpi_timeseries_rate_limited", status=429, retry_at=retry_at
    )
    result = scheduler.due_check()
    target = result["targets"][0]
    assert target["auto"]["next_attempt_at"] == retry_at.isoformat()
    assert target["auto"]["actual_http"] == 1
    clock.advance(899)
    assert scheduler.due_check()["action"] == "not_due"
    assert len(service.updates) == 1
    clock.advance(1)
    retry_at = clock() + timedelta(seconds=300)
    service.error = ProviderError(
        "cpi_timeseries_request_budget_exhausted", attempts=0, retry_at=retry_at
    )
    result = scheduler.due_check()
    target = result["targets"][0]
    assert target["auto"]["actual_http"] == 1
    assert target["target"]["attempts"] == 1
    assert target["auto"]["next_attempt_at"] == retry_at.isoformat()
    assert not target["target_available"]


def test_enabled_strategy_requires_auto_mode_without_collecting(tmp_path):
    scheduler, service, clock = make(tmp_path)
    auto = auto_for(scheduler, clock, tmp_path)
    auto.update(enabled=True, history_start="2010-04")
    with pytest.raises(CpiScheduleError, match="schedule_auto_mode_required"):
        auto.tick()
    assert service.updates == []


def test_real_profile_service_and_source_share_request_cooldown_with_cpi(tmp_path):
    clock = Clock("2026-11-13T13:30:00+00:00")
    control = CpiRequestControl(
        tmp_path / "shared-requests.json", clock=clock, min_interval_seconds=0
    )

    class RateLimitedTransport:
        calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            return HttpResult(429, b"", {"Retry-After": "900"})

    transport = RateLimitedTransport()
    source = BlsRangeSource(
        profile=PPI_PROFILE,
        registered=False,
        transport=transport,
        clock=clock,
        max_transport_attempts=1,
        request_control=control,
    )
    service = CpiTimeSeriesService(
        InMemoryTimeSeriesStore(profile=PPI_PROFILE, clock=clock),
        source,
        profile=PPI_PROFILE,
    )
    scheduler = PpiReleaseScheduler(
        service,
        tmp_path / "real-profile-schedule.json",
        clock=clock,
        history_start="2010-04",
        auto_mode=True,
        calendar_fetcher=lambda: calendar_html(OCTOBER),
        reference_fetcher=None,
    )
    result = scheduler.due_check()
    assert result["targets"][0]["auto"]["actual_http"] == transport.calls == 1
    assert (
        result["targets"][0]["auto"]["next_attempt_at"]
        == (clock() + timedelta(seconds=900)).isoformat()
    )
    cpi_transport = WindowTransport()
    cpi_source = BlsRangeSource(
        profile=CPI_PROFILE,
        registered=False,
        transport=cpi_transport,
        clock=clock,
        max_transport_attempts=1,
        request_control=control,
    )
    with pytest.raises(CpiSourceError, match="cooldown") as error:
        cpi_source.fetch(CPI_PROFILE.series_ids, "2026-10", "2026-10")
    assert error.value.attempts == 0
    assert cpi_transport.requests == []
    assert len(json.loads(control.state_path.read_text())["requests"]) == 1


def test_real_profile_service_completes_only_ppi_points(tmp_path):
    clock = Clock("2026-11-13T13:30:00+00:00")
    transport = WindowTransport()
    source = BlsRangeSource(
        profile=PPI_PROFILE,
        registered=False,
        transport=transport,
        clock=clock,
        max_transport_attempts=1,
    )
    service = CpiTimeSeriesService(
        InMemoryTimeSeriesStore(profile=PPI_PROFILE, clock=clock),
        source,
        profile=PPI_PROFILE,
    )
    scheduler = PpiReleaseScheduler(
        service,
        tmp_path / "real-profile-schedule.json",
        clock=clock,
        history_start="2010-04",
        auto_mode=True,
        calendar_fetcher=lambda: calendar_html(OCTOBER),
        reference_fetcher=None,
    )
    result = scheduler.due_check()
    assert result["action"] == "updated"
    assert result["targets"][0]["verified_target_series"] == list(
        PPI_PROFILE.series_ids
    )
    assert transport.requests[0]["seriesid"] == list(PPI_PROFILE.series_ids)
