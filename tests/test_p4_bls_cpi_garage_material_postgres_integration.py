"""Transaction-scoped PostgreSQL proof using only an in-memory material store."""

import os
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from test_p4_bls_cpi_direct import encode, live_evidence, response

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import ContractError, digest
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.historical import bls_cpi_direct as direct
from market_data_foundation.historical import bls_cpi_material_store as material_store
from market_data_foundation.historical.bls_cpi_material_store import (
    InMemoryMaterialStore,
    MaterialIdentity,
    PostgresAdvisoryWriterProtection,
    materialize_persist_publish,
)

pytestmark = pytest.mark.postgres_integration

SCHEMA = "mdf_p4_bls_cpi_garage_material_r1"
BASE = datetime(2026, 10, 6, tzinfo=UTC)


def _connect(*, autocommit=False):
    runtime = tomllib.loads(Path("config/local/runtime.toml").read_text())["postgres"]
    password = os.environ["MDF_POSTGRES_PASSWORD"]
    assert runtime["host"] == "192.168.10.4"
    return psycopg.connect(
        host=runtime["host"],
        port=runtime["port"],
        dbname=runtime["database"],
        user=runtime["user"],
        sslmode=runtime["sslmode"],
        password=password,
        connect_timeout=5,
        autocommit=autocommit,
    )


def test_fake_material_verification_pg_handoff_publication_and_cleanup():
    if os.environ.get("MDF_RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("private PostgreSQL integration is explicit opt-in")
    if "MDF_POSTGRES_PASSWORD" not in os.environ:
        pytest.skip("PostgreSQL password must be supplied without reading secret.toml")
    assert os.environ.get("MDF_PG_TEST_SCHEMA") == SCHEMA
    raw = encode(response())
    plan = direct.build_future_live_plan(
        live_evidence(raw),
        identity="p4-bls-cpi-garage-material-pg",
        finished_at=BASE + timedelta(seconds=2),
        frozen_at=BASE + timedelta(seconds=3),
    )
    connection = _connect()
    ledger = PostgresLedger(connection, SCHEMA)
    assert not ledger.namespace_exists()
    try:
        ledger.create_namespace()
        connection.commit()
        result = materialize_persist_publish(
            CanonicalKernel(ledger, ledger.server_now),
            plan,
            raw,
            InMemoryMaterialStore(),
            PostgresAdvisoryWriterProtection(connection),
            authoritative_commit=connection.commit,
        )
        connection.close()
        connection = _connect()
        observed = PostgresLedger(connection, SCHEMA)
        assert observed.get(result.plan.raw_material.key) == result.plan.raw_material
        assert observed.get(result.version.key) == result.version
        assert observed.get_content(result.version.key) == plan.content
        assert (
            observed.publication_by_occurrence(plan.publication_request.occurrence)
            == result.publication
        )
        assert result.plan.binding_candidate.limitations[0] == (
            "candidate_only_not_active"
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
            assert cleanup_ledger.namespace_exists() is False
        finally:
            cleanup.close()


def test_session_advisory_lock_excludes_competitor_until_confirmed_release():
    if os.environ.get("MDF_RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("private PostgreSQL integration is explicit opt-in")
    if "MDF_POSTGRES_PASSWORD" not in os.environ:
        pytest.skip("PostgreSQL password must be supplied without reading secret.toml")
    raw = encode(response())
    identity = MaterialIdentity(digest(raw), len(raw))
    owner = _connect()
    competitor = _connect()
    try:
        owner_protection = PostgresAdvisoryWriterProtection(owner)
        competitor_protection = PostgresAdvisoryWriterProtection(competitor)
        ticket = owner_protection.acquire(identity)
        ticket.assert_held(identity)
        keys = material_store._advisory_keys(identity)
        observed_keys = owner.execute(
            """SELECT classid::bigint, objid::bigint FROM pg_locks
            WHERE locktype = 'advisory' AND pid = pg_backend_pid()
              AND objsubid = 2 AND granted"""
        ).fetchall()
        assert keys in observed_keys
        assert all(0 <= key <= 2**31 - 1 for key in keys)
        with pytest.raises(ContractError, match="material_writer_busy"):
            competitor_protection.acquire(identity)
        ticket.release(identity)
        competitor_ticket = competitor_protection.acquire(identity)
        competitor_ticket.assert_held(identity)
        competitor_ticket.release(identity)
    finally:
        owner.close()
        competitor.close()
