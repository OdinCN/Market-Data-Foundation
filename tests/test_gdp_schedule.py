"""Synthetic calendar/job lifecycle tests; no live provider or private DB in CI."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from market_data_foundation.historical.gdp_calendar import (
    GdpScheduleError,
    backup_events,
    parse_official_html,
    parse_official_ics,
    title_identity,
)
from market_data_foundation.historical.gdp_schedule import (
    GdpAutoStrategy,
    GdpReleaseScheduler,
)
from market_data_foundation.web.gdp_jobs import GdpJobRunner, WebError
from tests.test_gdp_chain import service

AT = datetime(2026, 11, 2, 12, tzinfo=UTC)


def ics(*events):
    lines = ["BEGIN:VCALENDAR"]
    for kind, q, at, uid, status, seq in events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"SUMMARY:GDP ({kind} Estimate), {q}",
            f"DTSTART:{at}",
            f"STATUS:{status}",
            f"SEQUENCE:{seq}",
            "END:VEVENT",
        ]
    return ("\r\n".join(lines + ["END:VCALENDAR"]) + "\r\n").encode()


def event(
    kind="Second", at="20261125T133000Z", uid="second", status="CONFIRMED", seq=0
):
    return kind, "3rd Quarter 2026", at, uid, status, seq


def html(day="25", kind="Second"):
    return (
        f'<table id="release-schedule-table"><tr><th>Year 2026</th></tr>'
        f"<tr><td>November {day} 8:30 AM</td><td></td>"
        f"<td>GDP ({kind} Estimate), 3rd Quarter 2026</td></tr></table>"
    ).encode()


def setup(tmp_path, raw=None):
    s, wire, clock, _ = service()
    clock[0] = AT
    runner = GdpJobRunner(s, tmp_path / "jobs", clock=lambda: clock[0])
    raw = raw or ics(event())
    scheduler = GdpReleaseScheduler(
        runner,
        tmp_path / "schedule.json",
        clock=lambda: clock[0],
        history_start="2026-Q3",
        calendar_fetcher=lambda: raw,
    )
    return scheduler, runner, wire, clock


@pytest.mark.parametrize(
    ("title", "quarter", "kind"),
    [
        (
            "Gross Domestic Product, 4th Quarter and Year 2024 (Advance Estimate)",
            "2024-Q4",
            "advance",
        ),
        (
            "Gross Domestic Product, 3rd Quarter 2025 (Initial Estimate) "
            "and Corporate Profits (Preliminary)",
            "2025-Q3",
            "initial",
        ),
        (
            "GDP (Third Estimate), Industries, Corporate Profits, State GDP, "
            "and State Personal Income, 2nd Quarter 2026; State PCE, 2025",
            "2026-Q2",
            "third",
        ),
        (
            "Gross Domestic Product, 3rd Quarter 2025 (Updated Estimate)",
            "2025-Q3",
            "updated",
        ),
    ],
)
def test_native_calendar_identity(title, quarter, kind):
    assert title_identity(title) == {
        "reference_quarter": quarter,
        "estimate_kind": kind,
    }


def test_regional_gdp_not_national_and_unknown_kept_unmapped():
    assert title_identity("Gross Domestic Product by State, 3rd Quarter 2026") is None
    assert title_identity("GDP by Industry, 3rd Quarter 2026") is None
    assert (
        title_identity("Gross Domestic Product, Annual Update")["reference_quarter"]
        is None
    )


def test_three_estimates_same_quarter_and_timezone():
    parsed = parse_official_ics(
        ics(
            event("Advance", "20261029T123000Z", "a"),
            event(),
            event("Third", "20261223T133000Z", "t"),
        )
    )
    assert [p["estimate_kind"] for p in parsed.values()] == [
        "advance",
        "second",
        "third",
    ]
    h = next(iter(parse_official_html(html()).values()))
    assert h["release_at"] == "2026-11-25T13:30:00+00:00"
    assert h["event_id"] == "bea:gdp:2026-11-25:2026-Q3:second"


@pytest.mark.parametrize(
    "raw",
    [
        b"<html>blocked</html>",
        ics(event(seq=-1)),
        ics(event(status="BOGUS")),
        ics(event(), event(uid="duplicate")),
        ics(event(at="20260925T133000Z")),
        ics(event()).replace(b"20261125T133000Z", b"20261125"),
        ics(event()).replace(b"SUMMARY:", b"UID:extra\r\nSUMMARY:"),
    ],
)
def test_ambiguous_or_malformed_calendar_rejected(raw):
    with pytest.raises(GdpScheduleError):
        parse_official_ics(raw)


def test_fallback_preserves_tombstone_and_quarantines_move():
    backup = parse_official_html(html())
    for original in (ics(event(status="CANCELLED")), ics(event(at="20261126T133000Z"))):
        merged = backup_events(backup, parse_official_ics(original))
        assert merged["second"]["classification"] == "calendar_fallback_conflict"


def test_read_has_no_side_effect_and_strategy_quarters(tmp_path):
    scheduler, _, wire, _ = setup(tmp_path)
    strategy = GdpAutoStrategy(scheduler, tmp_path / "strategy.json", clock=lambda: AT)
    assert strategy.get()["enabled"] is False
    assert not list(tmp_path.iterdir()) and not wire.calls
    strategy.update(enabled=True, history_start="2026-Q3")
    assert strategy.get()["history_start"] == "2026-Q3"
    with pytest.raises(Exception):
        strategy.update(enabled=True, history_start="2026-09")


def test_calendar_future_only_and_separate_phase_targets(tmp_path):
    scheduler, _, wire, clock = setup(
        tmp_path,
        ics(
            event("Advance", "20261029T123000Z", "a"),
            event(),
            event("Third", "20261223T133000Z", "t"),
        ),
    )
    state = scheduler.refresh_calendar()
    assert [t["estimate_kind"] for t in state["targets"]] == ["second", "third"]
    assert len(state["calendar_publications"]) == 3 and not wire.calls
    assert scheduler.due_check()["action"] == "not_due"
    # Refresh at the event time, then one target and one low-priority history batch.
    clock[0] = datetime(2026, 11, 25, 13, 30, tzinfo=UTC)
    result = scheduler.due_check()
    assert result["state"] == "complete"
    targets = scheduler.status()["targets"]
    assert targets[0]["target"]["result"]["event_binding_status"] == "unconfirmed"
    assert targets[1]["target"]["state"] == "pending"
    assert scheduler.due_check()["state"] == "complete"
    assert scheduler.due_check()["action"] == "not_due"


def test_calendar_failure_uses_valid_cache_but_expired_stops(tmp_path):
    scheduler, _, wire, clock = setup(tmp_path)
    scheduler.refresh_calendar()

    def fail():
        raise GdpScheduleError("bea_calendar_http_500", http_status=500)

    scheduler.calendar_fetcher = fail
    scheduler.fallback_fetcher = lambda: html()
    state = scheduler.refresh_calendar()
    assert state["calendar"]["refresh_mode"] == "fallback"
    scheduler.fallback_fetcher = fail
    state = scheduler.refresh_calendar()
    assert state["calendar"]["refresh_mode"] == "cache"
    assert state["calendar_status"] == "fresh"
    clock[0] += timedelta(days=2)
    assert scheduler.due_check()["action"] == "calendar_unavailable"
    assert not wire.calls


@pytest.mark.parametrize(
    "code,status",
    [
        ("bea_calendar_http_429", 429),
        ("bea_calendar_tls_invalid", None),
        ("bea_calendar_redirect_rejected", None),
    ],
)
def test_throttle_tls_and_redirect_do_not_fallback(tmp_path, code, status):
    scheduler, _, _, _ = setup(tmp_path)

    def fail():
        raise GdpScheduleError(
            code, http_status=status, retry_at=AT + timedelta(hours=2)
        )

    scheduler.calendar_fetcher = fail
    scheduler.fallback_fetcher = lambda: pytest.fail("unexpected fallback")
    state = scheduler.refresh_calendar()
    assert len(state["calendar"]["refresh_attempts"]) == 1
    assert datetime.fromisoformat(
        state["calendar"]["next_refresh_at"]
    ) >= AT + timedelta(hours=2)


def test_moved_cancelled_removed_and_sequence_reversal(tmp_path):
    scheduler, _, _, _ = setup(tmp_path)
    scheduler.import_calendar(ics(event(seq=2)), AT)
    with pytest.raises(GdpScheduleError):
        scheduler.import_calendar(ics(event(seq=1)), AT)
    scheduler.import_calendar(ics(event(at="20261126T133000Z", seq=3)), AT)
    target = scheduler.status()["targets"][0]
    assert target["target"]["next_attempt_at"].startswith("2026-11-26")
    scheduler.import_calendar(ics(event(status="CANCELLED", seq=4)), AT)
    assert scheduler.status()["targets"][0]["calendar_execution_blocked"]
    scheduler.import_calendar(ics(event("Third", "20261223T133000Z", "third")), AT)
    assert next(
        t for t in scheduler.status()["targets"] if t["key"].endswith("second")
    )["calendar_execution_blocked"]


def test_jobs_idempotency_conflict_and_interrupted_write(tmp_path):
    _, runner, wire, _ = setup(tmp_path)
    command = dict(
        request_id=str(uuid4()), action="init", start="2026-Q3", end="2026-Q3"
    )
    job = runner.submit(command)
    assert job["state"] == "complete" and job["result"]["readback"]["verified"]
    calls = len(wire.calls)
    assert runner.submit(command) == job and len(wire.calls) == calls
    with pytest.raises(WebError):
        runner.submit(dict(command, start="2026-Q2"))
    job["state"] = "running"
    runner.journal.save(job)
    assert runner.submit(command)["state"] == "unknown"
    assert len(wire.calls) == calls


def test_calendar_interrupted_attempt_not_replayed(tmp_path):
    scheduler, _, wire, _ = setup(tmp_path)
    scheduler.refresh_calendar()
    state = scheduler._read()
    state["targets"][0]["target"].update(state="running", attempts=1)
    scheduler._save(state)
    assert scheduler.due_check()["action"] == "execution_interrupted"
    assert scheduler.status()["targets"][0]["target"]["state"] == "unknown"
    assert not wire.calls


def test_job_rejects_future_before_writes(tmp_path):
    _, runner, wire, _ = setup(tmp_path)
    with pytest.raises(WebError):
        runner.submit(
            dict(
                request_id=str(uuid4()), action="update", start="2026-Q4", end="2026-Q4"
            )
        )
    assert not wire.calls and not Path(runner.journal.directory).exists()


def test_source_did_not_return_old_target_is_partial_not_success(tmp_path):
    import json

    from market_data_foundation.historical.bls_cpi_direct_live import HttpResult

    _, runner, wire, _ = setup(tmp_path)
    command = dict(
        request_id=str(uuid4()), action="init", start="2026-Q3", end="2026-Q3"
    )
    assert runner.submit(command)["state"] == "complete"
    post = wire.post

    def without_target(*args, **kwargs):
        result = post(*args, **kwargs)
        doc = json.loads(result.body)
        data = doc["BEAAPI"]["Results"]["Data"]
        doc["BEAAPI"]["Results"]["Data"] = [
            row for row in data if row["TimePeriod"] != "2026Q3"
        ]
        return HttpResult(200, json.dumps(doc).encode(), {})

    wire.post = without_target
    result = runner.submit(dict(command, request_id=str(uuid4()), action="update"))
    assert result["state"] == "complete"
    assert result["result"]["completion"] == "source_coverage_partial"
    assert len(result["result"]["source_missing"]) == 8
    assert result["result"]["coverage"]["available"] == 8


def test_budget_stops_before_source_and_unknown_errors_do_not_retry(tmp_path):
    scheduler, runner, wire, clock = setup(tmp_path)
    scheduler.refresh_calendar()
    clock[0] = datetime(2026, 11, 25, 13, 30, tzinfo=UTC)
    scheduler.refresh_calendar()
    state = scheduler._read()
    state["data_admissions"] = [{"at": clock[0].isoformat(), "reserved_http": 80}]
    scheduler._save(state)
    assert scheduler.due_check()["action"] == "budget_exhausted"
    assert not wire.calls

    def crash(*args):
        raise RuntimeError("private text must not enter receipt")

    runner.service.init_range = crash
    result = runner.submit(
        dict(request_id=str(uuid4()), action="init", start="2026-Q3", end="2026-Q3")
    )
    assert result["state"] == "unknown"
    assert result["classification"] == "gdp_execution_unknown"
    assert "private text" not in str(result)


def test_actual_tls_failure_classification_blocks_fallback(monkeypatch):
    import ssl
    from urllib.error import URLError

    from market_data_foundation.historical import pce_release_schedule as bea
    from market_data_foundation.historical.calendar_resilience import may_fallback

    class Opener:
        def open(self, *args, **kwargs):
            raise URLError(ssl.SSLCertVerificationError("private certificate message"))

    monkeypatch.setattr(bea, "build_opener", lambda *args: Opener())
    with pytest.raises(GdpScheduleError) as error:
        bea.fetch_official_calendar()
    assert error.value.classification == "bea_calendar_tls_failure"
    assert not may_fallback(error.value)
