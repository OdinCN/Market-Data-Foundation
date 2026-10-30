"""PPI namespace defaults for the shared persistence ports."""

from market_data_foundation.historical import cpi_timeseries_store as _store
from market_data_foundation.historical.bls_timeseries_profile import PPI_PROFILE

SCHEMA = PPI_PROFILE.schema
RAW_PREFIX = PPI_PROFILE.raw_prefix
SERIES_IDS = PPI_PROFILE.series_ids
LOCK_ID = PPI_PROFILE.lock_id
PpiStorageError = _store.CpiStorageError


class GaragePpiRawStore(_store.GarageCpiRawStore):
    def __init__(self, credentials, *, profile=PPI_PROFILE, **kwargs):
        super().__init__(credentials, profile=profile, **kwargs)


class InMemoryTimeSeriesStore(_store.InMemoryTimeSeriesStore):
    def __init__(self, raw_store=None, *, profile=PPI_PROFILE, **kwargs):
        super().__init__(raw_store, profile=profile, **kwargs)


class PostgresTimeSeriesStore(_store.PostgresTimeSeriesStore):
    def __init__(self, connection, raw_store, *, profile=PPI_PROFILE, **kwargs):
        super().__init__(connection, raw_store, profile=profile, **kwargs)


def raw_locator(sha, *, profile=PPI_PROFILE):
    return _store.raw_locator(sha, profile=profile)
