"""Operator-only serialized read admission; no physical backend cost guarantee."""

from dataclasses import dataclass
from typing import Protocol

from psycopg import sql

from .codec import loads
from .model import (
    DatasetVersion,
    Key,
    Kind,
    Publication,
    Record,
    SourceBindingRevision,
    require,
)

MAX_VERSIONS = 128
MAX_BINDINGS = 128
MAX_PUBLICATIONS = 512
MAX_PAYLOAD_BYTES = 1048576
MAX_TOTAL_BYTES = 67108864
MAX_JSON_DEPTH = 64
MAX_MEMORY_SLOTS = 65536


class OperatorReadAdmissionError(Exception):
    """Internal producer limit signal, translated at the operator boundary."""


def _limit(value: int, maximum: int) -> None:
    if value > maximum:
        raise OperatorReadAdmissionError() from None


class OperatorReads(Protocol):
    def get(self, key: Key) -> Record | None: ...

    def binding_candidates(self, maximum: int) -> tuple[SourceBindingRevision, ...]: ...

    def versions_for_definition(
        self, definition: Key, maximum: int
    ) -> tuple[DatasetVersion, ...]: ...

    def publications_for_version(
        self, version: Key, maximum: int
    ) -> tuple[Publication, ...]: ...


class OperatorReadSource(Protocol):
    def operator_reads(self) -> OperatorReads: ...


@dataclass
class _Admission:
    bytes_read: int = 0
    slots: int = 0

    def check(self, payload: str) -> int:
        require(type(payload) is str, "stored_payload_invalid")
        # Bound the temporary UTF-8 encoding allocation even for multibyte text.
        _limit(len(payload), MAX_PAYLOAD_BYTES)
        size = len(payload.encode("utf-8"))
        _limit(size, MAX_PAYLOAD_BYTES)
        # Lexical nesting guard before json.loads AND recursive canonical decode.
        # Brackets in quoted/escaped strings do not count. JSON validity remains
        # the codec's responsibility; malformed inputs still fail classified.
        depth = 0
        quoted = escaped = False
        for char in payload:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                _limit(depth, MAX_JSON_DEPTH)
            elif char in "]}":
                depth -= 1
        return size

    def batch(self, payloads: list[str]) -> None:
        # Admit the entire bounded batch before decoding even its first record.
        sizes = [self.check(payload) for payload in payloads]
        total = self.bytes_read + sum(sizes)
        _limit(total, MAX_TOTAL_BYTES)
        self.bytes_read = total

    def slot(self) -> None:
        _limit(self.slots + 1, MAX_MEMORY_SLOTS)
        self.slots += 1


class MemoryOperatorReads:
    """Bound visited dictionary slots and whole-kind batches before decoding.

    Publication payloads are admitted once as a complete global batch, then
    filtered for each candidate. Unrelated records can conservatively reject a
    query. This session owns no snapshot; caller must supply consistent storage.
    """

    def __init__(self, records: dict[tuple[str, str, str], str]) -> None:
        self.records = records
        self.admission = _Admission()
        self._publications: tuple[Publication, ...] | None = None

    def get(self, key: Key) -> Record | None:
        payload = self.records.get((key.kind.value, key.identity, key.revision))
        if payload is None:
            return None
        self.admission.batch([payload])
        value = loads(payload)
        require(hasattr(value, "key"), "stored_record_invalid")
        return value

    def _kind(self, kind: Kind, maximum: int) -> tuple[Record, ...]:
        rows = []
        for key, payload in self.records.items():
            self.admission.slot()
            if key[0] == kind.value:
                _limit(len(rows) + 1, maximum)
                rows.append((key, payload))
        self.admission.batch([payload for _, payload in rows])
        values = []
        for key, payload in rows:
            value = loads(payload)
            require(
                hasattr(value, "key")
                and (value.key.kind.value, value.key.identity, value.key.revision)
                == key,
                "stored_record_key_mismatch",
            )
            values.append(value)
        return tuple(values)

    def versions_for_definition(
        self, definition: Key, maximum: int
    ) -> tuple[DatasetVersion, ...]:
        require(definition.kind == Kind.DEFINITION, "version_definition_kind")
        values = self._kind(Kind.VERSION, min(maximum, MAX_VERSIONS))
        require(
            all(isinstance(v, DatasetVersion) for v in values), "stored_version_invalid"
        )
        return tuple(
            sorted(
                (v for v in values if v.definition == definition),
                key=lambda v: v.identity,
            )
        )

    def binding_candidates(self, maximum: int) -> tuple[SourceBindingRevision, ...]:
        values = self._kind(Kind.BINDING, min(maximum, MAX_BINDINGS))
        require(
            all(isinstance(v, SourceBindingRevision) for v in values),
            "stored_binding_invalid",
        )
        return values

    def publications_for_version(
        self, version: Key, maximum: int
    ) -> tuple[Publication, ...]:
        require(version.kind == Kind.VERSION, "publication_version_kind")
        if self._publications is None:
            values = self._kind(Kind.PUBLICATION, MAX_PUBLICATIONS)
            require(
                all(isinstance(v, Publication) for v in values),
                "stored_publication_invalid",
            )
            self._publications = values
        history = tuple(p for p in self._publications if p.request.version == version)
        _limit(len(history), min(maximum, MAX_PUBLICATIONS))
        return tuple(sorted(history, key=lambda p: (p.decision_at, p.request.identity)))


class PostgresOperatorReads:
    """SQL return admission only; LIMIT does not bound scans, TOAST or sorting."""

    def __init__(self, connection, schema: str) -> None:
        self.connection = connection
        self.schema = schema
        self.admission = _Admission()

    def _payloads(self, table: str, predicate, parameters: tuple, maximum: int):
        # MATERIALIZED fixes the counted set within this one statement. Overflow
        # and byte failure return only bounded markers, never partial payloads.
        # No ORDER BY: all fitting rows are sorted after complete admission.
        remaining = MAX_TOTAL_BYTES - self.admission.bytes_read
        query = sql.SQL("""
            WITH bounded AS MATERIALIZED (
                SELECT payload, octet_length(convert_to(payload, 'UTF8')) AS bytes
                FROM {}.{} WHERE {} LIMIT %s
            ), admission AS (
                SELECT count(*) AS n, coalesce(sum(bytes), 0) AS total,
                       coalesce(max(bytes), 0) AS largest FROM bounded
            )
            SELECT CASE WHEN n <= %s AND largest <= %s AND total <= %s
                        THEN payload ELSE NULL END, bytes, n, total
            FROM bounded CROSS JOIN admission
        """).format(sql.Identifier(self.schema), sql.Identifier(table), predicate)
        rows = self.connection.execute(
            query, (*parameters, maximum + 1, maximum, MAX_PAYLOAD_BYTES, remaining)
        ).fetchall()
        _limit(len(rows), maximum)
        # A batch-wide size failure NULLs even an earlier, small row. Inspect
        # all limit markers before treating any NULL as storage corruption.
        for _, size, count, total in rows:
            _limit(count, maximum)
            _limit(size, MAX_PAYLOAD_BYTES)
            _limit(total, remaining)
        payloads = []
        for payload, size, _, _ in rows:
            require(payload is not None, "stored_payload_invalid")
            require(
                self.admission.check(payload) == size, "stored_payload_bytes_mismatch"
            )
            payloads.append(payload)
        self.admission.batch(payloads)
        return payloads

    def get(self, key: Key) -> Record | None:
        if key.kind == Kind.PUBLICATION:
            payloads = self._payloads(
                "publications", sql.SQL("identity = %s"), (key.identity,), 1
            )
        else:
            payloads = self._payloads(
                "records",
                sql.SQL("kind = %s AND identity = %s AND revision = %s"),
                (key.kind.value, key.identity, key.revision),
                1,
            )
        if not payloads:
            return None
        value = loads(payloads[0])
        require(hasattr(value, "key"), "stored_record_invalid")
        return value

    def versions_for_definition(
        self, definition: Key, maximum: int
    ) -> tuple[DatasetVersion, ...]:
        require(definition.kind == Kind.DEFINITION, "version_definition_kind")
        payloads = self._payloads(
            "records",
            sql.SQL("kind = %s"),
            (Kind.VERSION.value,),
            min(maximum, MAX_VERSIONS),
        )
        values = tuple(loads(p) for p in payloads)
        require(
            all(isinstance(v, DatasetVersion) for v in values), "stored_version_invalid"
        )
        return tuple(
            sorted(
                (v for v in values if v.definition == definition),
                key=lambda v: v.identity,
            )
        )

    def binding_candidates(self, maximum: int) -> tuple[SourceBindingRevision, ...]:
        payloads = self._payloads(
            "records",
            sql.SQL("kind = %s"),
            (Kind.BINDING.value,),
            min(maximum, MAX_BINDINGS),
        )
        values = tuple(loads(p) for p in payloads)
        require(
            all(isinstance(v, SourceBindingRevision) for v in values),
            "stored_binding_invalid",
        )
        # The inventory checks duplicates and exact-key readbacks for every row,
        # including unrelated candidates, before filtering any mapping scope.
        return values

    def publications_for_version(
        self, version: Key, maximum: int
    ) -> tuple[Publication, ...]:
        require(version.kind == Kind.VERSION, "publication_version_kind")
        payloads = self._payloads(
            "publications",
            sql.SQL("version_identity = %s"),
            (version.identity,),
            min(maximum, MAX_PUBLICATIONS),
        )
        values = tuple(loads(p) for p in payloads)
        require(
            all(isinstance(v, Publication) for v in values),
            "stored_publication_invalid",
        )
        return tuple(sorted(values, key=lambda p: (p.decision_at, p.request.identity)))
