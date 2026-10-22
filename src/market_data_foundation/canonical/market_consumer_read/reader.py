"""Complete bounded reconstruction, per-item selection and fail-closed paging."""

from dataclasses import replace
from datetime import date, datetime, timedelta
from time import monotonic

from market_data_foundation.canonical.model import Key, Kind
from market_data_foundation.domain_contracts.market_universe import InstrumentIdentity

from .contracts import (
    WIRE,
    Budget,
    Item,
    Limits,
    Query,
    ReadError,
    Slot,
    byte_digest,
    candidate_digest,
    cap,
    check,
    check_elapsed,
    digest,
    encode,
    public,
    query_subjects,
    strict_json,
    version_subjects,
)
from .cursor import CursorCodec, utc

AVAILABLE = {"PUBLISH", "RESTORE"}
TRANSITIONS = {
    "PUBLISH": {"WITHDRAW", "SUPERSEDE"},
    "RESTORE": {"WITHDRAW", "SUPERSEDE"},
    "WITHDRAW": {"RESTORE", "SUPERSEDE"},
    "SUPERSEDE": set(),
}
REASONS = {
    "OBSERVED_VALID",
    "MARKET_CLOSED",
    "NOT_YET_LISTED",
    "DELISTED_OR_TERMINATED",
    "NO_TRADE_CONFIRMED",
    "COLLECTION_FAILED",
    "MISSING_EXPECTED",
    "QUALITY_REJECTED",
    "UNKNOWN",
}
COMPLETE = {
    "OBSERVED_VALID",
    "MARKET_CLOSED",
    "NOT_YET_LISTED",
    "DELISTED_OR_TERMINATED",
    "NO_TRADE_CONFIRMED",
}


def validate_query(q: Query, limits: Limits):
    check(
        isinstance(q, Query)
        and q.wire_revision == WIRE
        and isinstance(q.definition, Key)
        and q.definition.kind == Kind.DEFINITION,
        "INVALID_QUERY",
        400,
    )
    check(type(q.items) is tuple and bool(q.items), "INVALID_QUERY", 400)
    cap(len(q.items), limits.items, "items")
    check(all(isinstance(i, Item) for i in q.items), "INVALID_QUERY", 400)
    check(len({i.item_id for i in q.items}) == len(q.items), "DUPLICATE_ITEM_ID", 400)
    check(
        q.mode in {"exact_versions", "as_of"}
        and type(q.page_size) is int
        and 0 < q.page_size <= limits.page_units,
        "INVALID_QUERY",
        400,
    )
    check(
        type(q.semantics) is tuple
        and all(
            type(p) is tuple
            and len(p) == 2
            and all(type(v) is str and bool(v) for v in p)
            for p in q.semantics
        )
        and tuple(sorted(q.semantics)) == q.semantics
        and len(dict(q.semantics)) == len(q.semantics),
        "INVALID_QUERY",
        400,
    )
    check(
        q.universe_revision is None
        or (type(q.universe_revision) is str and bool(q.universe_revision)),
        "INVALID_QUERY",
        400,
    )
    if q.at is not None:
        check(isinstance(q.at, datetime), "INVALID_AS_OF", 400)
        encode(q.at)
    check(q.mode == "as_of" or q.at is None, "INVALID_QUERY", 400)
    total = 0
    for i in q.items:
        check(
            type(i.item_id) is str
            and 0 < len(i.item_id) <= 128
            and isinstance(i.instrument, InstrumentIdentity)
            and type(i.start_date) is date
            and type(i.end_date_exclusive) is date,
            "INVALID_QUERY",
            400,
        )
        days = (i.end_date_exclusive - i.start_date).days
        check(days > 0, "INVALID_DATE_RANGE", 400)
        cap(days, limits.days, "days")
        total += days
        check(
            type(i.exact_versions) is tuple
            and len(set(i.exact_versions)) == len(i.exact_versions)
            and all(
                isinstance(k, Key) and k.kind == Kind.VERSION for k in i.exact_versions
            ),
            "INVALID_EXACT_LIST",
            400,
        )
        check(
            bool(i.exact_versions) == (q.mode == "exact_versions"),
            "INVALID_EXACT_LIST",
            400,
        )
    cap(total, limits.slots, "logical_slots")
    cap(
        len({k for i in q.items for k in i.exact_versions}),
        limits.selected_versions,
        "exact_versions",
    )
    cap(len(encode(q)), limits.query, "query_bytes")


def complete_map(result, keys):
    check(set(result) == set(keys), "INDEX_INCONSISTENT")
    return result


def active(history, at):
    eligible = [
        p
        for p in history
        if p.decision_at <= at
        and p.usable_at <= at
        and (p.effective_at is None or p.effective_at <= at)
    ]
    return eligible[-1] if eligible else None


def validate_history(version, history, guard):
    check(
        guard.version == version.key
        and type(guard.change_no) is int
        and guard.change_no >= 0
        and len(history) == guard.history_count
        and digest(history) == guard.history_digest,
        "INDEX_INCONSISTENT",
    )
    check(
        len({p.key for p in history}) == len(history)
        and len({p.occurrence for p in history}) == len(history)
        and len({p.decision_at for p in history}) == len(history),
        "PUBLICATION_STATE_INVALID",
    )
    previous = None
    for p in history:
        encode((p.decision_at, p.usable_at, p.effective_at))
        check(
            p.key.kind == Kind.PUBLICATION
            and p.version == version.key
            and bool(p.occurrence and p.rule_revision and p.canonical_digest)
            and bool(p.evidence)
            and p.usable_at >= p.decision_at
            and p.decision in TRANSITIONS
            and (
                (
                    p.decision == "SUPERSEDE"
                    and p.supersedes_version is not None
                    and p.supersedes_version.kind == Kind.VERSION
                    and p.supersedes_version != version.key
                )
                or (p.decision != "SUPERSEDE" and p.supersedes_version is None)
            ),
            "PUBLICATION_STATE_INVALID",
        )
        if previous is None:
            check(
                p.predecessor is None and p.decision == "PUBLISH",
                "PUBLICATION_STATE_INVALID",
            )
        else:
            check(
                p.predecessor == previous.key
                and p.decision_at > previous.decision_at
                and p.decision in TRANSITIONS[previous.decision],
                "PUBLICATION_STATE_INVALID",
            )
        previous = p


class MarketReader:
    def __init__(
        self,
        projection,
        binding,
        cursor: CursorCodec,
        clock,
        limits: Limits = Limits(),
        timer=monotonic,
    ):
        # Limits may be reduced for a deployment, never silently raised.
        baseline = Limits()
        from dataclasses import fields

        check(
            all(
                0 < getattr(limits, f.name) <= getattr(baseline, f.name)
                for f in fields(limits)
            ),
            "INVALID_LIMIT_CONFIGURATION",
            503,
        )
        self.projection, self.binding, self.cursor = projection, binding, cursor
        self.clock, self.limits, self.timer = clock, limits, timer

    def read(self, query: Query, cursor: str | None = None) -> dict:
        validate_query(query, self.limits)
        self.cursor.configured()
        qd = digest(query)
        try:
            host_now = self.clock()
            check(isinstance(host_now, datetime), "CLOCK_UNAVAILABLE", 503)
            encode(host_now)
        except Exception:
            raise ReadError("CLOCK_UNAVAILABLE", 503) from None
        token = (
            self.cursor.verify(cursor, qd, host_now, self.limits)
            if cursor is not None
            else None
        )
        budget = Budget(self.limits, self.timer)
        try:
            with self.projection.read_view(budget) as view:
                budget.transaction_started = self.timer()
                encode(view.now)
                check(
                    abs((view.now - host_now).total_seconds()) <= self.cursor.skew,
                    "CLOCK_UNAVAILABLE",
                    503,
                )
                check(
                    view.binding.ledger == self.binding.ledger
                    and view.binding.schema_revision == self.binding.schema_revision
                    and view.binding.algorithm_revision
                    == self.binding.algorithm_revision,
                    "PROJECTION_BINDING_MISMATCH",
                    503,
                )
                check(view.binding.ready, "INDEX_NOT_READY", 503)
                if token:
                    check(
                        token["binding"]
                        == digest(replace(view.binding, generation="", ready=True)),
                        "CURSOR_BINDING_CHANGED",
                        409,
                    )
                    check(
                        token["generation"] == view.binding.generation,
                        "CURSOR_GENERATION_CHANGED",
                        409,
                    )
                a = utc(token["a"]) if token else (query.at or view.now)
                t0 = utc(token["t0"]) if token else view.now
                check(
                    a <= t0 and view.now >= t0 - timedelta(seconds=self.cursor.skew),
                    "INVALID_AS_OF",
                    400,
                )
                result = self._rebuild(view, query, a, budget)
                response = self._page(query, qd, view.binding, a, t0, result, token)
                budget.check_time()
            check_elapsed(
                self.timer() - budget.started,
                self.limits.request_seconds,
                "request_time",
            )
            return response
        except ReadError:
            raise
        except Exception:
            raise ReadError("STORAGE_UNAVAILABLE", 503) from None

    def _call(self, budget, method, *args):
        start, before = self.timer(), budget.calls
        result = method(*args, budget)
        check(budget.calls > before, "PORT_BUDGET_NOT_CHARGED", 503)
        check_elapsed(
            self.timer() - start, self.limits.statement_seconds, "statement_time"
        )
        budget.check_time()
        budget.add_metadata(result)
        return result

    def _rebuild(self, view, q, a, b):
        limits = self.limits
        slots = tuple(
            dict.fromkeys(
                Slot(q.definition, i.instrument, day)
                for i in q.items
                for day in i.days()
            )
        )
        months = tuple(
            dict.fromkeys(
                (s.definition, s.instrument, s.day.replace(day=1)) for s in slots
            )
        )
        cap(len(months), limits.months, "months")
        directories = self._call(b, view.directories, months)
        index = {(d.definition, d.instrument, d.month): d for d in directories}
        check(
            len(index) == len(directories) and set(index) <= set(months),
            "INDEX_INCONSISTENT",
        )
        check(
            set(index) == set(months) and all(d.ready for d in directories),
            "INDEX_NOT_COVERED",
        )
        summaries = {}
        for key, directory in index.items():
            days = dict(directory.days)
            next_month = (key[2].replace(day=28) + timedelta(days=4)).replace(day=1)
            expected = {
                key[2] + timedelta(days=n) for n in range((next_month - key[2]).days)
            }
            check(
                len(days) == len(directory.days) and set(days) == expected,
                "INDEX_INCONSISTENT",
            )
            for day, summary in days.items():
                check(
                    type(summary.count) is int and summary.count >= 0,
                    "INDEX_INCONSISTENT",
                )
                summaries[Slot(key[0], key[1], day)] = summary
        refs = {}
        if q.mode == "as_of":
            for s in slots:
                cap(summaries[s].count, limits.candidates_per_slot, "slot_candidates")
                check(not summaries[s].overflow, "READ_CAPACITY_EXCEEDED")
            cap(
                sum(summaries[s].count for s in slots),
                limits.candidate_refs,
                "candidate_references",
            )
            for start in range(0, len(slots), 512):
                batch = slots[start : start + 512]
                found = self._call(b, view.candidates, batch)
                complete_map(found, batch)
                for s, keys in found.items():
                    check(
                        type(keys) is tuple
                        and len(set(keys)) == len(keys)
                        and len(keys) == summaries[s].count,
                        "INDEX_INCONSISTENT",
                    )
                refs.update({s: tuple(sorted(found[s], key=encode)) for s in batch})
            keys = tuple(sorted({k for ks in refs.values() for k in ks}, key=encode))
        else:
            keys = tuple(
                sorted({k for i in q.items for k in i.exact_versions}, key=encode)
            )
        cap(len(keys), limits.versions, "candidate_versions")
        versions = self._call(b, view.versions, keys)
        if q.mode == "as_of":
            complete_map(versions, keys)
        else:
            check(set(versions) <= set(keys), "INDEX_INCONSISTENT")
        present = tuple(k for k in keys if k in versions)
        canonical = self._call(b, view.canonical_versions, present)
        check(versions == canonical, "INDEX_INCONSISTENT")
        for k, v in versions.items():
            check(
                v.key == k
                and k.kind == Kind.VERSION
                and v.start < v.end_exclusive
                and (v.end_exclusive - v.start).days <= 31
                and v.start.replace(day=1)
                == (v.end_exclusive - timedelta(days=1)).replace(day=1)
                and v.source.kind == Kind.SOURCE
                and v.binding.kind == Kind.BINDING
                and bool(
                    v.canonical_digest
                    and v.mapping_revision
                    and v.calendar_revision
                    and v.range_key
                ),
                "INDEX_INCONSISTENT",
            )
            check(
                len(set(v.lineage_keys)) == len(v.lineage_keys)
                and {Kind.SOURCE, Kind.BINDING, Kind.ACQUISITION, Kind.MATERIAL}
                <= {key.kind for key in v.lineage_keys}
                and v.source in v.lineage_keys
                and v.binding in v.lineage_keys,
                "LINEAGE_INVALID",
            )
            cap(len(v.lineage_keys), limits.lineage_per_version, "version_lineage")
        cap(
            len({r for v in versions.values() for r in v.lineage_keys}),
            limits.lineage,
            "lineage_references",
        )
        if q.mode == "as_of":
            for s, ks in refs.items():
                check(
                    all(
                        versions[k].definition == s.definition
                        and versions[k].instrument == s.instrument
                        and versions[k].contains(s.day)
                        for k in ks
                    )
                    and candidate_digest(tuple(versions[k] for k in ks))
                    == summaries[s].digest,
                    "INDEX_INCONSISTENT",
                )
        else:
            reverse = self._call(b, view.version_slots, present)
            complete_map(reverse, present)
            for k in present:
                v = versions[k]
                expected = tuple(
                    Slot(v.definition, v.instrument, v.start + timedelta(days=n))
                    for n in range((v.end_exclusive - v.start).days)
                )
                check(reverse[k] == expected, "INDEX_INCONSISTENT")
        guards = self._call(b, view.version_guards, present)
        complete_map(guards, present)
        for g in guards.values():
            check(
                type(g.history_count) is int and g.history_count >= 0,
                "INDEX_INCONSISTENT",
            )
            cap(g.history_count, limits.history_per_version, "version_history")
        cap(sum(g.history_count for g in guards.values()), limits.events, "events")
        histories = self._call(b, view.histories, present)
        complete_map(histories, present)
        for k in present:
            cap(len(histories[k]), limits.history_per_version, "version_history")
            check(
                len(histories[k]) == guards[k].history_count
                and digest(histories[k]) == guards[k].history_digest,
                "INDEX_INCONSISTENT",
            )
        all_events = tuple(p for k in present for p in histories[k])
        cap(len(all_events), limits.events, "events")
        check(
            len({p.key for p in all_events}) == len(all_events),
            "PUBLICATION_STATE_INVALID",
        )
        canonical_events = self._call(
            b, view.canonical_publications, tuple(p.key for p in all_events)
        )
        check(canonical_events == {p.key: p for p in all_events}, "INDEX_INCONSISTENT")
        history_bad = set()
        for k in present:
            # Bad canonical publication semantics fail the item; incomplete projection
            # counts/digests fail the request, including on the first page.
            try:
                validate_history(versions[k], histories[k], guards[k])
            except ReadError as exc:
                if exc.code == "INDEX_INCONSISTENT":
                    raise
                history_bad.add(k)
        admission = self._call(b, view.admission, q, tuple(versions.values()))
        check(
            admission.definition == q.definition
            and admission.universe_revision == q.universe_revision,
            "ADMISSION_INCONSISTENT",
            503,
        )
        gate_map = {g.subject: g for g in admission.guards}
        check(
            len(gate_map) == len(admission.guards)
            and all(
                g.status in {"QUALIFIED", "UNQUALIFIED", "UNKNOWN"}
                and type(g.change_no) is int
                and g.change_no >= 0
                and bool(g.revision)
                for g in admission.guards
            ),
            "ADMISSION_INCONSISTENT",
            503,
        )
        required = set(query_subjects(q))
        required.update(s for v in versions.values() for s in version_subjects(v))
        check(required <= set(gate_map), "ADMISSION_INCONSISTENT", 503)
        gate_digest = digest(tuple(gate_map[s] for s in sorted(required)))
        states, selections, units, selected = [], [], [], set()
        current = {
            k: active(histories[k], view.now) if k not in history_bad else None
            for k in present
        }
        historical = {
            k: active(histories[k], a) if k not in history_bad else None
            for k in present
        }
        for ordinal, item in enumerate(q.items):
            state = {
                "item_id": item.item_id,
                "instrument": public(item.instrument),
                "start_date": item.start_date.isoformat(),
                "end_date_exclusive": item.end_date_exclusive.isoformat(),
                "status": "VERIFIED",
                "qualification": "QUALIFIED",
                "publication": "UNPUBLISHED",
                "coverage": "UNKNOWN",
            }
            choice = []
            error = None
            qualification = "QUALIFIED"
            if admission.semantics != q.semantics:
                error = "SEMANTICS_MISMATCH"
            elif q.universe_revision and item.instrument not in admission.members:
                error = "UNIVERSE_MEMBERSHIP_MISMATCH"
            elif any(gate_map[s].status != "QUALIFIED" for s in query_subjects(q)):
                error = "QUALIFICATION_FAILED"
                qualification = (
                    "UNQUALIFIED"
                    if any(
                        gate_map[s].status == "UNQUALIFIED" for s in query_subjects(q)
                    )
                    else "UNKNOWN"
                )
            if q.mode == "exact_versions":
                for k in item.exact_versions:
                    if k not in versions:
                        error = error or "VERSION_NOT_FOUND"
                    elif (
                        versions[k].definition != q.definition
                        or versions[k].instrument != item.instrument
                        or not (
                            versions[k].start < item.end_date_exclusive
                            and item.start_date < versions[k].end_exclusive
                        )
                    ):
                        error = error or "VERSION_SCOPE_MISMATCH"
            for day in item.days():
                candidates = (
                    refs[Slot(q.definition, item.instrument, day)]
                    if q.mode == "as_of"
                    else tuple(
                        k
                        for k in item.exact_versions
                        if k in versions and versions[k].contains(day)
                    )
                )
                if any(k in history_bad for k in candidates):
                    error = error or "PUBLICATION_STATE_INVALID"
                available = [
                    k
                    for k in candidates
                    if historical.get(k) and historical[k].decision in AVAILABLE
                ]
                if q.mode == "exact_versions" and len(candidates) > 1:
                    error = error or "EXACT_OVERLAP"
                if len(available) > 1:
                    error = error or "AMBIGUOUS"
                if len(available) == 1:
                    k = available[0]
                    v = versions[k]
                    if not current[k] or current[k].decision not in AVAILABLE:
                        error = error or "CURRENT_PUBLICATION_UNAVAILABLE"
                        state["publication"] = (
                            "WITHDRAWN"
                            if current[k] and current[k].decision == "WITHDRAW"
                            else "SUPERSEDED"
                            if current[k]
                            else "UNPUBLISHED"
                        )
                    if v.semantics != q.semantics:
                        error = error or "SEMANTICS_MISMATCH"
                    if (
                        q.universe_revision
                        and v.universe_revision != q.universe_revision
                    ):
                        error = error or "UNIVERSE_REVISION_MISMATCH"
                    bad = [
                        gate_map[s].status
                        for s in version_subjects(v)
                        if gate_map[s].status != "QUALIFIED"
                    ]
                    if bad:
                        error = error or "QUALIFICATION_FAILED"
                        qualification = (
                            "UNQUALIFIED" if "UNQUALIFIED" in bad else "UNKNOWN"
                        )
                    choice.append((day, k, historical[k]))
                else:
                    reason = "MISSING" if not candidates else "UNPUBLISHED"
                    if any(
                        historical.get(k) and historical[k].decision == "WITHDRAW"
                        for k in candidates
                    ):
                        reason = "WITHDRAWN"
                    elif any(
                        historical.get(k) and historical[k].decision == "SUPERSEDE"
                        for k in candidates
                    ):
                        reason = "SUPERSEDED"
                    if q.mode == "exact_versions" and candidates:
                        error = error or {
                            "WITHDRAWN": "VERSION_WITHDRAWN",
                            "SUPERSEDED": "VERSION_SUPERSEDED",
                        }.get(reason, "EXACT_PUBLICATION_UNAVAILABLE")
                        state["publication"] = reason
                    choice.append((day, None, reason))
            state.update(status=error or "VERIFIED", qualification=qualification)
            selections.append((ordinal, tuple(choice), state.copy()))
            if error is None:
                selected.update(k for _, k, _ in choice if k)
            states.append(state)
        cap(len(selected), limits.selected_versions, "selected_versions")
        cap(
            sum(versions[k].content_length for k in selected),
            limits.content,
            "content_bytes",
        )
        for k in selected:
            v = versions[k]
            check(v.content_length >= 0 and v.lineage_length >= 0, "INDEX_INCONSISTENT")
            cap(v.content_length, limits.version_content, "version_content")
            cap(len(v.lineage_keys), limits.lineage_per_version, "version_lineage")
        cap(
            len({r for k in selected for r in versions[k].lineage_keys}),
            limits.lineage,
            "lineage_references",
        )
        cap(
            b.metadata + sum(versions[k].lineage_length for k in selected),
            limits.metadata,
            "metadata",
        )
        content, content_errors = {}, {}
        chosen = tuple(versions[k] for k in sorted(selected, key=encode))
        start, before = self.timer(), b.calls
        blobs = view.contents(chosen, b)
        check(b.calls > before, "PORT_BUDGET_NOT_CHARGED", 503)
        check_elapsed(self.timer() - start, limits.statement_seconds, "statement_time")
        complete_map(blobs, selected)
        check(all(type(raw) is bytes for raw in blobs.values()), "INTEGRITY_FAILURE")
        b.content += sum(len(raw) for raw in blobs.values())
        cap(b.content, limits.content, "content_bytes")
        lineages = self._call(b, view.lineages, chosen)
        complete_map(lineages, selected)
        for k in sorted(selected, key=encode):
            v = versions[k]
            try:
                content[k] = self._content(v, blobs[k], lineages[k])
            except ReadError as exc:
                if exc.code in {"INTEGRITY_FAILURE", "LINEAGE_INVALID"}:
                    content_errors[k] = exc.code
                else:
                    raise
        for ordinal, choice, _ in selections:
            state = states[ordinal]
            if state["status"] != "VERIFIED":
                continue
            errors = [content_errors[k] for _, k, _ in choice if k in content_errors]
            if errors:
                state["status"] = errors[0]
                continue
            item_units = []
            reasons = []
            for day, k, p in choice:
                if k:
                    v = versions[k]
                    row, lineage = content[k]
                    data = row[day.isoformat()]
                    reasons.append(data["classification"])
                    unit = {
                        "key": [ordinal, day.isoformat()],
                        "item_id": state["item_id"],
                        "local_date": day.isoformat(),
                        "version": public(k),
                        "publication": public(p),
                        "content_digest": v.content_digest,
                        "source": public(v.source),
                        "binding": public(v.binding),
                        "mapping_revision": v.mapping_revision,
                        "range_key": v.range_key,
                        "version_window": {
                            "start_date": v.start.isoformat(),
                            "end_date_exclusive": v.end_exclusive.isoformat(),
                        },
                        "calendar_revision": v.calendar_revision,
                        "lineage": lineage,
                        **data,
                    }
                else:
                    reasons.append("UNKNOWN")
                    unit = {
                        "key": [ordinal, day.isoformat()],
                        "item_id": state["item_id"],
                        "local_date": day.isoformat(),
                        "version": None,
                        "publication": p,
                        "classification": "UNKNOWN",
                        "raw": {},
                        "evidence": [],
                    }
                cap(len(encode(unit)), limits.unit, "logical_unit")
                item_units.append(unit)
            state["coverage"] = (
                "COMPLETE"
                if all(r in COMPLETE for r in reasons)
                else "UNKNOWN"
                if "UNKNOWN" in reasons
                else "PARTIAL"
            )
            published = sum(k is not None for _, k, _ in choice)
            state["publication"] = (
                "PUBLISHED"
                if published == len(choice)
                else "PARTIAL"
                if published
                else next((p for _, _, p in choice if type(p) is str), "UNPUBLISHED")
            )
            units.extend(item_units)
        cap(len(encode(states)), limits.fixed, "fixed_metadata")
        cap(len(encode((states, units))), limits.logical, "logical_result")
        c = digest(
            (
                tuple(
                    (k, versions.get(k), guards.get(k), histories.get(k)) for k in keys
                ),
                tuple((s, refs[s]) for s in slots) if refs else (),
                historical,
            )
        )
        s = digest((selections, states))
        g = digest(
            (gate_digest, tuple((k, current[k]) for k in sorted(selected, key=encode)))
        )
        r = digest((q.definition, q.universe_revision, q.semantics, states, units))
        return {"c": c, "s": s, "g": g, "r": r, "states": states, "units": units}

    def _content(self, v, raw, lineage):
        check(type(raw) is bytes, "INTEGRITY_FAILURE")
        cap(len(raw), self.limits.version_content, "version_content")
        check(
            len(raw) == v.content_length and byte_digest(raw) == v.content_digest,
            "INTEGRITY_FAILURE",
        )
        check(
            type(lineage) is bytes
            and len(lineage) == v.lineage_length
            and byte_digest(lineage) == v.lineage_digest,
            "LINEAGE_INVALID",
        )
        try:
            rows, evidence = strict_json(raw), strict_json(lineage)
            check(
                encode(rows) == raw
                and type(rows) is list
                and len(rows) == (v.end_exclusive - v.start).days,
                "INTEGRITY_FAILURE",
            )
            check(
                encode(evidence) == lineage and type(evidence) is list,
                "LINEAGE_INVALID",
            )
            # Public, explicit evidence schema forbids arbitrary private locators.
            allowed = {"key", "record_digest", "references"}
            check(
                all(
                    type(e) is dict
                    and set(e) == allowed
                    and type(e["record_digest"]) is str
                    and e["record_digest"]
                    and type(e["references"]) is list
                    for e in evidence
                ),
                "LINEAGE_INVALID",
            )
            keys = [encode(e["key"]) for e in evidence]
            check(
                len(keys) == len(set(keys))
                and set(keys) == {encode(k) for k in v.lineage_keys}
                and all(
                    encode(ref) in set(keys) | {encode(v.definition)}
                    for e in evidence
                    for ref in e["references"]
                ),
                "LINEAGE_INVALID",
            )
            by_day = {}
            for row in rows:
                check(
                    type(row) is dict
                    and set(row) == {"day", "classification", "raw", "evidence"}
                    and type(row["day"]) is str
                    and type(row["classification"]) is str
                    and row["classification"] in REASONS
                    and type(row["raw"]) is dict
                    and type(row["evidence"]) is list
                    and all(type(e) is str and bool(e) for e in row["evidence"])
                    and (row["classification"] == "UNKNOWN" or row["evidence"])
                    and (
                        bool(row["raw"]) == (row["classification"] == "OBSERVED_VALID")
                    )
                    and all(
                        x is None or type(x) in (str, int, float, bool)
                        for x in row["raw"].values()
                    ),
                    "INTEGRITY_FAILURE",
                )
                check(row["day"] not in by_day, "INTEGRITY_FAILURE")
                by_day[row["day"]] = row
            check(
                set(by_day)
                == {
                    (v.start + timedelta(days=n)).isoformat() for n in range(len(rows))
                },
                "INTEGRITY_FAILURE",
            )
            return by_day, evidence
        except ReadError:
            raise ReadError("INTEGRITY_FAILURE") from None

    def _page(self, q, qd, binding, a, t0, result, token):
        units = result["units"]
        if token:
            check(token["g"] == result["g"], "CURSOR_QUALIFICATION_CHANGED", 409)
            check(
                all(token[k] == result[k] for k in ("c", "s", "r"))
                and token["total"] == len(units),
                "CURSOR_SELECTION_CHANGED",
                409,
            )
            pos = token["pos"]
            check(
                0 < pos < len(units) and units[pos - 1]["key"] == token["last"],
                "INVALID_CURSOR_POSITION",
                400,
            )
        else:
            pos = 0
        payload = {
            "binding": digest(replace(binding, generation="", ready=True)),
            "q": qd,
            "a": public(a),
            "t0": public(t0),
            "generation": binding.generation,
            **{k: result[k] for k in ("c", "s", "g", "r")},
            "iat": token["iat"] if token else int(t0.timestamp()),
            "exp": token["exp"] if token else int(t0.timestamp()) + self.cursor.ttl,
        }
        base = {
            "wire_revision": WIRE,
            "query_digest": qd,
            "read_as_of": public(a),
            "first_view_at": public(t0),
            "generation": binding.generation,
            "definition": public(q.definition),
            "universe_revision": q.universe_revision,
            "semantics": public(q.semantics),
            "status": (
                "PARTIAL_FAILURE"
                if any(s["status"] != "VERIFIED" for s in result["states"])
                else "OK"
            ),
            "items": result["states"],
            "total_units": len(units),
        }

        def build(end):
            next_cursor = (
                self.cursor.sign(
                    {
                        **payload,
                        "pos": end,
                        "last": units[end - 1]["key"],
                        "total": len(units),
                    },
                    self.limits,
                )
                if end < len(units)
                else None
            )
            return {
                **base,
                "range": [pos, end],
                "units": units[pos:end],
                "next_cursor": next_cursor,
                "page_id": digest(
                    (
                        qd,
                        public(t0),
                        result["c"],
                        result["s"],
                        result["g"],
                        result["r"],
                        pos,
                        end,
                    )
                ),
            }

        end = min(pos + q.page_size, len(units))
        response = build(end)
        # A smaller prefix always has <= bytes (cursor is present for every
        # non-final prefix). Binary search uses the exact encoded envelope.
        if len(encode(response)) > self.limits.page:
            low, high, best = pos + 1, end - 1, None
            while low <= high:
                mid = (low + high) // 2
                candidate = build(mid)
                if len(encode(candidate)) <= self.limits.page:
                    best, low = candidate, mid + 1
                else:
                    high = mid - 1
            if best is None:
                raise ReadError("READ_CAPACITY_EXCEEDED", 422, "page_bytes")
            response = best
        check(not units or response["range"][1] > pos, "READ_CAPACITY_EXCEEDED")
        return response
