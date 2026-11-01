"""Synthetic annual Census evidence, retention, precedence and read-only cutoff."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from market_data_foundation.historical.calendar_publications import publication_events
from market_data_foundation.historical.census_retail_release_schedule import (
    CALENDAR_URL,
    parse_annual_calendar,
)
from market_data_foundation.historical.retail_release_scheduler import (
    RetailReleaseScheduler,
)
from market_data_foundation.web.cpi_timeseries_api import TimeSeriesHost
from tests.test_census_retail_release_schedule import table

NOW = datetime(2026, 11, 1, tzinfo=UTC)


def annual(*rows):
    return (
        '<table id="calendar"><tr><th>Indicator</th><th>Release Date</th>'
        '<th>Time</th><th>Period Covered</th><td class="hiden">ignored</td></tr>'
        + "".join(
            "<tr><td>Advance Monthly Sales for Retail and Food Services</td>"
            f"<td>{date}</td><td>8:30 AM</td><td>{month}</td>"
            '<td class="hiden">not a month mapping</td></tr>'
            for month, date in rows
        )
        + "</table>"
    ).encode()


def scheduler(tmp_path, **kwargs):
    return RetailReleaseScheduler(
        None, tmp_path / "schedule.json", clock=lambda: NOW, **kwargs
    )


def test_explicit_delayed_cross_year_dst_and_exact_indicator():
    old = parse_annual_calendar(
        annual(("October 2025", "December 16, 2025")), year=2025
    )
    assert old[0]["reference_month"] == "2025-10"
    assert old[0]["release_at"] == "2025-12-16T13:30:00+00:00"
    raw = annual(
        ("November 2025", "January 14, 2026"), ("February 2026", "April 1, 2026")
    )
    rows = parse_annual_calendar(raw, year=2026)
    assert rows[0]["reference_month"] == "2025-11"
    assert rows[0]["release_at"] == "2026-01-14T13:30:00+00:00"
    assert rows[1]["release_at"] == "2026-04-01T12:30:00+00:00"
    with pytest.raises(Exception, match="advance_rows_missing"):
        parse_annual_calendar(
            raw.replace(b"Advance Monthly Sales", b"Monthly Sales"), year=2026
        )
    with pytest.raises(Exception, match="year_mismatch"):
        parse_annual_calendar(raw, year=2025)


def test_import_never_enqueues_and_recent_overrides_history_tba_cancel_retime(tmp_path):
    s = scheduler(tmp_path)
    s.import_history_calendar(
        annual(("October 2025", "December 16, 2025")), NOW, year=2025
    )
    s.import_history_calendar(
        annual(("November 2025", "January 14, 2026")), NOW, year=2026
    )
    assert s.status()["targets"] == []
    assert (
        s.status()["calendar"]["history_calendars"]["2025"]["events"][0][
            "reference_month_evidence"
        ]["period_covered"]
        == "October 2025"
    )
    for date, status, expected in (
        ("December 17, 2025", "scheduled", "2025-12-17T13:30:00+00:00"),
        ("TBA", "unknown", None),
        ("Cancelled", "cancelled", None),
    ):
        s.import_calendar(table([("October 2025", date)]).encode(), NOW)
        event = publication_events(s.status(), CALENDAR_URL)["2025-10"]
        assert (event["status"], event["time"]) == (status, expected)
    # Calendar-only import never modifies execution stages, including TBA stages.
    targets = s.status()["targets"]
    s.import_history_calendar(
        annual(("November 2025", "January 14, 2026")), NOW, year=2026
    )
    assert s.status()["targets"] == targets


def test_rolling_refresh_preserves_past_but_not_removed_future(tmp_path):
    s = scheduler(tmp_path)
    s.import_calendar(
        table(
            [
                ("August 2026", "September 16, 2026"),
                ("October 2026", "November 17, 2026"),
            ]
        ).encode(),
        NOW,
    )
    s.import_calendar(table([("November 2026", "December 16, 2026")]).encode(), NOW)
    events = publication_events(s.status(), CALENDAR_URL)
    assert events["2026-08"]["status"] == "scheduled"
    assert events["2026-10"]["status"] == "cancelled"
    assert events["2026-10"]["time"] is None


def test_independent_failures_low_frequency_and_retained_history(tmp_path):
    fetch = Mock(
        side_effect=lambda year: (
            annual(("October 2025", "December 16, 2025"))
            if year == 2025
            else annual(("November 2025", "January 14, 2026"))
        )
    )
    s = scheduler(tmp_path, history_calendar_fetcher=fetch)
    s.refresh_history_calendars()
    assert fetch.call_count == 2
    s.refresh_history_calendars()
    assert fetch.call_count == 2
    before = s.status()["calendar"]["history_calendars"]["2025"]["events"]
    later = NOW + timedelta(days=8)
    s.clock = lambda: later
    s.history_calendar_fetcher = Mock(side_effect=TimeoutError("private detail"))
    s.calendar_fetcher = lambda: table([("October 2026", "November 17, 2026")]).encode()
    s.refresh_calendar()
    s.refresh_history_calendars()
    assert s.status()["calendar"]["history_calendars"]["2025"]["events"] == before
    assert "private detail" not in str(s.status())
    assert (
        publication_events(s.status(), CALENDAR_URL)["2026-10"]["status"] == "scheduled"
    )
    s.calendar_fetcher = Mock(side_effect=TimeoutError())
    s.refresh_calendar()
    assert (
        publication_events(s.status(), CALENDAR_URL)["2025-10"]["status"] == "scheduled"
    )


@pytest.mark.parametrize(
    "asset,origin",
    [
        ("cpi", "https://www.bls.gov/"),
        ("ppi", "https://www.bls.gov/"),
        ("employment", "https://www.bls.gov/"),
        ("pce", "https://www.bea.gov/"),
        ("retail", CALENDAR_URL),
    ],
)
@pytest.mark.parametrize("native_cutoff", [False, True])
def test_five_family_projection_cutoff_and_read_only(asset, origin, native_cutoff):
    row = dict(
        reference_month="2025-10",
        release_at="2025-12-16T13:30:00+00:00",
        publication_status="scheduled",
        acquired_at=NOW.isoformat(),
        reference_month_evidence={"source": origin, "reference_month": "2025-10"},
    )
    snap = {"calendar_publications": [row]}
    host = SimpleNamespace(
        official_origin=origin, scheduler=SimpleNamespace(status=lambda: snap)
    )
    for delta, count in ((-1, 0), (0, 1)):
        result = dict(
            points=[{"month": "2025-10"}],
            start_month="2025-10",
            end_month="2025-10",
            as_of=(NOW + timedelta(microseconds=delta))
            if native_cutoff
            else (NOW + timedelta(microseconds=delta)).isoformat(),
        )
        value = TimeSeriesHost.publication_metadata(host, result)
        assert len(value["publication_events"]) == count
        assert bool(value["points"][0]["publication_event"]) == bool(count)
        assert value["metadata"]["publication_event"]["binds_current_revision"] is False
    # A cutoff cannot borrow newer mapping evidence for an older calendar event.
    row["reference_month_evidence"]["acquired_at"] = (
        NOW + timedelta(days=1)
    ).isoformat()
    assert publication_events(snap, origin, as_of=NOW.isoformat()) == {}


def test_annual_conflicts_withhold_time_and_import_rejects_reversal(tmp_path):
    s = scheduler(tmp_path)
    s.import_history_calendar(
        annual(("October 2025", "December 16, 2025")), NOW, year=2025
    )
    s.import_history_calendar(
        annual(("October 2025", "January 14, 2026")), NOW, year=2026
    )
    assert (
        publication_events(s.status(), CALENDAR_URL)["2025-10"]["status"] == "unknown"
    )
    before = s.state_path.read_bytes()
    with pytest.raises(Exception, match="older_than_current"):
        s.import_history_calendar(
            annual(("October 2025", "December 16, 2025")),
            NOW - timedelta(days=1),
            year=2025,
        )
    assert s.state_path.read_bytes() == before


def test_worker_automatically_imports_history_even_when_recent_fetch_fails(tmp_path):
    s = scheduler(
        tmp_path,
        calendar_fetcher=Mock(side_effect=TimeoutError()),
        history_calendar_fetcher=lambda year: annual(
            ("October 2025", "December 16, 2025")
            if year == 2025
            else ("November 2025", "January 14, 2026")
        ),
    )
    s.host = SimpleNamespace(
        auto_strategy=SimpleNamespace(_read=lambda: {"enabled": True}),
        submit=Mock(side_effect=AssertionError("no data jobs")),
    )
    assert s.due_check()["action"] == "calendar_unavailable"
    assert len(s.status()["calendar_publications"]) == 2
    assert s.status()["targets"] == []
    s.host.submit.assert_not_called()


def test_real_host_query_reads_history_without_fetch_or_writes(tmp_path, monkeypatch):
    from tests.test_retail_timeseries_web import retail

    _, runtime, data, wire, _ = retail(tmp_path, monkeypatch)
    host = runtime.retail_timeseries
    host.scheduler.import_history_calendar(
        annual(("October 2025", "December 16, 2025")), host.clock(), year=2025
    )
    saved = host.scheduler.state_path.read_bytes()
    host.scheduler.calendar_fetcher = Mock(
        side_effect=AssertionError("query calendar IO")
    )
    host.scheduler.history_calendar_fetcher = Mock(
        side_effect=AssertionError("query history IO")
    )
    host.scheduler._save = Mock(side_effect=AssertionError("query schedule write"))
    data.save_batch = Mock(side_effect=AssertionError("query DB write"))
    result = host.query({"start": "2025-10", "end": "2025-11", "local_only": "true"})
    assert result["publication_events"][0]["time"] == "2025-12-16T13:30:00+00:00"
    assert len(result["points"]) == 2
    assert host.scheduler.state_path.read_bytes() == saved
    host.scheduler.calendar_fetcher.assert_not_called()
    host.scheduler.history_calendar_fetcher.assert_not_called()
    assert not wire.calls


def test_current_and_history_calendar_share_census_admission(tmp_path, monkeypatch):
    from market_data_foundation.historical.census_retail_live import HttpResult
    from market_data_foundation.historical.cpi_request_control import CpiRequestControl
    from market_data_foundation.historical.retail_release_scheduler import (
        CensusCalendarTransport,
        calendar_fetcher,
    )

    calls = []

    def get(self, year=None):
        calls.append(year)
        return (
            HttpResult(200, annual(("October 2025", "December 16, 2025")), {})
            if year
            else HttpResult(429, b"", {"Retry-After": "7200"})
        )

    monkeypatch.setattr(CensusCalendarTransport, "get", get)
    control = CpiRequestControl(
        tmp_path / "census.json", clock=lambda: NOW, min_interval_seconds=0
    )
    fetch = calendar_fetcher(control)
    assert parse_annual_calendar(fetch(2025), year=2025)
    with pytest.raises(Exception, match="calendar_http_failure"):
        fetch()
    with pytest.raises(Exception, match="request_cooldown"):
        fetch(2026)
    assert calls == [2025, None]


@pytest.mark.parametrize("seconds_before_release", [0, 60, 180])
def test_history_maintenance_yields_to_imminent_collection(
    tmp_path, seconds_before_release
):
    release = datetime(2026, 11, 17, 13, 30, tzinfo=UTC)
    now = release - timedelta(seconds=seconds_before_release)
    fetch = Mock(
        side_effect=lambda year: annual(
            ("October 2025", "December 16, 2025")
            if year == 2025
            else ("November 2025", "January 14, 2026")
        )
    )
    s = RetailReleaseScheduler(
        None,
        tmp_path / "schedule.json",
        clock=lambda: now,
        history_calendar_fetcher=fetch,
    )
    s.host = SimpleNamespace(
        auto_strategy=SimpleNamespace(_read=lambda: {"enabled": True})
    )
    s.import_calendar(table([("October 2026", "November 17, 2026")]).encode(), now)
    s._run = Mock(return_value={"action": "collection_attempt"})
    outcome = s.due_check()
    assert fetch.call_count == (2 if seconds_before_release > 120 else 0)
    assert outcome["action"] == (
        "collection_attempt" if seconds_before_release == 0 else "not_due"
    )
    assert s._run.call_count == (1 if seconds_before_release == 0 else 0)
