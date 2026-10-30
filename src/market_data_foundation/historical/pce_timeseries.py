"""PCE defaults for the shared continuous index and cutoff service."""

from market_data_foundation.historical import cpi_timeseries as _core
from market_data_foundation.historical.bls_timeseries_profile import PCE_PROFILE

SERIES_IDS = PCE_PROFILE.series_ids
SERIES_METADATA = PCE_PROFILE.series_metadata
CALCULATION_METHOD = _core.PCE_CALCULATION_METHOD
PceTimeSeriesError = _core.CpiTimeSeriesError
month_range = _core.month_range


class PceTimeSeriesService(_core.CpiTimeSeriesService):
    def __init__(self, store, source, *, profile=PCE_PROFILE):
        super().__init__(store, source, profile=profile)


def build_source(*, profile=PCE_PROFILE, **kwargs):
    return _core.build_source(profile=profile, **kwargs)


def build_service(*, profile=PCE_PROFILE, **kwargs):
    return _core.build_service(profile=profile, **kwargs)
