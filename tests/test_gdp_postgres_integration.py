"""Opt-in temporary-schema PostgreSQL parity. No provider or Garage calls."""

import os
from datetime import timedelta
from uuid import uuid4

import pytest

from market_data_foundation.historical.gdp_store import PostgresGdpStore
from market_data_foundation.historical.gdp_timeseries import (
    GdpTimeSeriesService,
    build_service,
)
from tests.test_gdp_chain import service
from tests.test_gdp_source import AT

pytestmark = pytest.mark.postgres_integration


def test_quarterly_postgres_memory_parity_idempotency_cutoffs_and_views():
    if os.getenv("MDF_GDP_PG_TEST") != "1":
        pytest.skip("Explicit isolated GDP PostgreSQL opt-in required")
    runtime = build_service()
    conn = runtime.store.connection
    schema = "test_gdp_" + uuid4().hex
    memory, wire, clock, archive = service()
    pg = PostgresGdpStore(conn, archive, schema=schema, clock=memory.store.clock)
    candidate = GdpTimeSeriesService(pg, memory.source)
    try:
        assert (
            conn.execute(
                "SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)
            ).fetchone()
            is None
        )
        pg.ensure_schema()
        pg.ensure_schema()
        for s in (memory, candidate):
            r = s.init_range("2025-Q4", "2026-Q3")
            assert r["saved_points"] == 64
            assert r["saved_revisions"] == 0
        assert candidate.query_range("2025-Q4", "2026-Q3") == memory.query_range(
            "2025-Q4", "2026-Q3"
        )
        assert candidate.init_range("2025-Q4", "2026-Q3")["provider_requests"] == 0
        clock[0] += timedelta(days=1)
        wire.overrides["A191RX", "2026Q3"] = "26,400,000.1353"
        for s in (memory, candidate):
            assert s.update_range("2026-Q3", "2026-Q3")["saved_revisions"] == 1
        for cutoff in (None, AT + timedelta(hours=1), AT + timedelta(days=2)):
            assert candidate.query_range(
                "2025-Q4", "2026-Q3", as_of=cutoff
            ) == memory.query_range("2025-Q4", "2026-Q3", as_of=cutoff)
        rates = conn.execute(
            f"SELECT value,change_1q_ratio,change_4q_ratio,value_unit "
            f"FROM {schema}.gdp_timeseries_rates WHERE series_id='A191RX' "
            "AND quarter='2026-07-01'"
        ).fetchone()
        point = candidate.query_range("2026-Q3", "2026-Q3", series_ids=("A191RX",))[
            "points"
        ][0]
        assert [str(v) for v in rates[:3]] == [
            point["value"],
            point["change_1q_ratio"],
            point["change_4q_ratio"],
        ]
        assert rates[3] == "million_chained_2017_USD"
        with pytest.raises(Exception), conn.transaction():
            conn.execute(
                f"UPDATE {schema}.revisions SET quarter='2026-08-01' "
                "WHERE series_id='A191RX'"
            )
        assert pg.status()["observation_count"] == 65
        assert pg.status()["revision_count"] == 1
    finally:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        runtime.close()
