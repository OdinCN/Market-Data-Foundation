"""Explicit, bounded synthetic DEV acceptance. No provider, DELETE, DROP or service.

Credentials are read only in-process from the existing owner-local configuration.
Receipts contain classifications, never raw exceptions or credentials.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from market_data_foundation.canonical.market_material import byte_digest
from market_data_foundation.canonical.market_persistence import (
    CommitUnknown,
    Fault,
    InjectedFailure,
    Verification,
)
from market_data_foundation.canonical.model import ContractError, Key, Kind, require
from market_data_foundation.domain_contracts.market_universe import content_id
from market_data_foundation.historical.bls_cpi_material_store import GarageCredentials
from market_data_foundation.storage.market_dataset_garage import (
    BUCKET,
    ENDPOINT,
    SimpleGarageMaterials,
)
from market_data_foundation.storage.market_dataset_postgres import connect_dev
from market_data_foundation.storage.market_dataset_probe import synthetic_fixture
from market_data_foundation.storage.market_dataset_simple import (
    SimplePostgresDataset,
    migrate_simple,
    schema_for,
    simple_preflight,
)
from market_data_foundation.storage.postgres_runtime_config import load_pg_config

CONFIG = Path("/srv/market-data-foundation/config/local")


def credentials():
    load_pg_config(CONFIG / "runtime.toml")
    runtime = tomllib.loads((CONFIG / "runtime.toml").read_text())
    require(runtime["storage"]["backend"] == "s3", "runtime_backend_drift")
    s3 = runtime["s3"]
    require(
        s3["endpoint_url"] == ENDPOINT
        and s3["bucket"] == BUCKET
        and s3["region"] == "garage"
        and s3["key_prefix"] == ""
        and s3["addressing_style"] == "path",
        "runtime_garage_binding_drift",
    )
    secret = tomllib.loads((CONFIG / "secret.toml").read_text())
    return secret["postgres"]["password"], GarageCredentials(
        secret["s3"]["access_key_id"], secret["s3"]["secret_access_key"]
    )


def remaining_seconds(origin):
    now = time.time()
    remaining = origin + 295 - now
    require(origin <= now and remaining > 0, "original_run_wall_budget_exhausted")
    return remaining


def recovery_child(command, origin):
    remaining = remaining_seconds(origin)
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as child_process:
        try:
            child_process.communicate(timeout=min(120, remaining))
        except BaseException:
            child_process.kill()
            child_process.communicate()
            raise
        require(child_process.returncode == 0, "fresh_process_recovery_failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opt-in-dev-write", action="store_true")
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--resume-receipt", type=Path)
    parser.add_argument("--recover-child", help=argparse.SUPPRESS)
    parser.add_argument("--identity", help=argparse.SUPPRESS)
    parser.add_argument("--origin-epoch", type=float, help=argparse.SUPPRESS)
    opts = parser.parse_args()
    require(opts.opt_in_dev_write, "explicit_dev_write_opt_in_required")
    os.umask(0o077)
    receipt = opts.receipt.resolve()
    require(receipt.is_relative_to(Path("artifacts").resolve()), "receipt_scope")
    receipt.parent.mkdir(parents=True, exist_ok=True)
    previous = None
    if opts.resume_receipt:
        original_path = opts.resume_receipt.resolve()
        require(
            original_path.is_relative_to(Path("artifacts").resolve()), "resume_scope"
        )
        previous = json.loads(original_path.read_text())
        require(previous.get("result") == "STOP", "resume_requires_stopped_run")
        require(
            previous["garage_requests"] == 1
            and len(previous["garage_effects"]) == 1
            and previous["garage_effects"][0]["method"] == "GET"
            and previous["garage_effects"][0]["effect"] == "UNKNOWN"
            and previous.get("contract_code") == "garage_transport_or_response_unknown"
            and not any(
                key in previous
                for key in (
                    "child_receipt",
                    "child_garage_requests",
                    "previous_receipt",
                    "previous_garage_requests",
                    "total_garage_requests",
                )
            ),
            "resume_only_initial_get_failure",
        )
        require(
            previous["steps"][0]
            == dict(operation="create_one_isolated_schema", outcome="CONFIRMED"),
            "resume_requires_owned_schema",
        )
        origin = previous.get(
            "started_epoch", original_path.stat().st_mtime - previous["elapsed_seconds"]
        )
    else:
        require(
            opts.origin_epoch is None or opts.recover_child is not None,
            "origin_only_for_recovery_child",
        )
        origin = opts.origin_epoch if opts.origin_epoch is not None else time.time()
    remaining = remaining_seconds(origin)
    run_id = (
        previous["run_id"] if previous else opts.recover_child
    ) or "i3-" + uuid4().hex
    schema = schema_for(run_id)
    report = dict(
        run_id=run_id,
        started_epoch=origin,
        schema=schema,
        bucket=BUCKET,
        prefix=f"market-datasets/dev-simplified/{run_id}/",
        qualification="SYNTHETIC_DEV_ONLY",
        steps=[],
        retention="RETAINED_NO_DELETE_NO_AUTOMATIC_GC",
        budgets=dict(objects=16, bytes=4194304, requests=128, seconds=300),
    )
    if previous:
        report["previous_receipt"] = str(original_path)
        report["previous_garage_requests"] = previous["garage_requests"]
        report["previous_garage_effects"] = previous["garage_effects"]
    with receipt.open("x") as handle:
        json.dump(report, handle)
    started = time.monotonic()
    connection = store = None

    def save():
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["original_run_wall_seconds"] = round(time.time() - origin, 3)
        if store:
            report["garage_effects"] = store.effects
            report["garage_requests"] = store.requests
            report["object_intents"] = [
                dict(key=k, size=v) for k, v in store.intents.items()
            ]
        temporary = receipt.with_suffix(".pending")
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(receipt)

    def step(name, fn):
        entry = dict(operation=name, outcome="UNKNOWN")
        report["steps"].append(entry)
        save()
        value = fn()
        entry["outcome"] = "CONFIRMED"
        save()
        return value

    def alarm(*_):
        raise TimeoutError

    signal.signal(signal.SIGALRM, alarm)
    signal.setitimer(
        signal.ITIMER_REAL, min(115, remaining) if opts.recover_child else remaining
    )
    try:
        password, garage_creds = credentials()
        connection = connect_dev(password)
        report["preflight"] = simple_preflight(connection, run_id)
        store = SimpleGarageMaterials(
            garage_creds,
            run_id,
            max_requests=16
            if opts.recover_child
            else 80 - (previous["garage_requests"] if previous else 0),
            wall_seconds=min(120, remaining),
        )
        store.on_effect = save
        if opts.recover_child:
            require(report["preflight"]["namespace_exists"], "recovery_schema_missing")
            ledger = SimplePostgresDataset(connection, store, owner_run=run_id)
            step(
                "fresh_process_original_payload_recovery",
                lambda: ledger.recover(opts.identity),
            )
            report["pending_inventory"] = ledger.pending_inventory()
            report["result"] = "CHILD_RECOVERY_PASSED"
            return 0
        if previous:
            require(report["preflight"]["namespace_exists"], "resume_schema_missing")
            resumed = SimplePostgresDataset(connection, store, owner_run=run_id)
            with connection.transaction():
                resumed._identity()
            step("resume_original_schema_no_new_schema", lambda: None)
        else:
            require(
                not report["preflight"]["namespace_exists"], "fresh_schema_required"
            )
            step(
                "create_one_isolated_schema", lambda: migrate_simple(connection, run_id)
            )

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
        report["identities"] = dict(
            material=content_id(material.key),
            locator=material.locator,
            sha256=byte_digest(raw),
            size=len(raw),
            version=content_id(version.key),
            publication=content_id(request.key),
        )
        ledger = SimplePostgresDataset(connection, store, (decoder,), owner_run=run_id)

        def pg_failure():
            try:
                ledger.retain(material, raw, fault=Fault.BEFORE_COMMIT)
            except InjectedFailure:
                return
            raise ContractError("expected_pg_failure_missing")

        step("pg_rollback_leaves_object_and_pending", pg_failure)
        require(
            ledger.verify(
                "material", content_id(material.key), content_id((material, "raw"))
            )
            == Verification.ABSENT,
            "rollback_fact_present",
        )
        require(store.read(material.locator, len(raw)) == raw, "orphan_missing")
        connection.close()
        connection = None
        child_path = receipt.with_name(receipt.stem + "-child.json")

        def child_recover():
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--opt-in-dev-write",
                "--receipt",
                str(child_path),
                "--recover-child",
                run_id,
                "--identity",
                content_id(material.key),
                "--origin-epoch",
                str(origin),
            ]
            recovery_child(command, origin)

        step("real_subprocess_reopen", child_recover)
        report["child_receipt"] = str(child_path)
        child = json.loads(child_path.read_text())
        report["child_garage_requests"] = child["garage_requests"]
        connection = connect_dev(password)
        simple_preflight(connection, run_id)
        ledger = SimplePostgresDataset(connection, store, (decoder,), owner_run=run_id)
        step("same_identity_idempotent_retain", lambda: ledger.retain(material, raw))

        def repeat_put():
            # Deliberate duplicate same Key, same complete bytes, bypass GET dedup.
            status, _ = store._send(
                "PUT", store._key(material.locator), raw, maximum=1024
            )
            require(status in (200, 201), "duplicate_put_rejected")
            require(
                store.read(material.locator, len(raw)) == raw, "duplicate_put_corrupt"
            )

        step("deliberate_same_bytes_duplicate_put", repeat_put)

        def conflict():
            try:
                ledger.retain(replace(material, fixture_id="different-original"), raw)
            except ContractError:
                return
            raise ContractError("identity_conflict_accepted")

        step("business_identity_conflict_rejected", conflict)

        def lost_ack():
            try:
                ledger.commit_version(version, Fault.AFTER_COMMIT)
            except CommitUnknown:
                return
            raise ContractError("expected_commit_unknown_missing")

        step("commit_ack_loss_after_actual_pg_commit", lost_ack)
        connection.close()
        connection = connect_dev(password)
        ledger = SimplePostgresDataset(connection, store, (decoder,), owner_run=run_id)
        require(
            ledger.verify("version", content_id(version.key), content_id(version))
            == Verification.COMMITTED,
            "original_commit_not_verified",
        )
        step(
            "same_version_retry_unique_receipt", lambda: ledger.commit_version(version)
        )
        for subject in version.admission_subjects:
            ledger.set_admission(subject, "synthetic-dev-only", True)
        step("publication", lambda: ledger.publish(request))
        step("projection", lambda: ledger.rebuild("simple-g1", (month,)))
        with ledger.read_snapshot("simple-g1") as view:
            require(
                view.content(version.key) == version.assessment.content,
                "content_readback",
            )
            require(
                view.histories((version.key,))[0][0].request == request,
                "publication_readback",
            )
        report["readback"] = "RAW_VERSION_PUBLICATION_EXACT"
        report["pending_inventory"] = ledger.pending_inventory()
        # Isolated failure followed by an unrelated job, same fixed physical bytes.
        other = replace(
            material, key=Key(Kind.MATERIAL, run_id + "-other"), job=run_id + "-other"
        )
        step("independent_other_job_runs", lambda: ledger.retain(other, raw))
        with connection.transaction():
            counts = {
                table: connection.execute(
                    f"SELECT count(*) FROM {schema}.{table}"
                ).fetchone()[0]
                for table in (
                    "materials",
                    "versions",
                    "publication_events",
                    "commit_receipts",
                    "ingest_pending",
                )
            }
        report["pg_counts"] = counts
        require(
            counts["materials"] == 2
            and counts["versions"] == 1
            and counts["publication_events"] == 1,
            "unexpected_fact_counts",
        )
        report["retained_objects"] = [
            dict(
                locator=material.locator,
                key=store._key(material.locator),
                size=len(raw),
                sha256=byte_digest(raw),
            )
        ]
        report["retained_object_count"] = 1
        report["retained_bytes"] = len(raw)
        report["total_garage_requests"] = (
            store.requests
            + child["garage_requests"]
            + (previous["garage_requests"] if previous else 0)
        )
        require(report["total_garage_requests"] <= 128, "aggregate_request_budget")
        report["result"] = "LIMITED_DEV_PASSED"
        return 0
    except Exception as exc:
        report["result"] = "STOP"
        report["exception_classification"] = type(exc).__name__
        if isinstance(exc, ContractError):
            report["contract_code"] = str(exc)
        return 2
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if connection:
            connection.close()
        save()
        print(json.dumps(dict(result=report.get("result"), receipt=str(receipt))))


if __name__ == "__main__":
    raise SystemExit(main())
