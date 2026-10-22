"""Evidence selection, completion and conservative frozen-D1 input bridge."""

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Mapping

from market_data_foundation.canonical.model import require
from market_data_foundation.domain_contracts.market_universe import (
    CalendarDay,
    CalendarRevision,
    DateWindow,
    Provenance,
    QualificationStatus,
    canonical_bytes,
    content_id,
    text_fields,
)

from .model import (
    ALGORITHM_REVISION,
    MAX_DAYS,
    MAX_LINEAGE_BYTES,
    MAX_LINEAGE_REFERENCES,
    MAX_MATERIALS,
    MAX_TOTAL_BYTES,
    MAX_TOTAL_ROWS,
    Claim,
    Completion,
    DayEvidence,
    ParsedMaterial,
    State,
    aware,
    utc,
)
from .parser import parse_material


@dataclass(frozen=True)
class CompletionCutoff:
    completed_through: date | None
    first_blocked_day: date | None
    reason: str
    evidence: tuple[DayEvidence, ...]

    @property
    def revision(self) -> str:
        return content_id((ALGORITHM_REVISION, self))


@dataclass(frozen=True)
class SessionAssignment:
    status: str
    trading_days: tuple[date, ...]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class D1CalendarInput:
    calendar: CalendarRevision
    as_of: datetime
    days: tuple[DayEvidence, ...]
    material_revisions: tuple[str, ...]

    @property
    def revision(self) -> str:
        return content_id((ALGORITHM_REVISION, self))


@dataclass(frozen=True)
class CalendarEvidence:
    materials: tuple[ParsedMaterial, ...]
    _index: Mapping = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        require(
            type(self.materials) is tuple and 0 < len(self.materials) <= MAX_MATERIALS,
            "calendar_material_limit",
        )
        require(
            all(isinstance(item, ParsedMaterial) for item in self.materials),
            "parsed_material_required",
        )
        require(
            sum(len(item.raw) for item in self.materials) <= MAX_TOTAL_BYTES,
            "total_material_byte_limit",
        )
        require(
            sum(len(item.claims) for item in self.materials) <= MAX_TOTAL_ROWS,
            "total_calendar_row_limit",
        )
        # Refuse forged dataclass contents and same material identity/different bytes.
        identities = {}
        for material in self.materials:
            require(
                material == parse_material(material.raw, material.receipt),
                "parsed_material_mismatch",
            )
            previous = identities.setdefault(material.receipt.material, material)
            require(previous == material, "material_identity_collision")
        require(len(identities) == len(self.materials), "duplicate_material")
        ordered = tuple(sorted(self.materials, key=lambda item: item.revision))
        object.__setattr__(self, "materials", ordered)
        index = {}
        for material in ordered:
            for claim in material.claims:
                index.setdefault((material.market, claim.day), []).append(
                    (material, claim)
                )
        object.__setattr__(
            self,
            "_index",
            MappingProxyType({key: tuple(items) for key, items in index.items()}),
        )

    @property
    def revision(self) -> str:
        return content_id(
            (ALGORITHM_REVISION, tuple(item.revision for item in self.materials))
        )

    def resolve(self, market: str, day: date, as_of: datetime) -> DayEvidence:
        """Resolve against all supplied dates in this market, including overlaps."""
        text_fields(market)
        require(type(day) is date, "local_date_required")
        at = utc(as_of)
        return self._market_snapshot(market, at).get(day) or self._resolve_date(
            market, day, at
        )

    def _resolve_date(self, market: str, day: date, as_of: datetime) -> DayEvidence:
        return self._resolve_claims(market, day, as_of)[0]

    def _resolve_claims(
        self, market: str, day: date, as_of: datetime
    ) -> tuple[DayEvidence, tuple[tuple[ParsedMaterial, Claim], ...]]:
        """Retain active direct claims even when the date cannot be resolved."""
        text_fields(market)
        require(type(day) is date, "local_date_required")
        at = utc(as_of)
        visible = tuple(
            (mat, claim)
            for mat, claim in self._index.get((market, day), ())
            if mat.receipt.known_at is not None and utc(mat.receipt.known_at) <= at
        )
        active = tuple(
            (mat, claim) for mat, claim in visible if claim.evidence_kind.direct
        )
        refs = tuple(sorted(mat.ref(claim) for mat, claim in visible))
        revisions = tuple(sorted({mat.revision for mat, _ in visible}))
        zones = {mat.timezone for mat, _ in visible}
        timezone = next(iter(zones)) if len(zones) == 1 else None

        def result(
            state: State, reason: str, claim: Claim | None = None
        ) -> tuple[DayEvidence, tuple[tuple[ParsedMaterial, Claim], ...]]:
            cutoff = claim.complete_after_utc if claim else None
            completion = (
                Completion.UNKNOWN
                if cutoff is None
                else (Completion.COMPLETE if at >= cutoff else Completion.PENDING)
            )
            return (
                DayEvidence(
                    market,
                    day,
                    at,
                    state,
                    reason,
                    timezone,
                    claim.sessions if claim else (),
                    cutoff,
                    completion,
                    refs,
                    revisions,
                ),
                active,
            )

        if not visible:
            return result(State.UNKNOWN, "no_visible_dated_evidence")
        direct = {mat.ref(claim): (mat, claim) for mat, claim in active}
        if not direct:
            return result(State.UNKNOWN, "absence_or_sdk_is_not_calendar_evidence")
        superseded = set()
        for mat, claim in direct.values():
            for target in claim.supersedes:
                previous = direct.get(target)
                if previous is None:
                    return result(
                        State.UNKNOWN, "supersession_target_not_visible_on_same_date"
                    )
                old_mat, _ = previous
                if (
                    old_mat.timezone != mat.timezone
                    or utc(old_mat.receipt.known_at) >= utc(mat.receipt.known_at)
                    or old_mat.receipt.source != mat.receipt.source
                    or old_mat.receipt.binding != mat.receipt.binding
                    or old_mat.receipt.mode != mat.receipt.mode
                ):
                    return result(State.UNKNOWN, "invalid_supersession_clock_or_scope")
                superseded.add(target)
        active = tuple(
            (mat, claim)
            for ref, (mat, claim) in direct.items()
            if ref not in superseded
        )
        active_zones = {mat.timezone for mat, _ in active}
        meanings = {
            canonical_bytes((mat.timezone, claim.meaning)) for mat, claim in active
        }
        if len(meanings) != 1 or len(active_zones) != 1:
            return result(State.CONFLICT, "conflicting_active_calendar_claims")
        timezone = next(iter(active_zones))
        selected = active[0][1]
        if selected.state == State.UNKNOWN:
            return result(State.UNKNOWN, "explicit_unknown")
        return result(selected.state, "explicit_dated_evidence", selected)

    def snapshot(
        self, market: str, window: DateWindow, as_of: datetime
    ) -> tuple[DayEvidence, ...]:
        require(isinstance(window, DateWindow), "date_window_required")
        span = (window.end_exclusive - window.start).days
        require(span <= MAX_DAYS, "calendar_window_limit")
        aware(as_of)
        text_fields(market)
        all_days = self._market_snapshot(market, as_of)
        return tuple(
            all_days.get(day) or self._resolve_date(market, day, as_of)
            for day in (window.start + timedelta(days=offset) for offset in range(span))
        )

    def _market_snapshot(self, market: str, as_of: datetime) -> dict[date, DayEvidence]:
        resolved = tuple(
            self._resolve_claims(market, day, as_of)
            for day in sorted(day for venue, day in self._index if venue == market)
        )
        days = tuple(item for item, _ in resolved)
        intervals = sorted(
            (session.start_utc, session.end_utc, item.day)
            for item, active in resolved
            for _, claim in active
            for session in claim.sessions
        )
        furthest_end, furthest_day = None, None
        parents = {}

        def root(day: date) -> date:
            parents.setdefault(day, day)
            while parents[day] != day:
                parents[day] = parents[parents[day]]
                day = parents[day]
            return day

        for start, end, day in intervals:
            if (
                furthest_end is not None
                and start < furthest_end
                and day != furthest_day
            ):
                parents[root(day)] = root(furthest_day)
            if furthest_end is None or end > furthest_end:
                furthest_end, furthest_day = end, day
        groups = {}
        group_sizes = {}
        for item in days:
            if item.day in parents:
                group = root(item.day)
                refs, revisions = groups.setdefault(group, (set(), set()))
                refs.update(item.evidence_refs)
                revisions.update(item.material_revisions)
                group_sizes[group] = group_sizes.get(group, 0) + 1

        reference_slots, serialized_bytes = 0, 0

        def reserve(refs, revisions, count=1):
            nonlocal reference_slots, serialized_bytes
            reference_slots += count * (len(refs) + len(revisions))
            require(reference_slots <= MAX_LINEAGE_REFERENCES, "calendar_lineage_limit")
            # Exact canonical size of [evidence_refs, material_revisions]. Count
            # fanout before sorting/freezing or replacing any conflicted day.
            serialized_bytes += count * (
                3
                + sum(
                    2
                    + max(len(values) - 1, 0)
                    + sum(len(canonical_bytes(value)) for value in values)
                    for values in (refs, revisions)
                )
            )
            require(serialized_bytes <= MAX_LINEAGE_BYTES, "calendar_lineage_limit")

        for group, (refs, revisions) in groups.items():
            reserve(refs, revisions, group_sizes[group])
        for item in days:
            if item.day not in parents:
                reserve(item.evidence_refs, item.material_revisions)
        # One immutable tuple pair per component, shared by every affected date.
        lineage = {
            group: (tuple(sorted(refs)), tuple(sorted(revisions)))
            for group, (refs, revisions) in groups.items()
        }
        return {
            item.day: (
                replace(
                    item,
                    state=State.CONFLICT,
                    reason="session_assigned_to_multiple_trading_dates",
                    sessions=(),
                    complete_after_utc=None,
                    completion=Completion.UNKNOWN,
                    evidence_refs=lineage[root(item.day)][0],
                    material_revisions=lineage[root(item.day)][1],
                )
                if item.day in parents
                else item
            )
            for item in days
        }

    def completed_cutoff(
        self, market: str, window: DateWindow, as_of: datetime
    ) -> CompletionCutoff:
        """A contiguous completed prefix; never jump over an UNKNOWN/conflict date."""
        days = self.snapshot(market, window, as_of)
        completed = None
        for item in days:
            if item.completion != Completion.COMPLETE:
                reason = (
                    item.reason
                    if item.state in (State.UNKNOWN, State.CONFLICT)
                    else f"completion_{item.completion.value.lower()}"
                )
                return CompletionCutoff(completed, item.day, reason, days)
            completed = item.day
        return CompletionCutoff(completed, None, "window_completed", days)

    def locate_session(
        self, market: str, at: datetime, window: DateWindow, as_of: datetime
    ) -> SessionAssignment:
        """Use half-open sessions; outside a known session does not mean closed."""
        event = utc(at)
        days = self.snapshot(market, window, as_of)
        matches = tuple(
            item
            for item in days
            if any(
                session.start_utc <= event < session.end_utc
                for session in item.sessions
            )
        )
        refs = tuple(sorted({ref for item in matches for ref in item.evidence_refs}))
        dates = tuple(item.day for item in matches)
        if len(matches) > 1:
            return SessionAssignment("CONFLICT", dates, refs)
        if any(item.state == State.CONFLICT for item in days):
            return SessionAssignment(
                "CONFLICT",
                dates,
                tuple(
                    sorted(
                        {
                            ref
                            for item in days
                            if item.state == State.CONFLICT
                            for ref in item.evidence_refs
                        }
                    )
                ),
            )
        if any(item.state == State.UNKNOWN for item in days):
            return SessionAssignment("UNKNOWN", dates, refs)
        return SessionAssignment(
            "MATCH" if matches else "OUTSIDE_KNOWN_SESSIONS", dates, refs
        )

    def to_d1(
        self, calendar_id: str, market: str, window: DateWindow, as_of: datetime
    ) -> D1CalendarInput:
        """No admission: export UNKNOWN qualification and omit unresolved dates."""
        days = self.snapshot(market, window, as_of)
        at = utc(as_of)
        referenced = {revision for item in days for revision in item.material_revisions}
        visible = tuple(
            item
            for item in self.materials
            if item.market == market
            and (item.effective.overlaps(window) or item.revision in referenced)
            and item.receipt.known_at is not None
            and utc(item.receipt.known_at) <= at
        )
        require(bool(visible), "d1_visible_material_required")
        origins = {
            (
                item.receipt.source,
                item.receipt.binding,
                item.receipt.mode,
                item.receipt.decoder_revision,
            )
            for item in visible
        }
        require(len(origins) == 1, "d1_single_provenance_integration_required")
        zones = {item.timezone for item in visible}
        require(len(zones) == 1, "d1_single_timezone_required")
        receipt = visible[0].receipt
        publication_times = tuple(
            utc(item.receipt.source_published_at)
            for item in visible
            if item.receipt.source_published_at is not None
        )
        provenance = Provenance(
            receipt.capability,
            receipt.source,
            receipt.binding,
            receipt.decoder_revision,
            tuple(
                sorted((item.receipt.material for item in visible), key=canonical_bytes)
            ),
            max(utc(item.receipt.obtained_at) for item in visible),
            max(publication_times) if publication_times else None,
            receipt.mode,
        )
        calendar = CalendarRevision(
            calendar_id,
            "a-cal.d1-projection.v1",
            market,
            next(iter(zones)),
            window,
            at,
            tuple(
                CalendarDay(
                    item.day,
                    item.state != State.CLOSED,
                    item.state.value,
                    f"a-cal.day:{item.revision}",
                )
                for item in days
                if item.state not in (State.UNKNOWN, State.CONFLICT)
            ),
            provenance,
            QualificationStatus.UNKNOWN,
        )
        return D1CalendarInput(
            calendar, at, days, tuple(item.revision for item in visible)
        )

    def validate_d1(
        self,
        candidate: CalendarRevision,
        calendar_id: str,
        market: str,
        window: DateWindow,
        as_of: datetime,
    ) -> None:
        expected = self.to_d1(calendar_id, market, window, as_of).calendar
        require(
            isinstance(candidate, CalendarRevision)
            and canonical_bytes(candidate) == canonical_bytes(expected),
            "d1_calendar_input_mismatch",
        )
