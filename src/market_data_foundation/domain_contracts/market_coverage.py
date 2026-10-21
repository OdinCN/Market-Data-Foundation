"""Conserved coverage and recovery *candidates*, never collection or repair."""

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from market_data_foundation.canonical.model import Key, Kind, require
from market_data_foundation.domain_contracts.market_planning import (
    GridPoint,
    Plan,
    PlanningBudget,
    SourceRoute,
)
from market_data_foundation.domain_contracts.market_universe import (
    DateWindow,
    MembershipMode,
    QualificationStatus,
    SourceMapping,
    canonical_bytes,
    content_id,
    frozen_tuple,
    text_fields,
)


class CoverageClass(StrEnum):
    OBSERVED_VALID = "OBSERVED_VALID"
    MARKET_CLOSED = "MARKET_CLOSED"
    NOT_YET_LISTED = "NOT_YET_LISTED"
    DELISTED_OR_TERMINATED = "DELISTED_OR_TERMINATED"
    NO_TRADE_CONFIRMED = "NO_TRADE_CONFIRMED"
    COLLECTION_FAILED = "COLLECTION_FAILED"
    MISSING_EXPECTED = "MISSING_EXPECTED"
    QUALITY_REJECTED = "QUALITY_REJECTED"
    UNKNOWN = "UNKNOWN"


class CoverageStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PointEvidence:
    point: GridPoint
    classification: CoverageClass
    references: tuple[str, ...]
    original_shards: tuple[str, ...] = ()
    attempt_identities: tuple[str, ...] = ()
    request_identities: tuple[str, ...] = ()
    material: Key | None = None
    source_row_locator: str | None = None
    no_trade_contract: str | None = None
    effects_confirmed_absent: bool = False
    retryable_confirmed: bool = False

    def __post_init__(self) -> None:
        require(
            isinstance(self.classification, CoverageClass), "invalid_coverage_class"
        )
        for items in (
            self.references,
            self.original_shards,
            self.attempt_identities,
            self.request_identities,
        ):
            frozen_tuple(items)
            text_fields(*items)
            require(len(set(items)) == len(items), "duplicate_evidence_reference")
        require(bool(self.references), "coverage_evidence_required")
        require(
            type(self.effects_confirmed_absent) is bool
            and type(self.retryable_confirmed) is bool,
            "invalid_effect_confirmation",
        )
        require(
            self.material is None or self.material.kind == Kind.MATERIAL,
            "material_reference_required",
        )
        if self.classification == CoverageClass.OBSERVED_VALID:
            require(
                self.material is not None and bool(self.source_row_locator),
                "validated_row_and_material_required",
            )
        if self.classification == CoverageClass.NO_TRADE_CONFIRMED:
            require(
                bool(self.no_trade_contract),
                "explicit_no_trade_source_contract_required",
            )
        if self.classification in (
            CoverageClass.COLLECTION_FAILED,
            CoverageClass.MISSING_EXPECTED,
        ):
            require(
                self.effects_confirmed_absent and bool(self.original_shards),
                "unresolved_effect_is_unknown",
            )
        if self.classification == CoverageClass.UNKNOWN:
            require(not self.retryable_confirmed, "unknown_cannot_be_retried")


@dataclass(frozen=True)
class CoverageEntry:
    point: GridPoint
    classification: CoverageClass
    evidence: PointEvidence | None


@dataclass(frozen=True)
class CoverageAxes:
    member_enumeration: CoverageStatus
    lifecycle_calendar: CoverageStatus
    bar_obligations: CoverageStatus
    material_readback: CoverageStatus = CoverageStatus.UNKNOWN
    formal_publication: CoverageStatus = CoverageStatus.UNKNOWN


@dataclass(frozen=True)
class CoverageManifest:
    plan: Plan
    entries: tuple[CoverageEntry, ...]
    extras: tuple[PointEvidence, ...]
    duplicates: tuple[PointEvidence, ...]
    axes: CoverageAxes
    predecessor_revision: str | None

    def __post_init__(self) -> None:
        for items in (self.entries, self.extras, self.duplicates):
            frozen_tuple(items)
        require(
            tuple(entry.point for entry in self.entries) == self.plan.points,
            "coverage_partition_mismatch",
        )
        require(
            all(
                isinstance(entry.classification, CoverageClass)
                for entry in self.entries
            ),
            "invalid_coverage_class",
        )
        require(
            self.axes.material_readback
            == self.axes.formal_publication
            == CoverageStatus.UNKNOWN,
            "d1_publication_readback_remain_unknown",
        )

    @property
    def revision(self) -> str:
        return content_id(self)

    @property
    def denominator(self) -> int:
        return len(self.plan.points)

    @property
    def counts(self) -> tuple[tuple[CoverageClass, int], ...]:
        return tuple(
            (kind, sum(entry.classification == kind for entry in self.entries))
            for kind in CoverageClass
        )

    @property
    def global_status(self) -> CoverageStatus:
        # D1 has no independent material readback or formal publication evidence.
        return CoverageStatus.UNKNOWN


def lifecycle_calendar_state(plan: Plan, point: GridPoint) -> CoverageClass | None:
    """Structural explanation, independent of daily-source qualification."""
    universe = plan.spec.universe
    member = next(
        member for member in universe.members if member.instrument == point.instrument
    )
    lifecycle = member.lifecycle.state(point.day)
    if lifecycle in ("NOT_YET_LISTED", "DELISTED_OR_TERMINATED"):
        return CoverageClass(lifecycle)
    # A closed calendar day cannot explain an unknown lifecycle or membership gap.
    if lifecycle != "ACTIVE" or not member.includes(point.day):
        return CoverageClass.UNKNOWN
    calendar = next(item for item in universe.calendars if item.market == member.market)
    day = next((item for item in calendar.days if item.day == point.day), None)
    if calendar.qualification != QualificationStatus.QUALIFIED or day is None:
        return CoverageClass.UNKNOWN
    if not day.is_open:
        return CoverageClass.MARKET_CLOSED
    return None


def expected_state(plan: Plan, point: GridPoint) -> CoverageClass | None:
    """None is a qualified open-session obligation; absence alone stays UNKNOWN."""
    structural = lifecycle_calendar_state(plan, point)
    if structural is not None:
        return structural
    universe = plan.spec.universe
    member = next(
        member for member in universe.members if member.instrument == point.instrument
    )
    gates = dict(universe.qualifications)[member.category]
    route = next(
        route for route in plan.spec.routes if route.daily.category == member.category
    )
    if any(
        value != QualificationStatus.QUALIFIED
        for value in (
            route.qualification,
            gates.daily_bars,
            gates.units,
            gates.identity_mapping,
            gates.calendar,
        )
    ):
        return CoverageClass.UNKNOWN
    return None


def classify_coverage(
    plan: Plan,
    evidence: tuple[PointEvidence, ...] = (),
    *,
    predecessor: CoverageManifest | None = None,
) -> CoverageManifest:
    frozen_tuple(evidence)
    if predecessor is not None:
        require(
            predecessor.plan.identity == plan.identity,
            "coverage_predecessor_scope_mismatch",
        )
    points = set(plan.points)
    grouped: dict[GridPoint, list[PointEvidence]] = {}
    extras: list[PointEvidence] = []
    for item in evidence:
        if item.point not in points:
            extras.append(item)
        else:
            grouped.setdefault(item.point, []).append(item)
    entries: list[CoverageEntry] = []
    duplicates: list[PointEvidence] = []
    shards = {shard.identity: shard for shard in plan.shards}
    for point in plan.points:
        items = grouped.get(point, [])
        base = expected_state(plan, point)
        if len(items) > 1:
            # Keep all conflicting rows; do not choose one to balance counts.
            duplicates.extend(items)
            entries.append(CoverageEntry(point, CoverageClass.QUALITY_REJECTED, None))
            continue
        if not items:
            entries.append(CoverageEntry(point, base or CoverageClass.UNKNOWN, None))
            continue
        item = items[0]
        require(
            all(
                identity in shards and point in shards[identity].target_points
                for identity in item.original_shards
            ),
            "coverage_original_shard_mismatch",
        )
        if base is not None and base != CoverageClass.UNKNOWN:
            require(
                item.classification == base,
                "coverage_contradicts_calendar_or_lifecycle",
            )
        elif base == CoverageClass.UNKNOWN:
            require(
                item.classification
                in (CoverageClass.UNKNOWN, CoverageClass.QUALITY_REJECTED),
                "coverage_obligation_not_qualified",
            )
        else:
            require(
                item.classification
                not in (
                    CoverageClass.MARKET_CLOSED,
                    CoverageClass.NOT_YET_LISTED,
                    CoverageClass.DELISTED_OR_TERMINATED,
                ),
                "coverage_explanation_not_evidenced",
            )
        entries.append(CoverageEntry(point, item.classification, item))
    classified = {entry.classification for entry in entries}
    status = CoverageStatus.COMPLETE
    if CoverageClass.UNKNOWN in classified:
        status = CoverageStatus.UNKNOWN
    elif (
        extras
        or duplicates
        or classified
        & {
            CoverageClass.COLLECTION_FAILED,
            CoverageClass.MISSING_EXPECTED,
            CoverageClass.QUALITY_REJECTED,
        }
    ):
        status = CoverageStatus.PARTIAL
    universe = plan.spec.universe
    categories = {
        member.category
        for member in universe.members
        if any(window.overlaps(plan.spec.window) for window in member.membership)
    }
    gates = dict(universe.qualifications)
    member_status = (
        CoverageStatus.COMPLETE
        if universe.enumeration_evidence
        and all(
            gates[category].current_members == QualificationStatus.QUALIFIED
            and (
                universe.mode == MembershipMode.SNAPSHOT
                or gates[category].historical_members == QualificationStatus.QUALIFIED
            )
            for category in categories
        )
        else CoverageStatus.UNKNOWN
    )
    lifecycle_status = (
        CoverageStatus.COMPLETE
        if all(
            lifecycle_calendar_state(plan, point) != CoverageClass.UNKNOWN
            for point in plan.points
        )
        else CoverageStatus.UNKNOWN
    )
    return CoverageManifest(
        plan,
        tuple(entries),
        tuple(sorted(extras, key=canonical_bytes)),
        tuple(sorted(duplicates, key=canonical_bytes)),
        CoverageAxes(member_status, lifecycle_status, status),
        predecessor.revision if predecessor else None,
    )


@dataclass(frozen=True)
class GapCandidate:
    original_job: str
    original_shards: tuple[str, ...]
    manifest_revision: str
    route: SourceRoute
    mapping: SourceMapping
    window: DateWindow
    target_points: tuple[GridPoint, ...]
    request_superset: tuple[GridPoint, ...]
    budget: PlanningBudget
    publication_occurrence_rule: str = "new_occurrence_preserve_original_history"

    @property
    def identity(self) -> str:
        return content_id(self)


@dataclass(frozen=True)
class RecoveryCandidates:
    backfill: tuple[GapCandidate, ...]
    reprocess_first: tuple[GridPoint, ...]
    investigate_original: tuple[GridPoint, ...]


def plan_recovery(
    manifest: CoverageManifest, budget: PlanningBudget
) -> RecoveryCandidates:
    """Only confirmed retryable gaps; no success, no-trade, or UNKNOWN retry."""
    require(
        not manifest.extras and not manifest.duplicates,
        "coverage_anomalies_require_review",
    )
    shards = {shard.identity: shard for shard in manifest.plan.shards}
    groups: list[tuple[SourceRoute, SourceMapping, list[CoverageEntry]]] = []
    reprocess: list[GridPoint] = []
    investigate: list[GridPoint] = []
    for entry in manifest.entries:
        item = entry.evidence
        if entry.classification == CoverageClass.UNKNOWN:
            investigate.append(entry.point)
        elif entry.classification == CoverageClass.QUALITY_REJECTED or (
            item
            and item.material is not None
            and entry.classification
            in (CoverageClass.COLLECTION_FAILED, CoverageClass.MISSING_EXPECTED)
        ):
            reprocess.append(entry.point)
        elif (
            entry.classification
            in (CoverageClass.COLLECTION_FAILED, CoverageClass.MISSING_EXPECTED)
            and item
            and item.retryable_confirmed
        ):
            original = shards[item.original_shards[0]]
            if (
                groups
                and groups[-1][0] == original.route
                and groups[-1][1] == original.mapping
                and groups[-1][2][-1].point.instrument == entry.point.instrument
                and groups[-1][2][-1].point.day + timedelta(days=1) == entry.point.day
                and len(groups[-1][2]) < original.route.request_span_days
            ):
                groups[-1][2].append(entry)
            else:
                require(
                    len(groups) < budget.max_shards, "recovery_shard_budget_exceeded"
                )
                groups.append((original.route, original.mapping, [entry]))
    gap_points = sum(len(group) for _, _, group in groups)
    require(
        gap_points <= budget.max_grid_points
        and gap_points <= budget.max_rows
        and len({group[0].point.instrument for _, _, group in groups})
        <= budget.max_members,
        "recovery_grid_budget_exceeded",
    )
    requests = sum(route.pages.page_count for route, _, _ in groups)
    rows = sum(route.pages.page_count * route.pages.page_size for route, _, _ in groups)
    targets = {entry.point for _, _, group in groups for entry in group}
    units = sum(
        any(read.point in targets for read in unit.expand())
        for unit in manifest.plan.units
    )
    require(
        requests <= budget.recovery_requests
        and requests <= budget.max_requests
        and rows <= budget.max_rows
        and requests * budget.max_response_bytes <= budget.max_material_bytes,
        "recovery_request_budget_exceeded",
    )
    require(
        len(groups) <= budget.max_connections
        and len(groups) <= budget.max_probes
        and units <= budget.max_publication_units
        and units <= budget.max_versions
        and units <= budget.max_publications,
        "recovery_connection_or_publication_budget_exceeded",
    )
    candidates: list[GapCandidate] = []
    manifest_revision = manifest.revision
    for route, mapping, group in groups:
        points = tuple(entry.point for entry in group)
        window = DateWindow(points[0].day, points[-1].day + timedelta(days=1))
        require(
            (window.end_exclusive - window.start).days <= budget.max_days,
            "recovery_day_budget_exceeded",
        )
        original_shards = tuple(
            sorted(
                {
                    identity
                    for entry in group
                    for identity in entry.evidence.original_shards
                }
            )
        )
        candidates.append(
            GapCandidate(
                manifest.plan.spec.identity,
                original_shards,
                manifest_revision,
                route,
                mapping,
                window,
                points,
                points,
                budget,
            )
        )
    return RecoveryCandidates(tuple(candidates), tuple(reprocess), tuple(investigate))
