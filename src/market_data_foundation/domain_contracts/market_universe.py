"""D1 immutable, local-date market content. No source or storage effects."""

import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from market_data_foundation.canonical.model import Key, Kind, digest, instant, require
from market_data_foundation.qualification.contracts import Capability


def canonical_bytes(value: object) -> bytes:
    """D1 v1: field names, ordered arrays, ISO dates, UTC instants, no floats."""

    def encode(item: object) -> object:
        if isinstance(item, datetime):
            instant(item)
            return item.astimezone(UTC).isoformat()
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, StrEnum):
            return item.value
        if is_dataclass(item) and not isinstance(item, type):
            return {
                field.name: encode(getattr(item, field.name)) for field in fields(item)
            }
        if isinstance(item, tuple):
            return [encode(part) for part in item]
        require(item is None or type(item) in (str, int, bool), "noncanonical_content")
        return item

    return json.dumps(
        encode(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def content_id(value: object) -> str:
    return digest(canonical_bytes(value))


def text_fields(*values: str) -> None:
    require(
        all(isinstance(value, str) and bool(value.strip()) for value in values),
        "empty_contract_field",
    )


def frozen_tuple(value: tuple) -> None:
    require(type(value) is tuple, "immutable_tuple_required")


@dataclass(frozen=True, order=True)
class DateWindow:
    start: date
    end_exclusive: date

    def __post_init__(self) -> None:
        require(
            type(self.start) is date and type(self.end_exclusive) is date,
            "local_date_required",
        )
        require(self.start < self.end_exclusive, "empty_or_reversed_window")

    def contains(self, day: date) -> bool:
        return self.start <= day < self.end_exclusive

    def encloses(self, other: DateWindow) -> bool:
        return self.start <= other.start and other.end_exclusive <= self.end_exclusive

    def overlaps(self, other: DateWindow) -> bool:
        return self.start < other.end_exclusive and other.start < self.end_exclusive


class AssetClass(StrEnum):
    A_SHARE = "a_share"
    ETF = "etf"
    INDEX = "index"
    SECTOR_INDEX = "sector_index"


class QualificationStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    QUALIFIED = "QUALIFIED"


class EvidenceMode(StrEnum):
    SYNTHETIC = "synthetic"
    RETAINED = "retained"
    LIVE = "live"


class MembershipMode(StrEnum):
    SNAPSHOT = "snapshot"
    HISTORICAL = "historical"


@dataclass(frozen=True)
class Qualification:
    current_members: QualificationStatus = QualificationStatus.UNKNOWN
    historical_members: QualificationStatus = QualificationStatus.UNKNOWN
    identity_mapping: QualificationStatus = QualificationStatus.UNKNOWN
    calendar: QualificationStatus = QualificationStatus.UNKNOWN
    daily_bars: QualificationStatus = QualificationStatus.UNKNOWN
    units: QualificationStatus = QualificationStatus.UNKNOWN
    usage: QualificationStatus = QualificationStatus.UNKNOWN
    retention: QualificationStatus = QualificationStatus.UNKNOWN

    def __post_init__(self) -> None:
        require(
            all(
                isinstance(getattr(self, field.name), QualificationStatus)
                for field in fields(self)
            ),
            "invalid_qualification",
        )


# Each category has its own gates; no evidence inheritance across categories.
REAL_QUALIFICATIONS = tuple((category, Qualification()) for category in AssetClass)


@dataclass(frozen=True)
class Provenance:
    capability: Capability
    source: Key
    binding: Key
    mapping_revision: str
    materials: tuple[Key, ...]
    obtained_at: datetime
    source_published_at: datetime | None
    mode: EvidenceMode

    def __post_init__(self) -> None:
        require(
            isinstance(self.capability, Capability)
            and isinstance(self.mode, EvidenceMode),
            "invalid_provenance",
        )
        require(
            self.source.kind == Kind.SOURCE and self.binding.kind == Kind.BINDING,
            "source_binding_required",
        )
        text_fields(self.mapping_revision)
        frozen_tuple(self.materials)
        require(
            bool(self.materials)
            and all(key.kind == Kind.MATERIAL for key in self.materials),
            "material_evidence_required",
        )
        instant(self.obtained_at)
        if self.source_published_at is not None:
            instant(self.source_published_at)
            require(
                self.source_published_at <= self.obtained_at,
                "source_publication_after_obtained",
            )


@dataclass(frozen=True, order=True)
class InstrumentIdentity:
    namespace: str
    instrument_id: str

    def __post_init__(self) -> None:
        text_fields(self.namespace, self.instrument_id)


@dataclass(frozen=True)
class Lifecycle:
    """Termination is the first non-tradable date; absence stays unknown."""

    known_window: DateWindow | None
    first_trade: date | None
    terminated_on: date | None
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        frozen_tuple(self.evidence)
        require(
            all(
                type(day) is date
                for day in (self.first_trade, self.terminated_on)
                if day is not None
            ),
            "local_date_required",
        )
        require(
            self.first_trade is None
            or self.terminated_on is None
            or self.first_trade < self.terminated_on,
            "invalid_lifecycle",
        )
        require(
            (
                self.known_window is None
                and self.first_trade is None
                and self.terminated_on is None
            )
            or bool(self.evidence),
            "lifecycle_evidence_required",
        )
        text_fields(*self.evidence)

    def state(self, day: date) -> str:
        if self.known_window is None or not self.known_window.contains(day):
            return "UNKNOWN"
        if self.first_trade is not None and day < self.first_trade:
            return "NOT_YET_LISTED"
        if self.terminated_on is not None and day >= self.terminated_on:
            return "DELISTED_OR_TERMINATED"
        return "ACTIVE" if self.first_trade is not None else "UNKNOWN"


@dataclass(frozen=True)
class UniverseMember:
    instrument: InstrumentIdentity
    category: AssetClass
    market: str
    original_code: str
    code_namespace: str
    membership: tuple[DateWindow, ...]
    lifecycle: Lifecycle

    def __post_init__(self) -> None:
        require(isinstance(self.category, AssetClass), "invalid_asset_class")
        text_fields(self.market, self.original_code, self.code_namespace)
        frozen_tuple(self.membership)
        require(bool(self.membership), "membership_intervals_required")
        ordered = tuple(sorted(self.membership))
        require(
            all(
                left.end_exclusive <= right.start
                for left, right in zip(ordered, ordered[1:])
            ),
            "overlapping_membership",
        )
        object.__setattr__(self, "membership", ordered)

    def includes(self, day: date) -> bool:
        return any(window.contains(day) for window in self.membership)


@dataclass(frozen=True)
class SourceMapping:
    instrument: InstrumentIdentity
    source: Key
    binding: Key
    revision: str
    request_namespace: str
    request_code: str
    validity: DateWindow
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        require(
            self.source.kind == Kind.SOURCE and self.binding.kind == Kind.BINDING,
            "source_binding_required",
        )
        text_fields(self.revision, self.request_namespace, self.request_code)
        frozen_tuple(self.evidence)
        require(bool(self.evidence), "mapping_evidence_required")
        text_fields(*self.evidence)


@dataclass(frozen=True)
class CalendarDay:
    day: date
    is_open: bool
    arrangement: str
    evidence: str

    def __post_init__(self) -> None:
        require(
            type(self.day) is date and type(self.is_open) is bool,
            "invalid_calendar_day",
        )
        text_fields(self.arrangement, self.evidence)


@dataclass(frozen=True)
class CalendarRevision:
    calendar_id: str
    schema_revision: str
    market: str
    timezone: str
    window: DateWindow
    known_at: datetime
    days: tuple[CalendarDay, ...]
    provenance: Provenance
    qualification: QualificationStatus = QualificationStatus.UNKNOWN

    def __post_init__(self) -> None:
        text_fields(self.calendar_id, self.schema_revision, self.market, self.timezone)
        try:
            ZoneInfo(self.timezone)
        except ValueError, ZoneInfoNotFoundError:
            require(False, "invalid_exchange_timezone")
        instant(self.known_at)
        require(self.known_at >= self.provenance.obtained_at, "known_at_backdated")
        require(
            self.provenance.capability == Capability.TRADING_CALENDAR,
            "calendar_capability_required",
        )
        require(
            isinstance(self.qualification, QualificationStatus), "invalid_qualification"
        )
        frozen_tuple(self.days)
        require(
            len({item.day for item in self.days}) == len(self.days),
            "duplicate_calendar_day",
        )
        require(
            all(self.window.contains(item.day) for item in self.days),
            "calendar_day_outside_window",
        )
        object.__setattr__(
            self, "days", tuple(sorted(self.days, key=lambda item: item.day))
        )

    @property
    def revision(self) -> str:
        return content_id(self)


@dataclass(frozen=True)
class UniverseRevision:
    universe_id: str
    schema_revision: str
    effective: DateWindow
    history_window: DateWindow
    known_at: datetime
    mode: MembershipMode
    inclusion_rule: str
    exclusion_rule: str
    authority_scope: str
    enumeration_evidence: tuple[str, ...]
    members: tuple[UniverseMember, ...]
    calendars: tuple[CalendarRevision, ...]
    mappings: tuple[SourceMapping, ...]
    provenance: Provenance
    qualifications: tuple[tuple[AssetClass, Qualification], ...]
    completeness: str = "bounded_explicit_membership"

    def __post_init__(self) -> None:
        text_fields(
            self.universe_id,
            self.schema_revision,
            self.inclusion_rule,
            self.exclusion_rule,
            self.authority_scope,
        )
        require(
            self.completeness == "bounded_explicit_membership",
            "full_market_not_admitted",
        )
        require(isinstance(self.mode, MembershipMode), "invalid_membership_mode")
        if self.mode == MembershipMode.SNAPSHOT:
            require(
                self.effective == self.history_window
                and (self.history_window.end_exclusive - self.history_window.start).days
                == 1,
                "snapshot_cannot_claim_history",
            )
        instant(self.known_at)
        require(self.known_at >= self.provenance.obtained_at, "known_at_backdated")
        require(
            self.provenance.capability == Capability.SECURITY_UNIVERSE,
            "universe_capability_required",
        )
        for items in (
            self.enumeration_evidence,
            self.members,
            self.calendars,
            self.mappings,
            self.qualifications,
        ):
            frozen_tuple(items)
        text_fields(*self.enumeration_evidence)
        require(
            all(type(pair) is tuple and len(pair) == 2 for pair in self.qualifications),
            "immutable_qualification_pairs_required",
        )
        require(bool(self.members), "empty_universe")
        require(
            len({item.instrument for item in self.members}) == len(self.members),
            "duplicate_instrument",
        )
        require(
            len({item.market for item in self.calendars}) == len(self.calendars),
            "duplicate_market_calendar",
        )
        require(
            all(item.known_at <= self.known_at for item in self.calendars),
            "calendar_not_yet_known",
        )
        require(
            all(
                item.provenance.mode == self.provenance.mode for item in self.calendars
            ),
            "mixed_evidence_modes",
        )
        require(
            {category for category, _ in self.qualifications} == set(AssetClass)
            and len(self.qualifications) == len(AssetClass),
            "category_qualification_matrix_required",
        )
        require(
            all(
                isinstance(category, AssetClass) and isinstance(gates, Qualification)
                for category, gates in self.qualifications
            ),
            "invalid_qualification_matrix",
        )
        # This D1 candidate never upgrades real evidence or its source admission.
        if self.provenance.mode != EvidenceMode.SYNTHETIC:
            require(
                all(gates == Qualification() for _, gates in self.qualifications),
                "real_qualification_remains_unknown",
            )
            require(
                all(
                    item.qualification == QualificationStatus.UNKNOWN
                    for item in self.calendars
                ),
                "real_calendar_remains_unknown",
            )
        identities = {item.instrument for item in self.members}
        require(
            all(item.instrument in identities for item in self.mappings),
            "mapping_extra_instrument",
        )
        # Only intervals for the same identity or request code can conflict.
        for attribute, classification in (
            ("instrument", "ambiguous_source_mapping"),
            ("request_code", "source_code_collision"),
        ):
            groups: dict[tuple, list[DateWindow]] = {}
            for mapping in self.mappings:
                key = (
                    (mapping.source, mapping.instrument)
                    if attribute == "instrument"
                    else (
                        mapping.source,
                        mapping.request_namespace,
                        mapping.request_code,
                    )
                )
                groups.setdefault(key, []).append(mapping.validity)
            for windows in groups.values():
                ordered = sorted(windows)
                require(
                    all(
                        left.end_exclusive <= right.start
                        for left, right in zip(ordered, ordered[1:])
                    ),
                    classification,
                )
        object.__setattr__(
            self,
            "members",
            tuple(sorted(self.members, key=lambda item: item.instrument)),
        )
        object.__setattr__(
            self,
            "calendars",
            tuple(sorted(self.calendars, key=lambda item: item.market)),
        )
        object.__setattr__(
            self, "mappings", tuple(sorted(self.mappings, key=canonical_bytes))
        )
        object.__setattr__(
            self,
            "qualifications",
            tuple(sorted(self.qualifications, key=lambda item: item[0])),
        )

    @property
    def revision(self) -> str:
        return content_id(self)

    @property
    def member_digest(self) -> str:
        return content_id(self.members)

    def mapping_for(
        self,
        instrument: InstrumentIdentity,
        day: date,
        source: Key,
        binding: Key,
        revision: str,
    ) -> SourceMapping:
        matches = tuple(
            item
            for item in self.mappings
            if item.instrument == instrument
            and item.validity.contains(day)
            and item.source == source
            and item.binding == binding
            and item.revision == revision
        )
        require(len(matches) == 1, "explicit_source_mapping_required")
        return matches[0]
