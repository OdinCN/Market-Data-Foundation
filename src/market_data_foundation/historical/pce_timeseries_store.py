"""PCE namespace defaults for the shared two-table persistence ports."""

from market_data_foundation.historical import cpi_timeseries_store as _store
from market_data_foundation.historical.bls_timeseries_profile import PCE_PROFILE

SCHEMA = PCE_PROFILE.schema
RAW_PREFIX = PCE_PROFILE.raw_prefix
SERIES_IDS = PCE_PROFILE.series_ids
LOCK_ID = PCE_PROFILE.lock_id
PceStorageError = _store.CpiStorageError


class GaragePceRawStore(_store.GarageCpiRawStore):
    def __init__(self, credentials, *, profile=PCE_PROFILE, **kwargs):
        super().__init__(credentials, profile=profile, **kwargs)


class InMemoryTimeSeriesStore(_store.InMemoryTimeSeriesStore):
    def __init__(self, raw_store=None, *, profile=PCE_PROFILE, **kwargs):
        super().__init__(raw_store, profile=profile, **kwargs)


class PostgresTimeSeriesStore(_store.PostgresTimeSeriesStore):
    def __init__(self, connection, raw_store, *, profile=PCE_PROFILE, **kwargs):
        super().__init__(connection, raw_store, profile=profile, **kwargs)


def raw_locator(sha, *, profile=PCE_PROFILE):
    return _store.raw_locator(sha, profile=profile)
