"""D3 Memory ledger -> real D4 Consumer port, for offline qualification only.

The D3 public port lacks canonical point reads. This deliberately named memory
adapter reads its immutable view's private canonical maps by exact key, never
enumerates/copies the ledger and never calls a writer. A database implementation
must implement D4's port independently; it must not inherit this adapter.
"""

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import UTC

from market_data_foundation.canonical.market_consumer_read import contracts as c
from market_data_foundation.canonical.market_material import (
    DailyDecoder,
    MaterialReceipt,
    assess_daily,
    read_material,
)
from market_data_foundation.canonical.market_persistence import required_admissions
from market_data_foundation.canonical.market_projection import (
    MemoryProjectionView,
    MonthKey,
    ProjectionError,
    ReadCode,
    SlotKey,
    version_metadata,
    version_slots,
)
from market_data_foundation.canonical.model import ContractError, Key, Kind
from market_data_foundation.domain_contracts.market_planning import Plan
from market_data_foundation.domain_contracts.market_universe import (
    canonical_bytes,
    content_id,
)
from market_data_foundation.integration.market_vertical import (
    validate_frozen_plan,
    validate_plan_evidence,
)

REVISION = "market.vertical.consumer.i1.r1"


@dataclass(frozen=True)
class ConsumerCodec:
    """Explicit capability decoder and pure native-field projection, no guessing.

    native_fields receives exact original row bytes, not normalized DailyBar.
    Returned names/scalars are retained as-is. This is a trusted adapter port.
    """

    decoder: DailyDecoder
    native_fields: Callable[[bytes, MaterialReceipt], dict]


def semantics(daily):
    return tuple(
        sorted(
            (f.name, getattr(daily, f.name))
            for f in fields(daily)
            if f.name
            in {
                "schema_revision",
                "price_unit",
                "volume_unit",
                "amount_unit",
                "price_basis",
            }
        )
    )


def _publication(event):
    request = event.request
    return c.Publication(
        event.key,
        request.version,
        request.occurrence,
        request.predecessor,
        request.decision.name,
        event.decision_at.astimezone(UTC),
        event.usable_at.astimezone(UTC),
        request.effective_at.astimezone(UTC) if request.effective_at else None,
        request.rule_revision,
        request.evidence,
        content_id(event),
    )


class MemoryConsumerProjection:
    def __init__(
        self,
        projection,
        ledger_binding: str,
        plans: tuple[Plan, ...],
        codecs: tuple[ConsumerCodec, ...],
    ):
        for plan in plans:
            validate_frozen_plan(plan)
        self.projection, self.ledger_binding = projection, ledger_binding
        self.codecs = {content_id(v.decoder.contract): v for v in codecs}
        c.check(len(self.codecs) == len(codecs), "DUPLICATE_CODEC")
        self.units = {unit.identity: unit for plan in plans for unit in plan.units}
        self.universes = {p.spec.universe.revision: p.spec.universe for p in plans}
        self.jobs = {p.spec.identity: p for p in plans}
        self.daily = {r.daily.definition: r.daily for p in plans for r in p.spec.routes}

    @contextmanager
    def read_view(self, budget):
        try:
            for plan in self.jobs.values():
                validate_frozen_plan(plan)
            budget.statement()
            view = self.projection.open(self.ledger_binding)
            c.check(isinstance(view, MemoryProjectionView), "MEMORY_PORT_REQUIRED", 503)
            yield _View(view, self, budget)
        except ProjectionError as exc:
            raise c.ReadError(
                exc.code.value,
                503
                if exc.code
                in {
                    ReadCode.UNAVAILABLE,
                    ReadCode.NOT_READY,
                    ReadCode.BINDING,
                }
                else 422,
            ) from None
        except ContractError:
            raise c.ReadError("INDEX_INCONSISTENT", 503) from None


class _View:
    def __init__(self, view, owner, budget):
        self.view, self.owner, self.budget = view, owner, budget
        # Deliberate memory-only interface delta; never a writer or new snapshot.
        self.state, self.store = view._state, view._store
        self.now = view.now.astimezone(UTC)
        g = view.generation
        self.binding = c.Binding(
            g.ledger_binding,
            g.schema_revision,
            g.algorithm_revision + ":" + REVISION,
            g.generation,
            g.ready,
        )
        self.descriptors, self.blobs, self.lineage_blobs, self.history = {}, {}, {}, {}
        self.seen, self.material_bytes = set(), 0

    def _charge(self, budget):
        c.check(budget is self.budget, "PORT_BUDGET_MISMATCH", 503)
        budget.statement()

    def _reserve_content(self, amount):
        self.material_bytes += amount
        c.cap(self.material_bytes, self.budget.limits.content, "material_validation")

    def _commit(self, operation, key, payload, result):
        receipt = self.state.receipts.get((operation, content_id(key)))
        c.check(
            receipt is not None
            and receipt.operation == operation
            and receipt.identity == content_id(key)
            and receipt.payload_digest == content_id(payload)
            and receipt.result_digest == content_id(result),
            "INDEX_INCONSISTENT",
        )

    def _descriptor(self, key):
        if key in self.descriptors:
            return self.descriptors[key]
        self.seen.add(key)
        c.cap(len(self.seen), self.budget.limits.versions, "adapter_versions")
        metadata = self.view.versions((key,))[0]
        version = self.state.versions[key]
        self._commit("version", key, version, version)
        assessment = version.assessment
        unit, contract = assessment.unit, assessment.contract
        c.check(metadata == version_metadata(version), "INDEX_INCONSISTENT")
        c.check(self.owner.units.get(unit.identity) == unit, "UNREGISTERED_UNIT")
        c.check(
            set(required_admissions(version)) <= set(version.admission_subjects),
            "ADMISSION_INCONSISTENT",
        )
        codec = self.owner.codecs.get(content_id(contract))
        c.check(codec is not None, "UNREGISTERED_CONSUMER_CODEC")
        receipts = self.view.lineage(key)
        plan = self.owner.jobs.get(receipts[0].job) if receipts else None
        c.check(plan is not None, "LINEAGE_SCOPE_MISMATCH")
        validate_plan_evidence(plan, unit, receipts, assessment.evidence)
        # Assessment replay and native projection each independently read bytes.
        self._reserve_content(2 * sum(r.byte_length for r in receipts))
        for receipt in receipts:
            c.check(
                receipt.requested_at is not None
                and receipt.obtained_at is not None
                and receipt.requested_at.astimezone(UTC)
                <= receipt.obtained_at.astimezone(UTC)
                <= version.frozen_at.astimezone(UTC)
                <= self.now,
                "MATERIAL_CLOCK_UNVERIFIED",
            )
            self._commit("material", receipt.key, receipt, receipt)
            plan = self.owner.jobs.get(receipt.job)
            c.check(plan is not None and unit in plan.units, "LINEAGE_SCOPE_MISMATCH")
            shards = {s.identity: s for s in plan.shards}
            shard = shards.get(receipt.shard)
            c.check(
                shard is not None
                and shard.instrument == unit.instrument
                and shard.route.source == receipt.source
                and shard.route.binding == receipt.binding
                and shard.route.mapping_revision == receipt.mapping_revision,
                "LINEAGE_SCOPE_MISMATCH",
            )
        verified = assess_daily(
            unit,
            receipts,
            assessment.evidence,
            codec.decoder,
            self.store,
            assessment.evaluated_at,
        )
        c.check(
            canonical_bytes(verified) == canonical_bytes(assessment),
            "ASSESSMENT_REPLAY_MISMATCH",
        )
        self._normalize_payload(version, codec, receipts)
        blob, lineage = self.blobs[key], self.lineage_blobs[key]
        lineage_keys = tuple(self._lineage_records(version, receipts))
        value = c.Version(
            key,
            unit.definition,
            unit.instrument,
            unit.window.start,
            unit.window.end_exclusive,
            c.byte_digest(blob),
            len(blob),
            lineage_keys,
            c.byte_digest(lineage),
            len(lineage),
            contract.source,
            contract.binding,
            contract.mapping_revision,
            unit.calendar_revision,
            unit.universe_revision,
            semantics(contract.daily),
            version.digest,
            version.range_key,
        )
        self.descriptors[key] = value
        return value

    def _lineage_records(self, version, receipts):
        contract = version.assessment.contract
        # Public reference records derived from the validated receipt graph.
        # These do not claim registration in the older canonical kernel.
        records = {
            contract.source: (content_id(contract.source), ()),
            contract.binding: (content_id(contract), (contract.source,)),
        }
        for receipt in receipts:
            acquisition = Key(Kind.ACQUISITION, receipt.attempt)
            records[acquisition] = (
                content_id(
                    (
                        receipt.job,
                        receipt.shard,
                        receipt.attempt,
                        receipt.request,
                        receipt.pages,
                    )
                ),
                (receipt.source, receipt.binding),
            )
            records[receipt.key] = (
                content_id(receipt),
                (acquisition, version.assessment.unit.definition),
            )
        return dict(sorted(records.items(), key=lambda pair: c.encode(pair[0])))

    def _normalize_payload(self, version, codec, receipts):
        by_key = {r.key: r for r in receipts}
        raw = {r.key: read_material(self.store, r) for r in receipts}
        rows = []
        for evidence in version.assessment.evidence:
            native = {}
            if evidence.classification.value == "OBSERVED_VALID":
                receipt = by_key[evidence.material]
                locator = next(
                    r for r in receipt.rows if r.identity == evidence.source_row_locator
                )
                native = codec.native_fields(
                    raw[receipt.key][locator.offset : locator.offset + locator.length],
                    receipt,
                )
                c.check(
                    type(native) is dict
                    and bool(native)
                    and all(type(k) is str and bool(k) for k in native)
                    and all(
                        v is None or type(v) in (str, int, bool, float)
                        for v in native.values()
                    ),
                    "INVALID_NATIVE_PROJECTION",
                )
            rows.append(
                {
                    "day": evidence.point.day.isoformat(),
                    "classification": evidence.classification.value,
                    "raw": native,
                    "evidence": list(evidence.references),
                }
            )
        records = self._lineage_records(version, receipts)
        c.cap(len(records), self.budget.limits.lineage_per_version, "version_lineage")
        self.blobs[version.key] = c.encode(rows)
        self._reserve_content(len(self.blobs[version.key]))
        c.cap(
            len(self.blobs[version.key]),
            self.budget.limits.version_content,
            "version_content",
        )
        self.lineage_blobs[version.key] = c.encode(
            [
                {"key": key, "record_digest": record, "references": refs}
                for key, (record, refs) in records.items()
            ]
        )
        self.budget.add_metadata(
            (self.blobs[version.key], self.lineage_blobs[version.key])
        )

    def directories(self, keys, budget):
        self._charge(budget)
        result = []
        for definition, instrument, month in keys:
            entries = self.view.month_directory(
                (MonthKey(definition, instrument, month.year, month.month),)
            )
            days = []
            for entry in entries:
                # Overflow counts are useful to exact readers; do not enumerate.
                descriptors = ()
                if entry.count and not entry.overflow:
                    candidates = self.view.candidates((entry.slot,))[0]
                    descriptors = tuple(self._descriptor(r.version) for r in candidates)
                days.append(
                    (
                        entry.slot.day,
                        c.SlotSummary(
                            entry.count, c.candidate_digest(descriptors), entry.overflow
                        ),
                    )
                )
            result.append(
                c.MonthDirectory(definition, instrument, month, True, tuple(days))
            )
        return tuple(result)

    def candidates(self, slots, budget):
        self._charge(budget)
        values = self.view.candidates(
            tuple(SlotKey(s.definition, s.instrument, s.day) for s in slots)
        )
        return {
            s: tuple(r.version for r in rows)
            for s, rows in zip(slots, values, strict=True)
        }

    def versions(self, keys, budget):
        self._charge(budget)
        return {
            key: self._descriptor(key) for key in keys if key in self.state.versions
        }

    def canonical_versions(self, keys, budget):
        self._charge(budget)
        # _descriptor verified the canonical commit, assessment and material bytes
        # from this same immutable view before caching the normalized descriptor.
        return {key: self._descriptor(key) for key in keys}

    def version_slots(self, keys, budget):
        self._charge(budget)
        self.view.versions(keys, exact=True)
        return {
            key: tuple(
                c.Slot(s.definition, s.instrument, s.day)
                for s in version_slots(self.state.versions[key])
            )
            for key in keys
        }

    def _histories(self, keys):
        missing = tuple(key for key in keys if key not in self.history)
        if missing:
            for key, events in zip(missing, self.view.histories(missing), strict=True):
                for event in events:
                    self._commit("publication", event.key, event.request, event)
                    c.check(
                        self.state.occurrences.get(event.request.occurrence)
                        == event.key,
                        "INDEX_INCONSISTENT",
                    )
                self.history[key] = tuple(_publication(e) for e in events)
        return {key: self.history[key] for key in keys}

    def version_guards(self, keys, budget):
        self._charge(budget)
        guards = self.view.version_guards(keys)
        # Preflight all history counts before normalization/digest translation.
        for guard in guards:
            c.cap(guard.count, budget.limits.history_per_version, "version_history")
        c.cap(sum(g.count for g in guards), budget.limits.events, "events")
        histories = self._histories(keys)
        return {
            g.version: c.VersionGuard(
                g.version, g.count, c.digest(histories[g.version]), g.change
            )
            for g in guards
        }

    def histories(self, keys, budget):
        self._charge(budget)
        return self._histories(keys)

    def canonical_publications(self, keys, budget):
        self._charge(budget)
        result = {}
        for key in keys:
            event = self.state.publications.get(key)
            c.check(event is not None, "INDEX_INCONSISTENT")
            self._commit("publication", key, event.request, event)
            result[key] = _publication(event)
        return result

    def _guard(self, subject):
        guard = self.state.admissions.get(subject)
        if guard is not None:
            self._commit(
                "admission",
                (subject, guard.revision),
                (subject, guard.revision, guard.allowed),
                guard,
            )
        return guard

    def admission(self, query, versions, budget):
        self._charge(budget)
        mapped = {}

        def add(target, subjects):
            mapped.setdefault(target, set()).update(subjects)

        definition = "definition:" + content_id(query.definition)
        for target in c.query_subjects(query):
            add(target, (definition,))
        if query.universe_revision:
            add(
                c.subject("universe", query.universe_revision),
                ("universe:" + query.universe_revision,),
            )
        for version in versions:
            # Keep all extra D3 dependencies on the exact Version guard. A denied
            # sibling Version must not contaminate the shared Source guard.
            guards = self.view.admission_guards(version.key)
            add(c.subject("version", version.key), (g.subject for g in guards))
            add(
                c.subject("source", version.source),
                ("source:" + content_id(version.source),),
            )
            add(
                c.subject("binding", version.binding),
                ("binding:" + content_id(version.binding),),
            )
            # D4 names mapping by revision alone. Union the corresponding exact
            # D3 Source/Binding/Mapping subjects instead of taking the first one.
            add(
                c.subject("mapping", version.mapping_revision),
                (
                    "mapping:"
                    + content_id(
                        (version.source, version.binding, version.mapping_revision)
                    ),
                ),
            )
            add(
                c.subject("calendar", version.calendar_revision),
                ("calendar:" + version.calendar_revision,),
            )
            for key in version.lineage_keys:
                add(c.subject("retention", key), ("retention:market.daily.d3.r1",))
        translated = []
        for target, subjects in sorted(mapped.items()):
            guards = tuple(self._guard(s) for s in sorted(subjects))
            status = (
                "UNKNOWN"
                if any(g is None for g in guards)
                else "QUALIFIED"
                if all(g.allowed for g in guards)
                else "UNQUALIFIED"
            )
            translated.append(
                c.Guard(
                    target,
                    content_id(guards),
                    sum(g.change for g in guards if g),
                    status,
                )
            )
        universe = self.owner.universes.get(query.universe_revision)
        daily = self.owner.daily.get(query.definition)
        c.check(daily is not None, "UNREGISTERED_DEFINITION")
        return c.Admission(
            query.definition,
            semantics(daily),
            query.universe_revision,
            tuple(m.instrument for m in universe.members) if universe else (),
            tuple(translated),
        )

    def contents(self, versions, budget):
        self._charge(budget)
        result = {}
        for version in versions:
            # Recheck current publication/admission and original material readback.
            assessment = self.state.versions[version.key].assessment
            self._reserve_content(
                sum(r.byte_length for r in assessment.materials)
                + len(assessment.content)
                + len(self.blobs[version.key])
            )
            self.view.content(version.key)
            result[version.key] = self.blobs[version.key]
        return result

    def lineages(self, versions, budget):
        self._charge(budget)
        return {v.key: self.lineage_blobs[v.key] for v in versions}
