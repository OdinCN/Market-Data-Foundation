"""Deterministic T01 BLS PPI final-demand vertical checks."""

import base64
import json
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from psycopg.pq import TransactionStatus

from market_data_foundation.canonical import codec
from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    ReadFailure,
    read_exact_version,
)
from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import ContractError, Grade, digest
from market_data_foundation.canonical.postgres import PostgresLedger, validate_schema
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.domain_contracts.macro import BLS_PPI_FINAL_DEMAND
from market_data_foundation.historical.bls_cpi_direct_live import (
    HttpResult,
    LiveExecutionError,
)
from market_data_foundation.historical.bls_cpi_material_store import (
    GarageCredentials,
    GarageMaterialStore,
    InMemoryMaterialStore,
    InMemoryWriterProtection,
    MaterialIdentity,
    PostgresAdvisoryWriterProtection,
    WriteDisposition,
)
from market_data_foundation.historical.bls_ppi_final_demand import (
    MEASUREMENTS,
    PPI_BINDING_CANDIDATE,
    PPI_SOURCE,
    REFERENCE,
    RELEASE,
    SERIES_IDS,
    BlsPpiRequest,
    PpiLiveEvidence,
    build_live_plan,
    parse_response,
)
from market_data_foundation.historical.bls_ppi_live import (
    PpiLiveCandidate,
    execute_live_candidate,
)
from market_data_foundation.historical.cpi_job_journal import FileJobJournal
from market_data_foundation.historical.ppi_local_job import (
    DEV_SCHEMA,
    DirectPpiAcquirer,
    PpiJobJournal,
    PpiJobSpec,
    PpiLocalJob,
    _encode_plan,
)
from market_data_foundation.historical.ppi_local_read import (
    PpiReadRejected,
    _validate_content,
    read_local_ppi,
)
from market_data_foundation.historical.ppi_materialization import (
    PpiMaterializationOutcomeUnknown,
    materialize_persist_publish,
)

BASE = RELEASE + timedelta(seconds=1)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def row(year, month, value, *, pct=None, preliminary=False, latest=False):
    result = {
        "year": str(year),
        "period": f"M{month:02d}",
        "periodName": datetime(year, month, 1).strftime("%B"),
        "value": value,
        "footnotes": [{"code": "P", "text": "Preliminary"}] if preliminary else [{}],
    }
    if pct is not None:
        result["calculations"] = {"net_changes": {}, "pct_changes": pct}
    if latest:
        result["latest"] = "true"
    return result


def response():
    return {
        "status": "REQUEST_SUCCEEDED",
        "responseTime": 17,
        "message": [],
        "Results": {
            "series": [
                {
                    "seriesID": "WPSFD4",
                    "data": [
                        row(
                            2026,
                            9,
                            "155.321",
                            pct={"1": "1.3", "12": "6.1"},
                            preliminary=True,
                            latest=True,
                        ),
                        row(2026, 8, "153.328"),
                        row(2025, 9, "146.401"),
                    ],
                },
                {
                    "seriesID": "WPUFD4",
                    "data": [
                        row(
                            2026,
                            9,
                            "154.887",
                            pct={"1": "0.8", "12": "6.3"},
                            preliminary=True,
                            latest=True,
                        ),
                        row(2026, 8, "153.659"),
                        row(2025, 9, "145.711"),
                    ],
                },
            ]
        },
    }


def evidence(raw=None):
    return PpiLiveEvidence(
        raw or encode(response()),
        BlsPpiRequest(),
        BASE,
        BASE + timedelta(milliseconds=1),
        "mdf-bls-ppi-final-demand-test",
        "mdf-artifact://t01-test/run",
    )


def plan(identity="t01-ppi-test"):
    return build_live_plan(
        evidence(),
        identity=identity,
        finished_at=BASE + timedelta(milliseconds=2),
        frozen_at=BASE + timedelta(milliseconds=3),
    )


def job_candidate(job_id, artifact_directory):
    job_evidence = replace(
        evidence(),
        acquisition_identity=f"mdf-bls-ppi-final-demand-{job_id}-a1",
    )
    job_plan = build_live_plan(
        job_evidence,
        identity=f"t01-bls-ppi-final-demand-{job_id}-a1",
        finished_at=BASE + timedelta(milliseconds=2),
        frozen_at=BASE + timedelta(milliseconds=3),
    )
    return PpiLiveCandidate(job_evidence, job_plan, artifact_directory)


@pytest.mark.parametrize("attempts", [2, 3])
def test_retry_attempt_count_materializes_and_reads(attempts):
    retry_evidence = replace(evidence(), provider_data_attempts=attempts)
    retry_plan = build_live_plan(
        retry_evidence,
        identity=f"ppi-retry-read-{attempts}",
        finished_at=BASE + timedelta(milliseconds=2),
        frozen_at=BASE + timedelta(milliseconds=3),
    )
    ledger = InMemoryLedger()
    result = materialize_persist_publish(
        CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=1)),
        retry_plan,
        retry_evidence.raw_bytes,
        InMemoryMaterialStore(),
        InMemoryWriterProtection(),
    )
    for kwargs in (
        {"version_identity": result.version.identity},
        {},
    ):
        view = read_local_ppi(
            ledger,
            reference=REFERENCE,
            as_of=BASE + timedelta(seconds=2),
            **kwargs,
        )
        assert view.acquisitions[0].provider_data_attempts == attempts


def test_source_specific_parser_preserves_reference_measurement_unit_and_revision():
    projection = parse_response(encode(response()))
    assert projection.data["reference_period"] == REFERENCE
    assert set(projection.data["measurements"]) == set(MEASUREMENTS)
    assert projection.data["measurements"]["final_demand_mom_sa"] == {
        "series_id": "WPSFD4",
        "reference_period": "2026-09",
        "comparison_period": "2026-08",
        "measurement": "pct_changes/1",
        "value": "1.3",
        "unit": "percent",
        "revision_state": "preliminary",
        "footnotes": [{"code": "P", "text": "Preliminary"}],
    }
    assert (
        projection.data["measurements"]["final_demand_index_nsa"]["unit"]
        == "index_point"
    )


@pytest.mark.parametrize(
    "mutation", ["series", "reference", "calculation", "sensitive"]
)
def test_parser_fails_closed_on_wrong_source_shape(mutation):
    value = response()
    if mutation == "series":
        value["Results"]["series"].reverse()
    elif mutation == "reference":
        value["Results"]["series"][0]["data"].pop(0)
    elif mutation == "calculation":
        del value["Results"]["series"][1]["data"][0]["calculations"]["pct_changes"][
            "12"
        ]
    else:
        value["registrationKey"] = "must-not-be-accepted"
    with pytest.raises(ContractError):
        parse_response(encode(value))


def test_plan_has_exact_source_binding_release_and_revision_lineage():
    value = plan()
    assert value.version.definition == BLS_PPI_FINAL_DEMAND.key
    assert value.version.binding == value.binding_candidate.key
    assert value.version.mapping == "bls-ppi-final-demand"
    assert value.version.acquisitions == (value.acquisition.key,)
    assert value.acquisition.source == value.source.key
    assert dict(value.run.transform.parameters)["release_reference"] == REFERENCE
    assert (
        "retrieval_snapshot_not_provider_vintage" in value.binding_candidate.limitations
    )
    assert value.binding_candidate.silent_fallback is False


def _published(identity="t01-ppi-published"):
    ledger = InMemoryLedger()
    store = InMemoryMaterialStore()
    value = plan(identity)
    result = materialize_persist_publish(
        CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=2)),
        value,
        evidence().raw_bytes,
        store,
        InMemoryWriterProtection(),
    )
    return ledger, store, result


def test_raw_is_durable_before_version_publication_and_local_reads_are_exact():
    ledger, store, result = _published()
    assert result.verification.disposition == WriteDisposition.CREATED
    assert result.verification.reconciled_outcome_unknown is False
    assert result.plan.raw_material.durable is True
    assert result.plan.raw_material.replayable is True
    identity = MaterialIdentity.from_locator(
        result.plan.raw_material.controlled_ref,
        expected_sha256=result.plan.raw_material.digest,
        expected_size=len(evidence().raw_bytes),
    )
    assert store.read_exact(identity) == evidence().raw_bytes
    exact = read_local_ppi(
        ledger,
        reference=REFERENCE,
        version_identity=result.version.identity,
        as_of=BASE + timedelta(seconds=3),
    )
    as_of = read_local_ppi(
        ledger, reference=REFERENCE, as_of=BASE + timedelta(seconds=3)
    )
    assert exact == as_of
    assert exact.publication == result.publication
    assert exact.publication.request.claimed_grades == frozenset(
        {Grade.ORIGINAL, Grade.PROJECTED}
    )


def test_raw_write_unknown_readback_does_not_claim_put_created_the_object():
    raw = evidence().raw_bytes
    identity = MaterialIdentity(digest(raw), len(raw))
    store = InMemoryMaterialStore()
    store.outcome_unknown_after_create = True
    result = materialize_persist_publish(
        CanonicalKernel(InMemoryLedger(), lambda: BASE + timedelta(seconds=2)),
        plan("t01-ppi-raw-unknown"),
        raw,
        store,
        InMemoryWriterProtection(),
    )
    assert result.verification.identity == identity
    assert result.verification.disposition is None
    assert result.verification.reconciled_outcome_unknown is True
    assert store.create_attempts == 1
    assert store.reads == 1
    assert store.read_exact(identity) == raw


def test_local_read_rejects_wrong_reference_and_semantically_corrupt_content():
    ledger, _store, _result = _published("t01-ppi-reject")
    with pytest.raises(PpiReadRejected, match="reference_not_admitted"):
        read_local_ppi(ledger, reference="2026-08", as_of=BASE + timedelta(seconds=3))
    with pytest.raises(PpiReadRejected, match="ppi_payload_invalid_or_mismatched"):
        _validate_content(b"{}")


@pytest.mark.parametrize("mutation", ["series_id", "comparison_period", "raw"])
def test_local_read_replays_raw_and_rejects_self_consistent_tampered_version(mutation):
    ledger, _store, published = _published("t01-ppi-tampered-" + mutation)
    original = published.plan
    document = json.loads(original.content)
    if mutation == "series_id":
        document["data"]["measurements"]["final_demand_mom_sa"]["series_id"] = "WPUFD4"
    elif mutation == "comparison_period":
        document["data"]["measurements"]["final_demand_yoy_nsa"][
            "comparison_period"
        ] = "2025-08"
    else:
        replacement_raw = b"{}"
        document["raw"] = {
            "sha256": digest(replacement_raw),
            "size": len(replacement_raw),
            "base64": base64.b64encode(replacement_raw).decode(),
        }
    content = encode(document)
    tampered = replace(
        original,
        semantic_material=replace(original.semantic_material, digest=digest(content)),
        version=replace(original.version, content_digest=digest(content)),
        content=content,
    )
    for record in (tampered.semantic_material, tampered.version):
        key = record.key
        ledger._records[(key.kind.value, key.identity, key.revision)] = codec.dumps(
            record
        )
    ledger._contents[tampered.version.identity] = (
        tampered.version.content_digest,
        tampered.content,
    )
    with pytest.raises(PpiReadRejected, match="ppi_payload_invalid_or_mismatched"):
        read_local_ppi(
            ledger,
            reference=REFERENCE,
            version_identity=tampered.version.identity,
            as_of=BASE + timedelta(seconds=3),
        )


def test_ppi_local_admission_does_not_expand_public_consumer_wire_definition_set():
    ledger, _store, result = _published("t01-ppi-private-admission")
    with pytest.raises(ReadContractError) as caught:
        read_exact_version(
            ledger,
            BLS_PPI_FINAL_DEMAND.key,
            result.version.key,
            as_of=BASE + timedelta(seconds=3),
        )
    assert caught.value.classification == ReadFailure.DEFINITION_NOT_ADMITTED


def test_materialization_commit_unknown_requires_readback_and_never_auto_publishes():
    ledger = InMemoryLedger()
    value = plan("t01-ppi-unknown")
    with pytest.raises(PpiMaterializationOutcomeUnknown) as caught:
        materialize_persist_publish(
            CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=2)),
            value,
            evidence().raw_bytes,
            InMemoryMaterialStore(),
            InMemoryWriterProtection(),
            authoritative_commit=lambda: (_ for _ in ()).throw(OSError("synthetic")),
        )
    assert (
        ledger.publication_by_occurrence(value.publication_request.occurrence) is None
    )
    assert caught.value.reconcile(ledger) == "confirmed_committed"
    assert (
        ledger.publication_by_occurrence(value.publication_request.occurrence) is None
    )


def test_materialization_rejects_plan_for_raw_a_with_raw_b_before_store_effect():
    changed = response()
    changed["responseTime"] = 18
    raw_b = encode(changed)
    store = InMemoryMaterialStore()
    with pytest.raises(ContractError, match="bls_ppi_plan_lineage_mismatch"):
        materialize_persist_publish(
            CanonicalKernel(InMemoryLedger(), lambda: BASE + timedelta(seconds=2)),
            plan("t01-ppi-raw-a-plan"),
            raw_b,
            store,
            InMemoryWriterProtection(),
        )
    assert store.create_attempts == 0
    assert store.reads == 0


@pytest.mark.parametrize("mutation", ["source", "binding", "request", "material"])
def test_local_read_rejects_wrong_source_specific_lineage(mutation):
    ledger, _store, result = _published("t01-ppi-lineage-" + mutation)
    if mutation == "source":
        record = replace(PPI_SOURCE, owner="wrong-owner")
    elif mutation == "binding":
        record = replace(PPI_BINDING_CANDIDATE, compatibility_policy="wrong-policy")
    elif mutation == "request":
        record = replace(result.plan.acquisition, request_identity="wrong-request")
    else:
        record = replace(result.plan.raw_material, named_class="wrong-material")
    key = record.key
    ledger._records[(key.kind.value, key.identity, key.revision)] = codec.dumps(record)
    with pytest.raises(
        (PpiReadRejected, ReadContractError),
        match="ppi_lineage_invalid_or_mismatched|lineage_missing_or_mismatched",
    ):
        read_local_ppi(
            ledger,
            reference=REFERENCE,
            version_identity=result.version.identity,
            as_of=BASE + timedelta(seconds=3),
        )


def test_exact_transaction_postgres_namespace_only():
    assert (
        validate_schema("mdf_macro_bls_ppi_vertical_r1")
        == "mdf_macro_bls_ppi_vertical_r1"
    )
    with pytest.raises(ContractError, match="unsafe_postgres_namespace"):
        validate_schema("mdf_macro_bls_ppi_vertical_r1_extra")


class Clock:
    def __init__(self):
        self.value = BASE + timedelta(seconds=10)

    def __call__(self):
        self.value += timedelta(milliseconds=1)
        return self.value


def job_rig(
    tmp_path,
    name,
    *,
    ledger=None,
    store=None,
    protection=None,
    commit=lambda: None,
    readback=None,
):
    clock = Clock()
    ledger = ledger or InMemoryLedger()
    store = store or InMemoryMaterialStore()
    protection = protection or InMemoryWriterProtection()
    if readback is None:

        def snapshot_readback():
            return nullcontext(InMemoryLedger.from_snapshot(ledger.snapshot()))

        readback = snapshot_readback
    journal = PpiJobJournal(tmp_path / name)
    job = PpiLocalJob(
        journal,
        clock,
        CanonicalKernel(ledger, clock),
        store,
        protection,
        readback,
        commit,
    )
    return job, journal, clock, ledger, store, protection


def ppi_acquirer(tmp_path, name):
    secret = tmp_path / (name + "-secret.toml")
    secret.write_text('[bls]\napi_key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"\n')
    times = iter(BASE + timedelta(milliseconds=index) for index in range(4))
    return DirectPpiAcquirer(
        FakeTransport(),
        secret,
        tmp_path / (name + "-artifacts"),
        lambda: next(times),
    )


def test_job_success_records_bounded_effects_and_fresh_readback(tmp_path):
    job, _journal, _clock, _ledger, _store, _protection = job_rig(tmp_path, "jobs")
    job.submit("ppi-success", PpiJobSpec())
    result = job.run(
        "ppi-success",
        enabled=True,
        preflight=lambda _spec: None,
        acquirer=ppi_acquirer(tmp_path, "ppi-success"),
    )
    assert result["state"] == "succeeded"
    assert result["classification"] == "canonical_verified"
    assert result["effects"] == {
        "provider_requests_possible": 1,
        "provider_requests_returned": 1,
        "provider_attempts_observed": 1,
        "provider_response_received": True,
        "raw_creates_possible": 1,
        "ledger_handoffs_possible": 1,
        "publications_possible": 1,
        "canonical_possible": True,
        "ledger_commit_possible": True,
        "ledger_commit_confirmed": True,
        "publication_returned": 1,
        "raw_disposition": "created",
        "raw_write_reconciled_outcome_unknown": False,
    }
    assert result["last_success"] is not None
    assert codec.loads(result["plan"]["version"]).identity == (
        "t01-bls-ppi-final-demand-ppi-success-a1:version"
    )


def test_job_records_unknown_raw_write_as_reconciled_present_not_created(tmp_path):
    store = InMemoryMaterialStore()
    store.outcome_unknown_after_create = True
    job, _journal, _clock, _ledger, _store, _protection = job_rig(
        tmp_path, "raw-unknown-jobs", store=store
    )
    job.submit("ppi-raw-unknown", PpiJobSpec())
    result = job.run(
        "ppi-raw-unknown",
        enabled=True,
        preflight=lambda _spec: None,
        acquirer=ppi_acquirer(tmp_path, "ppi-raw-unknown"),
    )
    assert result["state"] == "succeeded"
    assert result["effects"]["raw_disposition"] == "unknown_reconciled_present"
    assert result["effects"]["raw_write_reconciled_outcome_unknown"] is True
    assert "created" not in result["effects"]["raw_disposition"]


def test_job_preflight_cancel_and_restart_are_bounded_and_unknown_is_monotone(
    tmp_path,
):
    job, _journal, _clock, _ledger, _store, _protection = job_rig(
        tmp_path, "failed-jobs"
    )
    job.submit("ppi-preflight", PpiJobSpec())
    failed = job.run(
        "ppi-preflight",
        enabled=True,
        preflight=lambda _spec: (_ for _ in ()).throw(RuntimeError("synthetic")),
        acquirer=ppi_acquirer(tmp_path, "ppi-preflight"),
    )
    assert failed["state"] == "failed"
    assert failed["effects"] == {}

    cancel_job, cancel_journal, clock, *_ = job_rig(tmp_path, "cancel-jobs")
    cancel_job.submit(
        "ppi-cancel", replace(PpiJobSpec(), effect_domain="owner_nonproduction")
    )
    cancel_journal.cancel("ppi-cancel", clock())
    cancelled = cancel_job.run(
        "ppi-cancel",
        enabled=True,
        preflight=lambda _spec: pytest.fail("must not preflight"),
        acquirer=ppi_acquirer(tmp_path, "ppi-cancel"),
    )
    assert cancelled["state"] == "cancelled"

    restart_job, restart_journal, clock, *_ = job_rig(tmp_path, "restart-jobs")
    restart_job.submit("ppi-restart", PpiJobSpec())
    restart_journal._update(
        "ppi-restart", clock(), state="running", stage="acquisition_intent"
    )
    with pytest.raises(ContractError, match="ppi_job_restart_reconciliation_required"):
        restart_job.run(
            "ppi-restart",
            enabled=True,
            preflight=lambda _spec: pytest.fail("must not preflight"),
            acquirer=ppi_acquirer(tmp_path, "ppi-restart"),
        )
    reconciled = restart_job.reconcile("ppi-restart", enabled=True)
    assert reconciled["state"] == "outcome_unknown"
    assert reconciled["reconciliation"]["outcome"] == "unresolved"


def test_job_retains_unknown_handle_then_records_authoritative_absence(tmp_path):
    def commit():
        raise OSError("synthetic")

    job, _journal, _clock, _ledger, _store, protection = job_rig(
        tmp_path, "unknown-jobs", commit=commit
    )
    job.submit("ppi-commit-unknown", PpiJobSpec())
    result = job.run(
        "ppi-commit-unknown",
        enabled=True,
        preflight=lambda _spec: None,
        acquirer=ppi_acquirer(tmp_path, "ppi-commit-unknown"),
    )
    assert result["state"] == "outcome_unknown"
    assert "ppi-commit-unknown" in job._unknown_handles
    reconciled = job.reconcile("ppi-commit-unknown", enabled=True)
    assert reconciled["state"] == "outcome_unknown"
    assert reconciled["reconciliation"]["outcome"] == "confirmed_not_published"
    assert "ppi-commit-unknown" not in job._unknown_handles
    identity = MaterialIdentity(digest(evidence().raw_bytes), len(evidence().raw_bytes))
    ticket = protection.acquire(identity)
    ticket.release(identity)


def test_job_publication_unknown_records_presence_without_rewriting_state(tmp_path):
    class PublishedThenRaisedLedger(InMemoryLedger):
        def publish(self, request, clock):
            super().publish(request, clock)
            raise OSError("synthetic post-publication disconnect")

    ledger = PublishedThenRaisedLedger()
    job, _journal, _clock, _ledger, _store, _protection = job_rig(
        tmp_path, "publication-unknown-jobs", ledger=ledger
    )
    job.submit("ppi-publish-unknown", PpiJobSpec())
    result = job.run(
        "ppi-publish-unknown",
        enabled=True,
        preflight=lambda _spec: None,
        acquirer=ppi_acquirer(tmp_path, "ppi-publish-unknown"),
    )
    assert result["state"] == "outcome_unknown"
    reconciled = job.reconcile("ppi-publish-unknown", enabled=True)
    assert reconciled["state"] == "outcome_unknown"
    assert reconciled["reconciliation"]["outcome"] == "confirmed_published"
    assert reconciled["last_success"] is not None
    assert "ppi-publish-unknown" not in job._unknown_handles


def test_fresh_process_reconciles_only_from_journal_ids_and_owned_readback(tmp_path):
    ledger = InMemoryLedger()
    calls = 0

    def first_readback():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic")
        return nullcontext(InMemoryLedger.from_snapshot(ledger.snapshot()))

    job, journal, clock, _ledger, store, protection = job_rig(
        tmp_path, "fresh-process", ledger=ledger, readback=first_readback
    )
    job.submit("ppi-fresh", PpiJobSpec())
    result = job.run(
        "ppi-fresh",
        enabled=True,
        preflight=lambda _spec: None,
        acquirer=ppi_acquirer(tmp_path, "ppi-fresh"),
    )
    assert result["state"] == "outcome_unknown"
    restarted = PpiLocalJob(
        journal,
        clock,
        CanonicalKernel(ledger, clock),
        store,
        protection,
        lambda: nullcontext(InMemoryLedger.from_snapshot(ledger.snapshot())),
        lambda: None,
    )
    reconciled = restarted.reconcile("ppi-fresh", enabled=True)
    assert reconciled["state"] == "outcome_unknown"
    assert reconciled["reconciliation"]["outcome"] == "confirmed_published"


def test_fake_boolean_readback_callback_and_wrong_postgres_host_are_rejected(tmp_path):
    job, _journal, _clock, _ledger, _store, _protection = job_rig(
        tmp_path, "no-fake-readback"
    )
    job.submit("ppi-no-fake", PpiJobSpec())
    with pytest.raises(TypeError):
        job.run(
            "ppi-no-fake",
            enabled=True,
            preflight=lambda _spec: None,
            acquirer=lambda: job_candidate("ppi-no-fake", tmp_path),
            independent_readback=lambda _result: {
                "exact": True,
                "as_of": True,
                "raw": True,
            },
        )
    fake_job, *_ = job_rig(tmp_path, "no-fake-acquirer")
    fake_job.submit("ppi-no-fake-acquirer", PpiJobSpec())
    rejected = fake_job.run(
        "ppi-no-fake-acquirer",
        enabled=True,
        preflight=lambda _spec: None,
        acquirer=lambda: job_candidate("ppi-no-fake-acquirer", tmp_path),
    )
    assert rejected["state"] == "failed"
    assert rejected["effects"] == {}

    connection = SimpleNamespace(info=SimpleNamespace(host="127.0.0.1"))
    wrong = PostgresLedger(connection, DEV_SCHEMA)
    with pytest.raises(ContractError, match="ppi_job_nonproduction_target_required"):
        PpiLocalJob(
            PpiJobJournal(tmp_path / "wrong-host"),
            Clock(),
            CanonicalKernel(wrong, Clock()),
            InMemoryMaterialStore(),
            InMemoryWriterProtection(),
            lambda: pytest.fail("must not read"),
            lambda: None,
        )


def test_owner_nonproduction_rejects_ambient_pg_transaction_without_cleanup(tmp_path):
    class Connection:
        closed = False

        def __init__(self):
            self.info = SimpleNamespace(
                host="192.168.10.4",
                backend_pid=4101,
                transaction_status=TransactionStatus.INTRANS,
            )
            self.rollback_calls = 0

        def rollback(self):
            self.rollback_calls += 1

    connection = Connection()
    ledger = PostgresLedger(connection, DEV_SCHEMA)
    journal = PpiJobJournal(tmp_path / "ambient-transaction")
    clock = Clock()
    job = PpiLocalJob(
        journal,
        clock,
        CanonicalKernel(ledger, clock),
        GarageMaterialStore(GarageCredentials("access", "secret")),
        PostgresAdvisoryWriterProtection(connection),
        lambda: pytest.fail("must not read"),
        lambda: pytest.fail("must not commit"),
    )
    job.submit(
        "ppi-ambient",
        replace(PpiJobSpec(), effect_domain="owner_nonproduction"),
    )
    result = job.run(
        "ppi-ambient",
        enabled=True,
        preflight=lambda _spec: pytest.fail("must not preflight"),
        acquirer=ppi_acquirer(tmp_path, "ppi-ambient"),
    )
    assert result["state"] == "failed"
    assert result["effects"] == {}
    assert connection.rollback_calls == 0


def test_synthetic_effect_domain_cannot_drive_postgres(tmp_path):
    connection = SimpleNamespace(
        closed=False,
        info=SimpleNamespace(
            host="192.168.10.4",
            backend_pid=4102,
            transaction_status=TransactionStatus.IDLE,
        ),
    )
    ledger = PostgresLedger(connection, DEV_SCHEMA)
    journal = PpiJobJournal(tmp_path / "synthetic-postgres")
    clock = Clock()
    job = PpiLocalJob(
        journal,
        clock,
        CanonicalKernel(ledger, clock),
        InMemoryMaterialStore(),
        InMemoryWriterProtection(),
        lambda: pytest.fail("must not read"),
        lambda: pytest.fail("must not commit"),
    )
    job.submit("ppi-synthetic-pg", PpiJobSpec())
    result = job.run(
        "ppi-synthetic-pg",
        enabled=True,
        preflight=lambda _spec: pytest.fail("must not preflight"),
        acquirer=ppi_acquirer(tmp_path, "ppi-synthetic-pg"),
    )
    assert result["state"] == "failed"
    assert result["effects"] == {}


def test_fresh_owner_controller_rejects_same_backend_pid_before_lock_reentry(
    tmp_path,
):
    class Connection:
        closed = False

        def __init__(self):
            self.info = SimpleNamespace(
                host="192.168.10.4",
                backend_pid=4201,
                transaction_status=TransactionStatus.IDLE,
            )
            self.execute_calls = 0

        def execute(self, *_args, **_kwargs):
            self.execute_calls += 1
            raise AssertionError("must reject before advisory-lock SQL")

    connection = Connection()
    ledger = PostgresLedger(connection, DEV_SCHEMA)
    clock = Clock()
    journal = PpiJobJournal(tmp_path / "same-backend")
    candidate = job_candidate("ppi-same-backend", tmp_path)
    raw = candidate.evidence.raw_bytes
    raw_identity = MaterialIdentity(digest(raw), len(raw))
    journal.submit(
        "ppi-same-backend",
        replace(PpiJobSpec(), effect_domain="owner_nonproduction").document(),
        replace(PpiJobSpec(), effect_domain="owner_nonproduction").scope,
        clock(),
    )
    journal._update(
        "ppi-same-backend",
        clock(),
        state="running",
        stage="materialization_intent",
        plan=_encode_plan(candidate.plan),
        raw={
            "sha256": raw_identity.sha256,
            "size": raw_identity.size,
            "locator": raw_identity.locator,
        },
        writer_session={
            "kind": "postgres",
            "host": "192.168.10.4",
            "backend_pid": 4201,
        },
    )
    journal._update(
        "ppi-same-backend", clock(), state="outcome_unknown", stage="stopped"
    )
    job = PpiLocalJob(
        journal,
        clock,
        CanonicalKernel(ledger, clock),
        GarageMaterialStore(GarageCredentials("access", "secret")),
        PostgresAdvisoryWriterProtection(connection),
        lambda: pytest.fail("must not read"),
        lambda: None,
    )
    result = job.reconcile("ppi-same-backend", enabled=True)
    assert result["state"] == "outcome_unknown"
    assert result["reconciliation"]["outcome"] == "unresolved"
    assert connection.execute_calls == 0


def test_shared_cpi_journal_has_no_unknown_terminal_rewrite(tmp_path):
    journal = FileJobJournal(tmp_path / "cpi-monotone")
    clock = Clock()
    journal.submit("cpi-unknown", {"kind": "test"}, "scope", clock())
    journal._update("cpi-unknown", clock(), state="running")
    journal._update("cpi-unknown", clock(), state="outcome_unknown")
    assert not hasattr(journal, "resolve_unknown")
    with pytest.raises(ContractError, match="job_state_regression"):
        journal._update("cpi-unknown", clock(), state="succeeded")


class FakeTransport:
    def __init__(self, body=None, outcomes=None):
        self.calls = []
        self.body = body or encode(response())
        self.outcomes = list(outcomes) if outcomes is not None else None

    def post(self, endpoint, body, headers, *, timeout, max_response_bytes):
        self.calls.append((endpoint, body, headers, timeout, max_response_bytes))
        if self.outcomes is not None:
            if not self.outcomes:
                raise AssertionError("unexpected extra transport attempt")
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return HttpResult(200, self.body, {"content-encoding": "identity"})


def test_live_edge_first_attempt_persists_no_registration_key(tmp_path):
    secret = tmp_path / "secret.toml"
    secret.write_text('[bls]\napi_key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"\n')
    transport = FakeTransport()
    times = iter(BASE + timedelta(milliseconds=index) for index in range(4))
    candidate = execute_live_candidate(
        secret_path=secret,
        artifact_root=tmp_path / "artifacts",
        transport=transport,
        clock=lambda: next(times),
        identity_factory=lambda: "fixed",
    )
    assert len(transport.calls) == 1
    assert candidate.plan.version.definition == BLS_PPI_FINAL_DEMAND.key
    assert b"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in b"".join(
        path.read_bytes() for path in candidate.artifact_directory.iterdir()
    )
    persisted = json.loads((candidate.artifact_directory / "request.json").read_text())
    assert "registrationkey" not in json.dumps(persisted).lower()


def test_live_edge_transport_retry_then_success_records_attempts(tmp_path):
    secret = tmp_path / "retry-secret.toml"
    secret.write_text('[bls]\napi_key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"\n')
    transport = FakeTransport(
        outcomes=[
            OSError("temporary transport failure"),
            HttpResult(200, encode(response()), {"content-encoding": "identity"}),
        ]
    )
    waits = []
    times = iter(BASE + timedelta(milliseconds=index) for index in range(4))
    candidate = execute_live_candidate(
        secret_path=secret,
        artifact_root=tmp_path / "retry-artifacts",
        transport=transport,
        clock=lambda: next(times),
        identity_factory=lambda: "retry-fixed",
        sleep=waits.append,
    )
    assert len(transport.calls) == 2
    assert waits == [1.0]
    assert candidate.evidence.provider_data_attempts == 2
    assert candidate.plan.acquisition.provider_data_attempts == 2
    observation = json.loads(
        (candidate.artifact_directory / "observation.json").read_text()
    )
    assert observation["provider_data_attempts"] == 2


def test_live_edge_503_503_200_uses_full_shared_budget(tmp_path):
    secret = tmp_path / "http-retry-secret.toml"
    secret.write_text('[bls]\napi_key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"\n')
    transport = FakeTransport(
        outcomes=[
            HttpResult(503, b"temporary", {}),
            HttpResult(503, b"temporary", {}),
            HttpResult(200, encode(response()), {"content-encoding": "identity"}),
        ]
    )
    waits = []
    times = iter(BASE + timedelta(milliseconds=index) for index in range(4))
    candidate = execute_live_candidate(
        secret_path=secret,
        artifact_root=tmp_path / "http-retry-artifacts",
        transport=transport,
        clock=lambda: next(times),
        identity_factory=lambda: "http-retry-fixed",
        sleep=waits.append,
    )
    assert len(transport.calls) == 3
    assert waits == [1.0, 2.0]
    assert candidate.evidence.provider_data_attempts == 3
    assert candidate.plan.acquisition.provider_data_attempts == 3


@pytest.mark.parametrize("status", [302, 401, 403, 404])
def test_live_edge_nonretryable_http_stops_after_one(tmp_path, status):
    secret = tmp_path / f"terminal-{status}.toml"
    secret.write_text('[bls]\napi_key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"\n')
    transport = FakeTransport(outcomes=[HttpResult(status, b"terminal", {})])
    waits = []
    with pytest.raises(LiveExecutionError) as caught:
        execute_live_candidate(
            secret_path=secret,
            artifact_root=tmp_path / f"terminal-{status}",
            transport=transport,
            identity_factory=lambda: f"terminal-{status}",
            sleep=waits.append,
        )
    assert caught.value.provider_data_attempts == 1
    assert len(transport.calls) == 1
    assert waits == []


def test_shape_type_error_is_stable_contract_and_live_failure(tmp_path):
    malformed = response()
    malformed["Results"]["series"][0]["data"][0]["year"] = {}
    raw = encode(malformed)
    with pytest.raises(ContractError, match="bls_ppi_response_shape_invalid"):
        parse_response(raw)
    secret = tmp_path / "shape-secret.toml"
    secret.write_text('[bls]\napi_key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"\n')
    times = iter(BASE + timedelta(milliseconds=index) for index in range(4))
    with pytest.raises(LiveExecutionError) as caught:
        execute_live_candidate(
            secret_path=secret,
            artifact_root=tmp_path / "shape-artifacts",
            transport=FakeTransport(raw),
            clock=lambda: next(times),
            identity_factory=lambda: "shape",
        )
    assert caught.value.classification == "bls_ppi_live_response_contract_failed"
    assert caught.value.provider_data_attempts == 1


def test_request_is_exact_two_series_and_has_no_persisted_credential():
    document = BlsPpiRequest().persisted()
    assert tuple(document["body"]["seriesid"]) == SERIES_IDS
    assert "registrationkey" not in json.dumps(document).lower()
