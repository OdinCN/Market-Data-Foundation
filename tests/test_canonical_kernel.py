from datetime import UTC, datetime, timedelta

import pytest

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import (
    AcquisitionOccurrence,
    ContractError,
    DataRun,
    DatasetDefinition,
    DatasetVersion,
    Decision,
    EvidenceMaterial,
    EvidenceRequirement,
    FiniteScope,
    Grade,
    Key,
    Kind,
    OutputMapping,
    PublicationRequest,
    RunKind,
    Scope,
    SourceBindingRevision,
    SourceOperationRef,
    Transform,
    digest,
)
from market_data_foundation.canonical.protection import (
    ProtectedMaterial,
    ProtectionState,
)
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.canonical.rules import Outcome

BASE = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self, *times: datetime) -> None:
        self.times = iter(times)

    def __call__(self) -> datetime:
        return next(self.times)


def build_source_graph(
    kernel: CanonicalKernel,
    *,
    source_scope: Scope = Scope(),
    mapping_scope: Scope = Scope(),
    evidence_requirement: EvidenceRequirement | None = None,
):
    source = SourceOperationRef(
        identity="kd:sector-analysis.current_rank",
        contract_revision="1",
        owner="kpl-daemon",
        product="kpl-daemon",
        operation="sector-analysis.current_rank",
        projection_revision="p1",
        variant="socket17000:3007:plateType7",
        build_revision="build-1",
        registry_revision="registry-1",
        provider_app_revision=None,
        access_effect="read",
        acquisition_mode="current_state",
        scope=source_scope,
        temporal_contract="source_received_time",
        completeness_contract="bounded_current_window",
        pagination_contract="none",
        freshness_contract="source_qualified",
        evidence_capability=frozenset({Grade.PROJECTED}),
    )
    definition = DatasetDefinition(
        identity="mdf:sector-current-state",
        revision="d1",
        domain_contract="sector current-state sample",
        purpose="source-qualified observation",
        source_qualified=True,
        key_semantics="sector-id",
        unit_semantics="provider-native",
        temporal_semantics="source-received",
        revision_semantics="immutable sample",
        completeness_semantics="bounded",
        scope=mapping_scope,
    )
    mapping = OutputMapping(
        identity="rank",
        definition=definition.key,
        semantic_revision="s1",
        temporal_revision="t1",
        scope=mapping_scope,
        evidence=evidence_requirement
        or EvidenceRequirement(
            allowed=frozenset({Grade.PROJECTED}),
            durable=False,
            replayable=False,
        ),
    )
    binding = SourceBindingRevision(
        identity="binding:kd-sector-rank",
        revision="b1",
        source=source.key,
        mappings=(mapping,),
        compatibility_policy="controlled_adopt",
    )
    run = DataRun(
        identity="run-1",
        kind=RunKind.SAMPLE,
        requested_scope=mapping_scope,
        started_at=BASE,
        finished_at=BASE + timedelta(seconds=1),
        result="success",
        binding=binding.key,
    )
    acquisition = AcquisitionOccurrence(
        identity="acq-1",
        run=run.key,
        request_identity="request-1",
        source=source.key,
        binding=binding.key,
        mapping="rank",
        scope=mapping_scope,
        parameters=(("plateType", "7"),),
        requested_at=BASE,
        obtained_at=BASE + timedelta(milliseconds=100),
        source_observed_at=BASE - timedelta(milliseconds=50),
        source_frame_identity="frame-42",
        result_mode="current_state",
        completeness="bounded",
        freshness="fresh",
        terminal_result="success",
        provider_data_attempts=1,
    )
    source_bytes = b'{"frame":42,"plateType":7}'
    material = EvidenceMaterial(
        identity="material-1",
        grade=Grade.PROJECTED,
        digest=digest(source_bytes),
        scope=mapping_scope,
        acquired_from=acquisition.key,
        controlled_ref="kd://receipt/acq-1/projection",
    )
    canonical_content = b'[{"sector":"AI","strength":"provider-native"}]'
    version = DatasetVersion(
        identity="version-1",
        definition=definition.key,
        finite_scope=FiniteScope(("sector:AI",)),
        scope=mapping_scope,
        frozen_at=BASE + timedelta(seconds=2),
        run=run.key,
        content_digest=digest(canonical_content),
        binding=binding.key,
        mapping="rank",
        acquisitions=(acquisition.key,),
        materials=(material.key,),
        completeness="bounded",
    )

    for record in (source, definition, binding, run, acquisition, material):
        kernel.add(record)
    kernel.persist_version(version, canonical_content)
    return (
        source,
        definition,
        binding,
        run,
        acquisition,
        material,
        version,
        canonical_content,
    )


def request(
    *,
    identity: str,
    occurrence: str,
    version: DatasetVersion,
    material: EvidenceMaterial,
    decision: Decision,
    predecessor: Key | None = None,
    effective_at: datetime | None = None,
    reason: str = "test",
    claimed: frozenset[Grade] = frozenset({Grade.PROJECTED}),
) -> PublicationRequest:
    return PublicationRequest(
        identity=identity,
        occurrence=occurrence,
        version=version.key,
        rule_revision="publication-rule-1",
        decision=decision,
        scope=version.scope,
        evidence=(material.key,),
        claimed_grades=claimed,
        reason=reason,
        predecessor=predecessor,
        effective_at=effective_at,
        required_completeness="bounded",
    )


def test_exact_revision_closure_and_restart_snapshot() -> None:
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, Clock(BASE + timedelta(seconds=3)))
    *_, material, version, content = build_source_graph(kernel)

    publication = kernel.publish(
        request(
            identity="p1",
            occurrence="occ-p1",
            version=version,
            material=material,
            decision=Decision.PUBLISH,
        )
    )
    assert kernel.eligible(version, BASE + timedelta(seconds=4))

    restored = InMemoryLedger.from_snapshot(ledger.snapshot())
    restored_kernel = CanonicalKernel(restored, Clock(BASE + timedelta(seconds=5)))
    assert restored.get_content(version.key) == content
    assert restored.get(version.definition) is not None
    assert restored.get(version.binding) is not None
    assert restored_kernel.eligible(version, BASE + timedelta(seconds=4))
    assert restored.get(publication.key) == publication

    wrong_revision = DatasetVersion(
        **{
            **version.__dict__,
            "identity": "version-wrong-definition",
            "definition": Key(Kind.DEFINITION, version.definition.identity, "d2"),
        }
    )
    with pytest.raises(ContractError, match="version_definition_missing"):
        kernel.persist_version(wrong_revision, content)


def test_cached_frame_is_two_occurrences_not_two_upstream_updates() -> None:
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, Clock(BASE))
    source, _definition, binding, run, acquisition, _material, _version, _ = (
        build_source_graph(kernel)
    )
    second = AcquisitionOccurrence(
        **{
            **acquisition.__dict__,
            "identity": "acq-2",
            "request_identity": "request-2",
            "requested_at": acquisition.requested_at + timedelta(seconds=1),
            "obtained_at": acquisition.obtained_at + timedelta(seconds=1),
        }
    )
    kernel.add(second)
    assert ledger.get(acquisition.key) == acquisition
    assert ledger.get(second.key) == second
    assert second.source_frame_identity == acquisition.source_frame_identity
    assert second.source_observed_at == acquisition.source_observed_at
    assert second.source == source.key
    assert second.binding == binding.key
    assert second.run == run.key


def test_zero_acquisition_reprocess_and_scope_non_expansion() -> None:
    restricted = Scope(frozenset({"kd:principal-a", "kd:principal-b"}))
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, Clock(BASE))
    _source, definition, _binding, _run, _acq, material, version, _ = (
        build_source_graph(
            kernel,
            source_scope=restricted,
            mapping_scope=restricted,
        )
    )
    rerun = DataRun(
        identity="run-reprocess",
        kind=RunKind.REPROCESS,
        requested_scope=Scope(frozenset({"kd:principal-a"})),
        started_at=BASE + timedelta(seconds=10),
        finished_at=BASE + timedelta(seconds=11),
        result="success",
        inputs=(version.key,),
    )
    kernel.add(rerun)
    derived_content = b'{"normalized":true}'
    derived = DatasetVersion(
        identity="version-derived",
        definition=definition.key,
        finite_scope=version.finite_scope,
        scope=Scope(frozenset({"kd:principal-a"})),
        frozen_at=BASE + timedelta(seconds=12),
        run=rerun.key,
        content_digest=digest(derived_content),
        inputs=(version.key,),
        materials=(material.key,),
        completeness="bounded",
    )
    kernel.persist_version(derived, derived_content)
    assert not derived.acquisitions

    widened = DatasetVersion(
        **{
            **derived.__dict__,
            "identity": "version-widened",
            "scope": Scope(),
        }
    )
    with pytest.raises(ContractError, match="scope_widening"):
        kernel.persist_version(widened, derived_content)


def test_evidence_grade_fail_closed() -> None:
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, Clock(BASE + timedelta(seconds=3)))
    *_, material, version, _ = build_source_graph(kernel)
    with pytest.raises(ContractError, match="evidence_grade_inflation"):
        kernel.publish(
            request(
                identity="p-bad-grade",
                occurrence="occ-bad-grade",
                version=version,
                material=material,
                decision=Decision.PUBLISH,
                claimed=frozenset({Grade.ORIGINAL}),
            )
        )


def test_publication_chain_idempotence_and_no_backdating() -> None:
    times = tuple(BASE + timedelta(seconds=value) for value in (3, 4, 5, 10, 11))
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, Clock(*times))
    *_, material, version, _ = build_source_graph(kernel)

    p1 = kernel.publish(
        request(
            identity="p1",
            occurrence="occ-p1",
            version=version,
            material=material,
            decision=Decision.PUBLISH,
        )
    )
    p2 = kernel.publish(
        request(
            identity="p2",
            occurrence="occ-p2",
            version=version,
            material=material,
            decision=Decision.WITHDRAW,
            predecessor=p1.key,
        )
    )
    p3 = kernel.publish(
        request(
            identity="p3",
            occurrence="occ-p3",
            version=version,
            material=material,
            decision=Decision.RESTORE,
            predecessor=p2.key,
        )
    )
    p4_request = request(
        identity="p4",
        occurrence="occ-p4",
        version=version,
        material=material,
        decision=Decision.WITHDRAW,
        predecessor=p3.key,
        effective_at=BASE + timedelta(seconds=6),
    )
    p4 = kernel.publish(p4_request)

    assert p2.request.decision == p4.request.decision
    assert p2.request.occurrence != p4.request.occurrence
    assert kernel.publish(p4_request) == p4
    assert kernel.eligible(version, BASE + timedelta(seconds=6))
    assert not kernel.eligible(version, BASE + timedelta(seconds=10))

    conflicting = PublicationRequest(**{**p4_request.__dict__, "reason": "different"})
    with pytest.raises(ContractError, match="occurrence_identity_conflict"):
        kernel.publish(conflicting)


def test_outcome_unknown_reconciliation_preserves_semantic_identity() -> None:
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, Clock(BASE + timedelta(seconds=3)))
    *_, material, version, _ = build_source_graph(kernel)

    unresolved = kernel.reconcile_publication("occ-lost-ack", Outcome.OUTCOME_UNKNOWN)
    assert unresolved.outcome == Outcome.OUTCOME_UNKNOWN
    assert unresolved.canonical_identity is None

    publication = kernel.publish(
        request(
            identity="p-lost-ack",
            occurrence="occ-lost-ack",
            version=version,
            material=material,
            decision=Decision.PUBLISH,
        )
    )
    recovered = kernel.reconcile_publication("occ-lost-ack", Outcome.OUTCOME_UNKNOWN)
    assert recovered.outcome == Outcome.CONFIRMED_COMMITTED
    assert recovered.canonical_identity == publication.key

    absent = kernel.reconcile_publication(
        "occ-explicit-rollback",
        Outcome.CONFIRMED_NOT_COMMITTED,
    )
    assert absent.outcome == Outcome.CONFIRMED_NOT_COMMITTED
    with pytest.raises(ContractError, match="confirmed_absence_conflict"):
        kernel.reconcile_publication(
            "occ-lost-ack",
            Outcome.CONFIRMED_NOT_COMMITTED,
        )


def test_f3_preliminary_check_cannot_cross_protection_gap() -> None:
    original = b"frozen-material"
    expected = {"E": digest(original)}
    material = ProtectedMaterial({"E": original})

    material.preliminary_verify(expected)
    material.replace("E", b"mutated-before-protection")
    material.activate()
    with pytest.raises(ContractError, match="material_mismatch"):
        material.final_verify(expected)


def test_f3_final_verify_must_remain_continuously_protected() -> None:
    content = b"frozen-material"
    expected = {"E": digest(content)}
    material = ProtectedMaterial({"E": content})
    material.activate()
    material.final_verify(expected)
    assert material.payload("E", expected["E"]) == content

    material.pending()
    assert material.state == ProtectionState.OUTCOME_UNKNOWN
    with pytest.raises(ContractError, match="unresolved_protection"):
        material.release()

    material.resolve()
    material.release()
    assert material.state == ProtectionState.UNPROTECTED


def test_first_decision_requires_publish_and_predecessor_is_latest() -> None:
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(
        ledger,
        Clock(
            BASE + timedelta(seconds=3),
            BASE + timedelta(seconds=4),
            BASE + timedelta(seconds=5),
        ),
    )
    *_, material, version, _ = build_source_graph(kernel)
    with pytest.raises(ContractError, match="first_decision_must_publish"):
        kernel.publish(
            request(
                identity="bad-withdraw",
                occurrence="bad-withdraw",
                version=version,
                material=material,
                decision=Decision.WITHDRAW,
            )
        )

    p1 = kernel.publish(
        request(
            identity="p1",
            occurrence="p1",
            version=version,
            material=material,
            decision=Decision.PUBLISH,
        )
    )
    with pytest.raises(ContractError, match="publication_predecessor_mismatch"):
        kernel.publish(
            request(
                identity="bad-pred",
                occurrence="bad-pred",
                version=version,
                material=material,
                decision=Decision.WITHDRAW,
                predecessor=None,
            )
        )
    assert p1.request.decision == Decision.PUBLISH


def test_restricted_input_cannot_be_reprocessed_to_public_scope() -> None:
    restricted = Scope(frozenset({"kd:principal-a"}))
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, Clock(BASE))
    *_, version, _ = build_source_graph(
        kernel,
        source_scope=restricted,
        mapping_scope=restricted,
    )
    rerun = DataRun(
        identity="run-public-reprocess",
        kind=RunKind.REPROCESS,
        requested_scope=Scope(),
        started_at=BASE + timedelta(seconds=20),
        finished_at=BASE + timedelta(seconds=21),
        result="success",
        inputs=(version.key,),
    )
    with pytest.raises(ContractError, match="scope_widening"):
        kernel.add(rerun)


def test_version_transform_must_match_producing_run() -> None:
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, Clock(BASE))
    _, definition, _, _, _, material, version, _ = build_source_graph(kernel)
    run_transform = Transform("normalize", "1", (("mode", "a"),))
    version_transform = Transform("normalize", "2", (("mode", "b"),))
    rerun = DataRun(
        identity="run-transform",
        kind=RunKind.DERIVE,
        requested_scope=Scope(),
        started_at=BASE + timedelta(seconds=20),
        finished_at=BASE + timedelta(seconds=21),
        result="success",
        inputs=(version.key,),
        transform=run_transform,
    )
    kernel.add(rerun)
    content = b'{"derived":true}'
    derived = DatasetVersion(
        identity="version-transform-mismatch",
        definition=definition.key,
        finite_scope=version.finite_scope,
        scope=Scope(),
        frozen_at=BASE + timedelta(seconds=22),
        run=rerun.key,
        content_digest=digest(content),
        inputs=(version.key,),
        materials=(material.key,),
        transform=version_transform,
        completeness="bounded",
    )
    with pytest.raises(ContractError, match="version_transform_mismatch"):
        kernel.persist_version(derived, content)


def test_source_version_acquisition_must_belong_to_producing_run() -> None:
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, Clock(BASE))
    _, definition, binding, _, acquisition, material, version, _ = build_source_graph(
        kernel
    )
    other_run = DataRun(
        identity="run-other",
        kind=RunKind.SAMPLE,
        requested_scope=Scope(),
        started_at=BASE + timedelta(seconds=30),
        finished_at=BASE + timedelta(seconds=31),
        result="success",
        binding=binding.key,
    )
    kernel.add(other_run)
    content = b'{"cross_run":true}'
    candidate = DatasetVersion(
        identity="version-cross-run",
        definition=definition.key,
        finite_scope=version.finite_scope,
        scope=Scope(),
        frozen_at=BASE + timedelta(seconds=32),
        run=other_run.key,
        content_digest=digest(content),
        binding=binding.key,
        mapping="rank",
        acquisitions=(acquisition.key,),
        materials=(material.key,),
        completeness="bounded",
    )
    with pytest.raises(ContractError, match="version_acquisition_run_mismatch"):
        kernel.persist_version(candidate, content)


def test_publication_scope_cannot_exceed_supporting_evidence_scope() -> None:
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, Clock(BASE + timedelta(seconds=3)))
    _, _, _, run, _, _, version, _ = build_source_graph(kernel)
    restricted = EvidenceMaterial(
        identity="restricted-publication-evidence",
        grade=Grade.PROJECTED,
        digest=digest(b"restricted"),
        scope=Scope(frozenset({"kd:principal-a"})),
        produced_by=run.key,
        controlled_ref="synthetic://restricted",
    )
    kernel.add(restricted)
    with pytest.raises(ContractError, match="scope_widening"):
        kernel.publish(
            request(
                identity="p-restricted-evidence",
                occurrence="occ-restricted-evidence",
                version=version,
                material=restricted,
                decision=Decision.PUBLISH,
            )
        )


def test_binding_evidence_requirement_must_fit_source_capability() -> None:
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, Clock(BASE))
    source, definition, *_ = build_source_graph(kernel)
    bad_binding = SourceBindingRevision(
        identity="binding:requires-original",
        revision="b1",
        source=source.key,
        mappings=(
            OutputMapping(
                identity="out",
                definition=definition.key,
                semantic_revision="s1",
                temporal_revision="t1",
                scope=Scope(),
                evidence=EvidenceRequirement(
                    allowed=frozenset({Grade.ORIGINAL}),
                ),
            ),
        ),
        compatibility_policy="exact",
    )
    with pytest.raises(ContractError, match="binding_evidence_capability_mismatch"):
        kernel.add(bad_binding)


def test_durable_evidence_requires_stable_material_identity() -> None:
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, Clock(BASE))
    _, _, _, run, acquisition, *_ = build_source_graph(kernel)
    material = EvidenceMaterial(
        identity="durable-flag-only",
        grade=Grade.PROJECTED,
        digest=None,
        scope=Scope(),
        acquired_from=acquisition.key,
        produced_by=run.key,
        controlled_ref=None,
        durable=True,
    )
    with pytest.raises(ContractError, match="durable_material_identity_required"):
        kernel.add(material)


def test_publication_rejects_unrelated_public_evidence() -> None:
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, Clock(BASE + timedelta(seconds=3)))
    _, _, _, run, _, _, version, _ = build_source_graph(kernel)
    unrelated = EvidenceMaterial(
        identity="unrelated-public-evidence",
        grade=Grade.PROJECTED,
        digest=digest(b"unrelated"),
        scope=Scope(),
        produced_by=run.key,
        controlled_ref="synthetic://unrelated",
    )
    kernel.add(unrelated)
    with pytest.raises(ContractError, match="publication_evidence_unrelated"):
        kernel.publish(
            request(
                identity="p-unrelated-evidence",
                occurrence="occ-unrelated-evidence",
                version=version,
                material=unrelated,
                decision=Decision.PUBLISH,
            )
        )
