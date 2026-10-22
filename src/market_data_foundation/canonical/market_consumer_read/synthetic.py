"""Explicit synthetic indexed projection. No canonical ledger or external effects.

Fixture construction is separate from the read port. A lock bounds each view;
there is no persistent query session or snapshot copy across HTTP pages.
"""

from contextlib import contextmanager
from datetime import timedelta
from threading import RLock

from .contracts import (
    Admission,
    Binding,
    Guard,
    MonthDirectory,
    Slot,
    SlotSummary,
    VersionGuard,
    candidate_digest,
    digest,
    encode,
    query_subjects,
    version_subjects,
)


class SyntheticProjection:
    def __init__(self, now, binding: Binding):
        self.now, self.binding = now, binding
        self.version_index, self.canonical_version_index = {}, {}
        self.slot_index, self.reverse_index, self.month_index = {}, {}, {}
        self.history_index, self.canonical_publication_index = {}, {}
        self.guard_index, self.admission_index = {}, {}
        self.content_index, self.lineage_index = {}, {}
        self.semantics = ()
        self.universe_revision = None
        self.members = ()
        self.lock = RLock()
        self.views_opened = self.views_closed = 0
        self.lookups = []

    @contextmanager
    def read_view(self, budget):
        with self.lock:
            self.views_opened += 1
            try:
                yield self
            finally:
                self.views_closed += 1

    def _read(self, name, index, keys, budget, default=None):
        budget.statement()
        self.lookups.append((name, len(keys)))
        return (
            {k: index[k] for k in keys if k in index}
            if default is None
            else {k: index.get(k, default) for k in keys}
        )

    def directories(self, keys, budget):
        return tuple(self._read("directories", self.month_index, keys, budget).values())

    def candidates(self, slots, budget):
        return self._read("candidates", self.slot_index, slots, budget, ())

    def versions(self, keys, budget):
        return self._read("versions", self.version_index, keys, budget)

    def version_slots(self, keys, budget):
        return self._read("version_slots", self.reverse_index, keys, budget)

    def canonical_versions(self, keys, budget):
        return self._read(
            "canonical_versions", self.canonical_version_index, keys, budget
        )

    def version_guards(self, keys, budget):
        return self._read("version_guards", self.guard_index, keys, budget)

    def histories(self, keys, budget):
        return self._read("histories", self.history_index, keys, budget)

    def canonical_publications(self, keys, budget):
        return self._read(
            "canonical_publications", self.canonical_publication_index, keys, budget
        )

    def admission(self, query, versions, budget):
        subjects = set(query_subjects(query))
        subjects.update(s for v in versions for s in version_subjects(v))
        guards = self._read(
            "admission", self.admission_index, tuple(sorted(subjects)), budget
        )
        return Admission(
            query.definition,
            self.semantics,
            self.universe_revision,
            self.members,
            tuple(guards.values()),
        )

    def contents(self, versions, budget):
        return self._read(
            "contents", self.content_index, tuple(v.key for v in versions), budget
        )

    def lineages(self, versions, budget):
        return self._read(
            "lineages", self.lineage_index, tuple(v.key for v in versions), budget
        )

    def seed_month(self, definition, instrument, month):
        """Explicit fixture setup only. Missing coverage is never inferred on read."""
        with self.lock:
            end = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
            days = []
            for n in range((end - month).days):
                day = month + timedelta(days=n)
                keys = self.slot_index.get(Slot(definition, instrument, day), ())
                versions = tuple(self.version_index[k] for k in keys)
                days.append(
                    (
                        day,
                        SlotSummary(
                            len(keys), candidate_digest(versions), len(keys) > 64
                        ),
                    )
                )
            self.month_index[(definition, instrument, month)] = MonthDirectory(
                definition, instrument, month, True, tuple(days)
            )

    def seed_version(self, version, content: bytes, lineage: bytes, history=()):
        with self.lock:
            self.version_index[version.key] = version
            self.canonical_version_index[version.key] = version
            self.content_index[version.key] = content
            self.lineage_index[version.key] = lineage
            slots = tuple(
                Slot(
                    version.definition,
                    version.instrument,
                    version.start + timedelta(days=n),
                )
                for n in range((version.end_exclusive - version.start).days)
            )
            self.reverse_index[version.key] = slots
            for s in slots:
                self.slot_index[s] = tuple(
                    sorted(set(self.slot_index.get(s, ())) | {version.key}, key=encode)
                )
            self.seed_history(version.key, history)
            self.seed_month(
                version.definition, version.instrument, version.start.replace(day=1)
            )

    def seed_history(self, key, history):
        with self.lock:
            self.history_index[key] = tuple(history)
            self.canonical_publication_index.update({p.key: p for p in history})
            change = (
                self.guard_index[key].change_no + 1 if key in self.guard_index else 0
            )
            self.guard_index[key] = VersionGuard(
                key, len(history), digest(tuple(history)), change
            )

    def seed_admission(self, query):
        subjects = set(query_subjects(query))
        subjects.update(
            s for v in self.version_index.values() for s in version_subjects(v)
        )
        for s in subjects:
            self.admission_index.setdefault(s, Guard(s, "synthetic-r1", 0, "QUALIFIED"))
        self.semantics = query.semantics
        self.universe_revision = query.universe_revision
        self.members = tuple(i.instrument for i in query.items)
