"""Print-only additive migration; Root applies it in an owner transaction."""

from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    PCE_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.historical.cpi_timeseries_store import _rates_view_sql
from market_data_foundation.historical.employment_timeseries import (
    five_series_migration_sql,
)
from market_data_foundation.historical.retail_timeseries_store import rates_view_sql


def migration_sql(*, retail_schema="mdf_retail_timeseries_dev_r1"):
    statements = [
        "-- Owner transaction required. Rehearse with ROLLBACK; no data rewrite.",
        "SET LOCAL lock_timeout = '5s';",
    ]
    for profile in (CPI_PROFILE, PPI_PROFILE):
        schema = profile.schema
        admitted = ",".join("'" + sid + "'" for sid in profile.series_ids)
        statements.append(f"""SELECT pg_advisory_xact_lock({profile.lock_id});
ALTER TABLE {schema}.revisions
 DROP CONSTRAINT revisions_series_id_check,
 ADD CONSTRAINT revisions_series_id_check CHECK(series_id IN ({admitted}));""")
    statements.extend(
        (
            five_series_migration_sql(),
            _rates_view_sql(profile=PCE_PROFILE) + ";",
            rates_view_sql(schema=retail_schema),
        )
    )
    return "\n".join(statements) + "\n"


if __name__ == "__main__":
    print(migration_sql(), end="")
