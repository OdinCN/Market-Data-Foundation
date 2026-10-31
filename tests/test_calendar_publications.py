"""Offline calendar display contracts shared by CPI/PPI/PCE."""

import copy
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI

from market_data_foundation.historical.calendar_publications import publication_events
from market_data_foundation.historical.cpi_release_schedule import CpiScheduleError
from market_data_foundation.web.cpi_timeseries_api import timeseries_router
from tests.test_cpi_release_schedule_ics import event, html, ics
from tests.test_cpi_timeseries_web import no_network  # noqa: F401
from tests.test_pce_release_schedule import ics as bea_ics
from tests.test_pce_timeseries_web import fixture as pce_fixture  # noqa: F401
from tests.test_ppi_timeseries_web import client, dual  # noqa: F401, F811


def files(root):
    return {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("asset", ["cpi", "ppi", "pce"])
def test_calendar_only_history_future_unknown_get_and_cutoff(asset, request, tmp_path):
    if asset == "pce":
        _, host, source, data, _ = request.getfixturevalue("pce_fixture")
        chunks = [
            bea_ics(
                "Personal Income and Outlays, May 2026",
                at="20260610T123000Z",
                uid="past",
            ),
            bea_ics(
                "Personal Income and Outlays, October 2026",
                at="20261125T133000Z",
                uid="future",
            ),
            bea_ics(
                "Personal Income and Outlays", at="20260901T123000Z", uid="unknown"
            ),
        ]
        raw = (
            b"BEGIN:VCALENDAR\r\n"
            + b"".join(
                b"BEGIN:VEVENT"
                + chunk.split(b"BEGIN:VEVENT", 1)[1].split(b"END:VCALENDAR", 1)[0]
                for chunk in chunks
            )
            + b"END:VCALENDAR\r\n"
        )
        host.scheduler.import_calendar(raw, host.clock())
    else:
        _, hosts, sources, stores = request.getfixturevalue("dual")
        host, source, data = hosts[asset], sources[asset], stores[asset]
        title = "Consumer Price Index" if asset == "cpi" else "Producer Price Index"
        host.scheduler.reference_fetcher = None
        host.scheduler.import_calendar(
            html(("October 2026", "Nov. 10, 2026", "08:30 AM")), host.clock()
        )
        host.scheduler.import_calendar(
            ics(
                event("future", summary=title),
                event("past", at="20260610T083000", summary=title),
                event("unknown", at="20260901T083000", summary=title),
            ),
            host.clock(),
        )
        before = copy.deepcopy(host.scheduler._read()["targets"])
        host.scheduler.import_display_history(
            html(("May 2026", "June 10, 2026", "08:30 AM")),
            host.clock(),
            source=host.scheduler.calendar_url,
            start="2026-05",
            end="2026-05",
        )
        assert host.scheduler._read()["targets"] == before
        assert "2026-05" not in before
    app = FastAPI()
    app.include_router(timeseries_router(host))
    before = files(tmp_path)
    with client(app) as http:
        for extra in (
            {},
            {"query_mode": "history", "as_of": "2026-06-01T00:00:00+00:00"},
        ):
            response = http.get(
                f"/api/r5/{asset}/timeseries/query",
                params={
                    "start": "2026-05",
                    "end": "2026-10",
                    "local_only": "true",
                    **extra,
                },
            )
            assert response.status_code == 200, response.text
            result = response.json()
            events = {e["reference_month"]: e for e in result["publication_events"]}
            assert set(events) == {"2026-05", "2026-10"}
            assert all(e["status"] == "scheduled" for e in events.values())
            assert events["2026-05"]["time"] == "2026-06-10T12:30:00+00:00"
            assert result["points"] == []
            assert result["metadata"]["publication_event"]["label"] == "官方日历时间"
    assert files(tmp_path) == before
    assert source.calls == 0 and data.status()["observation_count"] == 0


@pytest.mark.parametrize("origin", ["https://www.bls.gov/", "https://www.bea.gov/"])
def test_conflict_cancellation_and_calendar_retime_override_execution(origin):
    row = {
        "reference_month": "2026-05",
        "release_at": "2026-06-10T12:30:00Z",
        "reference_month_evidence": {
            "source": origin + "schedule",
            "reference_month": "2026-05",
        },
    }
    later = {**row, "release_at": "2026-06-11T12:30:00Z"}
    snapshot = {"targets": [row], "calendar_publications": [later]}
    assert (
        publication_events(snapshot, origin)["2026-05"]["time"] == later["release_at"]
    )
    snapshot["calendar_publications"].append(row)
    assert publication_events(snapshot, origin)["2026-05"]["status"] == "unknown"
    assert publication_events(snapshot, origin)["2026-05"]["time"] is None
    snapshot["calendar_publications"].append(
        {**row, "publication_status": "cancelled", "release_at": None}
    )
    assert publication_events(snapshot, origin)["2026-05"]["status"] == "cancelled"
    assert publication_events(snapshot, origin)["2026-05"]["time"] is None
    snapshot = {
        "targets": [{**row, "state": "calendar_removed"}],
        "calendar_publications": [later],
    }
    assert publication_events(snapshot, origin)["2026-05"]["status"] == "cancelled"
    row["reference_month_evidence"]["source"] = origin.rstrip("/") + ".invalid/schedule"
    assert publication_events({"targets": [row]}, origin) == {}


def test_bounded_annual_import_uses_explicit_month_and_preserves_queue(request):
    _, hosts, _, _ = request.getfixturevalue("dual")
    for asset, host in hosts.items():
        title = "Consumer Price Index" if asset == "cpi" else "Producer Price Index"
        raw = (
            "<table><tr><th>Date</th><th>Time</th><th>Release</th></tr>"
            "<tr><td>Wednesday, June 10, 2026</td><td>08:30 AM</td>"
            f"<td>{title} for March 2026</td></tr></table>"
        ).encode()
        scheduler = host.scheduler
        scheduler.import_display_history(
            raw,
            host.clock(),
            source="https://www.bls.gov/schedule/2026/",
            start="2026-03",
            end="2026-03",
        )
        value = scheduler.status()
        assert value["targets"] == []
        assert value["calendar_publications"][0]["reference_month"] == "2026-03"
        assert value["next_due"] is None
        saved = scheduler.state_path.read_bytes()
        for kwargs in (
            {
                "source": "https://third-party.invalid/",
                "start": "2026-03",
                "end": "2026-03",
            },
            {"source": scheduler.calendar_url, "start": "1999-01", "end": "2026-03"},
        ):
            with pytest.raises(CpiScheduleError):
                scheduler.import_display_history(raw, host.clock(), **kwargs)
        with pytest.raises(CpiScheduleError, match="acquired_at_in_future"):
            scheduler.import_display_history(
                raw,
                datetime(2027, 1, 1, tzinfo=UTC),
                source=scheduler.calendar_url,
                start="2026-03",
                end="2026-03",
            )
        assert scheduler.state_path.read_bytes() == saved


def test_display_mapping_survives_refresh_retime_and_dateless_cancellation(request):
    _, hosts, _, _ = request.getfixturevalue("dual")
    for asset, host in hosts.items():
        scheduler = host.scheduler
        title = "Consumer Price Index" if asset == "cpi" else "Producer Price Index"
        scheduler.import_calendar(
            ics(event("past", at="20260610T083000", summary=title)), host.clock()
        )
        scheduler.import_display_history(
            html(("May 2026", "June 10, 2026", "08:30 AM")),
            host.clock(),
            source=scheduler.calendar_url,
            start="2026-05",
            end="2026-05",
        )
        scheduler.import_calendar(
            ics(event("past", at="20260612T083000", sequence=2, summary=title)),
            host.clock(),
        )
        value = publication_events(scheduler.status(), host.official_origin)["2026-05"]
        assert value["time"] == "2026-06-12T12:30:00+00:00"
        assert scheduler.status()["targets"] == []
        scheduler.import_calendar(
            ics(
                event(
                    "past",
                    at=None,
                    sequence=3,
                    summary=None,
                    extra="STATUS:CANCELLED\r\n",
                )
            ),
            host.clock(),
        )
        value = publication_events(scheduler.status(), host.official_origin)["2026-05"]
        assert value["status"] == "cancelled" and value["time"] is None
        assert scheduler.status()["targets"] == []
        assert scheduler.status()["calendar"]["display_history"]


def test_bls_conflicting_month_evidence_never_assigns_one_event_twice(request):
    _, hosts, _, _ = request.getfixturevalue("dual")
    scheduler = hosts["cpi"].scheduler
    scheduler.import_calendar(
        ics(event("past", at="20260610T083000")), scheduler.clock()
    )
    scheduler.import_display_history(
        html(
            ("March 2026", "June 10, 2026", "08:30 AM"),
            ("May 2026", "June 10, 2026", "08:30 AM"),
        ),
        scheduler.clock(),
        source=scheduler.calendar_url,
        start="2026-03",
        end="2026-05",
    )
    events = publication_events(scheduler.status(), hosts["cpi"].official_origin)
    assert len(events) == 2
    assert all(e["status"] == "unknown" and e["time"] is None for e in events.values())
    assert scheduler.status()["targets"] == []
