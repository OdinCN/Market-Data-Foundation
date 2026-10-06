"""Bounded real-backend proof for the frozen P6 consumer-read contract."""

import os
import tomllib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from test_p4_bls_cpi_direct import encode, response

from market_data_foundation.canonical.consumer_read import (
    read_as_of,
    read_exact_version,
)
from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.domain_contracts.dense_market import RAW_DAILY_BARS
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from market_data_foundation.historical import bls_cpi_direct as cpi
from market_data_foundation.historical.daily_bars import (
    TdxDailyReplayBatch,
    TdxStockDailyRawRow,
    build_replay_plan,
    persist_replay_before_publication,
    publish_replay,
)

pytestmark = pytest.mark.postgres_integration

SCHEMA = "mdf_p6_consumer_read_r1"
BASE = datetime(2026, 10, 6, tzinfo=UTC)


def _connect(*, autocommit: bool = False):
    runtime = tomllib.loads(Path("config/local/runtime.toml").read_text())["postgres"]
    assert runtime["host"] == "192.168.10.4"
    return psycopg.connect(
        host=runtime["host"],
        port=runtime["port"],
        dbname=runtime["database"],
        user=runtime["user"],
        sslmode=runtime["sslmode"],
        password=os.environ["MDF_POSTGRES_PASSWORD"],
        connect_timeout=5,
        autocommit=autocommit,
    )


def _cpi_plan():
    request = cpi.BlsCpiRequest()
    evidence = cpi.DirectCpiLiveEvidence(
        raw_bytes=encode(response()),
        request=request,
        request_identity=request.identity,
        requested_at=BASE,
        obtained_at=BASE + timedelta(seconds=1),
        acquisition_identity="p6-consumer-read-cpi-acquisition",
        provider_data_attempts=1,
        controlled_external_receipt=(
            "synthetic://p6-consumer-read/cpi-external-receipt"
        ),
    )
    return cpi.build_future_live_plan(
        evidence,
        identity="p6-consumer-read-cpi",
        finished_at=BASE + timedelta(seconds=2),
        frozen_at=BASE + timedelta(seconds=3),
    )


def _daily_bars_plan():
    batch = TdxDailyReplayBatch(
        fixture_id="p6-consumer-read-daily-bars",
        security_id="600000.SH",
        identity_namespace="provider_suffix",
        requested_start=date(2024, 1, 2),
        requested_end=date(2024, 1, 4),
        rows=(
            TdxStockDailyRawRow(
                "2024-01-02T15:00:00",
                10000,
                11000,
                9000,
                10500,
                100,
                105000000,
            ),
        ),
    )
    return build_replay_plan(
        batch,
        run_identity="p6-consumer-read-bars-run",
        material_identity="p6-consumer-read-bars-material",
        version_identity="p6-consumer-read-bars-version",
        publication_identity="p6-consumer-read-bars-publication",
        publication_occurrence="p6-consumer-read-bars-occurrence",
        started_at=BASE,
        finished_at=BASE + timedelta(seconds=1),
        frozen_at=BASE + timedelta(seconds=2),
    )


def _storage_snapshot(connection) -> dict[str, object]:
    schema = sql.Identifier(SCHEMA)
    records = tuple(
        connection.execute(
            sql.SQL(
                "SELECT kind, identity, revision FROM {}.records "
                "ORDER BY kind, identity, revision"
            ).format(schema)
        ).fetchall()
    )
    contents = tuple(
        connection.execute(
            sql.SQL(
                "SELECT version_identity, digest FROM {}.contents "
                "ORDER BY version_identity"
            ).format(schema)
        ).fetchall()
    )
    publications = tuple(
        connection.execute(
            sql.SQL(
                "SELECT identity, occurrence, version_identity "
                "FROM {}.publications ORDER BY identity"
            ).format(schema)
        ).fetchall()
    )
    return {
        "counts": (len(records), len(contents), len(publications)),
        "records": records,
        "contents": contents,
        "publications": publications,
    }


def _admitted_at(publication):
    instants = [publication.decision_at, publication.usable_at]
    if publication.request.effective_at is not None:
        instants.append(publication.request.effective_at)
    return max(instants)


def test_consumer_reads_survive_reconnect_without_mutating_postgres() -> None:
    if os.environ.get("MDF_RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("private PostgreSQL integration is explicit opt-in")
    if "MDF_POSTGRES_PASSWORD" not in os.environ:
        pytest.skip("PostgreSQL password must be supplied through the environment")
    if "MDF_PG_TEST_SCHEMA" not in os.environ:
        pytest.skip("exact PostgreSQL test schema must be explicitly selected")
    assert os.environ["MDF_PG_TEST_SCHEMA"] == SCHEMA

    cpi_plan = _cpi_plan()
    bars_plan = _daily_bars_plan()
    writer = None
    try:
        writer = _connect()
        ledger = PostgresLedger(writer, SCHEMA)
        assert ledger.namespace_exists() is False
        ledger.create_namespace()
        writer.commit()

        kernel = CanonicalKernel(ledger, ledger.server_now)
        assert cpi.persist_live_plan_before_publication(kernel, cpi_plan) == (
            cpi_plan.version
        )
        assert persist_replay_before_publication(kernel, bars_plan) == bars_plan.version
        writer.commit()
        cpi_publication = cpi.publish_live_plan(kernel, cpi_plan)
        bars_publication = publish_replay(kernel, bars_plan)
        writer.close()
        writer = None

        with _connect() as observer:
            observed = PostgresLedger(observer, SCHEMA)
            with observer.transaction():
                observer.execute("SET TRANSACTION READ ONLY")
                assert observer.execute("SHOW transaction_read_only").fetchone()[0] == (
                    "on"
                )
                before = _storage_snapshot(observer)
                assert before["counts"] == (14, 2, 2)

                cpi_at = _admitted_at(cpi_publication)
                bars_at = _admitted_at(bars_publication)
                cpi_exact = read_exact_version(
                    observed,
                    BLS_CPI_HEADLINES.key,
                    cpi_plan.version.key,
                    as_of=cpi_at,
                )
                cpi_as_of = read_as_of(observed, BLS_CPI_HEADLINES.key, as_of=cpi_at)
                bars_exact = read_exact_version(
                    observed,
                    RAW_DAILY_BARS.key,
                    bars_plan.version.key,
                    as_of=bars_at,
                )
                bars_as_of = read_as_of(observed, RAW_DAILY_BARS.key, as_of=bars_at)

                for view in (cpi_exact, cpi_as_of):
                    assert view.definition == BLS_CPI_HEADLINES
                    assert view.version == cpi_plan.version
                    assert view.content == cpi_plan.content
                    assert view.publication == cpi_publication
                    assert view.binding == cpi_plan.binding_candidate
                    assert view.source == cpi_plan.source
                    assert tuple(item.identity for item in view.acquisitions) == (
                        cpi_plan.acquisition.identity,
                    )
                    assert view.acquisitions[0].external_receipt_present is True
                    assert all(item.controlled_ref_present for item in view.materials)
                    assert "external_receipt" not in vars(view.acquisitions[0])
                    assert all(
                        "controlled_ref" not in vars(item) for item in view.materials
                    )

                for view in (bars_exact, bars_as_of):
                    assert view.definition == RAW_DAILY_BARS
                    assert view.version == bars_plan.version
                    assert view.content == bars_plan.content
                    assert view.publication == bars_publication
                    assert view.binding is None
                    assert view.source is None
                    assert view.acquisitions == ()
                    assert tuple(item.identity for item in view.materials) == (
                        bars_plan.material.identity,
                    )
                    assert view.materials[0].controlled_ref_present is True

                public_projection = repr((cpi_exact, cpi_as_of, bars_exact, bars_as_of))
                private_values = (
                    cpi_plan.acquisition.external_receipt,
                    cpi_plan.raw_material.controlled_ref,
                    cpi_plan.semantic_material.controlled_ref,
                    bars_plan.material.controlled_ref,
                )
                assert all(value is not None for value in private_values)
                assert all(value not in public_projection for value in private_values)

                after = _storage_snapshot(observer)
                assert after == before
    finally:
        if writer is not None:
            writer.close()
        with _connect(autocommit=True) as cleanup:
            cleanup_ledger = PostgresLedger(cleanup, SCHEMA)
            if cleanup_ledger.namespace_exists():
                cleanup_ledger.drop_namespace()
        with _connect() as final_observer:
            final_ledger = PostgresLedger(final_observer, SCHEMA)
            with final_observer.transaction():
                final_observer.execute("SET TRANSACTION READ ONLY")
                assert final_ledger.namespace_exists() is False
