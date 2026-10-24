import json
import os
import tomllib
from datetime import datetime
from pathlib import Path

import psycopg
import pytest

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.historical.tdx_live_daily import (
    GuardedLiveReceipt,
    build_live_plan,
    persist_live_plan_before_publication,
    publish_live_plan,
)
from market_data_foundation.storage.postgres_runtime_config import load_pg_config

pytestmark = pytest.mark.postgres_integration

SCHEMA = "mdf_p4_tdx_daily_bars_live_admission_r1"


def _configuration():
    runtime = load_pg_config(Path("config/local/runtime.toml").absolute())
    secret = tomllib.loads(Path("config/local/secret.toml").read_text())["postgres"]
    assert runtime.host == "192.168.10.4"
    return runtime, secret


def _connect(*, autocommit: bool = False):
    runtime, secret = _configuration()
    return psycopg.connect(
        **runtime.connection_kwargs(),
        password=secret["password"],
        connect_timeout=5,
        autocommit=autocommit,
    )


def _load_receipt() -> GuardedLiveReceipt:
    root = Path(os.environ["MDF_TDX_LIVE_EVIDENCE_DIR"])
    observation = json.loads((root / "observation.json").read_text())
    guard = json.loads((root / "guard-result.json").read_text())
    hashes = json.loads((root / "source-hashes.json").read_text())
    timing = json.loads((root / "outer-timing.json").read_text())
    return GuardedLiveReceipt(
        observation=observation,
        raw_projection=(root / "raw.json").read_bytes(),
        guard_result=guard,
        probe_sha256=hashes["tdx_r1_probe.py"],
        guard_sha256=hashes["tdx_bounds.py"],
        artifact_ref=timing["artifact_ref"],
        guarded_started_at=datetime.fromisoformat(timing["started_at"]),
        guarded_finished_at=datetime.fromisoformat(timing["finished_at"]),
    )


def test_live_tdx_daily_admission_postgres_restart_readback() -> None:
    if os.environ.get("MDF_RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("private PostgreSQL integration is explicit opt-in")
    if "MDF_TDX_LIVE_EVIDENCE_DIR" not in os.environ:
        pytest.skip("requires the single authorized live evidence directory")
    assert os.environ.get("MDF_PG_TEST_SCHEMA") == SCHEMA

    receipt = _load_receipt()
    candidate = build_live_plan(
        receipt,
        run_identity="p4-tdx-live-pg-run",
        acquisition_identity="p4-tdx-live-pg-acquisition",
        material_identity="p4-tdx-live-pg-material",
        version_identity="p4-tdx-live-pg-version",
        publication_identity="p4-tdx-live-pg-publication",
        publication_occurrence="p4-tdx-live-pg-occurrence",
    )

    connection = _connect()
    ledger = PostgresLedger(connection, SCHEMA)
    assert not ledger.namespace_exists()

    try:
        ledger.create_namespace()
        connection.commit()

        kernel = CanonicalKernel(ledger, ledger.server_now)
        stored = persist_live_plan_before_publication(kernel, candidate)
        assert stored == candidate.version
        assert candidate.acquisition.provider_data_attempts == 1
        assert candidate.acquisition.source_observed_at is None
        connection.commit()

        publication = publish_live_plan(kernel, candidate)

        observer = _connect(autocommit=True)
        try:
            observer_ledger = PostgresLedger(observer, SCHEMA)
            assert observer_ledger.get(candidate.run.key) == candidate.run
            assert (
                observer_ledger.get(candidate.acquisition.key) == candidate.acquisition
            )
            assert observer_ledger.get(candidate.material.key) == candidate.material
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
        assert ledger.get(candidate.acquisition.key) == candidate.acquisition
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
