"""Selective SQL loader into D3's existing bounded, fail-closed view checks.

Only explicitly requested months/slots/keys are loaded in one read-only snapshot.
No canonical scans, repair, provider access or material PUT is reachable here.
"""

import calendar
from datetime import date
from types import SimpleNamespace

from market_data_foundation.canonical.market_projection import (
    MemoryProjectionView,
    ProjectionIndex,
    ReadCode,
    SlotDirectory,
    SlotKey,
    check,
    version_slots,
)
from market_data_foundation.canonical.market_publication import PublicationHead
from market_data_foundation.domain_contracts.market_universe import (
    canonical_bytes,
    content_id,
)

from .market_dataset_codec import loads
from .market_dataset_postgres import BINDING, SLOT_WHERE, slot_args


class PostgresMarketView(MemoryProjectionView):
    def __init__(self, ledger, expected_generation):
        self.ledger = ledger
        self.closed = False
        self.loaded_bytes = 0
        self.loaded_histories = set()
        row = ledger._sql(
            "SELECT payload FROM {s}.projection_generations WHERE active"
        ).fetchone()
        check(row is not None, ReadCode.NOT_READY)
        generation = loads(row[0])
        check(generation.ledger_binding == BINDING, ReadCode.BINDING)
        check(generation.ready, ReadCode.NOT_READY)
        check(
            expected_generation is None or expected_generation == generation.generation,
            ReadCode.GENERATION,
        )
        from market_data_foundation.canonical.market_projection import (
            ALGORITHM_REVISION,
            SCHEMA_REVISION,
        )

        check(
            generation.schema_revision == SCHEMA_REVISION
            and generation.algorithm_revision == ALGORITHM_REVISION
        )
        state = SimpleNamespace(
            index=ProjectionIndex(generation),
            versions={},
            materials={},
            publications={},
            publication_heads={},
            admissions={},
            receipts={},
        )
        super().__init__(state, ledger.now(), ledger.store)

    def _open(self):
        check(not self.closed, ReadCode.UNAVAILABLE)

    def _coverage(self, slot):
        self._open()
        month = slot.month
        if month not in self._index.months:
            row = self.ledger._sql(
                "SELECT payload FROM {s}.projection_months WHERE definition_id=%s "
                "AND instrument_namespace=%s AND instrument_id=%s "
                "AND year=%s AND month=%s",
                (
                    content_id(month.definition),
                    month.instrument.namespace,
                    month.instrument.instrument_id,
                    month.year,
                    month.month,
                ),
            ).fetchone()
            check(row is not None, ReadCode.NOT_COVERED)
            check(loads(row[0]) == month)
            self._index.months.add(month)
        super()._coverage(slot)

    def _ensure_versions(self, keys):
        self._open()
        check(len(self._budget.versions_seen | set(keys)) <= 256, ReadCode.CAPACITY)
        self._budget.versions_seen.update(keys)
        for key in keys:
            if key in self._state.versions:
                continue
            identity = content_id(key)
            length = self.ledger._sql(
                "SELECT length(payload) FROM {s}.versions WHERE id=%s", (identity,)
            ).fetchone()
            check(length is not None, ReadCode.NOT_FOUND)
            self.loaded_bytes += length[0]
            check(self.loaded_bytes <= 16 * 1024 * 1024, ReadCode.CAPACITY)
            version = self.ledger._record("versions", identity)
            check(version.key == key)
            meta = self.ledger._sql(
                "SELECT payload FROM {s}.projection_versions WHERE version_id=%s",
                (identity,),
            ).fetchone()
            check(meta is not None)
            self._state.versions[key] = version
            self._index.versions[key] = loads(meta[0])
            rows = self.ledger._sql(
                "SELECT definition_id,instrument_namespace,instrument_id,"
                "local_date,payload FROM {s}.projection_rows "
                "WHERE version_id=%s ORDER BY local_date LIMIT 32",
                (identity,),
            ).fetchall()
            slots = version_slots(version)
            check(len(rows) == len(slots) <= 31)
            for row, slot in zip(rows, slots, strict=True):
                check((*row[:3], str(row[3])) == slot_args(slot))
                self._index.rows[(slot, key)] = loads(row[4])
            self._index.reverse[key] = slots

    def _slot(self, slot, *, directory_only=False):
        self._coverage(slot)
        args = slot_args(slot)
        row = self.ledger._sql(
            "SELECT candidate_count,candidate_digest,overflow FROM "
            "{s}.projection_slots WHERE " + SLOT_WHERE,
            args,
        ).fetchone()
        entry = (
            SlotDirectory(slot, row[0], row[1], bool(row[2]))
            if row
            else SlotDirectory(slot, 0, content_id(()), False)
        )
        check(
            type(entry.count) is int
            and entry.count >= 0
            and entry.overflow == (entry.count > 64)
        )
        self._index.directory[slot] = entry
        if directory_only:
            return entry
        check(not entry.overflow, ReadCode.CAPACITY)
        rows = self.ledger._sql(
            "SELECT payload FROM {s}.projection_rows WHERE " + SLOT_WHERE + " LIMIT 65",
            args,
        ).fetchall()
        check(len(rows) == entry.count)
        records = tuple(
            sorted(
                (loads(row[0]) for row in rows),
                key=lambda item: canonical_bytes(item.version),
            )
        )
        check(content_id(records) == entry.digest)
        self._index.slots[slot] = records
        return entry

    def month_directory(self, months):
        self._open()
        check(len(months) <= 448, ReadCode.CAPACITY)
        self._budget.months_seen.update(months)
        check(len(self._budget.months_seen) <= 448, ReadCode.CAPACITY)
        result = []
        for month in months:
            for day in range(1, calendar.monthrange(month.year, month.month)[1] + 1):
                result.append(
                    self._slot(
                        SlotKey(
                            month.definition,
                            month.instrument,
                            date(month.year, month.month, day),
                        ),
                        directory_only=True,
                    )
                )
        self._budget.charge_metadata(tuple(result))
        return tuple(result)

    def candidates(self, slots):
        check(self._budget.slots + len(slots) <= 8192, ReadCode.CAPACITY)
        references = self._budget.references
        for slot in slots:
            entry = self._slot(slot, directory_only=True)
            check(not entry.overflow, ReadCode.CAPACITY)
            references += entry.count
            check(references <= 32768, ReadCode.CAPACITY)
        for slot in slots:
            self._slot(slot)
        keys = tuple({r.version for s in slots for r in self._index.slots[s]})
        self._ensure_versions(keys)
        return super().candidates(slots)

    def versions(self, keys, *, exact=False):
        if exact:
            check(len(self._budget.exact_seen | set(keys)) <= 128, ReadCode.CAPACITY)
        self._ensure_versions(keys)
        return super().versions(keys, exact=exact)

    def _publication_guard(self, key):
        self._ensure_versions((key,))
        identity = content_id(key)
        row = self.ledger._sql(
            "SELECT event_count,head_id,history_digest FROM "
            "{s}.publication_heads WHERE version_id=%s",
            (identity,),
        ).fetchone()
        guard = self.ledger._sql(
            "SELECT payload FROM {s}.projection_guards WHERE version_id=%s", (identity,)
        ).fetchone()
        check(row is not None and guard is not None)
        head = None
        if row[1] is not None:
            event = self.ledger._record("publication_events", row[1])
            self._state.publications[event.key] = event
            head = event.key
        self._state.publication_heads[key] = PublicationHead(key, row[0], head, row[2])
        self._index.guards[key] = loads(guard[0])
        return super()._publication_guard(key)

    def version_guards(self, keys):
        self._ensure_versions(keys)
        return super().version_guards(keys)

    def histories(self, keys):
        self._ensure_versions(keys)
        total = self._budget.events
        # Preflight the WHOLE request before fetching any history.
        for key in keys:
            guard = self._publication_guard(key)
            check(guard.count <= 64, ReadCode.CAPACITY)
            total += guard.count
            check(total <= 2048, ReadCode.CAPACITY)
        for key in keys:
            if key not in self.loaded_histories:
                history = self.ledger._history(key, maximum=64)
                self._index.histories[key] = history
                self._state.publications.update((e.key, e) for e in history)
                self.loaded_histories.add(key)
        return super().histories(keys)

    def admission_guards(self, key):
        self._ensure_versions((key,))
        for subject in self._state.versions[key].admission_subjects:
            row = self.ledger._sql(
                "SELECT payload FROM {s}.admission_guards WHERE subject=%s", (subject,)
            ).fetchone()
            if row:
                guard = loads(row[0])
                receipt = self.ledger._receipt(
                    "admission", content_id((subject, guard.revision))
                )
                self._state.admissions[subject] = guard
                self._state.receipts[
                    ("admission", content_id((subject, guard.revision)))
                ] = receipt
        return super().admission_guards(key)

    def lineage(self, key):
        self._ensure_versions((key,))
        for receipt in self._state.versions[key].assessment.materials:
            self._state.materials[receipt.key] = self.ledger._record(
                "materials", content_id(receipt.key)
            )
        return super().lineage(key)
