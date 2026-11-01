"""Independent durable jobs over the shared BEA PIO official-calendar scheduler."""

from market_data_foundation.historical.bls_timeseries_profile import CONSUMPTION_PROFILE
from market_data_foundation.historical.cpi_auto_strategy import CpiAutoStrategy
from market_data_foundation.historical.cpi_release_schedule import (
    CpiScheduleError,
    now_utc,
)
from market_data_foundation.historical.pce_auto_strategy import AUTO_POLICY
from market_data_foundation.historical.pce_release_schedule import PceReleaseScheduler


class ConsumptionReleaseScheduler(PceReleaseScheduler):
    profile = CONSUMPTION_PROFILE
    asset_key = "consumption"
    series_ids = CONSUMPTION_PROFILE.series_ids


class ConsumptionAutoStrategy(CpiAutoStrategy):
    def __init__(self, scheduler, strategy_path, *, clock=now_utc):
        if scheduler.profile is not CONSUMPTION_PROFILE:
            raise CpiScheduleError("schedule_strategy_asset_mismatch")
        super().__init__(scheduler, strategy_path, clock=clock)

    def _view(self, value):
        return {**super()._view(value), "policy": AUTO_POLICY}
