"""Synthetic offline ICS checks, including the real CLI default factory."""

import json
import socket
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from market_data_foundation.historical import cpi_release_schedule as schedule
from market_data_foundation.historical.cpi_timeseries_cli import main


def event(
    uid="cpi-oct",
    at="20261110T083000",
    sequence=1,
    *,
    extra="",
    summary="Consumer Price Index",
):
    return (
        f"BEGIN:VEVENT\r\nUID:{uid}\r\nSEQUENCE:{sequence}\r\n"
        + (f"SUMMARY:{summary}\r\n" if summary is not None else "")
        + (f"DTSTART;TZID=US-Eastern:{at}\r\n" if at is not None else "")
        + extra
        + "END:VEVENT\r\n"
    )


def ics(*events, method="PUBLISH"):
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nMETHOD:"
        + method
        + "\r\n"
        + "".join(events)
        + "END:VCALENDAR\r\n"
    ).encode()


def html(*rows):
    return (
        "<table><tr><th>Reference Month</th><th>Release Date</th>"
        "<th>Release Time</th></tr>"
        + "".join(
            "<tr>" + "".join(f"<td>{v}</td>" for v in row) + "</tr>" for row in rows
        )
        + "</table>"
    ).encode()


OCTOBER = ("October 2026", "Nov. 10, 2026", "08:30 AM")
JANUARY = ("January 2027", "Feb. 11, 2027", "08:30 AM")
EMPTY_SIGNALS = b'<h1>Errata</h1><table id="errata_datatable"></table>'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("ICS tests must never open a network connection")

    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def scheduler(tmp_path):
    return schedule.CpiReleaseScheduler(
        None,
        tmp_path / "schedule.json",
        clock=lambda: datetime.fromisoformat("2026-10-28T20:00:00+00:00"),
        calendar_fetcher=lambda: ics(event()),
        reference_fetcher=None,
        signal_fetcher=lambda: EMPTY_SIGNALS,
    )


def seed(scheduler, *rows):
    return scheduler.import_calendar(html(*(rows or [OCTOBER])), scheduler.clock())


def targets(result):
    return {t["reference_month"]: t for t in result["targets"]}


def test_actual_bls_shape_filters_and_handles_folded_text_and_dst():
    raw = ics(
        event(at="20260311T083000", summary="Consumer Price\r\n\t Index"),
        event("winter", summary="Consumer Price Index"),
        event("real", summary="Real Earnings"),
        event("ppi", summary="Producer Price Index"),
        event("other", summary="Consumer Price Index (Other)"),
    ).replace(b"UID:cpi-oct", b"UID:cpi\\,oct\\;a\\\\b")
    parsed = {e["uid"]: e for e in schedule.parse_official_ics(raw)}
    assert set(parsed) == {"cpi,oct;a\\b", "winter"}
    assert parsed["cpi,oct;a\\b"]["release_at"] == "2026-03-11T12:30:00+00:00"
    assert parsed["winter"]["release_at"] == "2026-11-10T13:30:00+00:00"
    assert all("reference_month" not in e for e in parsed.values())


def test_utc_and_quoted_new_york_alias():
    raw = ics(event()).replace(b"TZID=US-Eastern", b'TZID="America/New_York"')
    eastern = schedule.parse_official_ics(raw)[0]
    raw = raw.replace(
        b'DTSTART;TZID="America/New_York":20261110T083000', b"DTSTART:20261110T133000Z"
    )
    assert schedule.parse_official_ics(raw)[0] == eastern


@pytest.mark.parametrize(
    "field",
    [
        "DTSTART:20261110T083000",
        "DTSTART;TZID=Europe/London:20261110T083000",
        "DTSTART;VALUE=DATE:20261110",
        "DTSTART;TZID=US-Eastern:20260308T023000",
        "DTSTART;TZID=US-Eastern:20261101T013000",
        "DTSTART:20260230T133000Z",
    ],
)
def test_unresolved_time_never_gets_a_guessed_offset(field):
    raw = ics(event()).replace(
        b"DTSTART;TZID=US-Eastern:20261110T083000", field.encode()
    )
    with pytest.raises(schedule.CpiScheduleError, match="calendar_ics_time_unresolved"):
        schedule.parse_official_ics(raw)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nEND:VCALENDAR",
        ics(event(extra="RRULE:FREQ=MONTHLY\r\n")),
        ics(event(extra="UID:duplicate\r\n")),
        ics(event(sequence=-1)),
        ics(event()).replace(b"UID:cpi-oct\r\n", b""),
    ],
)
def test_malformed_or_recurring_cpi_is_rejected(raw):
    with pytest.raises(schedule.CpiScheduleError):
        schedule.parse_official_ics(raw)


def test_uid_sequence_and_revision_timestamp_deduplicate_independent_of_order():
    older = event(at="20261109T083000")
    newer = event(sequence=2, extra="DTSTAMP:20261028T180000Z\r\n")
    newest = event(
        at="20261111T083000", sequence=2, extra="DTSTAMP:20261028T190000Z\r\n"
    )
    for values in [(older, newest, newer, newest), (newest, newer, older)]:
        releases = schedule.parse_official_ics(ics(*values))
        assert len(releases) == 1
        assert releases[0]["release_at"] == "2026-11-11T13:30:00+00:00"


def test_equal_revision_conflicts_fail_closed():
    with pytest.raises(
        schedule.CpiScheduleError, match="calendar_ics_revision_conflict"
    ):
        schedule.parse_official_ics(ics(event(), event(at="20261111T083000")))


def test_existing_html_binds_uid_and_preserves_uncovered_2027(scheduler):
    seeded = seed(scheduler, OCTOBER, JANUARY)
    original = targets(seeded)["2027-01"]
    result = scheduler.refresh_calendar()
    assert result["calendar"]["source"] == schedule.ICS_URL
    assert result["calendar"]["format"] == "ics"
    assert targets(result)["2026-10"]["ics_uid"] == "cpi-oct"
    retained = targets(result)["2027-01"]
    assert retained["release_at"] == original["release_at"]
    assert retained["state"] == "pending"
    assert retained["absent_from_latest_calendar"] is True
    evidence = retained["reference_month_evidence"]
    assert evidence["source"] == schedule.CALENDAR_URL
    assert Path(evidence["raw_path"]).read_bytes() == html(OCTOBER, JANUARY)
    assert result["calendar"]["changes"]["removed"] == []


def test_fresh_state_uses_html_only_as_explicit_month_evidence(scheduler):
    scheduler.reference_fetcher = lambda: html(OCTOBER)
    result = scheduler.refresh_calendar()
    target = targets(result)["2026-10"]
    assert target["release_at"] == "2026-11-10T13:30:00+00:00"
    assert target["reference_month_evidence"]["source"] == schedule.CALENDAR_URL
    assert result["calendar"]["reference_mapping_status"] == "verified"


def test_unmapped_new_event_and_unknown_retime_remain_pending(scheduler):
    seed(scheduler)
    scheduler.calendar_fetcher = lambda: ics(event("unknown", "20261112T083000"))
    result = scheduler.refresh_calendar()
    assert targets(result)["2026-10"]["release_at"] == "2026-11-10T13:30:00+00:00"
    assert "ics_uid" not in targets(result)["2026-10"]
    assert result["calendar"]["pending_events"][0]["reference_month"] is None
    assert result["calendar"]["reference_mapping_status"] == "pending"
    empty = schedule.CpiReleaseScheduler(
        None,
        scheduler.state_path.parent / "empty.json",
        clock=scheduler.clock,
        calendar_fetcher=scheduler.calendar_fetcher,
        reference_fetcher=None,
    ).refresh_calendar()
    assert empty["targets"] == []
    assert empty["next_due"] is None


def test_two_uids_competing_for_one_month_are_not_arbitrarily_bound(scheduler):
    seed(scheduler)
    scheduler.calendar_fetcher = lambda: ics(event("one"), event("two"))
    result = scheduler.refresh_calendar()
    assert len(result["calendar"]["pending_events"]) == 2
    assert "ics_uid" not in targets(result)["2026-10"]


def test_existing_month_without_source_evidence_does_not_establish_identity(scheduler):
    seed(scheduler)
    state = json.loads(scheduler.state_path.read_text())
    state["targets"]["2026-10"].pop("reference_month_evidence")
    for key in ("source", "sha256", "raw_path"):
        state["calendar"].pop(key)
    state["targets"]["2026-10"].pop("calendar_sha256")
    scheduler.state_path.write_text(json.dumps(state))
    result = scheduler.refresh_calendar()
    assert "ics_uid" not in targets(result)["2026-10"]
    assert result["calendar"]["pending_events"][0]["reference_month"] is None


def test_known_uid_retime_survives_restart_and_old_sequence_cannot_rollback(scheduler):
    seed(scheduler)
    scheduler.refresh_calendar()
    scheduler.calendar_fetcher = lambda: ics(event(at="20261112T083000", sequence=3))
    result = scheduler.refresh_calendar()
    assert result["calendar"]["changes"]["retimed"] == ["2026-10"]
    assert targets(result)["2026-10"]["next_attempt_at"] == "2026-11-12T13:30:00+00:00"
    restarted = schedule.CpiReleaseScheduler(
        None,
        scheduler.state_path,
        clock=scheduler.clock,
        calendar_fetcher=lambda: ics(event(sequence=2)),
        reference_fetcher=None,
    )
    result = restarted.refresh_calendar()
    assert targets(result)["2026-10"]["release_at"] == "2026-11-12T13:30:00+00:00"
    assert targets(result)["2026-10"]["ics_sequence"] == 3
    assert result["calendar"]["changes"] == {"added": [], "retimed": [], "removed": []}


@pytest.mark.parametrize(
    "method,extra", [("PUBLISH", "STATUS:CANCELLED\r\n"), ("CANCEL", "")]
)
def test_only_explicit_known_uid_cancellation_blocks_and_newer_revision_restores(
    scheduler, method, extra
):
    seed(scheduler)
    scheduler.refresh_calendar()
    scheduler.calendar_fetcher = lambda: ics(
        event(sequence=2, at=None, summary=None, extra=extra), method=method
    )
    result = scheduler.refresh_calendar()
    target = targets(result)["2026-10"]
    assert target["state"] == "calendar_removed"
    assert target["cancellation_evidence"]["uid"] == "cpi-oct"
    assert result["next_due"] is None
    scheduler.calendar_fetcher = lambda: ics(event())
    assert (
        targets(scheduler.refresh_calendar())["2026-10"]["state"] == "calendar_removed"
    )
    scheduler.calendar_fetcher = lambda: ics(event(sequence=3))
    target = targets(scheduler.refresh_calendar())["2026-10"]
    assert target["state"] == "pending"
    assert "cancellation_evidence" not in target


def test_absent_uid_cache_is_retained_and_duplicate_refresh_does_not_reset_attempts(
    scheduler,
):
    seed(scheduler)
    scheduler.refresh_calendar()
    state = json.loads(scheduler.state_path.read_text())
    state["targets"]["2026-10"]["target"].update(attempts=3, state="awaiting_provider")
    scheduler.state_path.write_text(json.dumps(state))
    result = scheduler.refresh_calendar()
    assert targets(result)["2026-10"]["target"]["attempts"] == 3
    scheduler.calendar_fetcher = lambda: ics()
    result = scheduler.refresh_calendar()
    assert (
        result["calendar"]["events"]["cpi-oct"]["absent_from_latest_calendar"] is True
    )
    assert targets(result)["2026-10"]["state"] == "pending"
    assert targets(result)["2026-10"]["target"]["attempts"] == 3


def test_failed_ics_does_not_fall_back_to_html_or_reset_cached_plan(scheduler):
    seed(scheduler)
    scheduler.refresh_calendar()

    def fail():
        raise schedule.CpiScheduleError("calendar_http_403")

    scheduler.calendar_fetcher = fail
    scheduler.reference_fetcher = lambda: pytest.fail(
        "ICS failure cannot use HTML fallback"
    )
    result = scheduler.refresh_calendar()
    assert result["calendar_status"] == "degraded_cached_calendar"
    assert result["calendar"]["error"] == "calendar_http_403"
    assert targets(result)["2026-10"]["ics_uid"] == "cpi-oct"


def test_html_failure_keeps_ics_and_verified_binding(scheduler):
    seed(scheduler)
    scheduler.calendar_fetcher = lambda: ics(
        event(), event("unknown", "20261211T083000")
    )

    def fail():
        raise RuntimeError("private exception text")

    scheduler.reference_fetcher = fail
    result = scheduler.refresh_calendar()
    assert result["calendar_status"] == "fresh"
    assert (
        result["calendar"]["reference_mapping_error"]
        == "calendar_reference_fetch_failed"
    )
    assert "private" not in json.dumps(result)
    assert targets(result)["2026-10"]["ics_uid"] == "cpi-oct"


def test_conflicting_refresh_preserves_all_previous_events_and_targets(scheduler):
    seed(scheduler)
    original = scheduler.refresh_calendar()
    scheduler.calendar_fetcher = lambda: ics(event(at="20261112T083000"))
    result = scheduler.refresh_calendar()
    assert result["calendar_status"] == "degraded_cached_calendar"
    assert result["calendar"]["error"] == "calendar_ics_revision_conflict"
    assert result["targets"] == original["targets"]
    assert result["calendar"]["events"] == original["calendar"]["events"]


def test_completed_target_and_history_never_reopen_after_retime_or_cancel(scheduler):
    seed(scheduler)
    scheduler.refresh_calendar()
    state = json.loads(scheduler.state_path.read_text())
    target = state["targets"]["2026-10"]
    target.update(
        state="complete",
        completed_at=scheduler.clock().isoformat(),
        next_attempt_at=None,
    )
    for stage in ("target", "history"):
        target[stage].update(state="complete", next_attempt_at=None, attempts=1)
    scheduler.state_path.write_text(json.dumps(state))
    scheduler.calendar_fetcher = lambda: ics(event(at="20261112T083000", sequence=2))
    result = scheduler.refresh_calendar()
    assert targets(result)["2026-10"]["calendar_changed_after_completion"] is True
    assert targets(result)["2026-10"]["release_at"] == target["release_at"]
    assert scheduler.refresh_calendar()["calendar"]["changes"]["retimed"] == []
    scheduler.calendar_fetcher = lambda: ics(
        event(sequence=3, extra="STATUS:CANCELLED\r\n")
    )
    result = scheduler.refresh_calendar()
    assert targets(result)["2026-10"]["state"] == "complete"
    assert result["next_due"] is None


def test_import_ics_uses_existing_evidence_without_network_and_rejects_old_acquisition(
    scheduler,
):
    seed(scheduler)
    result = scheduler.import_calendar(ics(event()), scheduler.clock())
    assert result["calendar"]["acquisition_method"] == "operator_import"
    assert Path(result["calendar"]["raw_path"]).suffix == ".ics"
    with pytest.raises(
        schedule.CpiScheduleError, match="calendar_import_older_than_current"
    ):
        scheduler.import_calendar(
            ics(event()), scheduler.clock() - timedelta(seconds=1)
        )


class NoEffects:
    def update_range(self, *args, **kwargs):
        pytest.fail("completed, cancelled, or unconfirmed targets must not run")

    def status(self):
        return {"latest_months": {}}


def test_tentative_bound_revision_blocks_target_until_confirmed(scheduler):
    seed(scheduler)
    scheduler.refresh_calendar()
    scheduler.calendar_fetcher = lambda: ics(
        event(sequence=2, extra="STATUS:TENTATIVE\r\n")
    )
    result = scheduler.refresh_calendar()
    assert targets(result)["2026-10"]["calendar_execution_blocked"] is True
    assert (
        result["calendar"]["pending_events"][0]["mapping_status"]
        == "release_time_pending"
    )
    assert result["next_due"] is None
    scheduler.service = NoEffects()
    scheduler.clock = lambda: datetime.fromisoformat("2026-11-10T13:30:00+00:00")
    scheduler.due_check()
    scheduler.calendar_fetcher = lambda: ics(event(sequence=3))
    assert (
        targets(scheduler.refresh_calendar())["2026-10"]["calendar_execution_blocked"]
        is False
    )


def test_retime_into_reference_month_is_pending_and_never_executed(scheduler):
    seed(scheduler)
    scheduler.refresh_calendar()
    scheduler.calendar_fetcher = lambda: ics(event(at="20261029T083000", sequence=2))
    result = scheduler.refresh_calendar()
    assert (
        result["calendar"]["pending_events"][0]["classification"]
        == "calendar_reference_date_invalid"
    )
    assert targets(result)["2026-10"]["release_at"] == "2026-11-10T13:30:00+00:00"
    scheduler.service = NoEffects()
    scheduler.due_check()


def test_failed_ics_refresh_is_cached_and_retried_only_at_backoff(scheduler):
    calls = []

    def fail():
        calls.append(1)
        raise schedule.CpiScheduleError("calendar_timeout")

    scheduler.calendar_fetcher = fail
    scheduler.service = NoEffects()
    scheduler.due_check()
    scheduler.due_check()
    assert calls == [1]
    next_at = datetime.fromisoformat(scheduler.status()["calendar"]["next_refresh_at"])
    scheduler.clock = lambda: next_at
    scheduler.due_check()
    assert calls == [1, 1]


def test_same_sequence_older_timestamp_cannot_undo_reschedule(scheduler):
    seed(scheduler)
    scheduler.calendar_fetcher = lambda: ics(
        event(extra="LAST-MODIFIED:20261028T180000Z\r\n")
    )
    scheduler.refresh_calendar()
    scheduler.calendar_fetcher = lambda: ics(
        event(at="20261112T083000", extra="LAST-MODIFIED:20261028T190000Z\r\n")
    )
    scheduler.refresh_calendar()
    scheduler.calendar_fetcher = lambda: ics(
        event(extra="LAST-MODIFIED:20261028T180000Z\r\n")
    )
    result = scheduler.refresh_calendar()
    assert targets(result)["2026-10"]["release_at"] == "2026-11-12T13:30:00+00:00"


def test_cli_refresh_default_factory_fetches_ics_and_html_without_data_service(
    tmp_path, monkeypatch, capsys
):
    calls = []

    def fetch(url, **options):
        calls.append((url, options))
        return ics(event()) if url == schedule.ICS_URL else html(OCTOBER)

    def forbidden_service(**kwargs):
        pytest.fail("calendar refresh must not initialize PG/Garage/data transport")

    monkeypatch.setattr(schedule, "fetch_bls_page", fetch)
    code = main(
        ["--schedule-state", str(tmp_path / "cli.json"), "schedule", "refresh"],
        service_factory=forbidden_service,
    )
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["calendar"]["source"] == schedule.ICS_URL
    assert output["targets"][0]["reference_month"] == "2026-10"
    assert [call[0] for call in calls] == [schedule.ICS_URL, schedule.CALENDAR_URL]
    assert calls[0][1]["headers"]["Accept"] == "text/calendar"
    assert calls[0][1]["allowed_path"] == "/schedule/news_release/bls.ics"


def test_known_bindings_and_historical_pending_never_fetch_or_parse_html(
    scheduler, monkeypatch
):
    seed(scheduler, OCTOBER, JANUARY)
    calls = []
    scheduler.reference_fetcher = lambda: calls.append("html") or html(OCTOBER)
    scheduler.calendar_fetcher = lambda: ics(
        event(), event("historical", "20250115T083000")
    )

    def forbidden_parse(*args):
        pytest.fail("existing evidence and historical pending need no HTML parse")

    monkeypatch.setattr(schedule, "parse_official_calendar", forbidden_parse)
    for _ in range(3):
        result = scheduler.refresh_calendar()
        assert targets(result)["2026-10"]["ics_uid"] == "cpi-oct"
        assert targets(result)["2027-01"]["state"] == "pending"
        pending = result["calendar"]["pending_events"]
        assert [e["uid"] for e in pending] == ["historical"]
    assert calls == []


def test_new_future_mapping_fetches_and_parses_once_then_survives_restart(
    scheduler, monkeypatch
):
    calls, parses = [], []
    original_parse = schedule.parse_official_calendar

    def parse(raw):
        parses.append(raw)
        return original_parse(raw)

    def reference():
        calls.append("html")
        return html(OCTOBER, JANUARY)

    scheduler.reference_fetcher = reference
    monkeypatch.setattr(schedule, "parse_official_calendar", parse)
    first = scheduler.refresh_calendar()
    assert targets(first)["2026-10"]["ics_uid"] == "cpi-oct"
    assert len(calls) == len(parses) == 1
    restarted = schedule.CpiReleaseScheduler(
        None,
        scheduler.state_path,
        clock=scheduler.clock,
        calendar_fetcher=lambda: ics(event(), event("jan", "20270211T083000")),
        reference_fetcher=reference,
        signal_fetcher=lambda: EMPTY_SIGNALS,
    )
    second = restarted.refresh_calendar()
    assert targets(second)["2027-01"]["ics_uid"] == "jan"
    restarted.refresh_calendar()
    assert len(calls) == len(parses) == 1  # cached reference rows are evidence too


def test_only_unmapped_historical_or_cancelled_events_do_not_request_html(scheduler):
    calls = []
    scheduler.reference_fetcher = lambda: calls.append("html") or html(OCTOBER)
    scheduler.calendar_fetcher = lambda: ics(
        event("historical", "20250115T083000"),
        event("cancelled", extra="STATUS:CANCELLED\r\n"),
    )
    for _ in range(2):
        result = scheduler.refresh_calendar()
        assert result["targets"] == []
        assert len(result["calendar"]["pending_events"]) == 2
    assert calls == []


def test_unknown_future_reference_failure_keeps_known_uid_and_2027_plan(scheduler):
    seed(scheduler, OCTOBER, JANUARY)
    scheduler.refresh_calendar()
    calls = []

    def denied():
        calls.append("html")
        raise schedule.CpiScheduleError("calendar_http_403")

    scheduler.reference_fetcher = denied
    scheduler.calendar_fetcher = lambda: ics(
        event(sequence=2, at="20261112T083000"),
        event("unknown", "20261211T083000"),
    )
    result = scheduler.refresh_calendar()
    assert calls == ["html"]
    assert result["calendar"]["error"] is None
    assert result["calendar"]["reference_mapping_error"] == "calendar_http_403"
    assert targets(result)["2026-10"]["release_at"] == "2026-11-12T13:30:00+00:00"
    assert targets(result)["2027-01"]["state"] == "pending"
    assert result["calendar"]["pending_events"][0]["uid"] == "unknown"
    assert set(targets(result)) == {"2026-10", "2027-01"}


def test_explicit_html_import_does_not_invoke_reference_fetcher(scheduler):
    def forbidden():
        pytest.fail("explicit HTML import already contains reference evidence")

    scheduler.reference_fetcher = forbidden
    result = seed(scheduler, OCTOBER, JANUARY)
    assert set(targets(result)) == {"2026-10", "2027-01"}
