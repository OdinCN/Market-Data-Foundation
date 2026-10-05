import os
import tomllib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.historical.daily_bars import (
    TdxDailyReplayBatch,
    TdxStockDailyRawRow,
    build_replay_plan,
    persist_replay_before_publication,
    publish_replay,
)

pytestmark = pytest.mark.postgres_integration

SCHEMA = "mdf_p4_raw_daily_bars_replay_r1"
BASE = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)


def _configuration():
    runtime = tomllib.loads(Path("config/local/runtime.toml").read_text())["postgres"]
    secret = tomllib.loads(Path("config/local/secret.toml").read_text())["postgres"]
    assert runtime["host"] == "192.168.10.4"
    return runtime, secret


def _connect(*, autocommit: bool = False):
    runtime, secret = _configuration()
    return psycopg.connect(
        host=runtime["host"],
        port=runtime["port"],
        dbname=runtime["database"],
        user=runtime["user"],
        sslmode=runtime["sslmode"],
        password=secret["password"],
        connect_timeout=5,
        autocommit=autocommit,
    )


def _raw(day: str, close: int = 10500) -> TdxStockDailyRawRow:
    return TdxStockDailyRawRow(
        time=f"{day}T15:00:00",
        open=10000,
        high=11000,
        low=9000,
        close=close,
        volume=100,
        amount=105000000,
    )


def _plan():
    batch = TdxDailyReplayBatch(
        fixture_id="tdx-r1-bars-daily-pg-replay",
        security_id="600000.SH",
        identity_namespace="provider_suffix",
        requested_start=date(2024, 1, 2),
        requested_end=date(2024, 1, 5),
        rows=(_raw("2024-01-02"), _raw("2024-01-04", 10600)),
    )
    return build_replay_plan(
        batch,
        run_identity="p4-pg-replay-run",
        material_identity="p4-pg-replay-material",
        version_identity="p4-pg-replay-version",
        publication_identity="p4-pg-replay-publication",
        publication_occurrence="p4-pg-replay-occurrence",
        started_at=BASE,
        finished_at=BASE + timedelta(seconds=1),
        frozen_at=BASE + timedelta(seconds=2),
    )


def test_p4_raw_daily_bars_replay_postgres_restart_readback() -> None:
    if os.environ.get("MDF_RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("private PostgreSQL integration is explicit opt-in")

    assert os.environ.get("MDF_PG_TEST_SCHEMA") == SCHEMA
    connection = _connect()
    ledger = PostgresLedger(connection, SCHEMA)
    assert not ledger.namespace_exists()

    candidate = _plan()
    try:
        ledger.create_namespace()
        connection.commit()

        kernel = CanonicalKernel(ledger, ledger.server_now)
        stored = persist_replay_before_publication(kernel, candidate)
        assert stored == candidate.version
        assert candidate.version.acquisitions == ()
        assert candidate.run.binding is None
        connection.commit()

        publication = publish_replay(kernel, candidate)

        observer = _connect(autocommit=True)
        try:
            observer_ledger = PostgresLedger(observer, SCHEMA)
            assert observer_ledger.get(candidate.source.key) == candidate.source
            assert observer_ledger.get(candidate.binding.key) == candidate.binding
            assert observer_ledger.get(candidate.version.key) == candidate.version
            assert (
                observer_ledger.get_content(candidate.version.key) == candidate.content
            )
            assert (
                observer_ledger.publication_by_occurrence(
                    candidate.publication_request.occurrence
                )
                == publication
            )
        finally:
            observer.close()

        connection.close()
        connection = _connect()
        ledger = PostgresLedger(connection, SCHEMA)
        kernel = CanonicalKernel(ledger, ledger.server_now)
        assert ledger.get(candidate.version.key) == candidate.version
        assert ledger.get_content(candidate.version.key) == candidate.content
        assert (
            ledger.publication_by_occurrence(candidate.publication_request.occurrence)
            == publication
        )
        assert kernel.eligible(
            candidate.version,
            publication.decision_at + timedelta(seconds=1),
        )
    finally:
        try:
            connection.close()
        except Exception:
            pass
        cleanup = _connect(autocommit=True)
        try:
            cleanup_ledger = PostgresLedger(cleanup, SCHEMA)
            if cleanup_ledger.namespace_exists():
                cleanup_ledger.drop_namespace()
        finally:
            cleanup.close()
