"""Print an additive release-record migration; never connect or execute DDL."""

from market_data_foundation.historical.bls_timeseries_profile import EMPLOYMENT_PROFILE
from market_data_foundation.historical.macro_release_records import migration_sql

if __name__ == "__main__":
    print(migration_sql(EMPLOYMENT_PROFILE))
