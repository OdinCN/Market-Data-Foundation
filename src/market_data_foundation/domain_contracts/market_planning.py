"""Frozen D1 job/shard/publication values and bounded, effect-free planning."""

from dataclasses import dataclass, fields
from datetime import date, timedelta
from enum import StrEnum

from market_data_foundation.canonical.model import Key, Kind, require
from market_data_foundation.domain_contracts.market_universe import (
    AssetClass,
    DateWindow,
    EvidenceMode,
    InstrumentIdentity,
    QualificationStatus,
    SourceMapping,
    UniverseRevision,
    canonical_bytes,
    content_id,
    frozen_tuple,
    text_fields,
)

ALGORITHM_REVISION = "mdf.market.plan.d1.v1"


@dataclass(frozen=True)
class DailyDefinition:
    category: AssetClass
    definition: Key
    schema_revision: str
    price_unit: str
    volume_unit: str
    amount_unit: str
    price_basis: str

    def __post_init__(self) -> None:
        require(isinstance(self.category, AssetClass), "invalid_asset_class")
        require(
            self.definition.kind == Kind.DEFINITION
            and self.definition.revision != "p3-r1",
            "new_daily_definition_required",
        )
        text_fields(self.schema_revision)
        if self.category in (AssetClass.A_SHARE, AssetClass.ETF):
            volume = "shares" if self.category == AssetClass.A_SHARE else "fund_shares"
            require(
                (self.price_unit, self.volume_unit, self.amount_unit, self.price_basis)
                == ("CNY", volume, "CNY", "unadjusted"),
                "stock_etf_units_or_basis_mismatch",
            )
        else:
            require(
                (self.price_unit, self.volume_unit, self.amount_unit)
                == ("index_points", "not_applicable", "not_applicable"),
                "index_units_not_admitted",
            )
            require(
                self.price_basis in ("price_index", "total_return_index"),
                "explicit_index_type_required",
            )


DAILY_DEFINITIONS = tuple(
    DailyDefinition(
        category,
        Key(Kind.DEFINITION, f"mdf.market.bars.daily.{category.value}", "d1-r1"),
        "daily.d1.v1",
        "CNY" if category in (AssetClass.A_SHARE, AssetClass.ETF) else "index_points",
        {AssetClass.A_SHARE: "shares", AssetClass.ETF: "fund_shares"}.get(
            category, "not_applicable"
        ),
        "CNY" if category in (AssetClass.A_SHARE, AssetClass.ETF) else "not_applicable",
        "unadjusted"
        if category in (AssetClass.A_SHARE, AssetClass.ETF)
        else "price_index",
    )
    for category in AssetClass
)


@dataclass(frozen=True)
class PagePlan:
    first_offset: int
    page_size: int
    page_count: int
    order: str
    consistency_rule: str
    termination_evidence: str

    def __post_init__(self) -> None:
        require(
            type(self.first_offset) is int and self.first_offset >= 0,
            "invalid_page_offset",
        )
        require(
            type(self.page_size) is int
            and type(self.page_count) is int
            and self.page_size > 0
            and self.page_count > 0,
            "invalid_page_bounds",
        )
        require(
            self.order in ("ascending", "descending"), "explicit_page_order_required"
        )
        text_fields(self.consistency_rule, self.termination_evidence)

    @property
    def end_offset_exclusive(self) -> int:
        return self.first_offset + self.page_size * self.page_count


@dataclass(frozen=True)
class SourceRoute:
    daily: DailyDefinition
    source: Key
    binding: Key
    mapping_revision: str
    implementation_identity: str
    endpoint_contract: str
    execution_inputs: tuple[tuple[str, str], ...]
    request_span_days: int
    pages: PagePlan
    qualification: QualificationStatus = QualificationStatus.UNKNOWN
    qualification_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require(
            self.source.kind == Kind.SOURCE and self.binding.kind == Kind.BINDING,
            "source_binding_required",
        )
        text_fields(
            self.mapping_revision, self.implementation_identity, self.endpoint_contract
        )
        require(
            isinstance(self.qualification, QualificationStatus), "invalid_qualification"
        )
        frozen_tuple(self.qualification_evidence)
        text_fields(*self.qualification_evidence)
        require(
            self.qualification == QualificationStatus.UNKNOWN
            or bool(self.qualification_evidence),
            "source_qualification_evidence_required",
        )
        require(
            type(self.request_span_days) is int and self.request_span_days > 0,
            "invalid_request_span",
        )
        frozen_tuple(self.execution_inputs)
        require(
            all(
                type(pair) is tuple and len(pair) == 2 for pair in self.execution_inputs
            ),
            "immutable_execution_inputs_required",
        )
        require(
            len(dict(self.execution_inputs)) == len(self.execution_inputs),
            "duplicate_execution_input",
        )
        for key, value in self.execution_inputs:
            text_fields(key, value)
        object.__setattr__(
            self, "execution_inputs", tuple(sorted(self.execution_inputs))
        )


@dataclass(frozen=True)
class PlanningBudget:
    max_members: int
    max_days: int
    max_grid_points: int
    max_shards: int
    max_publication_units: int
    max_requests: int
    max_rows: int
    max_response_bytes: int
    max_material_bytes: int
    max_versions: int
    max_publications: int
    max_connections: int
    max_probes: int
    max_concurrency: int
    requests_per_second: int
    timeout_seconds: int
    wall_seconds: int
    recovery_requests: int
    automatic_retries: int = 0
    sdk_redials: int = 0

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            require(type(value) is int and value >= 0, "invalid_budget")
        require(
            self.max_concurrency == 1
            and self.automatic_retries == self.sdk_redials == 0,
            "d1_single_host_zero_retry_required",
        )
        require(
            self.requests_per_second > 0
            and self.timeout_seconds > 0
            and self.wall_seconds > 0
            and self.max_response_bytes > 0,
            "positive_execution_limits_required",
        )


class JobMode(StrEnum):
    INITIALIZE = "initialize"
    PERIODIC = "periodic"
    BACKFILL = "backfill"


@dataclass(frozen=True)
class RecoveryOrigin:
    job_identity: str
    shard_identities: tuple[str, ...]
    manifest_revision: str
    gap_digest: str
    publication_occurrence_rule: str

    def __post_init__(self) -> None:
        text_fields(
            self.job_identity,
            self.manifest_revision,
            self.gap_digest,
            self.publication_occurrence_rule,
        )
        frozen_tuple(self.shard_identities)
        text_fields(*self.shard_identities)
        require(
            bool(self.shard_identities)
            and len(set(self.shard_identities)) == len(self.shard_identities),
            "original_shards_required",
        )


@dataclass(frozen=True)
class JobSpec:
    universe: UniverseRevision
    window: DateWindow
    completed_through: date
    routes: tuple[SourceRoute, ...]
    storage_policy_revision: str
    effect_domain: str
    mode: JobMode
    occurrence: str
    budget: PlanningBudget
    origin: RecoveryOrigin | None = None
    dataset_type: str = "bars.daily"
    algorithm_revision: str = ALGORITHM_REVISION

    def __post_init__(self) -> None:
        require(
            self.dataset_type == "bars.daily"
            and self.algorithm_revision == ALGORITHM_REVISION,
            "unsupported_planning_contract",
        )
        require(isinstance(self.mode, JobMode), "invalid_job_mode")
        require(
            (self.mode == JobMode.BACKFILL) == (self.origin is not None),
            "backfill_origin_required_or_unexpected",
        )
        text_fields(self.storage_policy_revision, self.effect_domain, self.occurrence)
        require(type(self.completed_through) is date, "local_date_required")
        require(
            self.window.end_exclusive - timedelta(days=1) <= self.completed_through,
            "incomplete_trading_date",
        )
        require(
            self.universe.effective.encloses(self.window)
            and self.universe.history_window.encloses(self.window),
            "universe_window_not_covered",
        )
        frozen_tuple(self.routes)
        if self.universe.provenance.mode != EvidenceMode.SYNTHETIC:
            require(
                all(
                    route.qualification == QualificationStatus.UNKNOWN
                    for route in self.routes
                ),
                "real_source_qualification_remains_unknown",
            )
        categories = {
            member.category
            for member in self.universe.members
            if any(part.overlaps(self.window) for part in member.membership)
        }
        require(
            len(self.routes) == len(categories)
            and {route.daily.category for route in self.routes} == categories,
            "exact_category_routes_required",
        )
        require(
            len({route.daily.definition for route in self.routes}) == len(self.routes),
            "category_definition_collision",
        )
        object.__setattr__(
            self,
            "routes",
            tuple(sorted(self.routes, key=lambda item: item.daily.category)),
        )

    @property
    def identity(self) -> str:
        return content_id(self)

    def require_execution(self) -> None:
        require(False, "d1_offline_only_execution_not_admitted")


@dataclass(frozen=True, order=True)
class GridPoint:
    instrument: InstrumentIdentity
    day: date

    def __post_init__(self) -> None:
        require(type(self.day) is date, "local_date_required")


@dataclass(frozen=True)
class LogicalReadUnit:
    definition: Key
    point: GridPoint

    def __post_init__(self) -> None:
        require(self.definition.kind == Kind.DEFINITION, "definition_required")

    @property
    def identity(self) -> str:
        return content_id(self)


@dataclass(frozen=True)
class PublicationUnit:
    definition: Key
    universe_revision: str
    calendar_revision: str
    instrument: InstrumentIdentity
    timezone: str
    window: DateWindow

    def __post_init__(self) -> None:
        require(self.definition.kind == Kind.DEFINITION, "definition_required")
        text_fields(self.universe_revision, self.calendar_revision, self.timezone)
        last = self.window.end_exclusive - timedelta(days=1)
        require(
            (self.window.start.year, self.window.start.month) == (last.year, last.month)
            and (self.window.end_exclusive - self.window.start).days <= 31,
            "publication_unit_must_be_month_bounded",
        )

    @property
    def identity(self) -> str:
        return content_id(self)

    def expand(self) -> tuple[LogicalReadUnit, ...]:
        return tuple(
            LogicalReadUnit(self.definition, GridPoint(self.instrument, day))
            for day in dates(self.window)
        )


@dataclass(frozen=True)
class Shard:
    job_identity: str
    algorithm_revision: str
    route: SourceRoute
    mapping: SourceMapping
    instrument: InstrumentIdentity
    window: DateWindow
    target_points: tuple[GridPoint, ...]

    def __post_init__(self) -> None:
        text_fields(self.job_identity)
        require(
            self.algorithm_revision == ALGORITHM_REVISION,
            "unsupported_planning_contract",
        )
        frozen_tuple(self.target_points)
        require(
            bool(self.target_points)
            and len(set(self.target_points)) == len(self.target_points),
            "unique_shard_targets_required",
        )
        require(
            all(
                point.instrument == self.instrument and self.window.contains(point.day)
                for point in self.target_points
            ),
            "shard_target_outside_scope",
        )
        require(
            self.mapping.instrument == self.instrument
            and self.mapping.source == self.route.source
            and self.mapping.binding == self.route.binding
            and self.mapping.revision == self.route.mapping_revision
            and self.mapping.validity.encloses(self.window),
            "shard_mapping_mismatch",
        )
        require(
            (self.window.end_exclusive - self.window.start).days
            <= self.route.request_span_days,
            "source_request_span_exceeded",
        )
        object.__setattr__(self, "target_points", tuple(sorted(self.target_points)))

    @property
    def request_superset(self) -> tuple[GridPoint, ...]:
        return tuple(GridPoint(self.instrument, day) for day in dates(self.window))

    @property
    def identity(self) -> str:
        return content_id(self)


@dataclass(frozen=True)
class Plan:
    spec: JobSpec
    points: tuple[GridPoint, ...]
    units: tuple[PublicationUnit, ...]
    shards: tuple[Shard, ...]
    request_count: int
    row_capacity: int
    material_byte_capacity: int

    @property
    def connection_capacity(self) -> int:
        return len(self.shards)

    @property
    def probe_capacity(self) -> int:
        return len(self.shards)

    @property
    def version_capacity(self) -> int:
        return len(self.units)

    @property
    def publication_capacity(self) -> int:
        return len(self.units)

    @property
    def identity(self) -> str:
        return content_id(self)


def dates(window: DateWindow) -> tuple[date, ...]:
    # Public callers must bound large windows before using this helper.
    return tuple(
        window.start + timedelta(days=offset)
        for offset in range((window.end_exclusive - window.start).days)
    )


def plan_job(spec: JobSpec) -> Plan:
    """No I/O. Bound the rectangular denominator *before* date expansion."""
    require(spec.mode != JobMode.BACKFILL, "backfill_requires_confirmed_gap_candidates")
    budget = spec.budget
    members = tuple(
        member
        for member in spec.universe.members
        if any(part.overlaps(spec.window) for part in member.membership)
    )
    span = (spec.window.end_exclusive - spec.window.start).days
    require(
        0 < len(members) <= budget.max_members
        and span <= budget.max_days
        and len(members) * span <= budget.max_grid_points,
        "grid_budget_exceeded",
    )
    days = dates(spec.window)
    calendars = {item.market: item for item in spec.universe.calendars}
    routes = {route.daily.category: route for route in spec.routes}
    units: list[PublicationUnit] = []
    shards: list[Shard] = []
    points: list[GridPoint] = []
    job_identity = spec.identity
    universe_revision = spec.universe.revision
    for member in members:
        calendar = calendars.get(member.market)
        require(calendar is not None, "market_calendar_required")
        require(calendar.window.encloses(spec.window), "calendar_window_not_covered")
        route = routes[member.category]
        points.extend(GridPoint(member.instrument, day) for day in days)
        start = spec.window.start
        while start < spec.window.end_exclusive:
            next_month = (
                date(start.year + (start.month == 12), start.month % 12 + 1, 1)
                if (start.year, start.month) != (9999, 12)
                else spec.window.end_exclusive
            )
            end = min(
                next_month,
                start
                + timedelta(days=min(31, (spec.window.end_exclusive - start).days)),
                spec.window.end_exclusive,
            )
            require(
                len(units) < budget.max_publication_units,
                "publication_unit_budget_exceeded",
            )
            units.append(
                PublicationUnit(
                    route.daily.definition,
                    universe_revision,
                    calendar.revision,
                    member.instrument,
                    calendar.timezone,
                    DateWindow(start, end),
                )
            )
            start = end
        # Single-instrument requests only. Closed/nonmember dates remain in P;
        # request ranges are split at membership and explicit code changes.
        request_days = tuple(
            day
            for day in days
            if member.includes(day)
            and member.lifecycle.state(day)
            not in ("NOT_YET_LISTED", "DELISTED_OR_TERMINATED")
        )
        groups: list[tuple[SourceMapping, list[date]]] = []
        for day in request_days:
            mapping = spec.universe.mapping_for(
                member.instrument,
                day,
                route.source,
                route.binding,
                route.mapping_revision,
            )
            if (
                groups
                and groups[-1][0] == mapping
                and (day - groups[-1][1][-1]).days == 1
                and len(groups[-1][1]) < route.request_span_days
            ):
                groups[-1][1].append(day)
            else:
                require(
                    len(shards) + len(groups) < budget.max_shards,
                    "shard_budget_exceeded",
                )
                groups.append((mapping, [day]))
        for mapping, group in groups:
            shards.append(
                Shard(
                    job_identity,
                    spec.algorithm_revision,
                    route,
                    mapping,
                    member.instrument,
                    DateWindow(group[0], group[-1] + timedelta(days=1)),
                    tuple(GridPoint(member.instrument, day) for day in group),
                )
            )
    requests = sum(shard.route.pages.page_count for shard in shards)
    rows = sum(
        shard.route.pages.page_count * shard.route.pages.page_size for shard in shards
    )
    material = requests * budget.max_response_bytes
    require(
        requests <= budget.max_requests
        and rows <= budget.max_rows
        and material <= budget.max_material_bytes,
        "request_material_or_row_budget_exceeded",
    )
    require(
        len(units) <= budget.max_versions and len(units) <= budget.max_publications,
        "version_publication_budget_exceeded",
    )
    require(
        len(shards) <= budget.max_connections and len(shards) <= budget.max_probes,
        "connection_probe_budget_exceeded",
    )
    return Plan(
        spec,
        tuple(sorted(points)),
        tuple(sorted(units, key=canonical_bytes)),
        tuple(sorted(shards, key=canonical_bytes)),
        requests,
        rows,
        material,
    )
