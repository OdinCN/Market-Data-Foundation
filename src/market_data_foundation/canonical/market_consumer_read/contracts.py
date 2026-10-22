"""D4 normalized read port. No ledger enumeration, persistence or provider API."""

import json
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from hashlib import sha256
from math import isfinite
from typing import Protocol

from market_data_foundation.canonical.model import Key
from market_data_foundation.domain_contracts.market_universe import InstrumentIdentity

WIRE = "mdf.consumer.market.read.v1"
SERIALIZATION = "mdf.market.canonical-json.v1"


class ReadError(Exception):
    """Only stable classifications cross the consumer boundary."""

    def __init__(self, code: str, status: int = 422, dimension: str | None = None):
        self.code, self.status, self.dimension = code, status, dimension
        super().__init__(code)


def check(condition: bool, code: str, status: int = 422) -> None:
    if not condition:
        raise ReadError(code, status)


def public(value):
    if type(value) is bytes:
        return {"$bytes": value.hex()}
    if isinstance(value, InstrumentIdentity):
        return {"namespace": value.namespace, "identity": value.instrument_id}
    if isinstance(value, datetime):
        check(
            value.tzinfo is not None and value.utcoffset() == timedelta(0),
            "INVALID_UTC_TIME",
            400,
        )
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: public(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        if not all(type(k) is str for k in value):
            return {
                "$map": [
                    [public(k), public(value[k])] for k in sorted(value, key=encode)
                ]
            }
        return {k: public(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [public(v) for v in value]
    check(
        value is None or type(value) in (str, int, float, bool), "NONCANONICAL_CONTENT"
    )
    return value


def encode(value) -> bytes:
    try:
        return json.dumps(
            public(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except ValueError, UnicodeError, RecursionError:
        raise ReadError("NONCANONICAL_CONTENT") from None


def digest(value) -> str:
    return sha256(encode((SERIALIZATION, value))).hexdigest()


def byte_digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def strict_json(raw: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            check(key not in result, "INVALID_JSON", 400)
            result[key] = value
        return result

    try:

        def number(value):
            result = float(value)
            check(isfinite(result), "INVALID_JSON", 400)
            return result

        return json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_float=number,
            parse_constant=lambda _: check(False, "INVALID_JSON", 400),
        )
    except ValueError, UnicodeError, RecursionError:
        raise ReadError("INVALID_JSON", 400) from None


@dataclass(frozen=True)
class Limits:
    items: int = 32
    days: int = 366
    slots: int = 8192
    months: int = 448
    candidates_per_slot: int = 64
    candidate_refs: int = 32768
    versions: int = 256
    selected_versions: int = 128
    history_per_version: int = 64
    events: int = 2048
    version_content: int = 64 * 1024
    content: int = 8 * 1024 * 1024
    metadata: int = 16 * 1024 * 1024
    lineage_per_version: int = 128
    lineage: int = 4096
    logical: int = 16 * 1024 * 1024
    page_units: int = 1000
    unit: int = 16 * 1024
    fixed: int = 128 * 1024
    page: int = 1024 * 1024
    query: int = 64 * 1024
    request: int = 72 * 1024
    cursor: int = 4096
    decoded_cursor: int = 3072
    calls: int = 40
    statement_seconds: float = 1
    transaction_seconds: float = 5
    request_seconds: float = 8


def cap(actual: int | float, maximum: int | float, dimension: str) -> None:
    if actual > maximum:
        raise ReadError("READ_CAPACITY_EXCEEDED", 422, dimension)


def check_elapsed(actual: float, maximum: float, dimension: str) -> None:
    if actual > maximum:
        raise ReadError("READ_TIMEOUT", 503, dimension)


@dataclass(frozen=True)
class Item:
    item_id: str
    instrument: InstrumentIdentity
    start_date: date
    end_date_exclusive: date
    exact_versions: tuple[Key, ...] = ()

    def days(self) -> tuple[date, ...]:
        return tuple(
            self.start_date + timedelta(days=n)
            for n in range((self.end_date_exclusive - self.start_date).days)
        )


@dataclass(frozen=True)
class Query:
    definition: Key
    items: tuple[Item, ...]
    mode: str = "as_of"
    at: datetime | None = None
    page_size: int = 1000
    universe_revision: str | None = None
    semantics: tuple[tuple[str, str], ...] = ()
    wire_revision: str = WIRE


@dataclass(frozen=True, order=True)
class Slot:
    definition: Key
    instrument: InstrumentIdentity
    day: date


@dataclass(frozen=True)
class Binding:
    ledger: str
    schema_revision: str
    algorithm_revision: str
    generation: str
    ready: bool = True


@dataclass(frozen=True)
class SlotSummary:
    count: int
    digest: str
    overflow: bool = False


@dataclass(frozen=True)
class MonthDirectory:
    definition: Key
    instrument: InstrumentIdentity
    month: date
    ready: bool
    days: tuple[tuple[date, SlotSummary], ...]


@dataclass(frozen=True)
class Version:
    key: Key
    definition: Key
    instrument: InstrumentIdentity
    start: date
    end_exclusive: date
    content_digest: str
    content_length: int
    lineage_keys: tuple[Key, ...]
    lineage_digest: str
    lineage_length: int
    source: Key
    binding: Key
    mapping_revision: str
    calendar_revision: str
    universe_revision: str | None
    semantics: tuple[tuple[str, str], ...]
    canonical_digest: str
    range_key: str

    def contains(self, day: date) -> bool:
        return self.start <= day < self.end_exclusive


@dataclass(frozen=True)
class Publication:
    key: Key
    version: Key
    occurrence: str
    predecessor: Key | None
    decision: str
    decision_at: datetime
    usable_at: datetime
    effective_at: datetime | None
    rule_revision: str
    evidence: tuple[str, ...]
    canonical_digest: str
    supersedes_version: Key | None = None


@dataclass(frozen=True)
class VersionGuard:
    version: Key
    history_count: int
    history_digest: str
    change_no: int


@dataclass(frozen=True)
class Guard:
    subject: str
    revision: str
    change_no: int
    status: str


@dataclass(frozen=True)
class Admission:
    definition: Key
    semantics: tuple[tuple[str, str], ...]
    universe_revision: str | None
    members: tuple[InstrumentIdentity, ...]
    guards: tuple[Guard, ...]


def subject(axis: str, key) -> str:
    return axis + ":" + digest(key)


def query_subjects(q: Query) -> tuple[str, ...]:
    result = (subject("definition", q.definition), subject("units", q.definition))
    if q.universe_revision:
        result += (subject("universe", q.universe_revision),)
    return result


def version_subjects(v: Version) -> tuple[str, ...]:
    return (
        subject("version", v.key),
        subject("source", v.source),
        subject("binding", v.binding),
        subject("mapping", v.mapping_revision),
        subject("calendar", v.calendar_revision),
    ) + tuple(subject("retention", key) for key in v.lineage_keys)


def candidate_digest(versions: tuple[Version, ...]) -> str:
    return digest(tuple(sorted(versions, key=lambda v: encode(v.key))))


class ReadView(Protocol):
    """One repeatable-read/read-only view; all methods must obey passed limits.

    Canonical reads normalize exact records without dropping scope/evidence checks.
    They must validate the canonical graph and return the same normalized descriptor.
    Lineage is canonical PUBLIC evidence JSON; private locators never enter this port.
    Implementations must use indexed keys, server timeouts, bounded byte decoding,
    and charge every underlying SQL statement to the single request budget.
    """

    now: datetime
    binding: Binding

    def directories(
        self, keys: tuple[tuple[Key, InstrumentIdentity, date], ...], budget: "Budget"
    ) -> tuple[MonthDirectory, ...]: ...
    def candidates(
        self, slots: tuple[Slot, ...], budget: "Budget"
    ) -> Mapping[Slot, tuple[Key, ...]]: ...
    def versions(
        self, keys: tuple[Key, ...], budget: "Budget"
    ) -> Mapping[Key, Version]: ...
    def version_slots(
        self, keys: tuple[Key, ...], budget: "Budget"
    ) -> Mapping[Key, tuple[Slot, ...]]: ...
    def version_guards(
        self, keys: tuple[Key, ...], budget: "Budget"
    ) -> Mapping[Key, VersionGuard]: ...
    def histories(
        self, keys: tuple[Key, ...], budget: "Budget"
    ) -> Mapping[Key, tuple[Publication, ...]]: ...
    def canonical_versions(
        self, keys: tuple[Key, ...], budget: "Budget"
    ) -> Mapping[Key, Version]: ...
    def canonical_publications(
        self, keys: tuple[Key, ...], budget: "Budget"
    ) -> Mapping[Key, Publication]: ...
    def admission(
        self, query: Query, versions: tuple[Version, ...], budget: "Budget"
    ) -> Admission: ...
    def contents(
        self, versions: tuple[Version, ...], budget: "Budget"
    ) -> Mapping[Key, bytes]: ...
    def lineages(
        self, versions: tuple[Version, ...], budget: "Budget"
    ) -> Mapping[Key, bytes]: ...


class MarketProjectionRead(Protocol):
    def read_view(self, budget: "Budget") -> AbstractContextManager[ReadView]: ...


class Budget:
    """Shared across ALL batches and methods; adapters charge SQL, reader charges IO."""

    def __init__(self, limits: Limits, timer):
        self.limits, self.timer = limits, timer
        self.started = timer()
        self.transaction_started = self.started
        self.calls = self.metadata = self.content = 0

    def statement(self, count: int = 1) -> None:
        check(type(count) is int and count > 0, "INDEX_INCONSISTENT")
        self.calls += count
        cap(self.calls, self.limits.calls, "statements")
        self.check_time()

    def check_time(self):
        check_elapsed(
            self.timer() - self.started, self.limits.request_seconds, "request_time"
        )
        check_elapsed(
            self.timer() - self.transaction_started,
            self.limits.transaction_seconds,
            "transaction_time",
        )

    def add_metadata(self, value):
        if isinstance(value, Mapping) and all(type(v) is bytes for v in value.values()):
            size = sum(len(encode(k)) + len(v) for k, v in value.items())
        else:
            size = len(value) if type(value) is bytes else len(encode(value))
        self.metadata += size
        cap(self.metadata, self.limits.metadata, "metadata")
