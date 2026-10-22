"""Invented schedules only: these dates are not exchange calendar assertions."""

import copy
import json
import socket
import subprocess
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from r5_i6_market_fixture import job, universe

from market_data_foundation.candidates.market_calendar_evidence import (
    CalendarEvidence,
    Completion,
    MaterialReceipt,
    State,
    parse_material,
    parse_tdx_workday,
)
from market_data_foundation.candidates.market_calendar_evidence import (
    adapter as cal_adapter,
)
from market_data_foundation.candidates.market_calendar_evidence.model import (
    MAX_BYTES,
    MAX_DAYS,
    MAX_LINEAGE_BYTES,
    MAX_LINEAGE_REFERENCES,
    MAX_MATERIALS,
    MAX_ROWS,
    MAX_TOTAL_BYTES,
    MAX_TOTAL_ROWS,
)
from market_data_foundation.candidates.market_calendar_evidence.tdx import (
    TDX_COMMIT,
    TDX_VERSION,
)
from market_data_foundation.canonical.model import ContractError, Key, Kind
from market_data_foundation.domain_contracts.market_coverage import (
    CoverageClass,
    classify_coverage,
)
from market_data_foundation.domain_contracts.market_planning import plan_job
from market_data_foundation.domain_contracts.market_universe import (
    DateWindow,
    EvidenceMode,
    QualificationStatus,
    canonical_bytes,
)
from market_data_foundation.qualification.contracts import Capability

DAY = date(2024, 1, 2)
KNOWN = datetime(2024, 1, 1, tzinfo=UTC)
AFTER = datetime(2024, 12, 1, tzinfo=UTC)
SOURCE = Key(Kind.SOURCE, "synthetic.calendar", "v1")
BINDING = Key(Kind.BINDING, "synthetic.calendar", "v1")


def session(start, end, name="day", start_fold=None, end_fold=None):
    return dict(id=name, start=start, end=end, start_fold=start_fold, end_fold=end_fold)


def row(day="2024-01-02", state="OPEN", **changes):
    opened = state in ("OPEN", "HALF_DAY")
    result = dict(
        claim_id=f"invented-{day}-{state}",
        day=day,
        state=state,
        evidence_kind="NOTICE",
        arrangement=f"synthetic-{state}",
        source_ref="synthetic-notice:line-1",
        sessions=[session(f"{day}T09:00:00", f"{day}T15:00:00")] if opened else [],
        complete_after=f"{day}T15:30:00" if state != "UNKNOWN" else None,
        complete_after_fold=None,
        supersedes=[],
    )
    result.update(changes)
    return result


def document(
    rows, market="SH", tz="Asia/Shanghai", start="2024-01-02", end="2024-01-06"
):
    return dict(
        schema="mdf.calendar.evidence.v1",
        market=market,
        timezone=tz,
        effective=dict(start=start, end_exclusive=end),
        days=rows,
    )


def receipt(raw, known=KNOWN, mode=EvidenceMode.SYNTHETIC, **changes):
    checksum = sha256(raw).hexdigest()
    result = dict(
        source=SOURCE,
        binding=BINDING,
        material=Key(Kind.MATERIAL, f"synthetic:{checksum}:{known}"),
        capability=Capability.TRADING_CALENDAR,
        mode=mode,
        producer_operation="explicit.synthetic.calendar-material",
        upstream_version="synthetic.v1",
        upstream_revision="invented-not-upstream",
        decoder_revision="a-cal.explicit-json.v1",
        source_ref="synthetic-in-memory-document",
        raw_sha256=checksum,
        raw_length=len(raw),
        obtained_at=KNOWN - timedelta(hours=2),
        received_at=KNOWN - timedelta(hours=1),
        known_at=known,
    )
    result.update(changes)
    return MaterialReceipt(**result)


def parse_doc(doc, known=KNOWN, **changes):
    raw = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()
    return parse_material(raw, receipt(raw, known, **changes))


def material(rows=None, known=KNOWN, mode=EvidenceMode.SYNTHETIC, **doc_changes):
    return parse_doc(
        document([row()] if rows is None else rows, **doc_changes), known, mode=mode
    )


def book(rows=None, **changes):
    return CalendarEvidence((material(rows, **changes),))


def window(start="2024-01-02", end="2024-01-03"):
    return DateWindow(date.fromisoformat(start), date.fromisoformat(end))


def test_exact_raw_provenance_and_immutable_replay():
    mat = material([row(native_fields={"unverified_economic_field": 42.5})])
    ledger = CalendarEvidence((mat,))
    assert (
        mat.raw
        == json.dumps(
            document([row(native_fields={"unverified_economic_field": 42.5})]),
            separators=(",", ":"),
        ).encode()
    )
    assert mat.receipt.raw_sha256 == sha256(mat.raw).hexdigest()
    assert mat.claims[0].row_locator == "/days/0"
    output = ledger.to_d1("invented", "SH", window(), AFTER)
    assert output.days[0].evidence_refs == (mat.ref(mat.claims[0]),)
    assert output.days[0].material_revisions == (mat.revision,)
    assert output.calendar.provenance.materials == (mat.receipt.material,)
    assert output.calendar.qualification == QualificationStatus.UNKNOWN
    assert ledger.to_d1("invented", "SH", window(), AFTER).revision == output.revision
    with pytest.raises(FrozenInstanceError):
        mat.market = "SZ"
    with pytest.raises(TypeError):
        ledger._index[("SH", DAY)] = ()


def test_sh_sz_bj_have_no_calendar_inheritance_or_weekday_heuristic():
    ledger = CalendarEvidence(
        (
            material([row(state="CLOSED")], market="SH"),
            material([row()], market="SZ"),
            material([], market="BJ"),
        )
    )
    assert ledger.resolve("SH", DAY, AFTER).state == State.CLOSED
    assert ledger.resolve("SZ", DAY, AFTER).state == State.OPEN
    assert ledger.resolve("BJ", DAY, AFTER).state == State.UNKNOWN
    assert ledger.resolve("UNSPECIFIED", DAY, AFTER).state == State.UNKNOWN
    assert ledger.resolve("SH", date(2024, 1, 3), AFTER).state == State.UNKNOWN
    weekend = book([row("2024-01-06")], start="2024-01-06", end="2024-01-07")
    assert weekend.resolve("SH", date(2024, 1, 6), AFTER).state == State.OPEN


@pytest.mark.parametrize("kind", ["MISSING_BARS", "SDK_WORKDAY"])
@pytest.mark.parametrize("declared", ["OPEN", "CLOSED"])
def test_missing_bars_and_workday_cannot_establish_calendar_state(kind, declared):
    ledger = book(
        [
            row(
                state=declared,
                evidence_kind=kind,
                native_fields={"is_workday": False, "bars": []},
            )
        ]
    )
    answer = ledger.resolve("SH", DAY, AFTER)
    assert answer.state == State.UNKNOWN
    assert answer.complete_after_utc is None
    assert answer.completion == Completion.UNKNOWN
    assert answer.reason == "absence_or_sdk_is_not_calendar_evidence"
    assert ledger.to_d1("invented", "SH", window(), AFTER).calendar.days == ()


def test_missing_bars_do_not_overrule_a_dated_notice():
    ledger = CalendarEvidence(
        (
            material([row()]),
            material([row(state="CLOSED", evidence_kind="MISSING_BARS")]),
        )
    )
    answer = ledger.resolve("SH", DAY, AFTER)
    assert answer.state == State.OPEN
    assert len(answer.evidence_refs) == 2


def test_hk_half_day_special_closure_and_completion_boundary():
    half = row(
        state="HALF_DAY",
        sessions=[session("2024-01-02T09:30:00", "2024-01-02T12:00:00")],
        complete_after="2024-01-02T12:30:00",
    )
    ledger = book(
        [half, row("2024-01-03", "CLOSED", arrangement="synthetic-special-closure")],
        market="HK",
        tz="Asia/Hong_Kong",
    )
    before = datetime(2024, 1, 2, 4, 29, 59, tzinfo=UTC)
    boundary = before + timedelta(seconds=1)
    assert ledger.resolve("HK", DAY, before).completion == Completion.PENDING
    assert ledger.resolve("HK", DAY, boundary).completion == Completion.COMPLETE
    assert ledger.resolve("HK", date(2024, 1, 3), AFTER).state == State.CLOSED
    assert (
        ledger.locate_session(
            "HK", datetime(2024, 1, 2, 4, tzinfo=UTC), window(), AFTER
        ).status
        == "OUTSIDE_KNOWN_SESSIONS"
    )
    exported = ledger.to_d1("invented-hk", "HK", window(end="2024-01-04"), AFTER)
    assert [(item.is_open, item.arrangement) for item in exported.calendar.days] == [
        (True, "HALF_DAY"),
        (False, "CLOSED"),
    ]


def test_futures_friday_night_has_explicit_monday_trading_day():
    monday = row(
        "2024-01-08",
        sessions=[
            session("2024-01-05T21:00:00", "2024-01-06T02:30:00", "night"),
            session("2024-01-08T09:00:00", "2024-01-08T15:00:00", "day"),
        ],
    )
    ledger = book(
        [monday],
        market="SHFE.synthetic-contract-session",
        start="2024-01-08",
        end="2024-01-09",
    )
    scope = window("2024-01-08", "2024-01-09")
    event = datetime(2024, 1, 5, 13, tzinfo=UTC)
    found = ledger.locate_session(
        "SHFE.synthetic-contract-session", event, scope, AFTER
    )
    assert found.status == "MATCH"
    assert found.trading_days == (date(2024, 1, 8),)
    assert (
        ledger.resolve("SHFE.synthetic-contract-session", date(2024, 1, 5), AFTER).state
        == State.UNKNOWN
    )
    assert (
        ledger.locate_session(
            "SHFE.synthetic-contract-session",
            datetime(2024, 1, 6, 13, tzinfo=UTC),
            scope,
            AFTER,
        ).status
        == "OUTSIDE_KNOWN_SESSIONS"
    )
    assert (
        ledger.resolve(
            "SHFE.synthetic-contract-session",
            date(2024, 1, 8),
            datetime(2024, 1, 6, 3, tzinfo=UTC),
        ).completion
        == Completion.PENDING
    )


def test_foreign_local_session_changes_utc_across_dst_without_host_clock():
    rows = [
        row(
            day,
            sessions=[session(f"{day}T09:30:00", f"{day}T16:00:00")],
            complete_after=f"{day}T16:15:00",
        )
        for day in ("2024-03-08", "2024-03-11")
    ]
    ledger = book(
        rows,
        market="XNYS.synthetic",
        tz="America/New_York",
        start="2024-03-08",
        end="2024-03-12",
    )
    prior = ledger.resolve("XNYS.synthetic", date(2024, 3, 8), AFTER)
    after = ledger.resolve("XNYS.synthetic", date(2024, 3, 11), AFTER)
    assert prior.sessions[0].start_utc == datetime(2024, 3, 8, 14, 30, tzinfo=UTC)
    assert after.sessions[0].start_utc == datetime(2024, 3, 11, 13, 30, tzinfo=UTC)
    assert after.complete_after_utc == datetime(2024, 3, 11, 20, 15, tzinfo=UTC)
    assert (
        ledger.resolve(
            "XNYS.synthetic",
            date(2024, 3, 11),
            AFTER.astimezone(timezone(timedelta(hours=9))),
        )
        == after
    )


@pytest.mark.parametrize("fold,utc_hour", [(0, 5), (1, 6)])
def test_dst_repeated_local_hour_requires_explicit_fold(fold, utc_hour):
    day = "2024-11-03"
    repeated = row(
        day,
        sessions=[
            session(
                f"{day}T01:15:00", f"{day}T01:45:00", start_fold=fold, end_fold=fold
            )
        ],
        complete_after=f"{day}T02:00:00",
    )
    ledger = book(
        [repeated],
        market="synthetic.DST",
        tz="America/New_York",
        start=day,
        end="2024-11-04",
    )
    output = ledger.resolve("synthetic.DST", date(2024, 11, 3), AFTER)
    assert output.sessions[0].start_utc.hour == utc_hour
    assert output.sessions[0].start_fold == fold


def repeated_clock(minute, fold):
    return datetime(
        2024, 11, 3, 1, minute, tzinfo=ZoneInfo("America/New_York"), fold=fold
    )


@pytest.mark.parametrize("representation", ["local", "utc", "mixed"])
@pytest.mark.parametrize(
    "obtained,received,known,published,classification",
    [
        ((10, 1), (20, 1), (30, 0), None, "known_at_backdated"),
        ((10, 1), (50, 0), (30, 1), None, "received_before_obtained"),
        ((45, 0), (15, 1), (30, 1), (15, 1), "publication_after_obtained"),
        ((30, 0), (30, 1), (30, 0), None, "known_at_backdated"),
        ((45, 0), (15, 1), (30, 1), (30, 0), None),
        ((10, 1), (20, 1), (30, 1), (50, 0), None),
    ],
)
def test_receipt_chronology_uses_instants_across_dst_fold(
    representation, obtained, received, known, published, classification
):
    clocks = {
        name: repeated_clock(*value) if value is not None else None
        for name, value in zip(
            ("obtained_at", "received_at", "known_at", "source_published_at"),
            (obtained, received, known, published),
        )
    }
    clocks = {
        name: value.astimezone(UTC)
        if value is not None
        and (representation == "utc" or representation == "mixed" and i % 2)
        else value
        for i, (name, value) in enumerate(clocks.items())
    }
    if classification:
        with pytest.raises(ContractError, match=f"^{classification}$"):
            receipt(b"{}", **clocks)
    else:
        result = receipt(b"{}", **clocks)
        normalized = replace(
            result,
            **{
                name: value.astimezone(UTC) if value is not None else None
                for name, value in clocks.items()
            },
        )
        assert canonical_bytes(result) == canonical_bytes(normalized)


@pytest.mark.parametrize("representation", ["local", "utc", "old_utc", "new_utc"])
@pytest.mark.parametrize(
    "old_clock,new_clock,expected",
    [
        ((45, 0), (15, 1), State.CLOSED),
        ((15, 1), (45, 0), State.UNKNOWN),
        ((15, 0), (15, 1), State.CLOSED),
        ((15, 1), (15, 0), State.UNKNOWN),
        ((45, 0), (45, 0), State.UNKNOWN),
    ],
)
def test_supersession_orders_dst_folds_by_instant(
    representation, old_clock, new_clock, expected
):
    old_at, new_at = repeated_clock(*old_clock), repeated_clock(*new_clock)
    if representation in ("utc", "old_utc"):
        old_at = old_at.astimezone(UTC)
    if representation in ("utc", "new_utc"):
        new_at = new_at.astimezone(UTC)
    old = material(known=old_at)
    correction = material(
        [row(state="CLOSED", supersedes=[old.ref(old.claims[0])])], known=new_at
    )
    ledger = CalendarEvidence((old, correction))
    answer = ledger.resolve("SH", DAY, AFTER)
    assert answer.state == expected
    assert answer.completion == (
        Completion.COMPLETE if expected == State.CLOSED else Completion.UNKNOWN
    )
    if expected == State.UNKNOWN:
        assert answer.reason == "invalid_supersession_clock_or_scope"
    else:
        before = max(old_at.astimezone(UTC), new_at.astimezone(UTC)) - timedelta(
            microseconds=1
        )
        assert ledger.resolve("SH", DAY, before).state == State.OPEN
    assert set(answer.evidence_refs) == {
        old.ref(old.claims[0]),
        correction.ref(correction.claims[0]),
    }


def test_d1_provenance_aggregates_dst_instants_and_matches_utc_replay():
    mats = tuple(
        parse_doc(
            document([row(day)]),
            known=repeated_clock(30, 1),
            obtained_at=repeated_clock(obtained, fold),
            received_at=repeated_clock(obtained, fold),
            source_published_at=repeated_clock(published, fold),
        )
        for day, obtained, published, fold in (
            ("2024-01-02", 45, 40, 0),
            ("2024-01-03", 15, 10, 1),
        )
    )
    normalized = tuple(
        parse_material(
            mat.raw,
            replace(
                mat.receipt,
                obtained_at=mat.receipt.obtained_at.astimezone(UTC),
                received_at=mat.receipt.received_at.astimezone(UTC),
                known_at=mat.receipt.known_at.astimezone(UTC),
                source_published_at=mat.receipt.source_published_at.astimezone(UTC),
            ),
        )
        for mat in mats
    )
    scope = window(end="2024-01-04")
    local = CalendarEvidence(mats).to_d1("invented", "SH", scope, AFTER)
    replay = CalendarEvidence(normalized).to_d1("invented", "SH", scope, AFTER)
    assert local.calendar.provenance.obtained_at == datetime(
        2024, 11, 3, 6, 15, tzinfo=UTC
    )
    assert local.calendar.provenance.source_published_at == datetime(
        2024, 11, 3, 6, 10, tzinfo=UTC
    )
    assert canonical_bytes(local) == canonical_bytes(replay)
    assert local.revision == replay.revision


@pytest.mark.parametrize(
    "day,start,end,classification",
    [
        ("2024-03-10", "02:15:00", "03:30:00", "nonexistent_local_clock"),
        ("2024-11-03", "01:15:00", "02:30:00", "ambiguous_local_clock_requires_fold"),
    ],
)
def test_dst_nonexistent_and_unresolved_repeated_clocks_fail(
    day, start, end, classification
):
    schedule = row(
        day, sessions=[session(f"{day}T{start}", f"{day}T{end}")], complete_after=None
    )
    with pytest.raises(ContractError, match=f"^{classification}$"):
        material([schedule], tz="America/New_York", start=day, end="2024-11-04")


def test_unknown_conflict_and_missing_completion_stop_contiguous_cutoff():
    ledger = book([row(), row("2024-01-04")])
    result = ledger.completed_cutoff("SH", window(end="2024-01-05"), AFTER)
    assert result.completed_through == DAY
    assert result.first_blocked_day == date(2024, 1, 3)
    assert len(result.evidence) == 3
    assert result.evidence[-1].completion == Completion.COMPLETE
    no_cutoff = book([row(complete_after=None)])
    assert no_cutoff.resolve("SH", DAY, AFTER).state == State.OPEN
    assert no_cutoff.completed_cutoff("SH", window(), AFTER).completed_through is None
    assert (
        no_cutoff.completed_cutoff("SH", window(), AFTER).reason == "completion_unknown"
    )
    unknown = book([row(state="UNKNOWN")])
    assert unknown.resolve("SH", DAY, AFTER).reason == "explicit_unknown"


def test_same_date_conflict_is_retained_until_explicit_later_supersession():
    old = material([row()])
    later = KNOWN + timedelta(days=2)
    correction_row = row(state="CLOSED")
    revised = material([correction_row], known=later)
    conflicted = CalendarEvidence((old, revised))
    assert conflicted.resolve("SH", DAY, KNOWN).state == State.OPEN
    conflict = conflicted.resolve("SH", DAY, later)
    assert conflict.state == State.CONFLICT
    assert len(conflict.evidence_refs) == 2
    assert conflict.completion == Completion.UNKNOWN
    assert conflicted.to_d1("invented", "SH", window(), AFTER).calendar.days == ()
    corrected = material(
        [row(state="CLOSED", supersedes=[old.ref(old.claims[0])])], known=later
    )
    ledger = CalendarEvidence((corrected, old))
    assert ledger.resolve("SH", DAY, later).state == State.CLOSED
    assert len(ledger.resolve("SH", DAY, later).evidence_refs) == 2
    assert ledger.resolve("SH", DAY, KNOWN).evidence_refs == (old.ref(old.claims[0]),)
    assert ledger.resolve("SH", DAY, KNOWN).state == State.OPEN
    assert ledger.revision == CalendarEvidence((old, corrected)).revision


@pytest.mark.parametrize(
    "case",
    [
        "dangling",
        "same_clock",
        "future_target",
        "different_day",
        "different_source",
        "different_binding",
        "different_mode",
    ],
)
def test_supersession_needs_visible_same_scope_and_strictly_later_clock(case):
    old = material([row()])
    target = old.ref(old.claims[0])
    changes = {}
    known = KNOWN + timedelta(days=2)
    rows = [row(state="CLOSED", supersedes=[target])]
    if case == "dangling":
        rows[0]["supersedes"] = ["absent:claim"]
    elif case == "same_clock":
        known = KNOWN
    elif case == "future_target":
        known = KNOWN - timedelta(minutes=1)
    elif case == "different_day":
        rows = [row("2024-01-03", "CLOSED", supersedes=[target])]
    elif case == "different_source":
        changes["source"] = Key(Kind.SOURCE, "other.synthetic", "v1")
    elif case == "different_binding":
        changes["binding"] = Key(Kind.BINDING, "other.synthetic", "v1")
    elif case == "different_mode":
        changes["mode"] = EvidenceMode.RETAINED
    newer = parse_doc(document(rows), known=known, **changes)
    result = CalendarEvidence((old, newer)).resolve(
        "SH", date.fromisoformat(rows[0]["day"]), known
    )
    assert result.state == State.UNKNOWN
    assert result.completion == Completion.UNKNOWN


def test_consistent_duplicate_dates_preserve_both_row_locators():
    rows = [row(claim_id="first"), row(claim_id="second")]
    ledger = book(rows)
    answer = ledger.resolve("SH", DAY, AFTER)
    assert answer.state == State.OPEN
    assert len(answer.evidence_refs) == 2
    assert [item.row_locator for item in ledger.materials[0].claims] == [
        "/days/0",
        "/days/1",
    ]


def test_unknown_known_at_and_future_notice_never_backfill_evidence():
    for known in (None, AFTER + timedelta(days=1)):
        ledger = book(known=known)
        output = ledger.resolve("SH", DAY, AFTER)
        assert output.state == State.UNKNOWN
        assert output.evidence_refs == ()
        assert output.material_revisions == ()
        with pytest.raises(ContractError, match="^d1_visible_material_required$"):
            ledger.to_d1("invented", "SH", window(), AFTER)


def test_overlapping_cross_date_sessions_are_not_exported_or_completed():
    rows = [
        row(),
        row(
            "2024-01-03",
            sessions=[session("2024-01-02T14:00:00", "2024-01-03T15:00:00")],
        ),
    ]
    ledger = book(rows)
    scope = window(end="2024-01-04")
    result = ledger.snapshot("SH", scope, AFTER)
    assert all(item.state == State.CONFLICT for item in result)
    assert all(item.completion == Completion.UNKNOWN for item in result)
    assert len(result[0].evidence_refs) == 2
    assert ledger.completed_cutoff("SH", scope, AFTER).completed_through is None
    assert ledger.to_d1("invented", "SH", scope, AFTER).calendar.days == ()
    assert (
        ledger.locate_session(
            "SH", datetime(2024, 1, 2, 6, 30, tzinfo=UTC), scope, AFTER
        ).status
        == "CONFLICT"
    )


def test_cross_date_conflict_cannot_be_hidden_by_narrowing_query_window():
    first = material([row()])
    second = material(
        [
            row(
                "2024-01-03",
                sessions=[session("2024-01-02T14:00:00", "2024-01-03T15:00:00")],
            )
        ],
        start="2024-01-03",
        end="2024-01-04",
    )
    ledger = CalendarEvidence((first, second))
    assert ledger.resolve("SH", DAY, AFTER).state == State.CONFLICT
    exported = ledger.to_d1("invented", "SH", window(), AFTER)
    assert exported.calendar.days == ()
    assert len(exported.days[0].evidence_refs) == 2
    assert set(exported.calendar.provenance.materials) == {
        first.receipt.material,
        second.receipt.material,
    }


@pytest.mark.parametrize("start", ["2024-01-02", "2024-01-03"])
def test_same_date_conflict_cannot_hide_cross_date_overlap(start):
    first = material([row()], end="2024-01-03")
    competing = material(
        [row(sessions=[session("2024-01-02T10:00:00", "2024-01-02T15:00:00")])],
        end="2024-01-03",
    )
    next_day = material(
        [
            row(
                "2024-01-03",
                sessions=[session("2024-01-02T14:00:00", "2024-01-03T15:00:00")],
            )
        ],
        start="2024-01-03",
        end="2024-01-04",
    )
    ledger = CalendarEvidence((first, competing, next_day))
    scope = window(start, "2024-01-04")
    expected_refs = tuple(sorted(mat.ref(mat.claims[0]) for mat in ledger.materials))
    expected_revisions = tuple(sorted(mat.revision for mat in ledger.materials))
    for item in ledger.snapshot("SH", scope, AFTER):
        assert item.state == State.CONFLICT
        assert item.reason == "session_assigned_to_multiple_trading_dates"
        assert item.completion == Completion.UNKNOWN
        assert item.sessions == ()
        assert item.complete_after_utc is None
        assert item.evidence_refs == expected_refs
        assert item.material_revisions == expected_revisions
        assert ledger.resolve("SH", item.day, AFTER) == item
    cutoff = ledger.completed_cutoff("SH", scope, AFTER)
    assert cutoff.completed_through is None
    assert cutoff.first_blocked_day == scope.start
    assignment = ledger.locate_session(
        "SH", datetime(2024, 1, 2, 6, 30, tzinfo=UTC), scope, AFTER
    )
    assert assignment.status == "CONFLICT"
    assert assignment.evidence_refs == expected_refs
    exported = ledger.to_d1("invented", "SH", scope, AFTER)
    assert exported.calendar.days == ()
    assert set(exported.calendar.provenance.materials) == {
        mat.receipt.material for mat in ledger.materials
    }
    ledger.validate_d1(exported.calendar, "invented", "SH", scope, AFTER)


def test_invalid_supersession_cannot_hide_unresolved_dates_sessions():
    ledger = book(
        [
            row(supersedes=["missing:claim"]),
            row(
                "2024-01-03",
                sessions=[session("2024-01-02T14:00:00", "2024-01-03T15:00:00")],
            ),
        ]
    )
    answer = ledger.resolve("SH", date(2024, 1, 3), AFTER)
    assert answer.state == State.CONFLICT
    assert answer.completion == Completion.UNKNOWN
    assert len(answer.evidence_refs) == 2


def test_valid_supersession_retires_sessions_from_cross_date_conflict_detection():
    old = material([row()])
    later = KNOWN + timedelta(days=2)
    correction = material(
        [row(state="CLOSED", supersedes=[old.ref(old.claims[0])])], known=later
    )
    following = material(
        [
            row(
                "2024-01-03",
                sessions=[session("2024-01-02T14:00:00", "2024-01-03T15:00:00")],
            )
        ]
    )
    ledger = CalendarEvidence((old, correction, following))
    scope = window("2024-01-03", "2024-01-04")
    assert ledger.resolve("SH", scope.start, KNOWN).state == State.CONFLICT
    answer = ledger.resolve("SH", scope.start, AFTER)
    assert answer.state == State.OPEN
    assert answer.completion == Completion.COMPLETE
    assert answer.evidence_refs == (following.ref(following.claims[0]),)
    assert (
        ledger.locate_session(
            "SH", datetime(2024, 1, 2, 6, 30, tzinfo=UTC), scope, AFTER
        ).status
        == "MATCH"
    )


@pytest.mark.parametrize("kind", ["MISSING_BARS", "SDK_WORKDAY"])
def test_nondirect_sessions_do_not_create_cross_date_conflicts(kind):
    ledger = book(
        [
            row(evidence_kind=kind),
            row(
                "2024-01-03",
                sessions=[session("2024-01-02T14:00:00", "2024-01-03T15:00:00")],
            ),
        ]
    )
    assert ledger.resolve("SH", date(2024, 1, 3), AFTER).state == State.OPEN


def test_distinct_overlap_groups_keep_their_own_lineage():
    rows = [
        row(),
        row(
            "2024-01-03",
            sessions=[session("2024-01-02T14:00:00", "2024-01-03T15:00:00")],
        ),
        row("2024-01-04"),
        row(
            "2024-01-05",
            sessions=[session("2024-01-04T14:00:00", "2024-01-05T15:00:00")],
        ),
    ]
    ledger = book(rows)
    prior, later = (
        ledger.resolve("SH", DAY, AFTER),
        ledger.resolve("SH", date(2024, 1, 4), AFTER),
    )
    assert prior.state == later.state == State.CONFLICT
    assert len(prior.evidence_refs) == len(later.evidence_refs) == 2
    assert set(prior.evidence_refs).isdisjoint(later.evidence_refs)


def overlap_chain(count, start=DAY):
    return [
        row(
            (day := start + timedelta(days=offset)).isoformat(),
            claim_id=str(offset),
            arrangement="x",
            source_ref="x",
            sessions=[
                session(f"{day - timedelta(days=1)}T14:00:00", f"{day}T15:00:00")
            ],
        )
        for offset in range(count)
    ]


def test_overlap_component_shares_immutable_complete_lineage():
    count = 128
    mat = material(overlap_chain(count), end=(DAY + timedelta(days=count)).isoformat())
    ledger = CalendarEvidence((mat,))
    result = ledger.snapshot("SH", DateWindow(DAY, DAY + timedelta(days=count)), AFTER)
    expected = tuple(sorted(mat.ref(claim) for claim in mat.claims))
    assert len(result) == count
    assert all(item.state == State.CONFLICT for item in result)
    assert all(item.evidence_refs == expected for item in result)
    assert all(item.evidence_refs is result[0].evidence_refs for item in result)
    assert all(
        item.material_revisions is result[0].material_revisions for item in result
    )
    assert result[0].material_revisions == (mat.revision,)
    assert ledger.resolve("SH", DAY, AFTER) == result[0]


@pytest.fixture(scope="module")
def excessive_overlap_material():
    return material(overlap_chain(2000), end=(DAY + timedelta(days=2000)).isoformat())


@pytest.mark.parametrize("operation", ["resolve", "snapshot", "cutoff", "locate", "d1"])
def test_2000_date_overlap_rejects_before_expansion(
    excessive_overlap_material, operation, monkeypatch
):
    ledger = CalendarEvidence((excessive_overlap_material,))
    assert (
        ledger.resolve("SH", DAY, KNOWN - timedelta(seconds=1)).state == State.UNKNOWN
    )
    assert ledger.resolve("SZ", DAY, AFTER).state == State.UNKNOWN

    def forbidden_expansion(*args, **kwargs):
        pytest.fail("lineage budget must precede per-date conflict expansion")

    monkeypatch.setattr(cal_adapter, "replace", forbidden_expansion)
    calls = {
        "resolve": lambda: ledger.resolve("SH", DAY, AFTER),
        "snapshot": lambda: ledger.snapshot("SH", window(), AFTER),
        "cutoff": lambda: ledger.completed_cutoff("SH", window(), AFTER),
        "locate": lambda: ledger.locate_session("SH", AFTER, window(), AFTER),
        "d1": lambda: ledger.to_d1("invented", "SH", window(), AFTER),
    }
    with pytest.raises(ContractError, match="^calendar_lineage_limit$"):
        calls[operation]()
    # The original complete material remains available; no evidence was truncated.
    assert len(ledger.materials[0].claims) == 2000


def test_eleven_input_bounded_materials_cannot_expand_lineage(
    excessive_overlap_material,
):
    mat = excessive_overlap_material
    mats = tuple(
        parse_material(mat.raw, receipt(mat.raw, known=KNOWN + timedelta(seconds=i)))
        for i in range(11)
    )
    assert sum(len(item.raw) for item in mats) <= MAX_TOTAL_BYTES
    assert sum(len(item.claims) for item in mats) <= MAX_TOTAL_ROWS
    ledger = CalendarEvidence(mats)
    with pytest.raises(ContractError, match="^calendar_lineage_limit$"):
        ledger.resolve("SH", DAY, AFTER)


def test_serialized_lineage_budget_blocks_long_refs_below_reference_limit():
    count = 350
    rows = overlap_chain(count)
    for offset, item in enumerate(rows):
        item["claim_id"] = f"{offset:0128d}"
    mat = material(rows, end=(DAY + timedelta(days=count)).isoformat())
    refs = tuple(sorted(mat.ref(claim) for claim in mat.claims))
    assert count * (len(refs) + 1) < MAX_LINEAGE_REFERENCES
    assert count * len(canonical_bytes((refs, (mat.revision,)))) > MAX_LINEAGE_BYTES
    with pytest.raises(ContractError, match="^calendar_lineage_limit$"):
        CalendarEvidence((mat,)).resolve("SH", DAY, AFTER)


@pytest.mark.parametrize("budget", ["MAX_LINEAGE_REFERENCES", "MAX_LINEAGE_BYTES"])
def test_lineage_budget_aggregates_groups_and_nonconflict_dates(budget, monkeypatch):
    first = material(overlap_chain(4))
    second = material(
        overlap_chain(4, DAY + timedelta(days=10)),
        start=(DAY + timedelta(days=10)).isoformat(),
        end=(DAY + timedelta(days=14)).isoformat(),
    )
    single = material([row("2024-01-22", state="UNKNOWN")], end="2024-01-23")
    ledger = CalendarEvidence((first, second, single))
    # Count only supplied dates, across all components even for a one-day query.
    days = tuple(ledger._market_snapshot("SH", AFTER).values())
    limit = sum(
        len(item.evidence_refs) + len(item.material_revisions)
        if budget == "MAX_LINEAGE_REFERENCES"
        else len(canonical_bytes((item.evidence_refs, item.material_revisions)))
        for item in days
    )
    monkeypatch.setattr(cal_adapter, budget, limit)
    assert ledger.resolve("SH", DAY, AFTER) == days[0]
    monkeypatch.setattr(cal_adapter, budget, limit - 1)
    with pytest.raises(ContractError, match="^calendar_lineage_limit$"):
        ledger.resolve("SH", DAY, AFTER)


def test_locating_session_retains_unknown_dates_in_denominator():
    ledger = book()
    result = ledger.locate_session(
        "SH", datetime(2024, 1, 2, 2, tzinfo=UTC), window(end="2024-01-04"), AFTER
    )
    assert result.status == "UNKNOWN"
    assert result.trading_days == (DAY,)


def test_d1_input_validation_rejects_mutation_and_qualification_promotion():
    ledger = book([row(), row("2024-01-03", "UNKNOWN"), row("2024-01-04", "CLOSED")])
    scope = window(end="2024-01-05")
    exported = ledger.to_d1("invented", "SH", scope, AFTER)
    candidate = exported.calendar
    assert len(exported.days) == 3
    assert len(candidate.days) == 2
    assert candidate.known_at == AFTER
    ledger.validate_d1(candidate, "invented", "SH", scope, AFTER)
    changed = [
        replace(candidate, qualification=QualificationStatus.QUALIFIED),
        replace(candidate, market="SZ"),
        replace(candidate, days=()),
        replace(candidate, calendar_id="forged"),
    ]
    for item in changed:
        with pytest.raises(ContractError, match="^d1_calendar_input_mismatch$"):
            ledger.validate_d1(item, "invented", "SH", scope, AFTER)


def test_d1_coverage_consumes_unknown_qualification_without_false_closure():
    baseline = universe()
    ledger = book(
        [row("2024-01-31", "CLOSED")],
        market="SYNTHETIC_EXCHANGE",
        start="2024-01-29",
        end="2024-02-07",
    )
    exported = ledger.to_d1(
        "invented", "SYNTHETIC_EXCHANGE", baseline.history_window, baseline.known_at
    )
    candidate = replace(baseline, calendars=(exported.calendar,))
    coverage = classify_coverage(plan_job(job(candidate)), ())
    target = next(
        item for item in coverage.entries if item.point.day == date(2024, 1, 31)
    )
    assert target.classification == CoverageClass.UNKNOWN
    assert (
        coverage.denominator
        == (baseline.history_window.end_exclusive - baseline.history_window.start).days
    )


@pytest.mark.parametrize(
    "origin", ["source", "binding", "mode", "decoder_revision", "timezone"]
)
def test_d1_refuses_to_flatten_multiple_origins_or_timezones(origin):
    first = material([row()])
    changes = {}
    doc = document([row("2024-01-03")])
    if origin == "source":
        changes[origin] = Key(Kind.SOURCE, "other.synthetic", "v1")
    elif origin == "binding":
        changes[origin] = Key(Kind.BINDING, "other.synthetic", "v1")
    elif origin == "mode":
        changes[origin] = EvidenceMode.RETAINED
    elif origin == "decoder_revision":
        changes[origin] = "different.v2"
    else:
        doc["timezone"] = "Asia/Hong_Kong"
    ledger = CalendarEvidence((first, parse_doc(doc, **changes)))
    classification = (
        "d1_single_timezone_required"
        if origin == "timezone"
        else "d1_single_provenance_integration_required"
    )
    with pytest.raises(ContractError, match=f"^{classification}$"):
        ledger.to_d1("invented", "SH", window(end="2024-01-04"), AFTER)


@pytest.mark.parametrize("flag", [False, True, None])
def test_tdx_native_fields_do_not_become_calendar_or_source_clock(flag):
    raw = json.dumps(dict(id=7, unix=0, date="20240102", is_workday=flag)).encode()
    meta = receipt(
        raw,
        upstream_version=TDX_VERSION,
        upstream_revision=TDX_COMMIT,
        producer_operation="tdx.Workday",
    )
    parsed = parse_tdx_workday(raw, meta)
    assert (parsed.native_id, parsed.native_unix, parsed.native_date) == (
        7,
        0,
        "20240102",
    )
    assert parsed.raw == raw
    assert parsed.calendar_state == State.UNKNOWN
    assert parsed.clock_semantics == "UNKNOWN"
    assert parsed.is_workday is flag


@pytest.mark.parametrize(
    "change,classification",
    [
        ({"id": True}, "tdx_int64_required"),
        ({"unix": 2**63}, "tdx_int64_required"),
        ({"unix": -(2**63) - 1}, "tdx_int64_required"),
        ({"date": 20240102}, "text_required"),
        ({"is_workday": 0}, "tdx_bool_required"),
    ],
)
def test_tdx_native_type_faults(change, classification):
    data = dict(id=0, unix=0, date="20240102")
    data.update(change)
    raw = json.dumps(data).encode()
    meta = receipt(
        raw,
        upstream_version=TDX_VERSION,
        upstream_revision=TDX_COMMIT,
        producer_operation="tdx.Workday",
    )
    with pytest.raises(ContractError, match=f"^{classification}$"):
        parse_tdx_workday(raw, meta)


def test_tdx_version_and_producer_pin_are_required():
    raw = b'{"id":0,"unix":0,"date":"20240102"}'
    with pytest.raises(ContractError, match="^tdx_version_pin_required$"):
        parse_tdx_workday(raw, receipt(raw))
    with pytest.raises(ContractError, match="^tdx_workday_operation_required$"):
        parse_tdx_workday(
            raw,
            receipt(raw, upstream_version=TDX_VERSION, upstream_revision=TDX_COMMIT),
        )


@pytest.mark.parametrize(
    "change,classification",
    [
        ({"mode": EvidenceMode.LIVE}, "offline_material_required"),
        ({"mode": "synthetic"}, "offline_material_required"),
        ({"capability": Capability.BARS_DAILY}, "calendar_capability_required"),
        ({"known_at": KNOWN - timedelta(days=1)}, "known_at_backdated"),
        ({"received_at": KNOWN - timedelta(days=1)}, "received_before_obtained"),
        (
            {"source_published_at": KNOWN + timedelta(days=1)},
            "publication_after_obtained",
        ),
        ({"known_at": datetime(2024, 1, 1)}, "naive_time"),
        ({"obtained_at": "2024-01-01"}, "instant_required"),
        ({"raw_sha256": "Z" * 64}, "invalid_raw_digest"),
        ({"raw_length": True}, "material_byte_limit"),
        ({"raw_length": MAX_BYTES + 1}, "material_byte_limit"),
        ({"source_ref": ""}, "empty_contract_field"),
        (
            {"source": Key(Kind.MATERIAL, "invented")},
            "source_binding_material_required",
        ),
    ],
)
def test_receipt_faults_are_classified_without_raw_exception_messages(
    change, classification
):
    with pytest.raises(ContractError, match=f"^{classification}$"):
        receipt(b"{}", **change)


@pytest.mark.parametrize(
    "raw,classification",
    [
        (b'{"schema":1,"schema":2}', "duplicate_json_key"),
        (b'{"secret_marker":NaN}', "nonfinite_json"),
        (b'{"secret_marker":Infinity}', "nonfinite_json"),
        (b'{"secret_marker":1e9999}', "nonfinite_json"),
        (b'{"secret_marker":', "invalid_material_json"),
        (b"\xff", "invalid_material_json"),
        (b"[]", "object_required"),
        (b"{}", "invalid_fields"),
    ],
)
def test_malformed_json_faults(raw, classification):
    with pytest.raises(ContractError, match=f"^{classification}$"):
        parse_material(raw, receipt(raw))


@pytest.mark.parametrize(
    "escaped", ["\\ud800", "\\udfff", "\\ud800x", "\\udfff\\ud800"]
)
@pytest.mark.parametrize(
    "location",
    ["arrangement", "market", "timezone", "source_ref", "session", "native", "key"],
)
def test_unpaired_json_surrogates_fail_before_revision_encoding(escaped, location):
    doc = document([row()])
    if location in ("market", "timezone"):
        doc[location] = "SURROGATE_MARKER"
    elif location in ("arrangement", "source_ref"):
        doc["days"][0][location] = "SURROGATE_MARKER"
    elif location == "session":
        doc["days"][0]["sessions"][0]["id"] = "SURROGATE_MARKER"
    elif location == "native":
        doc["days"][0]["native_fields"] = {"nested": [{"value": "SURROGATE_MARKER"}]}
    else:
        doc["days"][0]["native_fields"] = {"SURROGATE_MARKER": 1}
    raw = json.dumps(doc).replace("SURROGATE_MARKER", escaped).encode()
    with pytest.raises(ContractError, match="^invalid_material_unicode$") as failure:
        parse_material(raw, receipt(raw))
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None


def test_valid_surrogate_pair_and_literal_unicode_keep_exact_material_bytes():
    doc = document([row(arrangement="交易日😀")], market="合成市场😀")
    raw_escaped = json.dumps(doc, ensure_ascii=True).encode()
    assert b"\\ud83d\\ude00" in raw_escaped
    raw_utf8 = json.dumps(doc, ensure_ascii=False).encode()
    for raw in (raw_escaped, raw_utf8):
        mat = parse_material(raw, receipt(raw))
        assert mat.market == "合成市场😀"
        assert mat.claims[0].arrangement == "交易日😀"
        assert mat.raw == raw
        assert (
            CalendarEvidence((mat,)).resolve(mat.market, DAY, AFTER).state == State.OPEN
        )


def test_raw_integrity_faults_and_no_silent_dataclass_forgery():
    mat = material()
    with pytest.raises(ContractError, match="^raw_length_mismatch$"):
        parse_material(mat.raw + b" ", mat.receipt)
    with pytest.raises(ContractError, match="^raw_digest_mismatch$"):
        parse_material(mat.raw, replace(mat.receipt, raw_sha256="0" * 64))
    with pytest.raises(ContractError, match="^parsed_material_mismatch$"):
        CalendarEvidence((replace(mat, market="SZ"),))
    with pytest.raises(ContractError, match="^duplicate_material$"):
        CalendarEvidence((mat, mat))
    other = material([row(state="CLOSED")])
    same_id = parse_material(
        other.raw, replace(other.receipt, material=mat.receipt.material)
    )
    with pytest.raises(ContractError, match="^material_identity_collision$"):
        CalendarEvidence((mat, same_id))


@pytest.mark.parametrize(
    "key,value,classification",
    [
        ("day", "2024-02-30", "invalid_local_date"),
        ("day", "2024-01-06", "claim_outside_effective_window"),
        ("state", [], "invalid_calendar_state"),
        ("evidence_kind", [], "invalid_evidence_kind"),
        ("state", "CONFLICT", "invalid_calendar_state"),
        ("claim_id", "bad:ref", "invalid_claim_id"),
        ("arrangement", "", "text_required"),
        ("sessions", [], "open_day_sessions_required"),
        ("sessions", {}, "session_limit"),
        ("complete_after", "2024-01-02T14:59:59", "completion_before_session_end"),
        ("complete_after", "2024-01-01T16:00:00", "completion_before_trading_date"),
        ("complete_after_fold", True, "invalid_fold"),
        ("complete_after_fold", 1, "invalid_fold_for_clock"),
        ("supersedes", ["same", "same"], "invalid_supersession_refs"),
        ("native_fields", [], "native_object_required"),
    ],
)
def test_row_faults(key, value, classification):
    data = row()
    data[key] = value
    with pytest.raises(ContractError, match=f"^{classification}$"):
        material([data])


@pytest.mark.parametrize(
    "start,end,classification",
    [
        ("2024-01-02T15:00:00", "2024-01-02T09:00:00", "empty_or_reversed_session"),
        ("2024-01-02T09:00:00+08:00", "2024-01-02T15:00:00", "local_clock_required"),
        ("2024-01-02T09:61:00", "2024-01-02T15:00:00", "invalid_local_clock"),
    ],
)
def test_session_clock_faults(start, end, classification):
    with pytest.raises(ContractError, match=f"^{classification}$"):
        material([row(sessions=[session(start, end)])])


def test_overlap_closed_unknown_and_duplicate_claim_faults():
    cases = [
        ([row(), row()], "duplicate_claim_id"),
        (
            [
                row(
                    sessions=[
                        session("2024-01-02T09:00:00", "2024-01-02T12:00:00", "am"),
                        session("2024-01-02T11:00:00", "2024-01-02T15:00:00", "pm"),
                    ]
                )
            ],
            "overlapping_sessions",
        ),
        ([row(state="CLOSED", sessions=row()["sessions"])], "nonopen_day_has_sessions"),
        (
            [row(state="UNKNOWN", complete_after="2024-01-02T16:00:00")],
            "unknown_day_has_completion",
        ),
        ([row(complete_after=None, complete_after_fold=0)], "fold_without_clock"),
    ]
    for rows, classification in cases:
        with pytest.raises(ContractError, match=f"^{classification}$"):
            material(rows)


@pytest.mark.parametrize(
    "change,classification",
    [
        ({"schema": "unknown"}, "unsupported_calendar_schema"),
        ({"timezone": "Unlisted/Timezone"}, "invalid_exchange_timezone"),
        ({"days": None}, "calendar_row_limit"),
        ({"extra": "must-not-ignore"}, "invalid_fields"),
        (
            {"effective": {"start": "2024-1-2", "end_exclusive": "2024-01-06"}},
            "invalid_local_date",
        ),
        (
            {"effective": {"start": "2024-01-06", "end_exclusive": "2024-01-02"}},
            "empty_or_reversed_window",
        ),
    ],
)
def test_document_faults(change, classification):
    doc = document([row()])
    doc.update(change)
    with pytest.raises(ContractError, match=f"^{classification}$"):
        parse_doc(doc)


def test_capacity_refuses_instead_of_truncating_or_expanding():
    with pytest.raises(ContractError, match="^calendar_material_limit$"):
        CalendarEvidence(tuple(material() for _ in range(MAX_MATERIALS + 1)))
    doc = document([row()] * (MAX_ROWS + 1))
    raw = json.dumps(doc, separators=(",", ":")).encode()
    # Byte budget is checked before row parsing (this doc also exceeds 1 MiB).
    assert len(raw) > MAX_BYTES
    with pytest.raises(ContractError, match="^material_byte_limit$"):
        receipt(raw)
    doc = document([None] * (MAX_ROWS + 1))
    with pytest.raises(ContractError, match="^calendar_row_limit$"):
        parse_doc(doc)
    doc = document([])
    doc["effective"]["end_exclusive"] = (DAY + timedelta(days=MAX_DAYS + 1)).isoformat()
    with pytest.raises(ContractError, match="^calendar_window_limit$"):
        parse_doc(doc)
    with pytest.raises(ContractError, match="^calendar_window_limit$"):
        book().snapshot(
            "SH", DateWindow(DAY, DAY + timedelta(days=MAX_DAYS + 1)), AFTER
        )
    large = row(native_fields={"raw": "x" * 950_000})
    mats = tuple(
        material([large], known=KNOWN + timedelta(seconds=i)) for i in range(9)
    )
    assert sum(len(mat.raw) for mat in mats) > MAX_TOTAL_BYTES
    with pytest.raises(ContractError, match="^total_material_byte_limit$"):
        CalendarEvidence(mats)


def test_total_row_limit_precedes_selection_and_exact_window_limit_is_supported():
    rows = [
        row(state="UNKNOWN", claim_id=str(i), arrangement="x", source_ref="x")
        for i in range(3300)
    ]
    mats = tuple(material(rows, known=KNOWN + timedelta(seconds=i)) for i in range(10))
    assert sum(len(mat.raw) for mat in mats) < MAX_TOTAL_BYTES
    assert sum(len(mat.claims) for mat in mats) > MAX_TOTAL_ROWS
    with pytest.raises(ContractError, match="^total_calendar_row_limit$"):
        CalendarEvidence(mats)
    scope = DateWindow(DAY, DAY + timedelta(days=MAX_DAYS))
    output = book([]).snapshot("SH", scope, AFTER)
    assert len(output) == MAX_DAYS
    assert all(item.state == State.UNKNOWN for item in output)


def test_large_valid_material_keeps_every_conflicting_or_unknown_row_reference():
    rows = [
        row(state="UNKNOWN", claim_id=str(i), arrangement="x", source_ref="x")
        for i in range(4000)
    ]
    ledger = book(rows)
    output = ledger.to_d1("invented", "SH", window(), AFTER)
    assert output.calendar.days == ()
    assert len(output.days[0].evidence_refs) == len(rows)
    assert output.days[0].state == State.UNKNOWN
    assert len(set(output.days[0].evidence_refs)) == len(rows)


def test_revision_binds_utc_resolution_not_only_raw_digest():
    mat = material()
    changed_session = replace(
        mat.claims[0].sessions[0],
        start_utc=mat.claims[0].sessions[0].start_utc + timedelta(seconds=1),
    )
    altered = replace(
        mat, claims=(replace(mat.claims[0], sessions=(changed_session,)),)
    )
    assert altered.receipt.raw_sha256 == mat.receipt.raw_sha256
    assert altered.revision != mat.revision
    with pytest.raises(ContractError, match="^parsed_material_mismatch$"):
        CalendarEvidence((altered,))


def test_date_extreme_clock_overflow_is_a_classification():
    schedule = row(
        "0001-01-01",
        sessions=[session("0001-01-01T00:00:00", "0001-01-01T15:00:00")],
        complete_after=None,
    )
    with pytest.raises(ContractError, match="^local_clock_out_of_range$"):
        material([schedule], start="0001-01-01", end="0001-01-02")


def test_cutoff_complete_window_closed_and_empty_evidence():
    ledger = book([row(state="CLOSED")])
    result = ledger.completed_cutoff("SH", window(), AFTER)
    assert result.completed_through == DAY
    assert result.first_blocked_day is None
    assert result.reason == "window_completed"
    empty = book([]).to_d1("invented", "SH", window(), AFTER)
    assert empty.calendar.days == ()
    assert empty.days[0].state == State.UNKNOWN


def test_replay_has_no_network_database_subprocess_or_write_effects(monkeypatch):
    import builtins

    import psycopg

    def forbidden(*args, **kwargs):
        raise AssertionError("offline calendar crossed an effect boundary")

    original_open = builtins.open

    def readonly_open(file, mode="r", *args, **kwargs):
        if any(char in mode for char in "wax+"):
            forbidden()
        return original_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(psycopg.Connection, "connect", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(builtins, "open", readonly_open)
    ledger = book([row(), row("2024-01-03", "CLOSED")])
    scope = window(end="2024-01-04")
    exported = ledger.to_d1("invented", "SH", scope, AFTER)
    ledger.validate_d1(exported.calendar, "invented", "SH", scope, AFTER)
    assert ledger.completed_cutoff("SH", scope, AFTER).completed_through == date(
        2024, 1, 3
    )
    assert canonical_bytes(exported)


def test_equivalent_material_insertion_order_is_stable_without_mutating_input():
    data = document([row()])
    before = copy.deepcopy(data)
    first, second = parse_doc(data), material([row("2024-01-03")])
    assert data == before
    a, b = CalendarEvidence((first, second)), CalendarEvidence((second, first))
    assert a.revision == b.revision
    assert a.to_d1("invented", "SH", window(end="2024-01-04"), AFTER) == b.to_d1(
        "invented", "SH", window(end="2024-01-04"), AFTER
    )
