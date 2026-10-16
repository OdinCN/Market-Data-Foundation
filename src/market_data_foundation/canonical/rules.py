"""Semantic gates for the bounded canonical kernel."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .model import (
    AcquisitionOccurrence,
    DataRun,
    DatasetDefinition,
    DatasetVersion,
    Decision,
    EvidenceMaterial,
    EvidenceRequirement,
    Grade,
    Key,
    Kind,
    Publication,
    PublicationRequest,
    Record,
    RunKind,
    Scope,
    SourceBindingRevision,
    SourceOperationRef,
    immutable_shape,
    instant,
    require,
)

Resolver = Callable[[Key], Record | None]


class Outcome(StrEnum):
    CONFIRMED_COMMITTED = "confirmed_committed"
    CONFIRMED_NOT_COMMITTED = "confirmed_not_committed"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True)
class OutcomeResolution:
    operation_identity: str
    outcome: Outcome
    canonical_identity: Key | None = None

    def __post_init__(self) -> None:
        require(bool(self.operation_identity), "empty_operation_identity")
        require(
            (self.outcome == Outcome.CONFIRMED_COMMITTED)
            == (self.canonical_identity is not None),
            "outcome_identity_mismatch",
        )


def resolve_as(record: Record | None, cls: type, classification: str):
    require(record is not None and isinstance(record, cls), classification)
    return record


def validate_scope_monotonic(output: Scope, inputs: Iterable[Scope]) -> None:
    for parent in inputs:
        require(output.within(parent), "scope_widening")


def _mapping(binding: SourceBindingRevision, identity: str):
    matches = [mapping for mapping in binding.mappings if mapping.identity == identity]
    require(len(matches) == 1, "mapping_not_unique_or_missing")
    return matches[0]


def _digest_shape(value: str) -> None:
    require(
        len(value) == 64
        and all(character in "0123456789abcdef" for character in value),
        "invalid_digest",
    )


def validate_record(record: Record, resolve: Resolver) -> None:
    """Validate one record against already-durable referenced records."""
    immutable_shape(record)

    if isinstance(record, SourceOperationRef):
        require(
            bool(record.owner and record.product and record.operation),
            "source_identity_incomplete",
        )
        require(
            bool(record.contract_revision and record.projection_revision),
            "source_revision_incomplete",
        )
        require(
            not record.access_effect.startswith("write"),
            "source_write_effect_not_admitted",
        )
        return

    if isinstance(record, DatasetDefinition):
        require(
            bool(record.identity and record.revision), "definition_identity_incomplete"
        )
        return

    if isinstance(record, SourceBindingRevision):
        require(record.source.kind == Kind.SOURCE, "binding_source_kind")
        source = resolve_as(
            resolve(record.source), SourceOperationRef, "binding_source_missing"
        )
        require(not record.silent_fallback, "silent_fallback")
        require(bool(record.mappings), "binding_without_mapping")
        identities = [mapping.identity for mapping in record.mappings]
        require(len(identities) == len(set(identities)), "duplicate_mapping")
        for mapping in record.mappings:
            require(
                mapping.definition.kind == Kind.DEFINITION, "mapping_definition_kind"
            )
            definition = resolve_as(
                resolve(mapping.definition),
                DatasetDefinition,
                "mapping_definition_missing",
            )
            validate_scope_monotonic(mapping.scope, (source.scope, definition.scope))
            source_grades = {Grade.ORIGINAL, Grade.DECODED, Grade.PROJECTED}
            require(
                mapping.evidence.allowed & source_grades <= source.evidence_capability,
                "binding_evidence_capability_mismatch",
            )
        return

    if isinstance(record, DataRun):
        instant(record.started_at)
        if record.finished_at is not None:
            instant(record.finished_at)
            require(record.finished_at >= record.started_at, "run_time_reversal")
        if record.deadline is not None:
            instant(record.deadline)
        if record.binding is not None:
            require(record.binding.kind == Kind.BINDING, "run_binding_kind")
            resolve_as(
                resolve(record.binding), SourceBindingRevision, "run_binding_missing"
            )
        if record.kind in {RunKind.COLLECT, RunKind.SAMPLE, RunKind.RECORDING}:
            require(record.binding is not None, "source_run_binding_required")
        if record.kind in {RunKind.REPROCESS, RunKind.DERIVE, RunKind.VALIDATE}:
            require(
                bool(record.inputs) or record.transform is not None,
                "offline_run_input_required",
            )
        for key in record.inputs:
            require(key.kind in {Kind.VERSION, Kind.MATERIAL}, "run_input_kind")
            parent = resolve_as(
                resolve(key), (DatasetVersion, EvidenceMaterial), "run_input_missing"
            )
            validate_scope_monotonic(record.requested_scope, (parent.scope,))
        return

    if isinstance(record, AcquisitionOccurrence):
        require(record.run.kind == Kind.RUN, "acquisition_run_kind")
        require(record.source.kind == Kind.SOURCE, "acquisition_source_kind")
        require(record.binding.kind == Kind.BINDING, "acquisition_binding_kind")
        run = resolve_as(resolve(record.run), DataRun, "acquisition_run_missing")
        source = resolve_as(
            resolve(record.source), SourceOperationRef, "acquisition_source_missing"
        )
        binding = resolve_as(
            resolve(record.binding),
            SourceBindingRevision,
            "acquisition_binding_missing",
        )
        require(run.binding == record.binding, "acquisition_run_binding_mismatch")
        require(binding.source == record.source, "acquisition_source_binding_mismatch")
        mapping = _mapping(binding, record.mapping)
        validate_scope_monotonic(
            record.scope, (run.requested_scope, source.scope, mapping.scope)
        )
        instant(record.requested_at)
        for value in (record.obtained_at, record.source_observed_at):
            if value is not None:
                instant(value)
        if record.provider_data_attempts is not None:
            require(record.provider_data_attempts >= 0, "invalid_provider_attempts")
        return

    if isinstance(record, EvidenceMaterial):
        require(
            (record.acquired_from is not None) or (record.produced_by is not None),
            "material_without_producer",
        )
        if record.acquired_from is not None:
            require(
                record.acquired_from.kind == Kind.ACQUISITION,
                "material_acquisition_kind",
            )
            acquisition = resolve_as(
                resolve(record.acquired_from),
                AcquisitionOccurrence,
                "material_acquisition_missing",
            )
            validate_scope_monotonic(record.scope, (acquisition.scope,))
            if record.produced_by is not None:
                require(
                    record.produced_by == acquisition.run, "material_producer_mismatch"
                )
            if record.grade in {Grade.ORIGINAL, Grade.DECODED, Grade.PROJECTED}:
                source = resolve_as(
                    resolve(acquisition.source),
                    SourceOperationRef,
                    "material_source_missing",
                )
                require(
                    record.grade in source.evidence_capability,
                    "material_grade_capability_mismatch",
                )
        if record.produced_by is not None:
            require(record.produced_by.kind == Kind.RUN, "material_run_kind")
            producer = resolve_as(
                resolve(record.produced_by), DataRun, "material_run_missing"
            )
            validate_scope_monotonic(record.scope, (producer.requested_scope,))
        if record.digest is not None:
            _digest_shape(record.digest)
        if record.replayable:
            require(record.durable, "replayable_not_durable")
        if record.durable:
            require(
                record.digest is not None
                and bool(record.controlled_ref and record.controlled_ref.strip()),
                "durable_material_identity_required",
            )
        if record.assessment is not None:
            require(record.grade == Grade.ASSESSMENT, "assessment_grade_mismatch")
            require(
                record.assessment.target.kind == Kind.VERSION, "assessment_target_kind"
            )
            target = resolve_as(
                resolve(record.assessment.target),
                DatasetVersion,
                "assessment_target_missing",
            )
            instant(record.assessment.evaluated_at)
            validate_scope_monotonic(record.scope, (target.scope,))
            for key in record.assessment.basis:
                basis = resolve_as(
                    resolve(key),
                    (DatasetVersion, EvidenceMaterial),
                    "assessment_basis_missing",
                )
                validate_scope_monotonic(record.scope, (basis.scope,))
        return

    if isinstance(record, DatasetVersion):
        require(record.definition.kind == Kind.DEFINITION, "version_definition_kind")
        require(record.run.kind == Kind.RUN, "version_run_kind")
        definition = resolve_as(
            resolve(record.definition), DatasetDefinition, "version_definition_missing"
        )
        run = resolve_as(resolve(record.run), DataRun, "version_run_missing")
        instant(record.frozen_at)
        _digest_shape(record.content_digest)
        validate_scope_monotonic(record.scope, (definition.scope, run.requested_scope))
        require(record.transform == run.transform, "version_transform_mismatch")
        require(
            set(record.inputs)
            == {key for key in run.inputs if key.kind == Kind.VERSION},
            "version_run_inputs_mismatch",
        )
        inherited_materials = material_lineage(run.inputs, resolve)
        if record.binding is None:
            require(record.mapping is None, "mapping_without_binding")
            require(
                bool(record.inputs) or bool(record.materials),
                "derived_version_without_inputs",
            )
        else:
            require(record.binding.kind == Kind.BINDING, "version_binding_kind")
            binding = resolve_as(
                resolve(record.binding),
                SourceBindingRevision,
                "version_binding_missing",
            )
            require(record.binding == run.binding, "version_run_binding_mismatch")
            require(record.mapping is not None, "version_mapping_required")
            mapping = _mapping(binding, record.mapping)
            require(
                mapping.definition == record.definition, "definition_mapping_mismatch"
            )
            validate_scope_monotonic(record.scope, (mapping.scope,))
            require(bool(record.acquisitions), "source_version_without_acquisition")
        for key in record.acquisitions:
            require(key.kind == Kind.ACQUISITION, "version_acquisition_kind")
            acquisition = resolve_as(
                resolve(key), AcquisitionOccurrence, "version_acquisition_missing"
            )
            require(acquisition.run == record.run, "version_acquisition_run_mismatch")
            require(
                record.binding == acquisition.binding
                and record.mapping == acquisition.mapping,
                "version_acquisition_context_mismatch",
            )
            validate_scope_monotonic(record.scope, (acquisition.scope,))
        for key in record.materials:
            require(key.kind == Kind.MATERIAL, "version_material_kind")
            material = resolve_as(
                resolve(key), EvidenceMaterial, "version_material_missing"
            )
            validate_scope_monotonic(record.scope, (material.scope,))
            require(
                key in inherited_materials
                or material.acquired_from in record.acquisitions
                or material.produced_by == record.run,
                "version_material_provenance_mismatch",
            )
        for key in record.inputs:
            require(key.kind == Kind.VERSION, "version_input_kind")
            parent = resolve_as(resolve(key), DatasetVersion, "version_input_missing")
            validate_scope_monotonic(record.scope, (parent.scope,))
        for key in record.quality_evidence:
            require(key.kind == Kind.MATERIAL, "quality_evidence_kind")
            material = resolve_as(
                resolve(key), EvidenceMaterial, "quality_evidence_missing"
            )
            validate_scope_monotonic(record.scope, (material.scope,))
            require(
                key in inherited_materials
                or material.produced_by == record.run
                or material.acquired_from in record.acquisitions,
                "quality_evidence_provenance_mismatch",
            )
        if record.supersedes is not None:
            require(record.supersedes.kind == Kind.VERSION, "supersedes_kind")
            resolve_as(resolve(record.supersedes), DatasetVersion, "supersedes_missing")
        return

    if isinstance(record, Publication):
        require(record.request.version.kind == Kind.VERSION, "publication_version_kind")
        version = resolve_as(
            resolve(record.request.version),
            DatasetVersion,
            "publication_version_missing",
        )
        instant(record.decision_at)
        instant(record.usable_at)
        require(record.usable_at >= record.decision_at, "usable_before_decision")
        validate_pce_publication_time(record, version, resolve)
        validate_scope_monotonic(record.request.scope, (version.scope,))
        validate_evidence_for_publication(record.request, version, resolve)
        if record.request.predecessor is not None:
            require(
                record.request.predecessor.kind == Kind.PUBLICATION,
                "publication_predecessor_kind",
            )
        return

    raise TypeError(type(record))


def validate_pce_publication_time(
    publication: Publication, version: DatasetVersion, resolve: Resolver
) -> None:
    """T04's named Source boundary; other products retain their time contracts.

    Compare observed instants. Never shift source/freeze times or manufacture a
    later decision to accommodate an acquisition from the future.
    """
    if version.definition != Key(
        Kind.DEFINITION, "mdf.macro.bea.pce.headline-core", "t04-r1"
    ):
        return
    require(
        version.frozen_at <= publication.decision_at,
        "pce_publication_before_freeze",
    )
    for key in version.acquisitions:
        acquisition = resolve_as(
            resolve(key), AcquisitionOccurrence, "version_acquisition_missing"
        )
        require(
            acquisition.obtained_at is not None
            and acquisition.obtained_at <= version.frozen_at
            and acquisition.obtained_at <= publication.decision_at,
            "pce_publication_before_acquisition",
        )


def material_lineage(keys: Iterable[Key], resolve: Resolver) -> set[Key]:
    """Follow frozen input-version/material relations, never current config."""
    pending = list(keys)
    visited: set[Key] = set()
    materials: set[Key] = set()
    while pending:
        key = pending.pop()
        if key in visited:
            continue
        visited.add(key)
        value = resolve(key)
        if isinstance(value, EvidenceMaterial):
            materials.add(key)
        elif isinstance(value, DatasetVersion):
            pending.extend((*value.inputs, *value.materials, *value.quality_evidence))
    return materials


def requirement_for_version(
    version: DatasetVersion, resolve: Resolver
) -> EvidenceRequirement | None:
    if version.binding is None:
        return None
    binding = resolve_as(
        resolve(version.binding), SourceBindingRevision, "version_binding_missing"
    )
    require(version.mapping is not None, "version_mapping_required")
    return _mapping(binding, version.mapping).evidence


def validate_evidence_for_publication(
    request: PublicationRequest,
    version: DatasetVersion,
    resolve: Resolver,
) -> None:
    requirement = requirement_for_version(version, resolve)
    materials: list[EvidenceMaterial] = []
    relevant = material_lineage(
        (*version.materials, *version.quality_evidence, *version.inputs), resolve
    )
    for key in request.evidence:
        require(key.kind == Kind.MATERIAL, "publication_evidence_kind")
        material = resolve_as(
            resolve(key), EvidenceMaterial, "publication_evidence_missing"
        )
        validate_scope_monotonic(request.scope, (material.scope,))
        require(
            key in relevant
            or (
                material.grade == Grade.ASSESSMENT
                and material.assessment is not None
                and material.assessment.target == version.key
            ),
            "publication_evidence_unrelated",
        )
        materials.append(material)

    actual = frozenset(material.grade for material in materials)
    require(request.claimed_grades <= actual, "evidence_grade_inflation")
    supporting = [
        material for material in materials if material.grade in request.claimed_grades
    ]
    if request.claims_replayable:
        require(
            any(
                material.replayable
                and material.durable
                and material.digest
                and material.controlled_ref
                for material in supporting
            ),
            "replayability_inflation",
        )
    if requirement is None:
        return

    require(bool(request.claimed_grades), "publication_evidence_required")
    require(request.claimed_grades <= requirement.allowed, "evidence_grade_not_allowed")
    require(bool(supporting), "publication_evidence_missing")
    if requirement.durable:
        require(
            any(
                material.durable and material.digest and material.controlled_ref
                for material in supporting
            ),
            "durable_evidence_required",
        )
    if requirement.replayable:
        require(
            request.claims_replayable
            and any(
                material.replayable
                and material.durable
                and material.digest
                and material.controlled_ref
                for material in supporting
            ),
            "replayable_evidence_required",
        )
    if request.claims_replayable:
        require(
            any(material.replayable for material in supporting),
            "replayability_inflation",
        )


def _effective(publication: Publication, at: datetime) -> bool:
    if publication.decision_at > at or publication.usable_at > at:
        return False
    effective_at = publication.request.effective_at
    return effective_at is None or effective_at <= at


def eligible_as_of(publications: Iterable[Publication], at: datetime) -> bool:
    instant(at)
    eligible = False
    ordered = sorted(publications, key=lambda publication: publication.decision_at)
    for publication in ordered:
        if not _effective(publication, at):
            continue
        if publication.request.decision in {Decision.PUBLISH, Decision.RESTORE}:
            eligible = True
        elif publication.request.decision in {Decision.WITHDRAW, Decision.SUPERSEDE}:
            eligible = False
    return eligible


def build_publication(
    request: PublicationRequest,
    *,
    resolve: Resolver,
    history: tuple[Publication, ...],
    existing_occurrence: Publication | None,
    decision_at: datetime,
) -> Publication:
    """Build one immutable decision; repository commit establishes durability."""
    instant(decision_at)
    require(
        bool(request.identity and request.occurrence and request.rule_revision),
        "publication_identity_incomplete",
    )
    version = resolve_as(
        resolve(request.version), DatasetVersion, "publication_version_missing"
    )
    validate_scope_monotonic(request.scope, (version.scope,))
    validate_evidence_for_publication(request, version, resolve)

    if request.effective_at is not None:
        instant(request.effective_at)
    requested_usable = request.usable_at or decision_at
    instant(requested_usable)
    require(requested_usable >= decision_at, "usable_before_decision")
    if request.required_completeness is not None:
        require(
            request.required_completeness == version.completeness,
            "completeness_requirement_failed",
        )

    if existing_occurrence is not None:
        require(existing_occurrence.request == request, "occurrence_identity_conflict")
        return existing_occurrence

    ordered = sorted(history, key=lambda publication: publication.decision_at)
    latest = ordered[-1] if ordered else None
    if latest is None:
        require(request.decision == Decision.PUBLISH, "first_decision_must_publish")
        require(request.predecessor is None, "unexpected_predecessor")
    else:
        require(request.predecessor == latest.key, "publication_predecessor_mismatch")
        state = latest.request.decision
        allowed = {
            Decision.PUBLISH: {Decision.WITHDRAW, Decision.SUPERSEDE},
            Decision.RESTORE: {Decision.WITHDRAW, Decision.SUPERSEDE},
            Decision.WITHDRAW: {Decision.RESTORE, Decision.SUPERSEDE},
            Decision.SUPERSEDE: set(),
        }[state]
        require(request.decision in allowed, "invalid_publication_transition")

    if request.decision == Decision.SUPERSEDE:
        require(request.supersedes_version is not None, "superseded_version_required")

    return Publication(
        request=request, decision_at=decision_at, usable_at=requested_usable
    )
