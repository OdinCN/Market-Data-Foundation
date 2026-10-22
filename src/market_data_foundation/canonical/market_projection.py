"""D3 -> D4 read-only projection port and bounded snapshot values.

This module has no persistence/provider effects. A view is one immutable committed
snapshot; a fresh view is required for each consumer page.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Protocol

from market_data_foundation.canonical.market_material import (
    MaterialReceipt,
    NormalizationContract,
)
from market_data_foundation.canonical.market_publication import (
    MarketPublication,
    MarketVersion,
    active_at,
    validate_history,
)
from market_data_foundation.canonical.model import ContractError, Key, instant
from market_data_foundation.domain_contracts.market_planning import PublicationUnit
from market_data_foundation.domain_contracts.market_universe import (
    InstrumentIdentity,
    canonical_bytes,
    content_id,
)

SCHEMA_REVISION = "market.projection.schema.d3.r1"
ALGORITHM_REVISION = "market.projection.algorithm.d3.r1"


class ReadCode(StrEnum):
    NOT_COVERED = "INDEX_NOT_COVERED"
    NOT_READY = "INDEX_NOT_READY"
    CAPACITY = "READ_CAPACITY_EXCEEDED"
    INCONSISTENT = "INDEX_INCONSISTENT"
    UNAVAILABLE = "INDEX_UNAVAILABLE"
    BINDING = "LEDGER_BINDING_MISMATCH"
    GENERATION = "INDEX_GENERATION_CHANGED"
    ADMISSION = "CURRENT_ADMISSION_DENIED"
    NOT_FOUND = "VERSION_NOT_FOUND"
    NOT_PUBLISHED = "VERSION_NOT_PUBLISHED"
    CHANGED = "CURRENT_PUBLICATION_DENIED"


class ProjectionError(ContractError):
    def __init__(self, code: ReadCode) -> None:
        self.code = code
        super().__init__(code.value)


def check(condition: bool, code: ReadCode = ReadCode.INCONSISTENT) -> None:
    if not condition:
        raise ProjectionError(code)


@dataclass(frozen=True)
class MonthKey:
    definition: Key
    instrument: InstrumentIdentity
    year: int
    month: int

    def __post_init__(self) -> None:
        date(self.year, self.month, 1)


@dataclass(frozen=True)
class SlotKey:
    definition: Key
    instrument: InstrumentIdentity
    day: date

    @property
    def month(self) -> MonthKey:
        return MonthKey(self.definition, self.instrument, self.day.year, self.day.month)


@dataclass(frozen=True)
class IndexGeneration:
    ledger_binding: str
    generation: str
    ready: bool
    schema_revision: str = SCHEMA_REVISION
    algorithm_revision: str = ALGORITHM_REVISION


@dataclass(frozen=True)
class CandidateRecord:
    version: Key
    range_key: str
    content_digest: str
    canonical_digest: str


@dataclass(frozen=True)
class SlotDirectory:
    slot: SlotKey
    count: int
    digest: str | None
    overflow: bool


@dataclass(frozen=True)
class VersionGuard:
    version: Key
    count: int
    history_digest: str
    change: int


@dataclass(frozen=True)
class AdmissionGuard:
    subject: str
    revision: str
    allowed: bool
    change: int


@dataclass(frozen=True)
class VersionMetadata:
    version: Key
    unit: PublicationUnit
    range_key: str
    frozen_at: datetime
    normalization: NormalizationContract
    assessment_digest: str
    canonical_digest: str
    content_length: int
    content_digest: str
    lineage_references: int


@dataclass
class ProjectionIndex:
    """Writer-owned internal tables. Never returned by the read port."""

    generation: IndexGeneration
    months: set[MonthKey] = field(default_factory=set)
    versions: dict[Key, VersionMetadata] = field(default_factory=dict)
    slots: dict[SlotKey, tuple[CandidateRecord, ...]] = field(default_factory=dict)
    rows: dict[tuple[SlotKey, Key], CandidateRecord] = field(default_factory=dict)
    directory: dict[SlotKey, SlotDirectory] = field(default_factory=dict)
    reverse: dict[Key, tuple[SlotKey, ...]] = field(default_factory=dict)
    histories: dict[Key, tuple[MarketPublication, ...]] = field(default_factory=dict)
    guards: dict[Key, VersionGuard] = field(default_factory=dict)


def version_slots(version: MarketVersion) -> tuple[SlotKey, ...]:
    unit = version.assessment.unit
    return tuple(
        SlotKey(unit.definition, unit.instrument, read.point.day)
        for read in unit.expand()
    )


def candidate(version: MarketVersion) -> CandidateRecord:
    return CandidateRecord(
        version.key, version.range_key, version.content_digest, version.digest
    )


def version_metadata(version: MarketVersion) -> VersionMetadata:
    content = version.assessment.content
    return VersionMetadata(
        version.key,
        version.assessment.unit,
        version.range_key,
        version.frozen_at,
        version.assessment.contract,
        version.assessment.digest,
        version.digest,
        len(content),
        version.content_digest,
        len(version.assessment.materials)
        + sum(len(item.rows) for item in version.assessment.materials),
    )


def index_version(index: ProjectionIndex, version: MarketVersion) -> None:
    key = version.key
    index.versions[key] = version_metadata(version)
    slots = version_slots(version)
    index.reverse[key] = slots
    for slot in slots:
        index.rows[(slot, key)] = candidate(version)
        records = tuple(
            sorted(
                (*index.slots.get(slot, ()), candidate(version)),
                key=lambda item: canonical_bytes(item.version),
            )
        )
        index.slots[slot] = records
        index.directory[slot] = SlotDirectory(
            slot,
            len(records),
            content_id(records) if len(records) <= 64 else None,
            len(records) > 64,
        )
    index.histories[key] = ()
    index.guards[key] = VersionGuard(key, 0, content_id(()), 0)


def index_publication(index: ProjectionIndex, event: MarketPublication) -> None:
    key = event.request.version
    history = (*index.histories[key], event)
    index.histories[key] = history
    index.guards[key] = VersionGuard(
        key, len(history), content_id(history), index.guards[key].change + 1
    )


@dataclass
class ReadBudget:
    """One cumulative budget per view, including repeated calls."""

    slots: int = 0
    references: int = 0
    events: int = 0
    metadata: int = 0
    content: int = 0
    versions_seen: set[Key] = field(default_factory=set)
    exact_seen: set[Key] = field(default_factory=set)
    lineage_seen: set[tuple[Key, str]] = field(default_factory=set)
    months_seen: set[MonthKey] = field(default_factory=set)

    def charge_metadata(self, value: object) -> None:
        self.metadata += len(canonical_bytes(value))
        check(self.metadata <= 16 * 1024 * 1024, ReadCode.CAPACITY)


class MarketProjectionView(Protocol):
    generation: IndexGeneration
    now: datetime

    def month_directory(
        self, months: tuple[MonthKey, ...]
    ) -> tuple[SlotDirectory, ...]: ...
    def candidates(
        self, slots: tuple[SlotKey, ...]
    ) -> tuple[tuple[CandidateRecord, ...], ...]: ...
    def versions(
        self, keys: tuple[Key, ...], *, exact: bool = False
    ) -> tuple[VersionMetadata, ...]: ...
    def histories(
        self, keys: tuple[Key, ...]
    ) -> tuple[tuple[MarketPublication, ...], ...]: ...
    def content(self, key: Key) -> bytes: ...
    def lineage(self, key: Key) -> tuple[MaterialReceipt, ...]: ...
    def admission_guards(self, key: Key) -> tuple[AdmissionGuard, ...]: ...
    def version_guards(self, keys: tuple[Key, ...]) -> tuple[VersionGuard, ...]: ...


class MarketProjectionRead(Protocol):
    def open(
        self, ledger_binding: str, expected_generation: str | None = None
    ) -> MarketProjectionView: ...


class MemoryProjectionView:
    """Indexed reads only. Construction is reserved to the ledger's reader facade."""

    def __init__(self, state, now: datetime, store) -> None:
        self._state = state
        self._index = state.index
        self._store = store
        self.generation = state.index.generation
        self.now = now
        self._budget = ReadBudget()

    def _coverage(self, slot: SlotKey) -> None:
        check(self.generation.ready, ReadCode.NOT_READY)
        check(slot.month in self._index.months, ReadCode.NOT_COVERED)

    def month_directory(
        self, months: tuple[MonthKey, ...]
    ) -> tuple[SlotDirectory, ...]:
        import calendar

        self._budget.months_seen.update(months)
        check(len(self._budget.months_seen) <= 448, ReadCode.CAPACITY)
        result = []
        for month in months:
            for day in range(1, calendar.monthrange(month.year, month.month)[1] + 1):
                slot = SlotKey(
                    month.definition,
                    month.instrument,
                    date(month.year, month.month, day),
                )
                self._coverage(slot)
                entry = self._index.directory.get(
                    slot, SlotDirectory(slot, 0, content_id(()), False)
                )
                records = self._index.slots.get(slot, ())
                check(entry.count == len(records) and entry.slot == slot)
                check(entry.overflow == (entry.count > 64))
                if not entry.overflow:
                    check(entry.digest == content_id(records))
                result.append(entry)
        self._budget.charge_metadata(tuple(result))
        return tuple(result)

    def candidates(
        self, slots: tuple[SlotKey, ...]
    ) -> tuple[tuple[CandidateRecord, ...], ...]:
        self._budget.slots += len(slots)
        check(self._budget.slots <= 8192, ReadCode.CAPACITY)
        result = []
        for slot in slots:
            self._coverage(slot)
            directory = self._index.directory.get(
                slot, SlotDirectory(slot, 0, content_id(()), False)
            )
            records = self._index.slots.get(slot, ())
            check(directory.slot == slot and directory.count == len(records))
            check(not directory.overflow and directory.count <= 64, ReadCode.CAPACITY)
            check(directory.digest == content_id(records))
            check(len({item.version for item in records}) == len(records))
            self._budget.references += len(records)
            check(self._budget.references <= 32768, ReadCode.CAPACITY)
            for record in records:
                check(self._index.rows.get((slot, record.version)) == record)
                check(record.version in self._index.versions)
                check(record.version in self._state.versions)
                check(slot in self._index.reverse.get(record.version, ()))
                check(record == candidate(self._state.versions[record.version]))
            self._budget.versions_seen.update(item.version for item in records)
            check(len(self._budget.versions_seen) <= 256, ReadCode.CAPACITY)
            result.append(records)
        self._budget.charge_metadata(tuple(result))
        return tuple(result)

    def versions(
        self, keys: tuple[Key, ...], *, exact: bool = False
    ) -> tuple[VersionMetadata, ...]:
        self._budget.versions_seen.update(keys)
        check(len(self._budget.versions_seen) <= 256, ReadCode.CAPACITY)
        if exact:
            self._budget.exact_seen.update(keys)
            check(len(self._budget.exact_seen) <= 128, ReadCode.CAPACITY)
        result = []
        for key in keys:
            check(key in self._index.versions, ReadCode.NOT_FOUND)
            metadata = self._index.versions[key]
            check(key in self._state.versions)
            version = self._state.versions[key]
            check(metadata == version_metadata(version))
            slots = version_slots(version)
            check(self._index.reverse.get(key) == slots)
            for slot in slots:
                self._coverage(slot)
                check(self._index.rows.get((slot, key)) == candidate(version))
            check(
                metadata.content_length <= 65536 and metadata.lineage_references <= 128,
                ReadCode.CAPACITY,
            )
            result.append(metadata)
        self._budget.charge_metadata(tuple(result))
        return tuple(result)

    def histories(
        self, keys: tuple[Key, ...]
    ) -> tuple[tuple[MarketPublication, ...], ...]:
        self._budget.versions_seen.update(keys)
        check(len(self._budget.versions_seen) <= 256, ReadCode.CAPACITY)
        guards = []
        events = self._budget.events
        for key in keys:
            guard = self._publication_guard(key)
            check(guard.count <= 64, ReadCode.CAPACITY)
            events += guard.count
            check(events <= 2048, ReadCode.CAPACITY)
            guards.append(guard)
        # Reserve the complete request budget before touching any histories.
        self._budget.events = events
        result = []
        for key, guard in zip(keys, guards, strict=True):
            history = self._index.histories.get(key, ())
            check(guard.count == len(history))
            anchor = self._state.publication_heads[key]
            check((history[-1].key if history else None) == anchor.head)
            check(guard.history_digest == content_id(history))
            for event in history:
                check(self._state.publications.get(event.key) == event)
            try:
                validate_history(key, history)
            except ContractError:
                raise ProjectionError(ReadCode.INCONSISTENT) from None
            result.append(history)
        self._budget.charge_metadata(tuple(result))
        return tuple(result)

    def _publication_guard(self, key: Key) -> VersionGuard:
        """Constant point lookups against canonical authority, no history access."""
        check(key in self._state.versions, ReadCode.NOT_FOUND)
        anchor = self._state.publication_heads.get(key)
        guard = self._index.guards.get(key)
        check(anchor is not None and guard is not None)
        check(
            anchor.version == guard.version == key
            and type(anchor.count) is int
            and type(guard.count) is int
            and type(guard.change) is int
            and anchor.count == guard.count == guard.change >= 0
            and anchor.history_digest == guard.history_digest
            and (anchor.head is None) == (anchor.count == 0)
        )
        if anchor.head is not None:
            event = self._state.publications.get(anchor.head)
            check(
                event is not None
                and event.key == anchor.head
                and event.request.version == key
            )
        return guard

    def version_guards(self, keys: tuple[Key, ...]) -> tuple[VersionGuard, ...]:
        self._budget.versions_seen.update(keys)
        check(len(self._budget.versions_seen) <= 256, ReadCode.CAPACITY)
        result = tuple(self._publication_guard(key) for key in keys)
        self._budget.charge_metadata(result)
        return result

    def admission_guards(self, key: Key) -> tuple[AdmissionGuard, ...]:
        self._budget.versions_seen.add(key)
        check(len(self._budget.versions_seen) <= 256, ReadCode.CAPACITY)
        check(key in self._state.versions, ReadCode.NOT_FOUND)
        subjects = self._state.versions[key].admission_subjects
        result = tuple(
            self._state.admissions.get(
                subject, AdmissionGuard(subject, "UNKNOWN", False, 0)
            )
            for subject in subjects
        )
        for subject, guard in zip(subjects, result, strict=True):
            check(guard.subject == subject)
            if subject in self._state.admissions:
                receipt = self._state.receipts.get(
                    ("admission", content_id((subject, guard.revision)))
                )
                check(
                    guard.change > 0
                    and receipt is not None
                    and receipt.payload_digest
                    == content_id((subject, guard.revision, guard.allowed))
                    and receipt.result_digest == content_id(guard)
                )
        self._budget.charge_metadata(result)
        return result

    def lineage(self, key: Key) -> tuple[MaterialReceipt, ...]:
        self.versions((key,))
        version = self._state.versions[key]
        result = version.assessment.materials
        check(
            len(result) + sum(len(item.rows) for item in result) <= 128,
            ReadCode.CAPACITY,
        )
        for receipt in result:
            # Match the complete receipt identity, including each UTC instant.
            check(
                canonical_bytes(self._state.materials.get(receipt.key))
                == canonical_bytes(receipt)
            )
            self._budget.lineage_seen.add((receipt.key, ""))
            self._budget.lineage_seen.update(
                (receipt.key, row.identity) for row in receipt.rows
            )
        check(len(self._budget.lineage_seen) <= 4096, ReadCode.CAPACITY)
        self._budget.charge_metadata(result)
        return result

    def content(self, key: Key) -> bytes:
        from market_data_foundation.canonical.market_material import read_material

        self.versions((key,))
        version = self._state.versions[key]
        # Current guard validation is mandatory even for historical/exact content.
        history = self.histories((key,))[0]
        check(active_at(history, self.now), ReadCode.CHANGED)
        check(
            all(guard.allowed for guard in self.admission_guards(key)),
            ReadCode.ADMISSION,
        )
        content = version.assessment.content
        self._budget.content += len(content) + sum(
            receipt.byte_length for receipt in version.assessment.materials
        )
        check(self._budget.content <= 8 * 1024 * 1024, ReadCode.CAPACITY)
        for receipt in self.lineage(key):
            try:
                read_material(self._store, receipt)
            except Exception:
                raise ProjectionError(ReadCode.UNAVAILABLE) from None
        return content

    def select(self, slot: SlotKey, as_of: datetime) -> tuple[str, tuple[Key, ...]]:
        """Reference selection for offline proofs; D4 owns consumer response shape."""
        instant(as_of)
        check(as_of.astimezone(UTC) <= self.now.astimezone(UTC), ReadCode.INCONSISTENT)
        records = self.candidates((slot,))[0]
        keys = tuple(item.version for item in records)
        self.versions(keys)
        histories = self.histories(keys)
        selected = tuple(
            key
            for key, history in zip(keys, histories, strict=True)
            if active_at(history, as_of)
        )
        if len(selected) > 1:
            return "AMBIGUOUS", selected
        if not selected:
            return "NO_PUBLISHED_CANDIDATE", ()
        self.content(selected[0])
        return "SELECTED", selected
