"""Explicit opt-in, exact ignored capture, one transaction namespace only."""

import json
import os
import tomllib
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import Kind, digest
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.historical import cpi_retained_handoff as cpi
from market_data_foundation.storage.postgres_runtime_config import load_pg_config

pytestmark = pytest.mark.postgres_integration
SCHEMA = "mdf_p4_macro_bls_cpi_retained_handoff_r1"
TRANSACTION = "MDF-P4-MACRO-BLS-CPI-RETAINED-HANDOFF-R1"
ARTIFACT_ROOT = Path("artifacts/macro-bls-cpi-retained-handoff-r1")


def _connect():
    runtime = load_pg_config(Path("config/local/runtime.toml").absolute())
    secret = tomllib.loads(Path("config/local/secret.toml").read_text())["postgres"]
    assert runtime.host == "192.168.10.4"
    try:
        # Publication's prechecks are reads. Autocommit leaves the accepted
        # kernel's publication connection idle; ordinary writes use a transaction.
        return psycopg.connect(
            **runtime.connection_kwargs(),
            password=secret["password"],
            connect_timeout=5,
            autocommit=True,
        )
    except psycopg.Error as exc:
        raise AssertionError("pg_connection_failed:" + type(exc).__name__) from None


def _receipt():
    root = Path(os.environ["MDF_CPI_HANDOFF_EVIDENCE_DIR"])
    assert root.resolve() == (ARTIFACT_ROOT / "capture-001").resolve()
    manifest = json.loads((root / "manifest.json").read_bytes())
    assert manifest["transaction_id"] == TRANSACTION
    assert len(manifest["members"]) == 4
    assert {m["path"] for m in manifest["members"]} == {
        "current.json",
        "retained.json",
        "original.bin",
        "lineage.json",
    }
    for member in manifest["members"]:
        content = (root / member["path"]).read_bytes()
        assert len(content) == member["size_bytes"]
        assert digest(content) == member["sha256"]
    receipt = cpi.CpiHandoffReceipt(
        (root / "current.json").read_bytes(),
        (root / "retained.json").read_bytes(),
        (root / "original.bin").read_bytes(),
        (root / "lineage.json").read_bytes(),
    )
    cpi.admit(receipt)
    return receipt, digest((root / "manifest.json").read_bytes())


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
        assert row[0] in {"192.168.10.4", "192.168.10.4/32"}
        assert row[1] == 5432
        assert row[3] == "on" and row[4] is False and row[5] is True
        assert row[6] is False
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


def test_exact_cpi_handoff_durable_readback_and_cleanup():
    if os.environ.get("MDF_RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("private PostgreSQL requires explicit opt-in")
    if "MDF_CPI_HANDOFF_EVIDENCE_DIR" not in os.environ:
        pytest.skip("requires the exact existing ignored CPI evidence bundle")
    assert os.environ.get("MDF_PG_TEST_SCHEMA") == SCHEMA
    receipt, manifest_sha = _receipt()
    evidence = {
        "transaction_id": TRANSACTION,
        "schema": SCHEMA,
        "manifest_sha256": manifest_sha,
        "raw_sha256": cpi.RAW_SHA256,
        "raw_size": cpi.RAW_SIZE,
        "semantic_record_sha256": cpi.SEMANTIC_RECORD_SHA256,
    }
    created = False
    try:
        with _connect() as preflight:
            evidence["preflight"] = _preflight(preflight)
        with _connect() as connection:
            ledger = PostgresLedger(connection, SCHEMA)
            assert not ledger.namespace_exists()
            started = ledger.server_now()
            finished = ledger.server_now()
            frozen = ledger.server_now()
            plan = cpi.build_handoff_plan(
                receipt,
                identity="p4-cpi-retained-handoff-pg-proof",
                started_at=started,
                finished_at=finished,
                frozen_at=frozen,
            )
            with connection.transaction():
                ledger.create_namespace()
            created = True
            kernel = CanonicalKernel(ledger, ledger.server_now)
            with connection.transaction():
                assert cpi.persist_handoff(kernel, plan) == plan.version
            publication = cpi.publish_handoff(kernel, plan)
            assert publication.decision_at >= frozen
            assert publication.usable_at == publication.decision_at
        # All writer connections have closed. This is durable, fresh readback.
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
                readback = cpi.readback_materials(
                    observed.get_content(plan.version.key)
                )
                assert readback.raw_bytes == receipt.raw_bytes
                assert readback.retained_bytes == receipt.retained_bytes
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
                read_kernel = CanonicalKernel(observed, observed.server_now)
                for at in (
                    cpi._time(cpi.OBTAINED),
                    cpi._time(cpi.USABLE),
                    publication.decision_at - timedelta(microseconds=1),
                ):
                    assert not read_kernel.eligible(plan.version, at)
                assert read_kernel.eligible(plan.version, publication.decision_at)
                assert len(observed.publications_for_version(plan.version.key)) == 1
                evidence["integration"] = {
                    "verdict": "PASS",
                    "counts": counts,
                    "acquisitions": 0,
                    "source_bindings": 0,
                    "publications": 1,
                    "content_sha256": digest(plan.content),
                    "exact_raw_and_semantic_readback": True,
                    "reconnected_run_material_version_publication_readback": True,
                    "temporal_revision_transform_readback": True,
                    "historical_as_of_excludes_predecision_publication": True,
                    "run_started_at": started.isoformat(),
                    "run_finished_at": finished.isoformat(),
                    "frozen_at": frozen.isoformat(),
                    "decision_at": publication.decision_at.isoformat(),
                    "publication_usable_at": publication.usable_at.isoformat(),
                    "mdd_obtained_at": cpi.OBTAINED,
                    "mdd_usable_at": cpi.USABLE,
                    "provider_revision_state": "not_declared",
                    "provider_released_at": None,
                    "provider_vintage": None,
                    "parser_revision": cpi.PARSER,
                    "observation_change_identity": cpi.OBSERVATION_CHANGE,
                }
    except Exception as exc:
        # Safe observations contain classifications, never raw exception text.
        evidence["failure_classification"] = type(exc).__name__
        raise AssertionError("cpi_pg_proof_failed:" + type(exc).__name__) from None
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
                    present = PostgresLedger(postflight, SCHEMA).namespace_exists()
                    observed_at = PostgresLedger(postflight, SCHEMA).server_now()
                    evidence["postflight"] = {
                        "transaction_read_only": "on",
                        "namespace_exists": present,
                        "observed_at": observed_at.isoformat(),
                    }
                    assert present is False
        except Exception as exc:
            evidence["cleanup_failure_classification"] = type(exc).__name__
            raise AssertionError(
                "cpi_pg_cleanup_failed:" + type(exc).__name__
            ) from None
        finally:
            ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
            (ARTIFACT_ROOT / "pg-proof.json").write_text(
                json.dumps(evidence, indent=2) + "\n"
            )
