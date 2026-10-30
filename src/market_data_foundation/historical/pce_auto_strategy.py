"""Explicit PCE strategy and heartbeat with a BEA-specific scheduling policy."""

from market_data_foundation.historical.bls_timeseries_profile import PCE_PROFILE
from market_data_foundation.historical.cpi_auto_strategy import CpiAutoStrategy
from market_data_foundation.historical.cpi_release_schedule import (
    CpiScheduleError,
    now_utc,
)

AUTO_POLICY = {
    "provider": "BEA",
    "history_priority": "low",
    "history_batch_months": 60,
    "minimum_operation_interval_seconds": 3.1,
    "pre_release_probe_seconds": 30,
    "max_attempts_per_stage": 10,
    "retry_seconds": [15, 30, 60, 120, 300, 900, 1800, 3600],
    "event_max_reserved_http": 30,
    "rolling_24h_max_reserved_http": 100,
    "initial_history": "manual_range_only",
    "calendar_reference_mapping": "explicit_official_months_only",
    "official_limits": {
        "requests_per_minute": 100,
        "megabytes_per_minute": 100,
        "errors_per_minute": 30,
    },
    "transport_limits": "independent_bea_source_control",
}


class PceAutoStrategy(CpiAutoStrategy):
    def __init__(self, scheduler, strategy_path, *, clock=now_utc):
        if scheduler.profile is not PCE_PROFILE:
            raise CpiScheduleError("schedule_strategy_asset_mismatch")
        super().__init__(scheduler, strategy_path, clock=clock)

    def _view(self, value):
        return {**super()._view(value), "policy": AUTO_POLICY}
