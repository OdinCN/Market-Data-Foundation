"""Deterministic counterexamples for the bounded BLS raw-material handoff."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from test_p4_bls_cpi_direct import encode, live_evidence, response

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import ContractError, digest
from market_data_foundation.canonical.postgres import validate_schema
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical import bls_cpi_direct as direct
from market_data_foundation.historical import bls_cpi_material_store as material_store
from market_data_foundation.historical.bls_cpi_material_store import (
    BUCKET,
    FILENAME,
    GARAGE_ENDPOINT,
    KEY_PREFIX,
    ConfirmedOutcome,
    GarageCredentials,
    GarageMaterialStore,
    InMemoryMaterialStore,
    InMemoryWriterProtection,
    MaterialIdentity,
    MaterializationOutcomeUnknown,
    MaterialStoreFailure,
    MaterialWriteOutcomeUnknown,
    ReconciliationPhase,
    WireResponse,
    WriteDisposition,
    materialize_persist_publish,
)

BASE = datetime(2026, 10, 6, tzinfo=UTC)
CONTRACT_DIGEST = "3adb1869c72c16bd7ee5a95dcd6d0a91d4740bb2abf0620386669dc7a3f0cf2c"
CONTRACT_LOCATOR = (
    "mdf-s3://mdf-artifacts/materials/sha256/" + CONTRACT_DIGEST + "/raw-response.bin"
)


def _plan():
    return direct.build_future_live_plan(
        live_evidence(),
        identity="p4-bls-cpi-garage-material-synthetic",
        finished_at=BASE + timedelta(seconds=2),
        frozen_at=BASE + timedelta(seconds=3),
    )


class FakeWire:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _garage(wire):
    return GarageMaterialStore(
        GarageCredentials("test-access", "test-secret"),
        transport=wire,
        clock=lambda: datetime(2026, 10, 6, 1, 2, 3, tzinfo=UTC),
    )


def test_postgres_allowlist_is_exact_transaction_namespace():
    schema = "mdf_p4_bls_cpi_garage_material_r1"
    assert validate_schema(schema) == schema
    with pytest.raises(ContractError, match="unsafe_postgres_namespace"):
        validate_schema(schema + "_extra")


def test_exact_transaction_locator_is_admitted_without_trusting_locator_fields():
    identity = MaterialIdentity.from_locator(
        CONTRACT_LOCATOR,
        expected_sha256=CONTRACT_DIGEST,
        expected_size=16_194,
    )
    assert identity.bucket == BUCKET
    assert identity.key == f"{KEY_PREFIX}{CONTRACT_DIGEST}/{FILENAME}"
    assert identity.locator == CONTRACT_LOCATOR


@pytest.mark.parametrize(
    "locator",
    [
        CONTRACT_LOCATOR.replace("mdf-artifacts", "other-bucket"),
        CONTRACT_LOCATOR.replace(CONTRACT_DIGEST, "0" * 64),
        CONTRACT_LOCATOR.replace("raw-response.bin", "other.bin"),
        CONTRACT_LOCATOR.replace("mdf-s3://", "s3://"),
    ],
)
def test_wrong_locator_digest_bucket_or_key_is_rejected(locator):
    with pytest.raises(ContractError, match="material_locator_not_admitted"):
        MaterialIdentity.from_locator(
            locator,
            expected_sha256=CONTRACT_DIGEST,
            expected_size=16_194,
        )


def test_identity_rejects_wrong_bucket_key_digest_and_size():
    with pytest.raises(ContractError, match="material_bucket_not_admitted"):
        MaterialIdentity(CONTRACT_DIGEST, 16_194, bucket="other")
    with pytest.raises(ContractError, match="material_key_not_admitted"):
        MaterialIdentity(CONTRACT_DIGEST, 16_194, key="materials/arbitrary")
    with pytest.raises(ContractError, match="material_digest_invalid"):
        MaterialIdentity("A" * 64, 16_194)
    with pytest.raises(ContractError, match="material_size_invalid"):
        MaterialIdentity(CONTRACT_DIGEST, 0)


def test_existing_mismatched_object_fails_closed_without_overwrite():
    class MismatchedExistingStore:
        def __init__(self):
            self.create_attempts = 0
            self.existing = b"mismatched existing bytes"

        def create_if_absent(self, identity, payload):
            self.create_attempts += 1
            return WriteDisposition.ALREADY_PRESENT

        def read_exact(self, identity):
            return self.existing

    raw = encode(response())
    store = MismatchedExistingStore()
    with pytest.raises(ContractError, match="material_existing_mismatch"):
        materialize_persist_publish(
            CanonicalKernel(InMemoryLedger(), lambda: BASE),
            _plan(),
            raw,
            store,
            InMemoryWriterProtection(),
        )
    assert store.existing == b"mismatched existing bytes"
    assert store.create_attempts == 1


def test_no_overwrite_semantics_reuses_only_exact_existing_content():
    raw = encode(response())
    store = InMemoryMaterialStore()
    first = materialize_persist_publish(
        CanonicalKernel(InMemoryLedger(), lambda: BASE),
        _plan(),
        raw,
        store,
        InMemoryWriterProtection(),
    )
    second = materialize_persist_publish(
        CanonicalKernel(InMemoryLedger(), lambda: BASE),
        _plan(),
        raw,
        store,
        InMemoryWriterProtection(),
    )
    assert first.verification.disposition == WriteDisposition.CREATED
    assert second.verification.disposition == WriteDisposition.ALREADY_PRESENT
    assert store.create_attempts == 2
    assert not hasattr(store, "overwrite")
    assert not hasattr(store, "delete")


def test_outcome_unknown_reconciles_same_identity_without_second_create():
    raw = encode(response())
    identity = MaterialIdentity(digest(raw), len(raw))
    store = InMemoryMaterialStore()
    store.outcome_unknown_after_create = True
    verified = materialize_persist_publish(
        CanonicalKernel(InMemoryLedger(), lambda: BASE),
        _plan(),
        raw,
        store,
        InMemoryWriterProtection(),
    ).verification
    assert verified.reconciled_outcome_unknown is True
    assert verified.identity == identity
    assert store.create_attempts == 1
    assert store.reads == 1


def test_unreconciled_outcome_unknown_stays_unknown_and_is_not_retried():
    class UnknownStore:
        def __init__(self):
            self.attempts = 0

        def create_if_absent(self, identity, payload):
            self.attempts += 1
            raise MaterialWriteOutcomeUnknown

        def read_exact(self, identity):
            return None

    raw = encode(response())
    store = UnknownStore()
    with pytest.raises(ContractError, match="material_write_outcome_unknown"):
        materialize_persist_publish(
            CanonicalKernel(InMemoryLedger(), lambda: BASE),
            _plan(),
            raw,
            store,
            InMemoryWriterProtection(),
        )
    assert store.attempts == 1


def test_protection_gap_blocks_final_verification_and_handoff():
    raw = encode(response())
    store = InMemoryMaterialStore()

    class GapProtection(InMemoryWriterProtection):
        def acquire(self, identity):
            ticket = super().acquire(identity)
            ticket.force_gap_for_counterexample()
            return ticket

    with pytest.raises(ContractError, match="material_writer_protection_gap"):
        materialize_persist_publish(
            CanonicalKernel(InMemoryLedger(), lambda: BASE),
            _plan(),
            raw,
            store,
            GapProtection(),
        )
    assert store.create_attempts == 0


def test_no_public_constructible_verification_can_promote_a_plan():
    plan = _plan()
    assert plan.raw_material.durable is plan.raw_material.replayable is False
    assert not hasattr(material_store, "VerifiedMaterial")
    assert not hasattr(material_store, "durable_live_plan")
    assert not hasattr(material_store, "verify_or_create")


def test_garage_absent_get_then_one_ordinary_put_then_orchestration_final_get():
    raw = encode(response())
    identity = MaterialIdentity(digest(raw), len(raw))
    wire = FakeWire(
        WireResponse(404, b""), WireResponse(201, b""), WireResponse(200, raw)
    )
    store = _garage(wire)
    result = materialize_persist_publish(
        CanonicalKernel(InMemoryLedger(), lambda: BASE),
        _plan(),
        raw,
        store,
        InMemoryWriterProtection(),
    )
    assert result.verification.disposition == WriteDisposition.CREATED
    assert [request.method for request in wire.requests] == ["GET", "PUT", "GET"]
    request = wire.requests[1]
    assert request.method == "PUT"
    assert request.url == f"{GARAGE_ENDPOINT}/{BUCKET}/{identity.key}"
    assert "if-none-match" not in request.headers
    assert request.headers["host"] == "192.168.10.8:3900"
    assert request.headers["x-amz-content-sha256"] == hashlib.sha256(raw).hexdigest()
    assert request.headers["x-amz-date"] == "20261006T010203Z"
    assert request.headers["authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=test-access/20261006/garage/s3/aws4_request, "
        "SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature="
    )
    assert "test-secret" not in repr(store)
    assert not hasattr(store, "overwrite")
    assert not hasattr(store, "delete")


def test_garage_exact_existing_object_uses_only_get_and_never_puts():
    raw = encode(response())
    wire = FakeWire(WireResponse(200, raw), WireResponse(200, raw))
    result = materialize_persist_publish(
        CanonicalKernel(InMemoryLedger(), lambda: BASE),
        _plan(),
        raw,
        _garage(wire),
        InMemoryWriterProtection(),
    )
    assert result.verification.disposition == WriteDisposition.ALREADY_PRESENT
    assert [request.method for request in wire.requests] == ["GET", "GET"]


def test_garage_mismatched_preliminary_get_fails_before_put():
    raw = encode(response())
    wire = FakeWire(WireResponse(200, b"wrong"))
    with pytest.raises(ContractError, match="material_existing_mismatch"):
        materialize_persist_publish(
            CanonicalKernel(InMemoryLedger(), lambda: BASE),
            _plan(),
            raw,
            _garage(wire),
            InMemoryWriterProtection(),
        )
    assert [request.method for request in wire.requests] == ["GET"]


def test_garage_fake_wire_get_validates_bytes_and_does_not_trust_etag():
    raw = encode(response())
    identity = MaterialIdentity(digest(raw), len(raw))
    wire = FakeWire(WireResponse(200, raw))
    assert _garage(wire).read_exact(identity) == raw
    request = wire.requests[0]
    assert request.method == "GET"
    assert "if-none-match" not in request.headers
    assert request.headers["x-amz-content-sha256"] == hashlib.sha256(b"").hexdigest()


def test_garage_write_transport_ambiguity_is_unknown_and_never_retried():
    raw = encode(response())
    identity = MaterialIdentity(digest(raw), len(raw))
    wire = FakeWire(WireResponse(404, b""), TimeoutError())
    with pytest.raises(MaterialWriteOutcomeUnknown):
        _garage(wire).create_if_absent(identity, raw)
    assert [request.method for request in wire.requests] == ["GET", "PUT"]


def test_garage_put_503_reconciles_same_identity_without_second_put():
    raw = encode(response())
    wire = FakeWire(
        WireResponse(404, b""), WireResponse(503, b"ignored"), WireResponse(200, raw)
    )
    result = materialize_persist_publish(
        CanonicalKernel(InMemoryLedger(), lambda: BASE),
        _plan(),
        raw,
        _garage(wire),
        InMemoryWriterProtection(),
    )
    assert result.verification.reconciled_outcome_unknown is True
    assert [request.method for request in wire.requests] == ["GET", "PUT", "GET"]


@pytest.mark.parametrize(
    ("method", "status", "classification"),
    [
        ("write", 403, "material_write_rejected"),
        ("read", 403, "material_read_rejected"),
        ("read", 503, "material_read_service_failure"),
    ],
)
def test_garage_status_classification_is_secret_safe(method, status, classification):
    raw = encode(response())
    identity = MaterialIdentity(digest(raw), len(raw))
    responses = [WireResponse(status, b"secret server body")]
    if method == "write":
        responses.insert(0, WireResponse(404, b""))
    store = _garage(FakeWire(*responses))
    with pytest.raises(MaterialStoreFailure, match=classification) as caught:
        if method == "write":
            store.create_if_absent(identity, raw)
        else:
            store.read_exact(identity)
    assert "secret server body" not in str(caught.value)


def test_garage_read_404_is_absent_and_mismatched_body_fails_closed():
    raw = encode(response())
    identity = MaterialIdentity(digest(raw), len(raw))
    assert _garage(FakeWire(WireResponse(404, b"ignored"))).read_exact(identity) is None
    with pytest.raises(ContractError, match="material_existing_mismatch"):
        _garage(FakeWire(WireResponse(200, b"wrong"))).read_exact(identity)


def test_fixed_garage_endpoint_cannot_be_substituted():
    with pytest.raises(ContractError, match="garage_endpoint_not_admitted"):
        GarageMaterialStore(
            GarageCredentials("test-access", "test-secret"),
            endpoint="http://example.invalid",
            transport=FakeWire(),
        )


def test_confirmed_pre_handoff_failure_releases_protection():
    protection = InMemoryWriterProtection()
    with pytest.raises(ContractError, match="material_content_mismatch"):
        materialize_persist_publish(
            CanonicalKernel(InMemoryLedger(), lambda: BASE),
            _plan(),
            b"wrong",
            InMemoryMaterialStore(),
            protection,
        )
    raw = encode(response())
    identity = MaterialIdentity(digest(raw), len(raw))
    protection.acquire(identity).release(identity)


def test_ledger_outcome_unknown_retains_same_identity_until_explicit_resolution():
    raw = encode(response())
    protection = InMemoryWriterProtection()

    def unknown_commit():
        raise OSError("lost acknowledgement")

    with pytest.raises(MaterializationOutcomeUnknown) as caught:
        materialize_persist_publish(
            CanonicalKernel(InMemoryLedger(), lambda: BASE),
            _plan(),
            raw,
            InMemoryMaterialStore(),
            protection,
            authoritative_commit=unknown_commit,
        )
    unresolved = caught.value
    assert unresolved.phase == ReconciliationPhase.LEDGER_HANDOFF
    assert unresolved.identity == MaterialIdentity(digest(raw), len(raw))
    assert unresolved.version_identity == _plan().version.identity
    assert unresolved.publication_occurrence == _plan().publication_request.occurrence
    unresolved.assert_protection_held()
    with pytest.raises(ContractError, match="material_writer_busy"):
        protection.acquire(unresolved.identity)
    assert not hasattr(unresolved, "resolve")
    with pytest.raises(AttributeError):
        unresolved.reconcile(ConfirmedOutcome.COMMITTED)
    unresolved.assert_protection_held()
    assert unresolved.reconcile(InMemoryLedger()) == ConfirmedOutcome.NOT_COMMITTED
    protection.acquire(unresolved.identity).release(unresolved.identity)


def test_ledger_outcome_unknown_derives_committed_from_exact_record_and_content():
    raw = encode(response())
    ledger = InMemoryLedger()
    protection = InMemoryWriterProtection()

    def unknown_commit():
        raise OSError("lost acknowledgement")

    with pytest.raises(MaterializationOutcomeUnknown) as caught:
        materialize_persist_publish(
            CanonicalKernel(ledger, lambda: BASE),
            _plan(),
            raw,
            InMemoryMaterialStore(),
            protection,
            authoritative_commit=unknown_commit,
        )
    assert caught.value.reconcile(ledger) == ConfirmedOutcome.COMMITTED


def test_ledger_outcome_unknown_mismatch_and_read_error_retain_protection():
    raw = encode(response())
    protection = InMemoryWriterProtection()

    def unknown_commit():
        raise OSError("lost acknowledgement")

    with pytest.raises(MaterializationOutcomeUnknown) as caught:
        materialize_persist_publish(
            CanonicalKernel(InMemoryLedger(), lambda: BASE),
            _plan(),
            raw,
            InMemoryMaterialStore(),
            protection,
            authoritative_commit=unknown_commit,
        )
    unresolved = caught.value

    class MismatchedLedger(InMemoryLedger):
        def get(self, key):
            return replace(unresolved.expected_version, completeness="wrong")

    with pytest.raises(ContractError, match="material_ledger_readback_mismatch"):
        unresolved.reconcile(MismatchedLedger())
    unresolved.assert_protection_held()

    class MismatchedContentLedger(InMemoryLedger):
        def get(self, key):
            return unresolved.expected_version

        def get_content(self, version):
            return b"wrong"

    with pytest.raises(
        ContractError, match="material_ledger_content_readback_mismatch"
    ):
        unresolved.reconcile(MismatchedContentLedger())
    unresolved.assert_protection_held()

    class ContentWithoutRecordLedger(InMemoryLedger):
        def get_content(self, version):
            return unresolved.expected_content

    with pytest.raises(
        ContractError, match="material_ledger_absence_readback_mismatch"
    ):
        unresolved.reconcile(ContentWithoutRecordLedger())
    unresolved.assert_protection_held()

    class ErrorLedger(InMemoryLedger):
        def get(self, key):
            raise OSError("readback unavailable")

    with pytest.raises(OSError, match="readback unavailable"):
        unresolved.reconcile(ErrorLedger())
    unresolved.assert_protection_held()
    with pytest.raises(ContractError, match="material_writer_busy"):
        protection.acquire(unresolved.identity)
    assert unresolved.reconcile(InMemoryLedger()) == ConfirmedOutcome.NOT_COMMITTED


def test_publication_outcome_unknown_derives_absence_from_occurrence_readback():
    class UnknownPublicationLedger(InMemoryLedger):
        def publish(self, request, clock):
            raise OSError("publication acknowledgement lost")

    raw = encode(response())
    plan = _plan()
    protection = InMemoryWriterProtection()
    with pytest.raises(MaterializationOutcomeUnknown) as caught:
        materialize_persist_publish(
            CanonicalKernel(UnknownPublicationLedger(), lambda: BASE),
            plan,
            raw,
            InMemoryMaterialStore(),
            protection,
        )
    unresolved = caught.value
    assert unresolved.phase == ReconciliationPhase.PUBLICATION
    assert unresolved.identity == MaterialIdentity(digest(raw), len(raw))
    assert unresolved.version_identity == plan.version.identity
    assert unresolved.publication_occurrence == plan.publication_request.occurrence
    unresolved.assert_protection_held()
    assert unresolved.reconcile(InMemoryLedger()) == ConfirmedOutcome.NOT_COMMITTED


def test_publication_outcome_unknown_derives_exact_committed_occurrence():
    class CommittedUnknownPublicationLedger(InMemoryLedger):
        def publish(self, request, clock):
            super().publish(request, clock)
            raise OSError("publication acknowledgement lost")

    raw = encode(response())
    ledger = CommittedUnknownPublicationLedger()
    with pytest.raises(MaterializationOutcomeUnknown) as caught:
        materialize_persist_publish(
            CanonicalKernel(ledger, lambda: BASE),
            _plan(),
            raw,
            InMemoryMaterialStore(),
            InMemoryWriterProtection(),
        )
    assert caught.value.reconcile(ledger) == ConfirmedOutcome.COMMITTED


def test_publication_mismatch_and_readback_error_keep_unknown_unresolved():
    class UnknownPublicationLedger(InMemoryLedger):
        def publish(self, request, clock):
            raise OSError("publication acknowledgement lost")

    raw = encode(response())
    plan = _plan()
    protection = InMemoryWriterProtection()
    with pytest.raises(MaterializationOutcomeUnknown) as caught:
        materialize_persist_publish(
            CanonicalKernel(UnknownPublicationLedger(), lambda: BASE),
            plan,
            raw,
            InMemoryMaterialStore(),
            protection,
        )
    unresolved = caught.value

    publication = materialize_persist_publish(
        CanonicalKernel(InMemoryLedger(), lambda: BASE),
        plan,
        raw,
        InMemoryMaterialStore(),
        InMemoryWriterProtection(),
    ).publication

    class MismatchedPublicationLedger(InMemoryLedger):
        def publication_by_occurrence(self, occurrence):
            return replace(
                publication,
                request=replace(publication.request, reason="wrong"),
            )

    with pytest.raises(ContractError, match="material_publication_readback_mismatch"):
        unresolved.reconcile(MismatchedPublicationLedger())
    unresolved.assert_protection_held()

    class ErrorPublicationLedger(InMemoryLedger):
        def publication_by_occurrence(self, occurrence):
            raise OSError("occurrence readback unavailable")

    with pytest.raises(OSError, match="occurrence readback unavailable"):
        unresolved.reconcile(ErrorPublicationLedger())
    unresolved.assert_protection_held()
    assert unresolved.reconcile(InMemoryLedger()) == ConfirmedOutcome.NOT_COMMITTED


def test_advisory_keys_are_deterministic_nonnegative_int32_values():
    raw = encode(response())
    identity = MaterialIdentity(digest(raw), len(raw))
    keys = material_store._advisory_keys(identity)
    assert keys == material_store._advisory_keys(identity)
    assert all(0 <= key <= 2**31 - 1 for key in keys)


def test_protection_is_continuous_through_ledger_handoff_and_publication_last():
    raw = encode(response())
    plan = _plan()
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=4))
    result = materialize_persist_publish(
        kernel,
        plan,
        raw,
        InMemoryMaterialStore(),
        InMemoryWriterProtection(),
    )
    assert result.plan.raw_material.durable is True
    assert result.plan.raw_material.replayable is True
    assert ledger.get(result.plan.raw_material.key) == result.plan.raw_material
    assert ledger.get_content(result.version.key) == plan.content
    assert ledger.publication_by_occurrence(plan.publication_request.occurrence) == (
        result.publication
    )
    assert "candidate_only_not_active" in plan.binding_candidate.limitations


def test_wrong_plan_digest_cannot_be_materialized():
    raw = encode(response())
    plan = _plan()
    wrong = replace(plan, raw_material=replace(plan.raw_material, digest="0" * 64))
    with pytest.raises(ContractError, match="material_content_mismatch"):
        materialize_persist_publish(
            CanonicalKernel(InMemoryLedger(), lambda: BASE),
            wrong,
            raw,
            InMemoryMaterialStore(),
            InMemoryWriterProtection(),
        )
