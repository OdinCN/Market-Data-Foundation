"""Synthetic-only PostgreSQL proof for the pre-provider BLS live candidate."""

import os
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from test_p4_bls_cpi_direct import encode, response

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import RunKind
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.historical import bls_cpi_direct as direct

pytestmark = pytest.mark.postgres_integration

SCHEMA = "mdf_p4_macro_bls_cpi_direct_live_r1"
BASE = datetime(2026, 10, 6, tzinfo=UTC)


def _connect(*, autocommit=False):
    runtime = tomllib.loads(Path("config/local/runtime.toml").read_text())["postgres"]
    secret = tomllib.loads(Path("config/local/secret.toml").read_text())["postgres"]
    assert runtime["host"] == "192.168.10.4"
    try:
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
    except psycopg.Error:
        raise AssertionError("bls_cpi_live_pg_connection_failed") from None


def _synthetic_plan():
    request = direct.BlsCpiRequest()
    evidence = direct.DirectCpiLiveEvidence(
        raw_bytes=encode(response()),
        request=request,
        request_identity=request.identity,
        requested_at=BASE,
        obtained_at=BASE + timedelta(seconds=1),
        acquisition_identity="p4-bls-cpi-direct-live-synthetic-pg-acquisition",
        provider_data_attempts=1,
        controlled_external_receipt=(
            "synthetic://p4-bls-cpi-direct-live/pre-provider-pg-proof"
        ),
    )
    return direct.build_future_live_plan(
        evidence,
        identity="p4-bls-cpi-direct-live-synthetic-pg",
        finished_at=BASE + timedelta(seconds=2),
        frozen_at=BASE + timedelta(seconds=3),
    )


def test_synthetic_live_plan_restart_readback_publication_and_cleanup():
    if os.environ.get("MDF_RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("private PostgreSQL integration is explicit opt-in")
    assert os.environ.get("MDF_PG_TEST_SCHEMA") == SCHEMA
    plan = _synthetic_plan()
    connection = _connect()
    ledger = PostgresLedger(connection, SCHEMA)
    assert not ledger.namespace_exists()
    try:
        ledger.create_namespace()
        connection.commit()
        kernel = CanonicalKernel(ledger, ledger.server_now)
        assert direct.persist_live_plan_before_publication(kernel, plan) == plan.version
        connection.commit()
        publication = direct.publish_live_plan(kernel, plan)
        connection.close()

        connection = _connect()
        observed = PostgresLedger(connection, SCHEMA)
        for record in (
            plan.source,
            plan.binding_candidate,
            plan.run,
            plan.acquisition,
            plan.raw_material,
            plan.semantic_material,
            plan.version,
        ):
            assert observed.get(record.key) == record
        assert observed.get_content(plan.version.key) == plan.content
        assert (
            observed.publication_by_occurrence(plan.publication_request.occurrence)
            == publication
        )
        assert plan.run.kind == RunKind.COLLECT
        assert plan.acquisition.provider_data_attempts == 1
        assert plan.acquisition.request_identity == direct.BlsCpiRequest().identity
        assert plan.version.binding == plan.binding_candidate.key
        assert plan.version.mapping == "bls-cpi-headlines"
        assert plan.version.acquisitions == (plan.acquisition.key,)
        assert "candidate_only_not_active" in plan.binding_candidate.limitations
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
            assert cleanup_ledger.namespace_exists() is False
        finally:
            cleanup.close()
