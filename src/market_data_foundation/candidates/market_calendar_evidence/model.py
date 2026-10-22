"""Offline calendar evidence content; no Source admission or provider effects."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum

from market_data_foundation.canonical.model import Key, Kind, instant, require
from market_data_foundation.domain_contracts.market_universe import (
    DateWindow,
    EvidenceMode,
    content_id,
    text_fields,
)
from market_data_foundation.qualification.contracts import Capability

SCHEMA = "mdf.calendar.evidence.v1"
ALGORITHM_REVISION = "a-cal.selection.v2"
MAX_BYTES = 1_048_576
MAX_ROWS = 8192
MAX_MATERIALS = 64
MAX_TOTAL_BYTES = 8_388_608
MAX_TOTAL_ROWS = 32768
MAX_DAYS = 8192
# Logical per-date lineage, including repeated refs when serialized (no truncation).
MAX_LINEAGE_REFERENCES = 262144
MAX_LINEAGE_BYTES = 16_777_216


class State(StrEnum):
    OPEN = "OPEN"
    HALF_DAY = "HALF_DAY"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"
    CONFLICT = "CONFLICT"


class EvidenceKind(StrEnum):
    NOTICE = "NOTICE"
    SESSION_SCHEDULE = "SESSION_SCHEDULE"
    MISSING_BARS = "MISSING_BARS"
    SDK_WORKDAY = "SDK_WORKDAY"

    @property
    def direct(self) -> bool:
        return self in (self.NOTICE, self.SESSION_SCHEDULE)


class Completion(StrEnum):
    COMPLETE = "COMPLETE"
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"


def aware(value: datetime) -> None:
    require(type(value) is datetime, "instant_required")
    instant(value)


@dataclass(frozen=True)
class MaterialReceipt:
    """Caller supplies identity; matching bytes do not establish rights."""

    source: Key
    binding: Key
    material: Key
    capability: Capability
    mode: EvidenceMode
    producer_operation: str
    upstream_version: str
    upstream_revision: str
    decoder_revision: str
    source_ref: str
    raw_sha256: str
    raw_length: int
    obtained_at: datetime
    received_at: datetime
    known_at: datetime | None
    source_published_at: datetime | None = None

    def __post_init__(self) -> None:
        require(
            isinstance(self.source, Key)
            and self.source.kind == Kind.SOURCE
            and isinstance(self.binding, Key)
            and self.binding.kind == Kind.BINDING
            and isinstance(self.material, Key)
            and self.material.kind == Kind.MATERIAL,
            "source_binding_material_required",
        )
        require(
            isinstance(self.capability, Capability)
            and self.capability == Capability.TRADING_CALENDAR,
            "calendar_capability_required",
        )
        require(
            isinstance(self.mode, EvidenceMode)
            and self.mode in (EvidenceMode.SYNTHETIC, EvidenceMode.RETAINED),
            "offline_material_required",
        )
        text_fields(
            self.producer_operation,
            self.upstream_version,
            self.upstream_revision,
            self.decoder_revision,
            self.source_ref,
        )
        require(
            type(self.raw_sha256) is str
            and len(self.raw_sha256) == 64
            and all(c in "0123456789abcdef" for c in self.raw_sha256),
            "invalid_raw_digest",
        )
        require(
            type(self.raw_length) is int and 0 < self.raw_length <= MAX_BYTES,
            "material_byte_limit",
        )
        obtained = utc(self.obtained_at)
        received = utc(self.received_at)
        require(obtained <= received, "received_before_obtained")
        if self.known_at is not None:
            require(received <= utc(self.known_at), "known_at_backdated")
        if self.source_published_at is not None:
            require(
                utc(self.source_published_at) <= obtained,
                "publication_after_obtained",
            )


@dataclass(frozen=True)
class Session:
    session_id: str
    start_local: str
    end_local: str
    start_fold: int | None
    end_fold: int | None
    start_utc: datetime
    end_utc: datetime


@dataclass(frozen=True)
class Claim:
    claim_id: str
    row_locator: str
    day: date
    state: State
    evidence_kind: EvidenceKind
    arrangement: str
    source_ref: str
    sessions: tuple[Session, ...]
    complete_after_local: str | None
    complete_after_fold: int | None
    complete_after_utc: datetime | None
    supersedes: tuple[str, ...]

    @property
    def meaning(self) -> tuple:
        # Same instant with a different local label/fold is not silently identical.
        return (
            self.state,
            self.arrangement,
            self.sessions,
            self.complete_after_local,
            self.complete_after_fold,
            self.complete_after_utc,
        )


@dataclass(frozen=True)
class ParsedMaterial:
    receipt: MaterialReceipt
    market: str
    timezone: str
    effective: DateWindow
    claims: tuple[Claim, ...]
    raw: bytes
    _revision: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Resolve this once: hashing all rows for each claim ref is quadratic.
        revision = content_id(
            (
                SCHEMA,
                "a-cal.parser.v1",
                self.receipt,
                self.market,
                self.timezone,
                self.effective,
                self.claims,
            )
        )
        object.__setattr__(self, "_revision", revision)

    @property
    def revision(self) -> str:
        return self._revision

    def ref(self, claim: Claim) -> str:
        return f"{self.revision}:{claim.claim_id}"


@dataclass(frozen=True)
class DayEvidence:
    market: str
    day: date
    as_of: datetime
    state: State
    reason: str
    timezone: str | None
    sessions: tuple[Session, ...]
    complete_after_utc: datetime | None
    completion: Completion
    evidence_refs: tuple[str, ...]
    material_revisions: tuple[str, ...]

    @property
    def revision(self) -> str:
        return content_id((ALGORITHM_REVISION, self))


def utc(value: datetime) -> datetime:
    aware(value)
    return value.astimezone(UTC)
