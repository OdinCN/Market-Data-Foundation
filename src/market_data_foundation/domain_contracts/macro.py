"""Named Macro release/revision/evidence contract for P3."""

from dataclasses import dataclass
from datetime import datetime

from market_data_foundation.canonical.model import (
    DatasetDefinition,
    Scope,
    instant,
    require,
)

BLS_CPI_HEADLINES = DatasetDefinition(
    identity="mdf.macro.bls.cpi.headlines",
    revision="p3-r1",
    domain_contract=(
        "Named CPI headline release/observation contract. The source operation "
        "name bls.cpi.headlines is a candidate semantic identity only; no "
        "current MDD runtime descriptor or active MDF binding is asserted."
    ),
    purpose=("revisioned release/evidence record with point-in-time availability"),
    source_qualified=True,
    key_semantics=(
        "series identity + report/reference period + provider revision/vintage"
    ),
    unit_semantics="series unit must be explicit in the eventual source mapping",
    temporal_semantics=(
        "reference/report/release/vintage time is distinct from MDF obtained, "
        "recorded, usable, and Publication decision time"
    ),
    revision_semantics=(
        "provider revision, acquisition, artifact, parser revision, and MDF "
        "semantic revision are independent axes"
    ),
    completeness_semantics=(
        "bounded to an exact named release/series contract; no generic macro universe"
    ),
    scope=Scope(),
)

BLS_PPI_FINAL_DEMAND = DatasetDefinition(
    identity="mdf.macro.bls.ppi.final-demand",
    revision="t01-r1",
    domain_contract=(
        "Named BLS Producer Price Index final-demand contract for the exact "
        "WPSFD4 seasonally adjusted and WPUFD4 not-seasonally-adjusted series."
    ),
    purpose=("revisioned final-demand release evidence and point-in-time reading"),
    source_qualified=True,
    key_semantics=(
        "series identity + monthly reference period + retrieval-bound revision state"
    ),
    unit_semantics=(
        "provider index values are index_point; provider calculations are percent"
    ),
    temporal_semantics=(
        "monthly reference, official release slot, obtained, frozen, decision, and "
        "usable times are distinct"
    ),
    revision_semantics=(
        "initial/interim/final PPI state, acquisition, raw identity, parser revision, "
        "and MDF semantic revision are independent; retrieval does not infer vintage"
    ),
    completeness_semantics=(
        "exact WPSFD4/WPUFD4 final-demand target and comparison rows for one release"
    ),
    scope=Scope(),
)


@dataclass(frozen=True)
class MacroSourceCandidate:
    operation: str
    binding_ready: bool
    missing_runtime_evidence: tuple[str, ...]
    evidence_requirement: str


BLS_CPI_SOURCE_CANDIDATE = MacroSourceCandidate(
    operation="bls.cpi.headlines",
    binding_ready=False,
    missing_runtime_evidence=(
        "current source operation descriptor",
        "exact source build/revision identity",
        "active material-retention contract",
    ),
    evidence_requirement=(
        "retained source evidence/artifact identity must be explicit before a "
        "binding claims replay/durable provider evidence"
    ),
)


@dataclass(frozen=True)
class MacroTimes:
    reference_or_report_at: datetime
    provider_release_or_vintage_at: datetime
    obtained_at: datetime
    recorded_at: datetime
    usable_at: datetime

    def __post_init__(self) -> None:
        for value in (
            self.reference_or_report_at,
            self.provider_release_or_vintage_at,
            self.obtained_at,
            self.recorded_at,
            self.usable_at,
        ):
            instant(value)
        require(self.recorded_at >= self.obtained_at, "recorded_before_obtained")
        require(
            self.usable_at >= self.recorded_at and self.usable_at >= self.obtained_at,
            "macro_usable_time_backdated",
        )


@dataclass(frozen=True)
class MacroRevisionAxes:
    series_id: str
    reference_period: str
    provider_revision: str | None
    acquisition_identity: str
    artifact_identity: str
    parser_revision: str
    semantic_revision: str

    def __post_init__(self) -> None:
        require(
            bool(self.series_id and self.reference_period),
            "macro_identity_incomplete",
        )
        require(
            bool(
                self.acquisition_identity
                and self.artifact_identity
                and self.parser_revision
                and self.semantic_revision
            ),
            "macro_revision_axes_incomplete",
        )

    @property
    def semantic_dedupe_key(self) -> tuple[str, str, str | None]:
        return self.series_id, self.reference_period, self.provider_revision


def require_parser_correction(
    before: MacroRevisionAxes, after: MacroRevisionAxes
) -> MacroRevisionAxes:
    require(
        before.series_id == after.series_id
        and before.reference_period == after.reference_period,
        "parser_correction_target_changed",
    )
    require(
        before.provider_revision == after.provider_revision,
        "parser_correction_cannot_change_provider_revision",
    )
    require(
        before.acquisition_identity == after.acquisition_identity
        and before.artifact_identity == after.artifact_identity,
        "parser_correction_requires_same_evidence",
    )
    require(
        before.parser_revision != after.parser_revision,
        "parser_correction_requires_new_parser_revision",
    )
    return after


def require_binding_ready(candidate: MacroSourceCandidate) -> MacroSourceCandidate:
    require(candidate.binding_ready, "macro_source_not_binding_ready")
    return candidate
