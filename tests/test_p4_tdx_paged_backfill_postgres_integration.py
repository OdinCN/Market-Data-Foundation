import os
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.historical.tdx_paged_backfill import (
    RetainedPagedHistoryReceipt,
    build_paged_backfill_plan,
    persist_paged_backfill_before_publication,
    publish_paged_backfill,
)

pytestmark = pytest.mark.postgres_integration

SCHEMA = "mdf_p4_tdx_daily_bars_paged_backfill_r1"
BASE = datetime(2026, 10, 5, 12, 30, tzinfo=UTC)


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


def _retained_receipt() -> RetainedPagedHistoryReceipt:
    root = Path(os.environ["MDF_TDX_PAGED_EVIDENCE_DIR"])
    history = root / "daily-SH-main-history.raw.json"
    page0 = root / "daily-SH-main-page0.raw.json"
    page2 = root / "daily-SH-main-page2.raw.json"
    return RetainedPagedHistoryReceipt(
        history_bytes=history.read_bytes(),
        page0_bytes=page0.read_bytes(),
        page2_bytes=page2.read_bytes(),
        history_ref=(
            "artifact://tdx-r1/successor/tdx-python/controlled/"
            "daily-SH-main-history.raw.json"
        ),
        page0_ref=(
            "artifact://tdx-r1/successor/tdx-python/controlled/"
            "daily-SH-main-page0.raw.json"
        ),
        page2_ref=(
            "artifact://tdx-r1/successor/tdx-python/controlled/"
            "daily-SH-main-page2.raw.json"
        ),
    )


def _plan():
    return build_paged_backfill_plan(
        _retained_receipt(),
        run_identity="p4-paged-pg-run",
        material_identity="p4-paged-pg-material",
        version_identity="p4-paged-pg-version",
        publication_identity="p4-paged-pg-publication",
        publication_occurrence="p4-paged-pg-occurrence",
        started_at=BASE,
        finished_at=BASE + timedelta(seconds=1),
        frozen_at=BASE + timedelta(seconds=2),
    )


def test_paged_backfill_postgres_restart_readback() -> None:
    if os.environ.get("MDF_RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("private PostgreSQL integration is explicit opt-in")
    if "MDF_TDX_PAGED_EVIDENCE_DIR" not in os.environ:
        pytest.skip("requires retained TDX R1 evidence directory")
    assert os.environ.get("MDF_PG_TEST_SCHEMA") == SCHEMA

    candidate = _plan()
    assert candidate.version.acquisitions == ()
    assert candidate.version.binding is None

    connection = _connect()
    ledger = PostgresLedger(connection, SCHEMA)
    assert not ledger.namespace_exists()

    try:
        ledger.create_namespace()
        connection.commit()

        kernel = CanonicalKernel(ledger, ledger.server_now)
        stored = persist_paged_backfill_before_publication(kernel, candidate)
        assert stored == candidate.version
        connection.commit()

        publication = publish_paged_backfill(kernel, candidate)

        observer = _connect(autocommit=True)
        try:
            observed = PostgresLedger(observer, SCHEMA)
            assert observed.get(candidate.run.key) == candidate.run
            assert observed.get(candidate.material.key) == candidate.material
            assert observed.get(candidate.version.key) == candidate.version
            assert observed.get_content(candidate.version.key) == candidate.content
            assert (
                observed.publication_by_occurrence(
                    candidate.publication_request.occurrence
                )
                == publication
            )
        finally:
            observer.close()

        connection.close()
        connection = _connect()
        ledger = PostgresLedger(connection, SCHEMA)
        assert ledger.get(candidate.run.key) == candidate.run
        assert ledger.get(candidate.material.key) == candidate.material
        assert ledger.get(candidate.version.key) == candidate.version
        assert ledger.get_content(candidate.version.key) == candidate.content
        assert (
            ledger.publication_by_occurrence(candidate.publication_request.occurrence)
            == publication
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
