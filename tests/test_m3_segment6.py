"""M3 current initialization, real evidence reads and persistent paused strategy."""

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_data_foundation.historical.cpi_release_schedule import CpiScheduleError
from market_data_foundation.historical.m3_schedule import M3Scheduler, parse_calendar
from market_data_foundation.web.m3_query_api import PREFIX, timeseries_router
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from tests.test_m3_capital_goods import no_network  # noqa: F401
from tests.test_m3_segment4 import host

CALENDAR = b"""<table><tr><td>Survey Month</td>
<td>Advance Report (8:30 a.m.)</td>
<td>Full Report (10:00 a.m.)</td></tr>
<tr><td>September 2026</td><td>10/27/2026</td><td>11/3/2026</td></tr>
<tr><td>October 2026</td><td>11/25/2026</td><td>12/3/2026</td></tr></table>"""


def test_initialization_uses_current_calendar_not_two_month_sample(tmp_path):
    h, s, _ = host(tmp_path)
    s.collect_range("2025-03", "2025-04", confirm_write=True)
    status = h.status()
    assert status["initialization"]["end"] == "2026-10"
    assert status["initialization"]["start"] == "2006-11"
    assert status["initialization"]["months"] == 240
    assert (
        status["initialization"]["anchor_basis"]
        == "previous_utc_calendar_month_not_release_readiness"
    )
    assert set(status["latest_months"].values()) == {"2025-04"}


def test_calendar_separates_stages_times_and_rejects_conflict():
    events = parse_calendar(CALENDAR)
    assert len(events) == 4
    assert (
        events[0]["dataset"] == "advm3"
        and events[0]["release_at"] == "2026-10-27T12:30:00+00:00"
    )
    assert (
        events[1]["dataset"] == "m3"
        and events[1]["release_at"] == "2026-11-03T15:00:00+00:00"
    )
    assert all(e["actual_at"] is None for e in events)
    with pytest.raises(CpiScheduleError):
        parse_calendar(CALENDAR.replace(b"11/3/2026", b"9/1/2026"))


def test_strategy_is_paused_without_fabricated_worker_and_persists(tmp_path):
    h, _, _ = host(tmp_path)
    assert (
        not h.strategy()["enabled"] and h.strategy()["worker"]["state"] == "not_started"
    )
    h.scheduler.import_calendar(
        CALENDAR, acquired_at=datetime(2026, 11, 2, 12, tzinfo=UTC)
    )
    h.update_strategy({"enabled": False})
    same, _, _ = host(tmp_path)
    assert same.strategy()["exists"] and not same.strategy()["enabled"]
    assert same.schedule()["calendar_status"] == "fresh"
    assert len(same.schedule()["targets"]) == 4
    assert same.strategy()["worker"]["last_heartbeat_at"] is None


def test_bounded_worker_tick_tracks_both_stage_events_once(tmp_path):
    calls = []
    at = datetime(2026, 11, 3, 16, tzinfo=UTC)

    def submit(command, **kwargs):
        calls.append((command, kwargs))
        return {"state": "complete", "result": {"coverage_complete": True}}

    scheduler = M3Scheduler(
        tmp_path / "calendar.json",
        history_start="2006-11",
        clock=lambda: at,
        submit=submit,
    )
    scheduler.import_calendar(CALENDAR, acquired_at=at)
    assert scheduler.due_check()["action"] == "release_update"
    assert calls[0][0]["start"] == calls[0][0]["end"] == "2026-09"
    assert scheduler.due_check()["action"] == "release_update" and len(calls) == 2
    assert calls[0][0]["request_id"] != calls[1][0]["request_id"]
    assert scheduler.due_check()["action"] == "already_checked"
    again = M3Scheduler(
        scheduler.state_path, history_start="2006-11", clock=lambda: at, submit=submit
    )
    assert again.due_check()["action"] == "already_checked"


def test_api_releases_empty_evidence_and_strategy_mutation_use_shared_guard(tmp_path):
    h, _, wire = host(tmp_path)
    app = FastAPI()
    app.include_router(timeseries_router(h))
    with TestClient(
        app, base_url=CANDIDATE_ORIGIN, client=("192.168.10.25", 1234)
    ) as http:
        response = http.get(PREFIX + "/releases?read_mode=event")
        assert response.status_code == 200
        data = response.json()
        assert (
            data["events"] == []
            and data["provider_requests"] == 0
            and not data["persisted"]
        )
        assert "official_release_evidence_not_available_at_cutoff" in data["missing"]
        assert (
            http.put(PREFIX + "/strategy", json={"enabled": False}).status_code == 403
        )
        response = http.put(
            PREFIX + "/strategy",
            json={"enabled": False},
            headers={"Origin": CANDIDATE_ORIGIN, "Content-Type": "application/json"},
        )
        assert response.status_code == 200 and not response.json()["enabled"]
    assert not wire.calls
