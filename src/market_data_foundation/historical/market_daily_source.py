"""D2 offline page port. Native fields are evidence, never normalized economics."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Callable, Protocol

from market_data_foundation.canonical.model import Key, Kind, instant, require
from market_data_foundation.domain_contracts.market_planning import (
    PlanningBudget,
    Shard,
)
from market_data_foundation.domain_contracts.market_universe import (
    EvidenceMode,
    content_id,
    frozen_tuple,
    text_fields,
)
from market_data_foundation.qualification.contracts import Capability


class Failure(StrEnum):
    UNKNOWN = "EFFECT_UNKNOWN"
    ABSENT = "CONFIRMED_NOT_PERFORMED"
    BUDGET = "BUDGET_EXCEEDED"
    CANCELLED = "CANCELLED"
    IDENTITY = "IDENTITY_MISMATCH"
    PAGE = "PAGE_INVALID"
    DRIFT = "PAGE_DRIFT"
    EXHAUSTION = "EXHAUSTION_UNKNOWN"
    TIMEOUT = "TIMEOUT"
    TRANSPORT = "TRANSPORT_FAILURE"
    LEASE = "LEASE_LOST"
    BUSY = "HOST_BUSY"
    CORRUPT = "JOURNAL_INCONSISTENT"
    UNAVAILABLE = "JOURNAL_UNAVAILABLE"


class DailyError(RuntimeError):
    def __init__(self, classification: Failure):
        self.classification = classification
        super().__init__(classification.value)


class NotPerformed(Exception):
    """Offline adapter guarantees no acquisition occurred; text is never recorded."""


@dataclass(frozen=True)
class OfflineBinding:
    mode: EvidenceMode
    source: Key
    binding: Key
    mapping_revision: str
    implementation_identity: str
    endpoint_contract: str
    producer_operation: str
    upstream_revision: str
    decode_revision: str
    capability: Capability = Capability.BARS_DAILY
    schema_revision: str = "mdf.market.daily.page.d2.v1"
    retained_materials: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require(
            self.mode in (EvidenceMode.SYNTHETIC, EvidenceMode.RETAINED),
            "d2_offline_source_only",
        )
        require(self.capability == Capability.BARS_DAILY, "daily_capability_required")
        require(
            self.source.kind == Kind.SOURCE and self.binding.kind == Kind.BINDING,
            "source_binding_required",
        )
        text_fields(
            self.mapping_revision,
            self.implementation_identity,
            self.endpoint_contract,
            self.producer_operation,
            self.upstream_revision,
            self.decode_revision,
            self.schema_revision,
        )
        frozen_tuple(self.retained_materials)
        text_fields(*self.retained_materials)
        require(
            self.mode != EvidenceMode.RETAINED or bool(self.retained_materials),
            "explicit_retained_materials_required",
        )


@dataclass(frozen=True)
class PageRequest:
    shard: Shard
    page_index: int
    schema_revision: str
    budget: PlanningBudget
    capability: Capability = Capability.BARS_DAILY

    def __post_init__(self) -> None:
        require(
            type(self.page_index) is int
            and 0 <= self.page_index < self.shard.route.pages.page_count,
            "invalid_page_index",
        )
        require(self.capability == Capability.BARS_DAILY, "daily_capability_required")
        text_fields(self.schema_revision)

    @property
    def offset(self) -> int:
        return (
            self.shard.route.pages.first_offset
            + self.page_index * self.shard.route.pages.page_size
        )

    @property
    def count(self) -> int:
        return self.shard.route.pages.page_size

    @property
    def identity(self) -> str:
        # Includes exact job, Source/Binding/Mapping/Definition and request code,
        # namespace, window, implementation/endpoint, execution inputs and pages.
        return content_id(self)

    @property
    def attempt_identity(self) -> str:
        return content_id(("mdf.market.daily.attempt.d2.v1", self.identity))


@dataclass(frozen=True)
class NativeField:
    name: str
    native_type: str
    value: str | int | bool | None
    source_ref: str
    transform_ref: str
    unit: str = "UNKNOWN"
    semantic_status: str = "UNKNOWN"
    clock_semantics: str = "UNKNOWN"

    def __post_init__(self) -> None:
        text_fields(
            self.name,
            self.native_type,
            self.source_ref,
            self.transform_ref,
            self.unit,
            self.semantic_status,
            self.clock_semantics,
        )
        require(
            self.value is None or type(self.value) in (str, int, bool),
            "native_value_requires_lossless_scalar_encoding",
        )


@dataclass(frozen=True)
class NativeRow:
    # A local-date locator is supplied by the offline adapter, not inferred from
    # a timezone-less upstream Time field.
    day: str
    locator: str
    fields: tuple[NativeField, ...]

    def __post_init__(self) -> None:
        text_fields(self.day, self.locator)
        frozen_tuple(self.fields)
        require(
            len({field.name for field in self.fields}) == len(self.fields),
            "duplicate_native_field",
        )


@dataclass(frozen=True)
class Page:
    request_identity: str
    raw: bytes
    rows: tuple[NativeRow, ...]
    snapshot_anchor: str
    terminal: bool | None
    termination_evidence: str | None
    producer_operation: str
    upstream_revision: str
    decode_revision: str
    obtained_at: datetime | None = None
    received_at: datetime | None = None
    known_at: datetime | None = None
    source_time: datetime | None = None
    material_kind: str = "raw"
    retained_reference: str | None = None

    def __post_init__(self) -> None:
        text_fields(
            self.request_identity,
            self.snapshot_anchor,
            self.producer_operation,
            self.upstream_revision,
            self.decode_revision,
        )
        require(type(self.raw) is bytes, "raw_bytes_required")
        frozen_tuple(self.rows)
        require(
            self.terminal is None or type(self.terminal) is bool, "invalid_terminal"
        )
        require(self.material_kind in ("raw", "derived"), "explicit_material_kind")
        for value in (
            self.obtained_at,
            self.received_at,
            self.known_at,
            self.source_time,
        ):
            if value is not None:
                instant(value)


class LookupStatus(StrEnum):
    FOUND = "FOUND"
    ABSENT = "ABSENT"
    UNKNOWN = "UNKNOWN"


MAX_LOOKUP_EVIDENCE_BYTES = 4096


@dataclass(frozen=True)
class Lookup:
    request_identity: str
    status: LookupStatus
    evidence: str
    page: Page | None = None

    def __post_init__(self) -> None:
        text_fields(self.request_identity, self.evidence)
        require(
            len(self.evidence.encode("utf-8")) <= MAX_LOOKUP_EVIDENCE_BYTES,
            "lookup_evidence_reference_too_large",
        )
        require(isinstance(self.status, LookupStatus), "invalid_lookup_status")
        require(
            (self.status == LookupStatus.FOUND) == (self.page is not None),
            "lookup_page_mismatch",
        )


class OfflineSource(Protocol):
    """No hidden effects: charge BEFORE each simulated transport operation.

    lookup_original is a read-only retained/synthetic identity lookup. It must
    never contact a Provider or create material. No real adapter is admitted here.
    """

    binding: OfflineBinding

    def fetch(
        self, request: PageRequest, charge: Callable[[str, int], None]
    ) -> Page: ...

    def lookup_original(self, request: PageRequest) -> Lookup: ...
