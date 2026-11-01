"""Root-only readback of the explicitly prepared and initialized DEV schema.

No DDL, provider call, runtime change or numeric write. Never runs in normal CI.
"""

import os
import tomllib
from pathlib import Path

import pytest

from market_data_foundation.historical.bls_timeseries_profile import (
    CONSUMPTION_PROFILE as P,
)
from market_data_foundation.storage.market_dataset_postgres import connect_dev

pytestmark = pytest.mark.postgres_integration


def test_prepared_native_schema_and_readonly_role_after_root_initialization():
    if os.getenv("MDF_CONSUMPTION_PG_READBACK") != "1":
        pytest.skip("Root explicit readback opt-in required")
    runtime = Path(os.environ["MDF_CONSUMPTION_RUNTIME"])
    secret = Path(os.environ["MDF_CONSUMPTION_SECRET"])
    password = tomllib.loads(secret.read_text())["postgres"]["password"]
    connection = connect_dev(password, runtime_path=runtime)
    try:
        with connection.transaction():
            connection.execute("SET TRANSACTION READ ONLY")
            rows = connection.execute(
                "SELECT table_name,column_name FROM information_schema.columns "
                "WHERE table_schema=%s",
                (P.schema,),
            ).fetchall()
            assert ("revisions", "value") in rows
            assert not any(
                col == "index_value" or col.startswith("bls_pct") for _, col in rows
            )
            amounts = connection.execute(
                "SELECT series_id,value,value_unit,annual_rate,chained_base "
                f"FROM {P.schema}.latest WHERE month='2026-09-01' "
                "AND version_status='active'"
            ).fetchall()
            assert {r[0] for r in amounts} == set(P.series_ids)
            assert all(r[1] is not None and r[1].is_finite() for r in amounts)
            assert all(r[3] == (r[0] != "A072RC") for r in amounts)
            grants = connection.execute(
                "SELECT c.relname,has_table_privilege('odin_dba',c.oid,'SELECT'),"
                "has_table_privilege('odin_dba',c.oid,"
                "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=%s AND c.relkind IN ('r','v')",
                (P.schema,),
            ).fetchall()
            assert grants and all(select and not write for _, select, write in grants)
            defaults = connection.execute(
                "SELECT x.privilege_type FROM pg_default_acl d "
                "JOIN pg_namespace n ON n.oid=d.defaclnamespace, "
                "LATERAL aclexplode(d.defaclacl) x "
                "WHERE n.nspname=%s AND x.grantee='odin_dba'::regrole "
                "AND d.defaclobjtype='r'",
                (P.schema,),
            ).fetchall()
            assert {r[0] for r in defaults} == {"SELECT"}
            assert (
                connection.execute(
                    f"SELECT count(*) FROM {P.schema}.release_events"
                ).fetchone()[0]
                >= 1
            )
    finally:
        connection.close()
