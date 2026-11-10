"""Offline full-query replay through real CPI/PPI hosts and persisted schedules."""

import copy
import json
from datetime import timedelta

import pytest

from market_data_foundation.historical.calendar_publications import publication_events
from market_data_foundation.historical.cpi_release_schedule import CpiReleaseScheduler
from tests.test_calendar_publications import files
from tests.test_cpi_release_schedule_ics import event, html, ics
from tests.test_cpi_timeseries_web import NOW, command, no_network  # noqa: F401
from tests.test_ppi_timeseries_web import client
from tests.test_ppi_timeseries_web import dual as dual_fixture


@pytest.fixture(name="dual")
def replay_assets(tmp_path):
    return dual_fixture.__wrapped__(tmp_path)


def prepare(dual, asset):
    app, hosts, sources, stores = dual
    host = hosts[asset]
    clock = [NOW]
    host.clock = host.scheduler.clock = lambda: clock[0]
    host.scheduler.reference_fetcher = None
    assert host.submit(command("init", start="2026-08"))["state"] == "complete"
    day = "14" if asset == "cpi" else "15"
    title = "Consumer Price Index" if asset == "cpi" else "Producer Price Index"
    raw = ics(event("past", at=f"202610{day}T083000", summary=title))
    host.scheduler.import_calendar(
        html(("September 2026", f"Oct. {day}, 2026", "08:30 AM")), NOW
    )
    host.scheduler.import_calendar(raw, NOW)
    return app, host, sources[asset], stores[asset], clock, title, raw


@pytest.mark.parametrize("asset", ["cpi", "ppi"])
@pytest.mark.parametrize("basis", ["observed", "available"])
@pytest.mark.parametrize(
    "change", ["refresh", "repeat", "retime", "cancel", "conflict"]
)
def test_complete_query_stays_identical_after_refresh_and_restart(
    dual, asset, basis, change, tmp_path
):
    app, host, source, store, clock, title, raw = prepare(dual, asset)
    cutoff = NOW + timedelta(seconds=10)
    params = dict(
        start="2026-09",
        end="2026-09",
        query_mode="history",
        local_only="true",
        as_of_basis=basis,
        as_of=cutoff.isoformat(),
    )
    before = {
        measure: host.query(dict(params, measure=measure))
        for measure in ["index", "change_1m_ratio"]
    }
    assert before["index"]["points"]
    assert before["index"]["publication_events"][0]["status"] == "scheduled"
    original = copy.deepcopy(store.status())
    calls = source.calls
    versions = len(host.scheduler.status()["calendar_publication_history"])
    clock[0] += timedelta(hours=1)
    if change in {"refresh", "repeat"}:
        host.scheduler.calendar_fetcher = lambda: raw
        host.scheduler.refresh_calendar()
        if change == "repeat":
            clock[0] += timedelta(minutes=1)
            host.scheduler.refresh_calendar()
        assert len(host.scheduler.status()["calendar_publication_history"]) == versions
    elif change == "retime":
        host.scheduler.import_calendar(
            ics(event("past", at="20261016T083000", sequence=2, summary=title)),
            clock[0],
        )
    elif change == "cancel":
        host.scheduler.import_calendar(
            ics(
                event(
                    "past",
                    at=None,
                    sequence=2,
                    summary=None,
                    extra="STATUS:CANCELLED\r\n",
                )
            ),
            clock[0],
        )
    else:
        # The real same-agency fallback accept path corroborates or blocks ICS;
        # it may not replace the primary event with the conflicting HTML date.
        with host.scheduler._locked():
            host.scheduler._accept_calendar(
                host.scheduler._read(),
                clock[0],
                html(("September 2026", "Oct. 16, 2026", "08:30 AM")),
                fallback=True,
            )
    saved = files(tmp_path)
    host.scheduler = CpiReleaseScheduler(
        None,
        host.scheduler.state_path,
        profile=host.profile,
        history_start=host.scheduler.history_start,
        clock=lambda: clock[0],
        calendar_fetcher=None,
        reference_fetcher=None,
        signal_fetcher=None,
    )
    with client(app) as http:
        for measure, prior in before.items():
            response = http.get(
                f"/api/r5/{asset}/timeseries/query",
                params=dict(params, measure=measure),
            )
            assert response.status_code == 200
            assert (
                response.json() == prior
            )  # Full result, including publication fields.
    current = host.query(dict(start="2026-09", end="2026-09", local_only="true"))
    actual = current["publication_events"][0]
    if change == "retime":
        assert actual["time"] == "2026-10-16T12:30:00+00:00"
    elif change in {"cancel", "conflict"}:
        assert actual["status"] == ("cancelled" if change == "cancel" else "unknown")
        assert actual["time"] is None
    else:
        assert actual == before["index"]["publication_events"][0]
    assert files(tmp_path) == saved
    assert store.status() == original and source.calls == calls


@pytest.mark.parametrize("asset", ["cpi", "ppi"])
@pytest.mark.parametrize("basis", ["observed", "available"])
def test_late_mapping_import_cannot_backdate_an_assembled_publication_view(
    dual, asset, basis, tmp_path
):
    _, hosts, sources, _ = dual
    host = hosts[asset]
    clock = [NOW]
    host.scheduler.clock = host.clock = lambda: clock[0]
    host.scheduler.reference_fetcher = None
    title = "Consumer Price Index" if asset == "cpi" else "Producer Price Index"
    host.scheduler.import_calendar(
        ics(event("past", at="20261014T083000", summary=title)), NOW
    )
    params = dict(
        start="2026-09",
        end="2026-09",
        local_only="true",
        as_of=NOW.isoformat(),
        as_of_basis=basis,
    )
    original = host.query(params)
    assert original["publication_events"] == []
    clock[0] += timedelta(days=1)
    host.scheduler.import_display_history(
        html(("September 2026", "Oct. 14, 2026", "08:30 AM")),
        NOW,
        source=host.scheduler.calendar_url,
        start="2026-09",
        end="2026-09",
    )
    saved = files(tmp_path)
    assert host.query(params) == original
    current = host.query({**params, "as_of": clock[0].isoformat()})
    assert current["publication_events"][0]["reference_month"] == "2026-09"
    history = host.scheduler.status()["calendar_publication_history"]
    assert history[-1]["observed_at"] == clock[0].isoformat()
    evidence = history[-1]["projection"]["calendar_publications"][0][
        "reference_month_evidence"
    ]
    assert evidence["acquired_at"] == NOW.isoformat()  # Original capture, not invented.
    assert files(tmp_path) == saved and sources[asset].calls == 0


@pytest.mark.parametrize("basis", ["observed", "available"])
def test_late_authority_cannot_mask_a_cutoff_eligible_target(basis):
    old = {
        "reference_month": "2026-09",
        "release_at": "2026-10-14T12:30:00Z",
        "acquired_at": NOW.isoformat(),
        "available_at": NOW.isoformat(),
        "reference_month_evidence": {"source": "https://www.bls.gov/schedule/"},
    }
    late = {**old, "acquired_at": (NOW + timedelta(days=1)).isoformat()}
    snapshot = {"targets": [old], "calendar_publications": [late]}
    expected = publication_events({"targets": [old]}, "https://www.bls.gov/", as_of=NOW)
    assert (
        publication_events(
            snapshot, "https://www.bls.gov/", as_of=NOW, as_of_basis=basis
        )
        == expected
    )


@pytest.mark.parametrize("asset", ["cpi", "ppi"])
def test_legacy_cache_is_not_invented_as_an_older_recorded_view(dual, asset):
    _, host, _, _, clock, _, raw = prepare(dual, asset)
    state = json.loads(host.scheduler.state_path.read_text())
    del state["calendar_publication_history"]
    host.scheduler.state_path.write_text(json.dumps(state))  # Isolated legacy fixture.
    clock[0] += timedelta(hours=1)
    host.scheduler.import_calendar(raw, clock[0])
    assert (
        publication_events(
            host.scheduler.status(),
            host.official_origin,
            as_of=NOW,
            as_of_basis="available",
        )
        == {}
    )


def test_recorded_view_observed_and_available_are_independently_enforced():
    from market_data_foundation.historical.calendar_publications import (
        record_publication_view,
    )

    row = {
        "reference_month": "2026-09",
        "release_at": "2026-10-14T12:30:00Z",
        "acquired_at": NOW.isoformat(),
        "reference_month_evidence": {"source": "https://www.bls.gov/schedule/"},
    }
    state = {}
    record_publication_view(state, {"targets": [row]}, NOW, "https://www.bls.gov/")
    state["calendar_publication_history"][0]["available_at"] = (
        NOW + timedelta(seconds=1)
    ).isoformat()
    assert publication_events(
        state, "https://www.bls.gov/", as_of=NOW, as_of_basis="observed"
    )
    assert not publication_events(
        state, "https://www.bls.gov/", as_of=NOW, as_of_basis="available"
    )


@pytest.mark.parametrize(
    "corrupt",
    ["not_list", "naive", "available_before_observed", "nested_history", "bad_rows"],
)
def test_invalid_journal_is_not_replaced_by_current_evidence(dual, corrupt):
    _, host, _, _, _, _, _ = prepare(dual, "cpi")
    state = json.loads(host.scheduler.state_path.read_text())
    history = state["calendar_publication_history"]
    if corrupt == "not_list":
        state["calendar_publication_history"] = {}
    elif corrupt == "naive":
        history[0]["observed_at"] = "2026-10-29T00:00:00"
    elif corrupt == "available_before_observed":
        history[0]["available_at"] = (NOW - timedelta(seconds=1)).isoformat()
    elif corrupt == "nested_history":
        history[0]["projection"]["calendar_publication_history"] = []
    else:
        history[0]["projection"]["calendar_publications"] = [None]
    host.scheduler.state_path.write_text(json.dumps(state))
    from market_data_foundation.historical.cpi_release_schedule import CpiScheduleError

    saved = host.scheduler.state_path.read_bytes()
    with pytest.raises(CpiScheduleError, match="schedule_state_invalid"):
        host.scheduler.status()
    assert host.scheduler.state_path.read_bytes() == saved
