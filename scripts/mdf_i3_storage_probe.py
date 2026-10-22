"""Explicit, bounded Host launcher for the dedicated DEV PG/Garage probe.

Run from the checkout with PYTHONPATH=src. Credentials are supplied by the Host
environment; this script never reads secret files or prints connection exceptions.
"""

import argparse
import json
import os
import re
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from market_data_foundation.canonical.market_material import byte_digest
from market_data_foundation.canonical.market_persistence import (
    CommitUnknown,
    Fault,
    Verification,
)
from market_data_foundation.canonical.market_projection import ProjectionError, ReadCode
from market_data_foundation.canonical.model import ContractError, require
from market_data_foundation.domain_contracts.market_universe import content_id
from market_data_foundation.historical.bls_cpi_material_store import GarageCredentials
from market_data_foundation.storage.market_dataset_garage import (
    BUCKET,
    ENDPOINT,
    RUN,
    GarageMarketMaterials,
)
from market_data_foundation.storage.market_dataset_postgres import (
    BINDING,
    DDL_PATH,
    SCHEMA,
    PostgresMarketDataset,
    connect_dev,
    migrate,
    preflight,
)
from market_data_foundation.storage.market_dataset_probe import synthetic_fixture

ENV = ("MDF_POSTGRES_PASSWORD", "MDF_GARAGE_ACCESS_KEY", "MDF_GARAGE_SECRET_KEY")


def classification(exc):
    if isinstance(exc, ContractError) and re.fullmatch(r"[a-z_]{1,79}", str(exc)):
        return str(exc).upper()
    if isinstance(exc, TimeoutError):
        return "WALL_BUDGET_EXHAUSTED"
    if isinstance(exc, OSError):
        return "HOST_IO_OR_NETWORK_FAILURE"
    return "STORAGE_OPERATION_FAILED"


def check_host_intake(path, run_id):
    value = json.loads(path.read_text())
    expected = {
        "run_id": run_id,
        "schema": SCHEMA,
        "bucket": BUCKET,
        "endpoint": ENDPOINT,
        "credential_scope": "MDF_DEDICATED",
        "bucket_read_write_delete": "VERIFIED",
        "probe_prefix_absent": "VERIFIED",
        "existing_materials_isolated": "VERIFIED",
        "disposable_probe_no_backup_dependency": True,
    }
    require(all(value.get(k) == v for k, v in expected.items()), "host_intake_mismatch")
    require(bool(value.get("evidence_ref")), "host_intake_evidence_missing")
    return {**expected, "intake_sha256": byte_digest(path.read_bytes())}


def run(options):
    os.umask(0o077)
    root = Path("artifacts").resolve()
    path = options.receipt.resolve()
    require(path.is_relative_to(root), "receipt_must_be_under_artifacts")
    path.parent.mkdir(parents=True, exist_ok=True)
    run_id = options.run_id or "i3-" + uuid4().hex
    require(RUN.fullmatch(run_id) is not None, "invalid_probe_run")
    checkout = Path(__file__).resolve().parents[1]
    candidate_paths = [
        DDL_PATH,
        *checkout.glob("scripts/mdf_i3_*.py"),
        *checkout.glob("src/market_data_foundation/storage/market_dataset_*.py"),
    ]
    report = {
        "schema_revision": "i3.host.probe.receipt.r1",
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "action": options.action,
        "target": {
            "pg_schema": SCHEMA,
            "pg_database": "market_data_foundation_dev",
            "pg_host": "192.168.10.4:5432",
            "garage_endpoint": ENDPOINT,
            "bucket": BUCKET,
            "prefix": f"market-datasets/v1/probes/{run_id}/",
        },
        "budget": {
            "wall_seconds": 120,
            "pg_connections": 3,
            "s3_requests": 96,
            "material_bytes": 8388608,
            "synthetic_materials": 1,
            "versions": 1,
            "publications": 1,
            "retries": 0,
        },
        "ddl_sha256": byte_digest(DDL_PATH.read_bytes()),
        "candidate_sha256": {
            str(p.relative_to(checkout)): byte_digest(p.read_bytes())
            for p in sorted(candidate_paths)
        },
        "effect_ledger": [],
        "status": "STARTED",
        "qualification": "DEV_SYNTHETIC_ONLY",
        "unknown": [
            "production_storage_adoption",
            "backup_PITR",
            "crash_recovery",
            "provider_semantics",
            "live_freshness",
            "representative_query_cost",
        ],
    }
    with path.open("x") as handle:
        json.dump(report, handle, indent=2)
    store = None
    connection = None
    started = time.monotonic()

    def save():
        if store is not None:
            report["garage_effects"] = store.effects
            report["garage_requests"] = store.requests
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        temporary = path.with_suffix(".pending")
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(path)

    def step(name, action):
        entry = {"operation": name, "effect": "UNKNOWN"}
        report["effect_ledger"].append(entry)
        save()
        result = action()
        entry["effect"] = "CONFIRMED"
        save()
        return result

    def alarm(*_):
        raise TimeoutError

    signal.signal(signal.SIGALRM, alarm)
    signal.alarm(120)
    try:
        report["credential_presence"] = {
            name: bool(os.environ.get(name)) for name in ENV
        }
        if not all(report["credential_presence"].values()):
            report.update(
                status="BLOCKED", exception_classification="CREDENTIALS_NOT_INJECTED"
            )
            return report
        connection = connect_dev(os.environ[ENV[0]])
        report["pg_intake"] = step("pg_readonly_intake", lambda: preflight(connection))
        if options.action == "intake":
            report["status"] = "READONLY_PG_INTAKE_COMPLETE_GARAGE_ACL_REQUIRES_HOST"
            return report
        require(options.opt_in_dev_write, "explicit_dev_write_opt_in_required")
        require(options.host_intake is not None, "host_intake_required")
        report["host_intake"] = check_host_intake(options.host_intake, run_id)
        require(
            not report["pg_intake"]["namespace_exists"], "postgres_namespace_conflict"
        )
        store = GarageMarketMaterials(
            GarageCredentials(os.environ[ENV[1]], os.environ[ENV[2]]),
            run_id,
            wall_seconds=120 - (time.monotonic() - started),
        )
        store.on_effect = save
        step("garage_conditional_create", store.qualify_conditional_create)
        report["migration"] = step("pg_migrate", lambda: migrate(connection, run_id))

        def locator(source, binding, contract, raw):
            return store.locator(
                "raw",
                content_id(source),
                content_id(binding),
                content_id(
                    (
                        contract.mapping_revision,
                        contract.upstream_revision,
                        contract.decoder_revision,
                    )
                ),
                byte_digest(raw),
            )

        material, raw, decoder, version, request, month = synthetic_fixture(
            run_id, locator
        )
        report["exact_identities"] = {
            "material": content_id(material.key),
            "version": content_id(version.key),
            "publication": content_id(request.key),
            "occurrence": request.occurrence,
            "locator": material.locator,
            "raw_sha256": material.digest,
            "source": content_id(material.source),
            "binding": content_id(material.binding),
            "mapping_revision": material.mapping_revision,
            "upstream_revision": material.upstream_revision,
            "decoder_revision": material.decode_revision,
            "fixture_id": material.fixture_id,
            "material_level": "raw",
            "evidence_mode": "synthetic",
        }
        ledger = PostgresMarketDataset(connection, store, (decoder,), owner_run=run_id)
        step("material_retain", lambda: ledger.retain(material, raw))

        def version_lost_ack():
            try:
                ledger.commit_version(version, Fault.AFTER_COMMIT)
            except CommitUnknown:
                return
            raise ContractError("expected_lost_ack_not_observed")

        step("version_commit_deliberate_lost_ack", version_lost_ack)
        connection.close()
        connection = connect_dev(os.environ[ENV[0]])
        preflight(connection)
        ledger = PostgresMarketDataset(connection, store, (decoder,), owner_run=run_id)
        report["version_reconciliation"] = ledger.verify(
            "version", content_id(version.key), content_id(version)
        ).value
        require(
            report["version_reconciliation"] == Verification.COMMITTED,
            "original_identity_reconciliation_failed",
        )
        for subject in version.admission_subjects:
            ledger.set_admission(subject, "synthetic-dev-only-r1", True)
        step("synthetic_publication", lambda: ledger.publish(request))
        step("projection_ready", lambda: ledger.rebuild(run_id + ".g1", (month,)))
        connection.close()
        connection = connect_dev(os.environ[ENV[0]])
        preflight(connection)
        ledger = PostgresMarketDataset(connection, store, (decoder,), owner_run=run_id)
        current_generation = run_id + ".g1"

        def readback():
            with ledger.read_snapshot(current_generation) as view:
                require(
                    view.content(version.key) == version.assessment.content,
                    "physical_content_readback_failed",
                )
                require(
                    view.histories((version.key,))[0][0].request == request,
                    "physical_publication_readback_failed",
                )
                require(
                    view.versions((version.key,), exact=True)[0].version == version.key,
                    "physical_exact_readback_failed",
                )
                return {
                    "content_sha256": version.content_digest,
                    "raw_sha256": byte_digest(store.read(material.locator, len(raw))),
                    "ledger_binding": BINDING,
                    "connection_restart": "VERIFIED",
                }

        report["readback"] = step("independent_readback", readback)

        def damage_owned_projection():
            # Only this newly created run's exact synthetic Version is affected.
            with ledger._write():
                ledger._sql(
                    "DELETE FROM {s}.projection_rows WHERE version_id=%s",
                    (content_id(version.key),),
                )

        step("owned_projection_single_table_failure", damage_owned_projection)

        def fail_closed():
            try:
                with ledger.read_snapshot(current_generation) as view:
                    view.content(version.key)
            except ProjectionError as exc:
                require(
                    exc.code == ReadCode.INCONSISTENT, "unexpected_readback_failure"
                )
                return
            raise ContractError("damaged_projection_was_served")

        step("damaged_projection_read_rejected", fail_closed)
        step("projection_suspended", ledger.suspend)
        current_generation = run_id + ".g2"
        step(
            "canonical_projection_rebuild",
            lambda: ledger.rebuild(current_generation, (month,)),
        )
        report["single_table_recovery"] = step("recovered_exact_readback", readback)
        if options.hold:
            report["cleanup"] = "HELD_FOR_EXPLICIT_HOST_CLEANUP_NO_AUTOMATIC_TTL"
        else:
            step("cleanup_owned_pg_schema", ledger.cleanup_probe_schema)
            require(
                not preflight(connection)["namespace_exists"], "pg_cleanup_unconfirmed"
            )
            step("cleanup_owned_garage_keys", store.cleanup_owned)
            report["cleanup"] = "EXACT_OBJECTS_ABSENT_VERIFIED"
        report["status"] = "DEV_SYNTHETIC_PROBE_COMPLETE"
    except Exception as exc:
        report.update(
            status="STOP",
            exception_classification=classification(exc),
            cleanup="NOT_AUTOMATIC_AFTER_FAILURE_RECONCILE_ORIGINAL_IDENTITIES",
        )
    finally:
        signal.alarm(0)
        if connection is not None:
            connection.close()
        save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("intake", "probe"), default="intake")
    parser.add_argument("--run-id")
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--opt-in-dev-write", action="store_true")
    parser.add_argument("--host-intake", type=Path)
    parser.add_argument("--hold", action="store_true")
    options = parser.parse_args()
    try:
        report = run(options)
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "receipt": str(options.receipt),
                    "exception_classification": report.get("exception_classification"),
                }
            )
        )
        return 0 if report["status"].endswith("COMPLETE") else 2
    except Exception as exc:
        print(
            json.dumps(
                {"status": "STOP", "exception_classification": classification(exc)}
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
