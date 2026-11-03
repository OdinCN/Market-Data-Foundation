"""The actual M3 strategy ports must never advertise inherited CPI windows."""

from datetime import UTC, datetime

from market_data_foundation.historical.m3_schedule import (
    POLICY,
    M3AutoStrategy,
    M3Scheduler,
)
from tests.test_m3_segment6 import CALENDAR


def test_all_strategy_views_use_implemented_m3_policy(tmp_path):
    now = datetime(2026, 11, 3, 19, tzinfo=UTC)
    scheduler = M3Scheduler(
        tmp_path / "calendar.json", history_start="2006-11", clock=lambda: now
    )
    scheduler.import_calendar(CALENDAR, acquired_at=now)
    strategy = M3AutoStrategy(scheduler, tmp_path / "strategy.json", clock=lambda: now)
    for view in (
        strategy.get(),
        strategy.update(enabled=False, history_start="2006-11"),
        strategy.heartbeat("paused"),
        strategy.tick(),
        scheduler.status(),
    ):
        assert view["policy"] == POLICY
        assert "release_windows" not in view["policy"]
        assert view["policy"]["retry_min_seconds"] == 300


def test_help_lists_actual_mom_parameter_and_missing_value_contract():
    from market_data_foundation.web.m3_query_api import PREFIX, M3QueryHost
    from tests.test_m3_segment3 import service
    from tests.test_m3_segment3_api import client

    s, _ = service()
    with client(M3QueryHost(read_factory=lambda: s, allow_read=True)) as http:
        help_data = http.get(PREFIX + "/api-help").json()
    assert help_data["parameters"]["include_mom"].startswith("false default")
    assert "zero_denominator" in help_data["fields"]["points[].mom_percent/mom_reason"]
    assert help_data["query_budget_seconds"] == 150
    assert help_data["web_timeout_seconds"] == 180
