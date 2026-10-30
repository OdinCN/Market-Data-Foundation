"""Explicitly enabled PPI strategy, sharing bounded release collection policy."""

from market_data_foundation.historical.bls_timeseries_profile import PPI_PROFILE
from market_data_foundation.historical.cpi_auto_strategy import (
    AUTO_POLICY,
    CpiAutoStrategy,
)
from market_data_foundation.historical.cpi_release_schedule import (
    CpiScheduleError,
    now_utc,
)


class PpiAutoStrategy(CpiAutoStrategy):
    def __init__(self, scheduler, strategy_path, *, clock=now_utc):
        if scheduler.profile is not PPI_PROFILE:
            raise CpiScheduleError("schedule_strategy_asset_mismatch")
        super().__init__(scheduler, strategy_path, clock=clock)


__all__ = ["AUTO_POLICY", "PpiAutoStrategy"]
