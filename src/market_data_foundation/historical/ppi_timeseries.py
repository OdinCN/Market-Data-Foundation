"""PPI defaults for the shared BLS continuous time-series implementation."""

from market_data_foundation.historical import cpi_timeseries as _core
from market_data_foundation.historical.bls_timeseries_profile import PPI_PROFILE

SERIES_IDS = PPI_PROFILE.series_ids
SERIES_METADATA = PPI_PROFILE.series_metadata
CALCULATION_METHOD = _core.CALCULATION_METHOD
PpiTimeSeriesError = _core.CpiTimeSeriesError
month_range = _core.month_range


class PpiTimeSeriesService(_core.CpiTimeSeriesService):
    def __init__(self, store, source, *, profile=PPI_PROFILE):
        super().__init__(store, source, profile=profile)


def build_source(*, profile=PPI_PROFILE, **kwargs):
    return _core.build_source(profile=profile, **kwargs)


def build_service(*, profile=PPI_PROFILE, **kwargs):
    return _core.build_service(profile=profile, **kwargs)
