"""One opt-in synthetic Job, one isolated PG schema/Garage prefix; no cleanup."""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from market_data_foundation.canonical.market_material import byte_digest, read_material
from market_data_foundation.canonical.market_persistence import (
    Fault,
    InjectedFailure,
    Verification,
)
from market_data_foundation.canonical.market_projection import MonthKey, SlotKey
from market_data_foundation.canonical.market_publication import MarketPublicationRequest
from market_data_foundation.canonical.model import (
    ContractError,
    Decision,
    Key,
    Kind,
    require,
)
from market_data_foundation.domain_contracts.market_coverage import CoverageClass
from market_data_foundation.domain_contracts.market_universe import (
    canonical_bytes,
    content_id,
)
from market_data_foundation.integration.market_vertical import (
    prepare_version,
    retain_page,
    validate_plan_evidence,
    verify_original,
)
from market_data_foundation.integration.market_vertical_storage import simple_intake
from market_data_foundation.storage.market_dataset_codec import dumps, loads
from market_data_foundation.storage.market_dataset_garage import (
    BUCKET,
    SimpleGarageMaterials,
)
from market_data_foundation.storage.market_dataset_postgres import connect_dev
from market_data_foundation.storage.market_dataset_simple import (
    SimplePostgresDataset,
    migrate_simple,
    schema_for,
    simple_preflight,
)
from scripts.mdf_i1_i3_synthetic import decoder_for, layout_for, run_job, version_for
from scripts.mdf_i3_storage_simple_dev import (
    credentials,
    recovery_child,
    remaining_seconds,
)

BASELINE = "dbf488d64282e4081f7902a8daf64089102a3815"
TABLES = (
    "materials",
    "versions",
    "publication_events",
    "version_materials",
    "commit_receipts",
    "ingest_pending",
    "admission_guards",
)


def candidate_revision():
    """Content identity includes tracked AND new candidate files; no Git writes."""
    require(
        subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        == BASELINE,
        "git_baseline_drift",
    )
    require(
        subprocess.check_output(
            ["git", "rev-parse", "refs/heads/main"], text=True
        ).strip()
        == BASELINE,
        "git_mainline_drift",
    )
    names = subprocess.check_output(
        ["git", "ls-files", "-c", "-o", "--exclude-standard", "-z"]
    ).split(b"\0")
    digest = hashlib.sha256()
    for name in sorted(set(filter(None, names))):
        path = Path(os.fsdecode(name))
        require(path.is_file() and not path.is_symlink(), "candidate_regular_file")
        digest.update(name + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def counts(ledger):
    with ledger.connection.transaction():
        ledger._sql("SET TRANSACTION READ ONLY")
        ledger._identity()
        return {
            table: ledger._sql(f"SELECT count(*) FROM {{s}}.{table}").fetchone()[0]
            for table in TABLES
        }


def rejected(fn, code):
    try:
        fn()
    except ContractError as exc:
        require(str(exc) == code, "unexpected_rejection_classification")
        return code
    raise ContractError("expected_rejection_missing")


def readonly_check(ledger, plan, expected_versions, publications):
    """Use SQL-backed product views; prohibit every write during this phase."""
    store = ledger.store
    before = counts(ledger)
    first_effect = len(store.effects)
    versions = []
    with ledger.read_snapshot("i1-job-g1") as view:
        for expected, publication in zip(expected_versions, publications, strict=True):
            meta = view.versions((expected.key,), exact=True)[0]
            # These values come from SQL, not from the expected/parent fixture.
            actual = view._state.versions[meta.version]
            require(
                canonical_bytes(actual) == canonical_bytes(expected), "cold_version"
            )
            lineage = view.lineage(actual.key)
            validate_plan_evidence(
                plan, actual.assessment.unit, lineage, actual.assessment.evidence
            )
            require(
                view.content(actual.key) == actual.assessment.content, "cold_content"
            )
            events = view.histories((actual.key,))[0]
            require(
                len(events) == 1 and events[0].request == publication,
                "cold_publication",
            )
            require(events[0].decision_at >= actual.frozen_at, "publication_clock")
            for material in lineage:
                require(
                    material.job == plan.spec.identity
                    and material.requested_at
                    <= material.obtained_at
                    <= material.received_at
                    <= material.known_at
                    <= actual.assessment.evaluated_at
                    <= actual.frozen_at,
                    "cold_business_clock",
                )
            point = next(
                e.point
                for e in actual.assessment.evidence
                if e.classification == CoverageClass.OBSERVED_VALID
            )
            status, selected = view.select(
                SlotKey(actual.assessment.unit.definition, point.instrument, point.day),
                view.now,
            )
            require(
                status == "SELECTED" and selected == (actual.key,), "cold_selection"
            )
            versions.append(
                dict(
                    version=content_id(actual.key),
                    unit=actual.assessment.unit.identity,
                    materials=[content_id(m.key) for m in lineage],
                    content_digest=actual.content_digest,
                    publication=content_id(events[0].key),
                    decision_at=events[0].decision_at.isoformat(),
                    coverage=[
                        dict(
                            day=e.point.day.isoformat(),
                            classification=e.classification.value,
                        )
                        for e in actual.assessment.evidence
                    ],
                )
            )
    require(before == counts(ledger), "read_changed_pg_counts")
    require(
        all(e["method"] == "GET" for e in store.effects[first_effect:]),
        "read_path_object_mutation",
    )
    return versions


class FrozenWriter:
    """Save original physical intent BEFORE the first pending/object effect."""

    def __init__(self, ledger, manifest_path, manifest):
        self.ledger, self.path, self.manifest = ledger, manifest_path, manifest
        self.inject = True

    def retain(self, material, raw, *, level):
        intent = self.ledger.material_intent(material, level=level)
        self.manifest["intents"].append(dumps(intent))
        self.path.write_text(json.dumps(self.manifest, indent=2) + "\n")
        if self.inject:
            self.inject = False
            try:
                self.ledger.retain(
                    material, raw, level=level, fault=Fault.BEFORE_COMMIT
                )
            except InjectedFailure:
                require(
                    verify_original(self.ledger, intent) == Verification.ABSENT,
                    "rollback_not_absent",
                )
            else:
                raise ContractError("rollback_injection_missing")
            # Reopen the connection and recover exactly the PG-staged payload.
            # A separate fresh process below also recovers this original intent.
            self.ledger.connection.close()
            password, _ = credentials()
            self.ledger.connection = connect_dev(password)
            return self.ledger.recover(content_id(material.key))
        return self.ledger.retain(material, raw, level=level)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opt-in-dev-write", action="store_true")
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--cold-child", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--origin-epoch", type=float, help=argparse.SUPPRESS)
    opts = parser.parse_args()
    require(opts.opt_in_dev_write, "explicit_dev_write_opt_in_required")
    require(candidate_revision() == opts.expected_revision, "candidate_revision_drift")
    require(
        (opts.cold_child is None) == (opts.origin_epoch is None),
        "child_origin_required",
    )
    os.umask(0o077)
    receipt = opts.receipt.resolve()
    require(receipt.is_relative_to(Path("artifacts").resolve()), "receipt_scope")
    receipt.parent.mkdir(parents=True, exist_ok=True)
    origin = opts.origin_epoch if opts.cold_child else time.time()
    report = dict(
        result="STOP",
        candidate_revision=opts.expected_revision,
        baseline=BASELINE,
        origin_epoch=origin,
        qualification="SYNTHETIC_JOB_DEV_ONLY_NOT_MARKET_COLLECTION",
        budgets=dict(seconds=300, requests=128, objects=8, bytes=4194304),
        steps=[],
        retained="NO_GARAGE_DELETE_NO_DROP_NO_CLEANUP",
    )
    with receipt.open("x") as handle:
        json.dump(report, handle)
    store = connection = child_path = ledger = None
    manifest_path = receipt.with_name("frozen-intents.json")

    def save():
        report["elapsed_seconds"] = round(time.time() - origin, 3)
        if store:
            report["garage_effects"] = store.effects
            report["garage_requests"] = store.requests
            report["object_intents"] = store.intents
        if child_path and child_path.exists():
            child = json.loads(child_path.read_text())
            report["child_receipt"] = str(child_path)
            report["child_garage_requests"] = child.get("garage_requests", 0)
            report["child_result"] = child.get("result")
        report["total_garage_requests"] = report.get("garage_requests", 0) + report.get(
            "child_garage_requests", 0
        )
        temp = receipt.with_suffix(".pending")
        temp.write_text(json.dumps(report, indent=2) + "\n")
        temp.replace(receipt)

    def step(name, fn):
        require(
            candidate_revision() == opts.expected_revision, "candidate_revision_drift"
        )
        entry = dict(operation=name, outcome="UNKNOWN")
        report["steps"].append(entry)
        save()
        value = fn()
        entry["outcome"] = "CONFIRMED"
        save()
        return value

    def alarm(*_):
        raise TimeoutError

    try:
        if opts.cold_child:
            require(
                opts.cold_child.resolve().is_relative_to(Path("artifacts").resolve()),
                "child_manifest_scope",
            )
            manifest = json.loads(opts.cold_child.read_text())
            run_id = manifest["run_id"]
            require(manifest["revision"] == opts.expected_revision, "manifest_revision")
            plan = loads(manifest["plan"])
            decoder = decoder_for(plan)
            versions = tuple(loads(v) for v in manifest["versions"])
            publications = tuple(loads(p) for p in manifest["publications"])
        else:
            run_id = "i3-" + uuid4().hex
            worker, decoder = step(
                "d1_plan_d2_run_journal_reopen",
                lambda: run_job(receipt.parent / "journal", run_id),
            )
            plan = worker.plan
            manifest = dict(
                run_id=run_id,
                revision=opts.expected_revision,
                plan=dumps(plan),
                intents=[],
                versions=[],
                publications=[],
            )
        # D2 owns its own bounded source timer. Real storage effects begin only
        # after D2 has completed; the original 295s deadline includes D2 elapsed.
        signal.signal(signal.SIGALRM, alarm)
        signal.setitimer(signal.ITIMER_REAL, remaining_seconds(origin))
        report.update(
            run_id=run_id,
            schema=schema_for(run_id),
            bucket=BUCKET,
            prefix=f"market-datasets/dev-simplified/{run_id}/",
            plan=content_id(plan),
            job=plan.spec.identity,
        )
        password, garage_credentials = credentials()
        connection = connect_dev(password)
        report["preflight"] = step(
            "pg_identity_tls_durability_permissions_backup",
            lambda: simple_preflight(connection, run_id),
        )
        store = SimpleGarageMaterials(
            garage_credentials,
            run_id,
            max_requests=32 if opts.cold_child else 96,
            wall_seconds=min(120, remaining_seconds(origin)),
        )
        transport = store._transport

        def bounded_transport(*args):
            remaining_seconds(origin)
            require(
                candidate_revision() == opts.expected_revision,
                "candidate_revision_drift",
            )
            return transport(*args)

        store._transport = bounded_transport
        store.on_effect = save
        ledger = SimplePostgresDataset(connection, store, (decoder,), owner_run=run_id)
        if opts.cold_child:
            require(report["preflight"]["namespace_exists"], "cold_schema_missing")
            original_intent = loads(manifest["intents"][0])
            step(
                "cold_pg_original_payload_recovery",
                lambda: ledger.recover(content_id(original_intent[0].key)),
            )
            require(
                verify_original(ledger, original_intent) == Verification.COMMITTED,
                "cold_original_intent",
            )
            for encoded in manifest["intents"]:
                material, level = loads(encoded)
                require(
                    verify_original(ledger, (material, level))
                    == Verification.COMMITTED,
                    "cold_material_receipt",
                )
                raw = read_material(store, material)
                require(byte_digest(raw) == material.digest, "cold_raw_digest")
            # Enforce read-only SQL in addition to the adapter's READ ONLY view.
            connection.execute("SET default_transaction_read_only=on")
            store.put = lambda *_a, **_kw: (_ for _ in ()).throw(
                ContractError("cold_read_put_forbidden")
            )
            report["cold_readback"] = step(
                "cold_pg_versions_publications_selection",
                lambda: readonly_check(ledger, plan, versions, publications),
            )
            report["pg_counts"] = counts(ledger)
            report["result"] = "COLD_PG_READBACK_PASSED"
            return 0
        require(not report["preflight"]["namespace_exists"], "fresh_schema_required")
        step("create_one_isolated_schema", lambda: migrate_simple(connection, run_id))
        report["created_preflight"] = simple_preflight(connection, run_id)
        writer = FrozenWriter(ledger, manifest_path, manifest)
        physical = simple_intake(store, level="raw")
        requests = tuple(r.request for s in worker.report().shards for r in s.receipts)
        require(1 <= len(requests) <= 8, "material_page_budget")
        retained = step(
            "i1_original_bytes_to_pg_garage",
            lambda: tuple(
                retain_page(
                    worker,
                    request,
                    layout_for(worker, request),
                    writer,
                    physical=physical,
                )
                for request in requests
            ),
        )
        connection = ledger.connection
        materials = tuple(r.material for r in retained)
        require(
            len({m.locator for m in materials}) <= 8
            and sum(m.byte_length for m in materials) <= 4194304,
            "object_byte_budget",
        )
        report["lineage"] = [
            dict(
                job=m.job,
                shard=m.shard,
                attempt=m.attempt,
                request=m.request,
                material=content_id(m.key),
                locator=m.locator,
                digest=m.digest,
                bytes=m.byte_length,
                physical_level=r.physical_level,
                response=r.response_identity,
            )
            for m, r in zip(materials, retained, strict=True)
        ]
        before = counts(ledger)
        retry = step(
            "original_request_idempotent",
            lambda: retain_page(
                worker,
                requests[0],
                layout_for(worker, requests[0]),
                ledger,
                physical=physical,
            ),
        )
        require(
            retry == retained[0] and before == counts(ledger), "request_retry_changed"
        )
        original = materials[0]
        raw = worker.journal.read_material(worker.journal.receipt(requests[0])).raw
        for label, changed_raw, changed_key in (
            ("same_request_changed_key", raw, Key(Kind.MATERIAL, "forged-key")),
            ("same_request_changed_bytes", raw + b" ", original.key),
            (
                "same_request_changed_key_and_bytes",
                raw + b" ",
                Key(Kind.MATERIAL, "forged-both"),
            ),
        ):
            changed = replace(
                original,
                key=changed_key,
                digest=byte_digest(changed_raw),
                byte_length=len(changed_raw),
            )
            changed = replace(
                changed, locator=physical.locator(changed, physical.level)
            )
            effects = store.requests
            step(
                label,
                lambda: rejected(
                    lambda: ledger.retain(changed, changed_raw, level="raw"),
                    "same_identity_different_content",
                ),
            )
            require(
                store.requests == effects and before == counts(ledger),
                "conflict_effect",
            )
        versions = step(
            "i1_prepare_versions",
            lambda: tuple(
                version_for(worker, decoder, store, materials, unit)
                for unit in plan.units
            ),
        )
        version = versions[0]
        evidence = tuple(
            replace(
                e,
                classification=CoverageClass.UNKNOWN,
                material=None,
                source_row_locator=None,
            )
            if e.classification == CoverageClass.OBSERVED_VALID
            else e
            for e in version.assessment.evidence
        )
        step(
            "incomplete_evidence_rejected",
            lambda: rejected(
                lambda: prepare_version(
                    version.key,
                    version.assessment.unit,
                    version.assessment.materials,
                    evidence,
                    decoder,
                    store,
                    version.assessment.evaluated_at,
                    version.frozen_at,
                    plan=plan,
                ),
                "coverage_not_publishable",
            ),
        )
        for v in versions:
            step("commit_version", lambda: ledger.commit_version(v))
        publications = tuple(
            MarketPublicationRequest(
                Key(Kind.PUBLICATION, "publish:" + v.key.identity),
                "publish:" + v.key.identity,
                v.key,
                Decision.PUBLISH,
                None,
                ("synthetic-dev-only",),
                "synthetic-only-r1",
            )
            for v in versions
        )
        step(
            "unadmitted_publication_rejected",
            lambda: rejected(
                lambda: ledger.publish(publications[0]), "publication_admission_denied"
            ),
        )
        require(counts(ledger)["publication_events"] == 0, "unadmitted_published")
        for v, p in zip(versions, publications, strict=True):
            for subject in v.admission_subjects:
                ledger.set_admission(subject, "synthetic-dev-only", True)
            step("publish", lambda: ledger.publish(p))
            ledger.commit_version(v)
            ledger.publish(p)
            require(
                verify_original(ledger, v) == Verification.COMMITTED
                and verify_original(ledger, p) == Verification.COMMITTED,
                "version_publication_receipts",
            )
        months = tuple(
            dict.fromkeys(
                MonthKey(
                    u.definition,
                    u.instrument,
                    u.window.start.year,
                    u.window.start.month,
                )
                for u in plan.units
            )
        )
        step("explicit_projection_build", lambda: ledger.rebuild("i1-job-g1", months))
        manifest.update(
            versions=[dumps(v) for v in versions],
            publications=[dumps(p) for p in publications],
        )
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        report["pg_counts"] = counts(ledger)
        require(
            report["pg_counts"]["materials"] == len(materials)
            and report["pg_counts"]["versions"] == 2
            and report["pg_counts"]["publication_events"] == 2,
            "final_counts",
        )
        report["pending_inventory"] = ledger.pending_inventory()
        require(
            all(row[3] == "complete" for row in report["pending_inventory"]),
            "pending_incomplete",
        )
        connection.close()
        connection = None
        child_path = receipt.with_name("cold-child.json")
        step(
            "fresh_process_cold_readback",
            lambda: recovery_child(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--opt-in-dev-write",
                    "--receipt",
                    str(child_path),
                    "--expected-revision",
                    opts.expected_revision,
                    "--cold-child",
                    str(manifest_path),
                    "--origin-epoch",
                    str(origin),
                ],
                origin,
            ),
        )
        save()
        child = json.loads(child_path.read_text())
        require(
            child["result"] == "COLD_PG_READBACK_PASSED"
            and child["pg_counts"] == report["pg_counts"],
            "cold_result",
        )
        report["retained_objects"] = [
            dict(locator=m.locator, bytes=m.byte_length, sha256=m.digest)
            for m in materials
        ]
        report["retained_bytes"] = sum(m.byte_length for m in materials)
        require(report["total_garage_requests"] <= 128, "aggregate_request_budget")
        require(
            candidate_revision() == opts.expected_revision, "candidate_revision_drift"
        )
        report["result"] = "LIMITED_SYNTHETIC_JOB_DEV_PASSED"
        return 0
    except Exception as exc:
        report["exception_classification"] = type(exc).__name__
        # Contract codes are fixed product classifications. Never log arbitrary
        # psycopg/OS exceptions, credentials, connection strings or S3 XML.
        if isinstance(exc, ContractError):
            report["contract_code"] = str(exc)
        return 2
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if ledger and not ledger.connection.closed:
            ledger.connection.close()
        if connection and not connection.closed:
            connection.close()
        save()
        print(json.dumps(dict(result=report["result"], receipt=str(receipt))))


if __name__ == "__main__":
    raise SystemExit(main())
