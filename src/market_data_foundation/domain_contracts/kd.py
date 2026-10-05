"""MDF-side candidate contract for the architecture-bounded KD B043 path."""

from dataclasses import dataclass

from market_data_foundation.canonical.model import (
    DatasetDefinition,
    Scope,
    require,
)

KD_B043_CURRENT_RANK = DatasetDefinition(
    identity="mdf.kd.sector-analysis.current-rank",
    revision="p3-r1",
    domain_contract=(
        "Inactive MDF candidate for KD sector-analysis.current_rank B043. "
        "Source basis is socket17000 cmd3007 / plateType7; completeness is "
        "bounded current-window only."
    ),
    purpose="source-qualified sampled current-state sector ranking",
    source_qualified=True,
    key_semantics=(
        "KD/Kaipanla sector identity within one bounded current-state sample"
    ),
    unit_semantics=(
        "strength/mainNetAmount/institutionIncrease retain KD/Kaipanla-qualified "
        "source semantics; no cross-provider universal meaning is asserted"
    ),
    temporal_semantics=(
        "current-state observed/received semantics only; MDF sampling forms "
        "sampled_current_state_history, not event-complete history"
    ),
    revision_semantics=(
        "real binding must pin fresh KD build, registry, operation-contract, "
        "projection, and source-variant identity"
    ),
    completeness_semantics=(
        "bounded_current_window; total/count does not prove a full universe/snapshot"
    ),
    scope=Scope(),
)


@dataclass(frozen=True)
class KDCurrentRankCandidate:
    operation: str
    source_variant_basis: str
    acquisition_mode: str
    completeness: str
    source_qualified_fields: tuple[str, ...]
    silent_fallback_allowed: bool
    binding_ready: bool
    requires_fresh_runtime_descriptor: bool


KD_B043 = KDCurrentRankCandidate(
    operation="sector-analysis.current_rank",
    source_variant_basis="socket17000:cmd3007:plateType7",
    acquisition_mode="current_state",
    completeness="bounded_current_window",
    source_qualified_fields=("strength", "mainNetAmount", "institutionIncrease"),
    silent_fallback_allowed=False,
    binding_ready=False,
    requires_fresh_runtime_descriptor=True,
)


@dataclass(frozen=True)
class KDRuntimeDescriptorIdentity:
    build_revision: str
    registry_revision: str
    operation_contract_revision: str
    projection_revision: str
    source_variant: str
    observed_from_current_runtime: bool


@dataclass(frozen=True)
class KDAdmissionDisposition:
    operation: str
    status: str
    reason: str
    global_p3_blocker: bool


KD_B044_DISPOSITION = KDAdmissionDisposition(
    operation="market-stock-ranking.current_a_share",
    status="NOT_ADMITTED_SELF_BLOCKED",
    reason=(
        "dynamic columns/pagination/freshness require B044-specific runtime evidence"
    ),
    global_p3_blocker=False,
)


def require_bounded_current_state(
    *, completeness: str, history_claim: str
) -> tuple[str, str]:
    require(
        completeness == "bounded_current_window",
        "kd_full_snapshot_not_admitted",
    )
    require(
        history_claim == "sampled_current_state_history",
        "kd_event_complete_history_not_admitted",
    )
    return completeness, history_claim


def require_runtime_binding_descriptor(
    candidate: KDCurrentRankCandidate,
    descriptor: KDRuntimeDescriptorIdentity | None,
) -> KDRuntimeDescriptorIdentity:
    require(candidate.requires_fresh_runtime_descriptor, "kd_descriptor_policy_invalid")
    require(descriptor is not None, "kd_runtime_descriptor_required")
    require(
        descriptor.observed_from_current_runtime,
        "kd_architecture_descriptor_not_runtime_evidence",
    )
    for value in (
        descriptor.build_revision,
        descriptor.registry_revision,
        descriptor.operation_contract_revision,
        descriptor.projection_revision,
        descriptor.source_variant,
    ):
        require(bool(value.strip()), "kd_runtime_descriptor_incomplete")
    require(
        descriptor.source_variant == candidate.source_variant_basis,
        "kd_source_variant_mismatch",
    )
    return descriptor


def require_no_silent_fallback(
    candidate: KDCurrentRankCandidate,
) -> KDCurrentRankCandidate:
    require(not candidate.silent_fallback_allowed, "kd_silent_fallback_prohibited")
    return candidate


def require_principal_scope_within(output_scope: Scope, source_scope: Scope) -> Scope:
    require(output_scope.within(source_scope), "scope_widening")
    return output_scope
