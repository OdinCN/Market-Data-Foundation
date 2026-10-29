"""Thin PPI source defaults; public batch and point types remain shared."""

from market_data_foundation.historical import cpi_timeseries_source as _source
from market_data_foundation.historical.bls_timeseries_profile import PPI_PROFILE

SERIES_IDS = PPI_PROFILE.series_ids
MAX_RANGE_REQUESTS = _source.MAX_RANGE_REQUESTS
PpiSourceError = _source.CpiSourceError
SourceBatch = _source.SourceBatch
SourcePoint = _source.SourcePoint
year_windows = _source.year_windows


class BlsRangeSource(_source.BlsRangeSource):
    def __init__(self, *, profile=PPI_PROFILE, **kwargs):
        super().__init__(profile=profile, **kwargs)


def parse_response(
    raw, series_ids, start_month, end_month, *, profile=PPI_PROFILE, **kwargs
):
    return _source.parse_response(
        raw, series_ids, start_month, end_month, profile=profile, **kwargs
    )
