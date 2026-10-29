"""Explicit profile contract for the shared CLI operations; no global mutation."""

from market_data_foundation.historical.bls_timeseries_profile import CPI_PROFILE
from market_data_foundation.historical.cpi_timeseries_cli import execute


def execute_profile(service, action, params=None, *, profile=CPI_PROFILE):
    if getattr(service, "profile", CPI_PROFILE) is not profile:
        raise ValueError("bls_timeseries_profile_mismatch")
    return execute(service, action, params)
