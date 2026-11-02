"""Deterministic rule/holiday/DST/cache/fallback/retry/recovery and paused heartbeat."""

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from market_data_foundation.historical.claims_calendar import (
    BACKUP_URL,
    PRIMARY_URL,
    RULE,
    parse_official_rule,
)
from market_data_foundation.historical.claims_contract import SERIES_IDS, ClaimsError
from market_data_foundation.historical.claims_schedule import (
    ClaimsAutoStrategy,
    ClaimsReleaseScheduler,
)

AT = datetime(2026, 11, 2, 18, tzinfo=UTC)


def html():
    return (
        f"<p>{RULE}</p><table><tr><th>Release Date</th><th>Release Time</th></tr>"
        "<tr><td>Wednesday, November 25, 2026</td><td>8:30 AM EST</td></tr>"
        "<tr><td>Wednesday, November 10, 2027</td><td>8:30 AM EST</td></tr>"
        "<tr><td>Wednesday, November 24, 2027</td><td>8:30 AM EST</td></tr></table>"
    ).encode()


def setup(tmp_path, **kwargs):
    clock, calls = [AT], []
    result = [
        dict(
            state="complete",
            job_id="synthetic",
            classification=None,
            result={
                "material": dict(status="source_not_updated"),
            },
        )
    ]

    def submit(command, **options):
        calls.append((command, options))
        return copy.deepcopy(result[0])

    scheduler = ClaimsReleaseScheduler(
        SimpleNamespace(submit=submit),
        tmp_path / "claims.json",
        clock=lambda: clock[0],
        calendar_fetcher=kwargs.get("primary", html),
        fallback_fetcher=kwargs.get("backup", html),
    )
    return scheduler, clock, calls, result


def test_rule_generates_weeks_exceptions_dst_and_no_invented_reference_week():
    parsed = parse_official_rule(html(), PRIMARY_URL, AT)
    events = parsed["events"]
    assert len(events) > 50
    for date in ("2026-11-25", "2027-11-10", "2027-11-24"):
        assert events["dol-claims-" + date]["release_at"] == date + "T13:30:00+00:00"
        assert (
            events["dol-claims-" + date]["schedule_basis"] == "official_exception_table"
        )
    assert "dol-claims-2026-11-26" not in events
    assert events["dol-claims-2027-07-01"]["release_at"] == "2027-07-01T12:30:00+00:00"
    assert all(
        e["reference_week"] is None and e["planned_only"] for e in events.values()
    )
    assert parsed["rules_valid_until"] == "2027-12-31"
    for broken in (
        html().replace(b"Thursday morning", b"Friday morning"),
        html().replace(b"Wednesday, November 25", b"Thursday, November 25"),
        html().replace(b"8:30 AM EST", b"9:00 AM EST"),
    ):
        with pytest.raises(ClaimsError, match="layout_unrecognized"):
            parse_official_rule(broken, PRIMARY_URL, AT)


def test_worker_paused_heartbeat_idle_and_no_get_refresh(tmp_path):
    scheduler, clock, calls, _ = setup(tmp_path)
    strategy = ClaimsAutoStrategy(
        scheduler, tmp_path / "strategy.json", clock=lambda: clock[0]
    )
    assert strategy.get()["worker"]["state"] == "not_started"
    assert not scheduler.state_path.exists() and not strategy.strategy_path.exists()
    assert strategy.tick()["action"] == "paused"
    assert strategy.get()["worker"]["state"] == "paused"
    assert not scheduler.state_path.exists() and not calls
    strategy.update(enabled=True, history_start=scheduler.history_start)
    assert strategy.tick()["action"] == "not_due"
    assert strategy.get()["worker"]["state"] == "idle"
    assert scheduler.status()["calendar_status"] == "fresh"
    assert not calls
    clock[0] += timedelta(seconds=121)
    assert strategy.get()["worker"]["state"] == "stale"


def test_fallback_retention_refresh_failure_uses_valid_cache_then_expires(tmp_path):
    def fail():
        raise ClaimsError("claims_http_failure", http_status=503)

    scheduler, clock, _, _ = setup(tmp_path, primary=fail)
    assert scheduler.due_check()["action"] == "not_due"
    state = scheduler.status()
    assert state["calendar"]["source"] == BACKUP_URL
    assert state["calendar"]["refresh_mode"] == "fallback"
    assert len(state["calendar"]["refresh_attempts"]) == 2
    assert (
        __import__("pathlib").Path(state["calendar"]["raw_locator"]).read_bytes()
        == html()
    )
    scheduler.fallback_fetcher = fail
    clock[0] += timedelta(hours=7)
    scheduler.due_check()
    assert scheduler.status()["calendar_status"] == "fresh"
    assert scheduler.status()["calendar"]["consecutive_refresh_failures"] == 1
    assert scheduler.status()["calendar"]["events"]
    clock[0] += timedelta(hours=18)
    assert scheduler.due_check()["action"] == "calendar_unavailable"
    assert scheduler.status()["calendar_status"] == "stale"


def test_429_does_not_fallback_and_backs_off(tmp_path):
    calls = []

    def primary():
        raise ClaimsError(
            "claims_http_failure", http_status=429, retry_at=AT + timedelta(hours=1)
        )

    scheduler, _, _, _ = setup(
        tmp_path, primary=primary, backup=lambda: calls.append(1) or html()
    )
    assert scheduler.due_check()["action"] == "calendar_unavailable"
    assert not calls
    assert (
        scheduler.status()["calendar"]["next_refresh_at"]
        == (AT + timedelta(hours=1)).isoformat()
    )


def test_due_old_report_retry_six_actual_weeks_and_low_priority_history(tmp_path):
    scheduler, clock, calls, result = setup(tmp_path)
    scheduler.due_check()
    clock[0] = datetime(2026, 11, 5, 13, 30, tzinfo=UTC)
    first = scheduler.due_check()
    assert first["state"] == "awaiting_provider"
    command, options = calls[0]
    assert (
        command["end"] == "2026-11-05"
        and options["event_id"] == "dol-claims-2026-11-05"
    )
    assert scheduler.due_check()["action"] == "not_due"
    clock[0] += timedelta(seconds=15)
    actuals = {
        s: "2026-10-31" if s.startswith("IC") else "2026-10-24" for s in SERIES_IDS
    }
    result[0]["result"] = dict(
        material=dict(
            status="available", event_id=options["event_id"], actual_weeks=actuals
        ),
        series_coverage={s: dict(latest_week=w) for s, w in actuals.items()},
    )
    assert scheduler.due_check()["state"] == "complete"
    assert scheduler.due_check()["state"] == "complete"
    assert calls[-1][0]["action"] == "init"
    assert calls[-1][0]["start"] == scheduler.history_start
    assert len(calls) == 3
    assert scheduler.due_check()["action"] == "not_due"


def test_interrupted_and_calendar_move_or_removal_never_silently_replays(tmp_path):
    scheduler, clock, calls, _ = setup(tmp_path)
    scheduler.due_check()
    state = scheduler._read()
    target = state["targets"][0]
    target["target"].update(state="running", attempts=1)
    scheduler._save(state)
    assert scheduler.due_check()["action"] == "execution_interrupted"
    assert scheduler._read()["targets"][0]["target"]["state"] == "unknown"
    state = scheduler._read()
    state["targets"][0]["release_at"] = "2026-11-05T14:30:00+00:00"
    scheduler._accept(state, html(), AT)
    assert state["targets"][0]["calendar_execution_blocked"]
    clock[0] = datetime(2026, 11, 5, 15, tzinfo=UTC)
    scheduler.due_check()
    assert not calls


def test_calendar_refresh_preserves_cancelled_or_conflicting_event(tmp_path):
    scheduler, _, _, _ = setup(tmp_path)
    scheduler.due_check()
    state = scheduler._read()
    event = next(iter(state["calendar"]["events"].values()))
    event["status"] = "CANCELLED"
    scheduler._accept(state, html(), AT, source=BACKUP_URL)
    assert state["targets"][0]["calendar_execution_blocked"]


def test_rule_expiry_is_fail_closed_even_inside_24_hour_cache(tmp_path):
    scheduler, clock, calls, _ = setup(tmp_path)
    clock[0] = datetime(2027, 12, 31, 23, tzinfo=UTC)
    scheduler.due_check()
    assert scheduler.status()["calendar_status"] == "fresh"
    clock[0] += timedelta(hours=7)
    assert scheduler.status()["calendar_status"] == "stale"
    assert scheduler.due_check()["action"] == "calendar_unavailable"
    assert not calls


def test_moved_execution_block_remains_after_repeated_refresh(tmp_path):
    scheduler, _, _, _ = setup(tmp_path)
    scheduler.due_check()
    state = scheduler._read()
    state["targets"][0]["target"]["attempts"] = 1
    state["targets"][0]["release_at"] = "2026-11-05T14:30:00+00:00"
    scheduler._accept(state, html(), AT)
    scheduler._accept(state, html(), AT)
    assert state["targets"][0]["calendar_execution_blocked"]
