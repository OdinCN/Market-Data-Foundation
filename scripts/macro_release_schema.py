"""Print an additive release-record migration; never connect or execute DDL."""

import argparse
from types import SimpleNamespace

from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    EMPLOYMENT_PROFILE,
    PCE_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.historical.macro_release_records import migration_sql

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--asset",
        choices=("cpi", "ppi", "pce", "employment", "retail"),
        default="employment",
    )
    args = parser.parse_args()
    profiles = {
        p.key: p for p in (CPI_PROFILE, PPI_PROFILE, PCE_PROFILE, EMPLOYMENT_PROFILE)
    }
    print(
        migration_sql(
            profiles.get(args.asset)
            or SimpleNamespace(
                schema="mdf_retail_timeseries_dev_r1", lock_id=78328741642028105
            )
        )
    )
