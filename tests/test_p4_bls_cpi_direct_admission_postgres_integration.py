"""Explicit offline-only PG proof in the active transaction namespace."""

import json
import os
import tomllib
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import Kind, digest
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.historical import bls_cpi_direct as direct
from market_data_foundation.historical import cpi_retained_handoff as retained

pytestmark = pytest.mark.postgres_integration
SCHEMA = "mdf_p4_macro_bls_cpi_direct_admission_r1"
TRANSACTION = "MDF-P4-MACRO-BLS-CPI-DIRECT-ADMISSION-R1"
SOURCE_TRANSACTION = "MDF-P4-MACRO-BLS-CPI-RETAINED-HANDOFF-R1"
RETAINED_ROOT = Path("artifacts/macro-bls-cpi-retained-handoff-r1/capture-001")
PROOF_ROOT = Path("artifacts/macro-bls-cpi-direct-admission-r1")


def _connect():
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
            autocommit=True,
        )
    except psycopg.Error as exc:
        raise AssertionError("pg_connection_failed:" + type(exc).__name__) from None


def _offline_replay():
    root = Path(os.environ["MDF_CPI_DIRECT_OFFLINE_EVIDENCE_DIR"])
    assert root.resolve() == RETAINED_ROOT.resolve()
    manifest_bytes = (root / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest["transaction_id"] == SOURCE_TRANSACTION
    assert {member["path"] for member in manifest["members"]} == {
        "current.json",
        "retained.json",
        "original.bin",
        "lineage.json",
    }
    for member in manifest["members"]:
        content = (root / member["path"]).read_bytes()
        assert len(content) == member["size_bytes"]
        assert digest(content) == member["sha256"]
    handoff = retained.CpiHandoffReceipt(
        (root / "current.json").read_bytes(),
        (root / "retained.json").read_bytes(),
        (root / "original.bin").read_bytes(),
        (root / "lineage.json").read_bytes(),
    )
    retained.admit(handoff)
    parsed = direct.parse_response(handoff.raw_bytes)
    retained_data = json.loads(handoff.retained_bytes)["retained"]["data"]
    assert parsed.data == retained_data
    equivalence = digest(
        json.dumps(
            parsed.data, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    )
    return handoff.raw_bytes, digest(manifest_bytes), equivalence


def _preflight(connection):
    with connection.transaction():
        connection.execute("SET TRANSACTION READ ONLY")
        row = connection.execute(
            """SELECT inet_server_addr()::text, inet_server_port(),
                      current_setting('server_version'),
                      current_setting('transaction_read_only'), pg_is_in_recovery(),
                      has_database_privilege(current_database(), 'CREATE'),
                      EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s),
                      clock_timestamp()""",
            (SCHEMA,),
        ).fetchone()
        return {
            "server_address": row[0],
            "server_port": row[1],
            "server_version": row[2],
            "transaction_read_only": row[3],
            "in_recovery": row[4],
            "create_privilege": row[5],
            "namespace_exists": row[6],
            "observed_at": row[7].isoformat(),
            "mutation": False,
            "verdict": "PASS",
        }


def test_offline_direct_parser_durable_readback_and_cleanup():
    if os.environ.get("MDF_RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("private PostgreSQL requires explicit opt-in")
    if "MDF_CPI_DIRECT_OFFLINE_EVIDENCE_DIR" not in os.environ:
        pytest.skip("requires exact existing ignored offline evidence")
    assert os.environ.get("MDF_PG_TEST_SCHEMA") == SCHEMA
    raw_bytes, manifest_sha, equivalence = _offline_replay()
    evidence = {
        "transaction_id": TRANSACTION,
        "schema": SCHEMA,
        "evidence_mode": "offline_retained_replay_not_provider_acquisition",
        "manifest_sha256": manifest_sha,
        "raw_sha256": digest(raw_bytes),
        "semantic_equivalence_sha256": equivalence,
        "retained_semantic_record_sha256": retained.SEMANTIC_RECORD_SHA256,
        "provider_request_executed": False,
        "acquisition_count": 0,
    }
    created = False
    try:
        with _connect() as preflight:
            evidence["preflight"] = _preflight(preflight)
        assert evidence["preflight"]["server_address"] in {
            "192.168.10.4",
            "192.168.10.4/32",
        }
        assert evidence["preflight"]["server_port"] == 5432
        assert evidence["preflight"]["transaction_read_only"] == "on"
        assert evidence["preflight"]["in_recovery"] is False
        assert evidence["preflight"]["create_privilege"] is True
        if evidence["preflight"]["namespace_exists"]:
            with _connect() as stale_cleanup:
                stale_ledger = PostgresLedger(stale_cleanup, SCHEMA)
                with stale_cleanup.transaction():
                    stale_ledger.drop_namespace()
            evidence["preexisting_namespace_cleanup"] = {
                "exact_allowlisted_schema_only": True,
                "verdict": "PASS",
            }
        with _connect() as connection:
            ledger = PostgresLedger(connection, SCHEMA)
            assert not ledger.namespace_exists()
            started = ledger.server_now()
            finished = ledger.server_now()
            frozen = ledger.server_now()
            plan = direct.build_offline_replay_plan(
                raw_bytes,
                identity="p4-bls-cpi-direct-offline-pg-proof",
                started_at=started,
                finished_at=finished,
                frozen_at=frozen,
            )
            with connection.transaction():
                ledger.create_namespace()
            created = True
            kernel = CanonicalKernel(ledger, ledger.server_now)
            with connection.transaction():
                assert direct.persist_offline_replay(kernel, plan) == plan.version
            publication = direct.publish_offline_replay(kernel, plan)
        with _connect() as observer:
            observed = PostgresLedger(observer, SCHEMA)
            with observer.transaction():
                observer.execute("SET TRANSACTION READ ONLY")
                for record in (
                    plan.run,
                    plan.raw_material,
                    plan.semantic_material,
                    plan.version,
                ):
                    assert observed.get(record.key) == record
                assert observed.get_content(plan.version.key) == plan.content
                assert (
                    observed.publication_by_occurrence(
                        plan.publication_request.occurrence
                    )
                    == publication
                )
                assert plan.run.binding is None
                assert plan.version.binding is plan.version.mapping is None
                assert plan.version.acquisitions == ()
                counts = dict(
                    observer.execute(
                        sql.SQL(
                            "SELECT kind, count(*) FROM {}.records GROUP BY kind"
                        ).format(sql.Identifier(SCHEMA))
                    ).fetchall()
                )
                assert counts == {
                    Kind.DEFINITION.value: 1,
                    Kind.RUN.value: 1,
                    Kind.MATERIAL.value: 2,
                    Kind.VERSION.value: 1,
                }
                assert Kind.ACQUISITION.value not in counts
                assert Kind.BINDING.value not in counts
                evidence["integration"] = {
                    "verdict": "PASS",
                    "counts": counts,
                    "acquisition_count": 0,
                    "source_binding_count": 0,
                    "publication_count": 1,
                    "exact_raw_readback": (
                        json.loads(plan.content)["raw"]["sha256"] == digest(raw_bytes)
                    ),
                    "semantic_equivalence_readback": (
                        digest(
                            json.dumps(
                                json.loads(plan.content)["data"],
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ).encode()
                        )
                        == equivalence
                    ),
                    "reconnected_readback": True,
                    "run_kind": plan.run.kind.value,
                    "evidence_mode": "offline_retained_replay_not_acquisition",
                    "provider_revision_state": "not_declared",
                    "provider_vintage": None,
                    "provider_released_at": None,
                    "parser_revision": direct.PARSER_REVISION,
                }
    except Exception as exc:
        evidence["failure_classification"] = type(exc).__name__
        raise AssertionError(
            "cpi_direct_pg_proof_failed:" + type(exc).__name__
        ) from None
    finally:
        try:
            if created:
                with _connect() as cleanup:
                    ledger = PostgresLedger(cleanup, SCHEMA)
                    assert ledger.namespace_exists()
                    with cleanup.transaction():
                        ledger.drop_namespace()
                evidence["cleanup"] = {"explicit_drop": True, "verdict": "PASS"}
            with _connect() as postflight:
                with postflight.transaction():
                    postflight.execute("SET TRANSACTION READ ONLY")
                    observed = PostgresLedger(postflight, SCHEMA)
                    present = observed.namespace_exists()
                    evidence["postflight"] = {
                        "transaction_read_only": "on",
                        "namespace_exists": present,
                        "observed_at": observed.server_now().isoformat(),
                    }
                    assert present is False
        except Exception as exc:
            evidence["cleanup_failure_classification"] = type(exc).__name__
            raise AssertionError(
                "cpi_direct_pg_cleanup_failed:" + type(exc).__name__
            ) from None
        finally:
            PROOF_ROOT.mkdir(parents=True, exist_ok=True)
            (PROOF_ROOT / "pg-proof.json").write_text(
                json.dumps(evidence, indent=2) + "\n"
            )
