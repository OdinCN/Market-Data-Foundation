"""Offline failure injection across all six families; no provider IO."""

import copy
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from market_data_foundation.historical.bls_timeseries_profile import (
    CONSUMPTION_PROFILE,
    CPI_PROFILE,
    EMPLOYMENT_PROFILE,
    PCE_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.historical.consumption_schedule import (
    ConsumptionReleaseScheduler,
)
from market_data_foundation.historical.cpi_release_schedule import (
    CpiReleaseScheduler,
    CpiScheduleError,
)
from market_data_foundation.historical.pce_release_schedule import (
    PceReleaseScheduler,
    parse_official_html,
)
from market_data_foundation.historical.retail_release_scheduler import (
    RetailReleaseScheduler,
)
from tests.test_census_retail_release_schedule import table
from tests.test_cpi_release_schedule_ics import event, html, ics
from tests.test_cpi_timeseries_web import no_network  # noqa: F401
from tests.test_pce_release_schedule import ics as bea_ics

NOW = datetime(2026, 11, 2, 4, tzinfo=UTC)
ROW = ("October 2026", "Nov. 10, 2026", "08:30 AM")


def failing(code="calendar_http_500", **kwargs):
    return Mock(side_effect=CpiScheduleError(code, **kwargs))


def bea_html(date="November 25", year=2026, month="October 2026", time="8:30 AM"):
    return (
        f'<table id="release-schedule-table"><tr><th>Year {year}</th>'
        "<th></th><th>Release</th></tr>"
        f"<tr><td><div>{date}</div><small>{time}</small></td><td>News</td>"
        f"<td>Personal Income and Outlays, {month}</td></tr></table>"
    ).encode()


def annual(year, month=None, date=None):
    month = month or ("October 2026" if year == 2026 else "December 2026")
    date = date or ("November 17, 2026" if year == 2026 else "January 15, 2027")
    return (
        '<table id="calendar"><tr><th>Indicator</th><th>Release Date</th>'
        "<th>Time</th><th>Period Covered</th></tr>"
        "<tr><td>Advance Monthly Sales for Retail and Food Services</td>"
        f"<td>{date}</td><td>8:30 AM</td><td>{month}</td></tr></table>"
    ).encode()


@pytest.mark.parametrize(
    "profile,title",
    [
        (CPI_PROFILE, "Consumer Price Index"),
        (PPI_PROFILE, "Producer Price Index"),
        (EMPLOYMENT_PROFILE, "Employment Situation"),
    ],
)
def test_bls_fallback_preserves_uid_attempts_and_primary_recovery(
    tmp_path, profile, title
):
    primary = Mock(return_value=ics(event(summary=title)))
    backup = Mock(return_value=html(ROW))
    value = CpiReleaseScheduler(
        None,
        tmp_path / "state",
        clock=lambda: NOW,
        profile=profile,
        history_start="2006-10",
        calendar_fetcher=primary,
        reference_fetcher=backup,
        fallback_fetcher=backup,
        signal_fetcher=None,
    )
    value.refresh_calendar()
    state = value._read()
    state["targets"]["2026-10"]["target"].update(attempts=3, request_id="preserve-me")
    value._save(state)
    primary.side_effect = CpiScheduleError("calendar_http_500")
    result = value.refresh_calendar()
    target = result["targets"][0]
    assert result["calendar"]["refresh_mode"] == "fallback"
    assert result["calendar"]["source"] == value.calendar_url
    assert target["ics_uid"] == "cpi-oct"
    assert target["target"]["request_id"] == "preserve-me"
    assert target["target"]["attempts"] == 3
    assert not target["calendar_execution_blocked"]
    primary.side_effect = None
    result = value.refresh_calendar()
    assert result["calendar"]["refresh_mode"] == "primary"
    assert result["calendar"]["primary_error"] is None
    assert result["targets"][0]["target"]["request_id"] == "preserve-me"


@pytest.mark.parametrize("extra", ["STATUS:CANCELLED\r\n", "STATUS:TENTATIVE\r\n", ""])
def test_bls_backup_never_reverses_primary_cancellation_or_retime(tmp_path, extra):
    value = CpiReleaseScheduler(
        None,
        tmp_path / "s",
        clock=lambda: NOW,
        calendar_fetcher=lambda: ics(event(extra=extra, at="20261111T083000")),
        reference_fetcher=None,
        fallback_fetcher=lambda: html(ROW),
    )
    value.import_calendar(html(("October 2026", "Nov. 11, 2026", "08:30 AM")), NOW)
    value.refresh_calendar()
    value.calendar_fetcher = failing()
    result = value.refresh_calendar()
    assert result["targets"][0]["calendar_execution_blocked"]
    assert result["targets"][0]["release_at"] == "2026-11-11T13:30:00+00:00"


@pytest.mark.parametrize(
    "klass,profile",
    [
        (PceReleaseScheduler, PCE_PROFILE),
        (ConsumptionReleaseScheduler, CONSUMPTION_PROFILE),
    ],
)
def test_bea_fallback_cache_expiry_and_recovery(tmp_path, klass, profile):
    clock = [NOW]
    service = Mock(profile=profile)
    primary = Mock(
        return_value=bea_ics(
            "Personal Income and Outlays, October 2026", at="20261125T133000Z"
        )
    )
    backup = Mock(return_value=bea_html())
    value = klass(
        service,
        tmp_path / "s",
        clock=lambda: clock[0],
        calendar_fetcher=primary,
        fallback_fetcher=backup,
    )
    value.refresh_calendar()
    original = copy.deepcopy(value.status()["targets"][0]["target"])
    primary.side_effect = CpiScheduleError("bea_calendar_http_500")
    result = value.refresh_calendar()
    assert result["calendar"]["refresh_mode"] == "fallback"
    assert result["targets"][0]["uid"] == "synthetic-bea"
    assert result["targets"][0]["target"] == original
    assert result["calendar_publications"][0]["reference_month_evidence"][
        "source"
    ].endswith("/full")
    acquired = result["calendar"]["acquired_at"]
    backup.side_effect = TimeoutError("do not expose this")
    clock[0] += timedelta(hours=7)
    result = value.refresh_calendar()
    assert result["calendar_status"] == "fresh"
    assert result["calendar"]["refresh_mode"] == "cache"
    assert result["calendar"]["acquired_at"] == acquired
    clock[0] += timedelta(hours=18)
    assert value.due_check()["action"] == "calendar_unavailable"
    service.update_range.assert_not_called()
    primary.side_effect = None
    assert value.refresh_calendar()["calendar"]["refresh_mode"] == "primary"
    assert "do not expose" not in value.state_path.read_text()


@pytest.mark.parametrize(
    "status,date",
    [("CANCELLED", "20261125T133000Z"), ("CONFIRMED", "20261126T133000Z")],
)
def test_bea_backup_cannot_undo_cancellation_or_retime(tmp_path, status, date):
    value = PceReleaseScheduler(
        Mock(profile=PCE_PROFILE),
        tmp_path / "s",
        clock=lambda: NOW,
        calendar_fetcher=failing(),
        fallback_fetcher=lambda: bea_html(),
    )
    value.import_calendar(
        bea_ics("Personal Income and Outlays, October 2026", at=date, status=status),
        NOW,
    )
    result = value.refresh_calendar()
    assert result["targets"][0]["calendar_execution_blocked"]
    assert result["targets"][0]["calendar_validation"] == "calendar_fallback_conflict"


@pytest.mark.parametrize(
    "date,year,month,expected",
    [
        ("January 15", 2027, "December 2026", "2027-01-15T13:30:00+00:00"),
        ("July 30", 2026, "June 2026", "2026-07-30T12:30:00+00:00"),
    ],
)
def test_bea_explicit_year_and_eastern_dst(date, year, month, expected):
    result = parse_official_html(bea_html(date, year, month))
    assert next(iter(result.values()))["release_at"] == expected


@pytest.mark.parametrize(
    "raw",
    [
        bea_html(time=""),
        bea_html(month="to be announced"),
        bea_html().replace(b"Year 2026", b"Year unknown"),
    ],
)
def test_bea_does_not_invent_missing_period_year_or_clock(raw):
    with pytest.raises(CpiScheduleError):
        parse_official_html(raw)


def test_retail_annual_fallback_cross_year_and_no_historical_backlog(tmp_path):
    backup = Mock(side_effect=annual)
    value = RetailReleaseScheduler(
        Mock(),
        tmp_path / "s",
        clock=lambda: NOW,
        calendar_fetcher=failing(),
        history_calendar_fetcher=backup,
    )
    result = value.refresh_calendar()
    assert result["calendar"]["refresh_mode"] == "fallback"
    assert [t["reference_month"] for t in result["targets"]] == ["2026-10", "2026-12"]
    assert [c.args for c in backup.call_args_list] == [(2026,), (2027,)]
    assert len(result["calendar"]["sources"]) == 2
    state = value._read()
    state["targets"][0]["target"].update(state="complete", request_id="stable-id")
    value._save(state)
    value.refresh_calendar()
    assert value.status()["targets"][0]["target"]["request_id"] == "stable-id"
    assert value.status()["targets"][0]["target"]["state"] == "complete"
    value.calendar_fetcher = lambda: table(
        [("October 2026", "November 17, 2026"), ("December 2026", "January 15, 2027")]
    ).encode()
    assert value.refresh_calendar()["calendar"]["refresh_mode"] == "primary"


@pytest.mark.parametrize("date", ["Cancelled", "TBA", "November 18, 2026"])
def test_retail_annual_cannot_restore_cancelled_tba_or_older_date(tmp_path, date):
    value = RetailReleaseScheduler(
        Mock(),
        tmp_path / "s",
        clock=lambda: NOW,
        calendar_fetcher=failing(),
        history_calendar_fetcher=annual,
    )
    value.import_calendar(table([("October 2026", date)]).encode(), NOW)
    result = value.refresh_calendar()
    target = next(t for t in result["targets"] if t["reference_month"] == "2026-10")
    assert target["calendar_execution_blocked"]


@pytest.mark.parametrize("kind", ["bls", "bea", "retail"])
def test_429_does_not_escape_provider_cooldown_via_backup(tmp_path, kind):
    retry = NOW + timedelta(hours=8)
    backup = Mock()
    primary = failing("calendar_http_429", http_status=429, retry_at=retry)
    if kind == "bls":
        value = CpiReleaseScheduler(
            None,
            tmp_path / "s",
            clock=lambda: NOW,
            calendar_fetcher=primary,
            reference_fetcher=None,
            fallback_fetcher=backup,
        )
    elif kind == "bea":
        value = PceReleaseScheduler(
            Mock(profile=PCE_PROFILE),
            tmp_path / "s",
            clock=lambda: NOW,
            calendar_fetcher=primary,
            fallback_fetcher=backup,
        )
    else:
        value = RetailReleaseScheduler(
            Mock(),
            tmp_path / "s",
            clock=lambda: NOW,
            calendar_fetcher=primary,
            history_calendar_fetcher=backup,
        )
    result = value.refresh_calendar()
    backup.assert_not_called()
    assert result["calendar"]["next_refresh_at"] == retry.isoformat()
    assert result["calendar"]["refresh_attempts"][0]["http_status"] == 429


def test_bls_expired_cache_blocks_new_collection(tmp_path):
    clock = [NOW]
    service = Mock(profile=CPI_PROFILE)
    value = CpiReleaseScheduler(
        service,
        tmp_path / "s",
        clock=lambda: clock[0],
        calendar_fetcher=lambda: html(ROW),
        reference_fetcher=None,
    )
    value.refresh_calendar()
    value.calendar_fetcher = failing()
    clock[0] += timedelta(days=10)
    assert value.due_check()["action"] == "calendar_unavailable"
    service.update_range.assert_not_called()
    assert value.status()["calendar_status"] == "stale"


def test_bea_shared_backup_reuses_bytes_without_rejuvenating_acquisition(tmp_path):
    from market_data_foundation.historical.bea_shared_material import calendar_fetcher

    fetch = Mock(return_value=bea_html())
    clock = [NOW]
    shared = calendar_fetcher(
        tmp_path / "shared.json",
        fetcher=fetch,
        parser=parse_official_html,
        clock=lambda: clock[0],
    )
    for klass, profile in [
        (PceReleaseScheduler, PCE_PROFILE),
        (ConsumptionReleaseScheduler, CONSUMPTION_PROFILE),
    ]:
        value = klass(
            Mock(profile=profile),
            tmp_path / profile.key / "s",
            clock=lambda: clock[0],
            calendar_fetcher=failing(),
            fallback_fetcher=shared,
        )
        result = value.refresh_calendar()
        assert result["calendar"]["acquired_at"] == NOW.isoformat()
        assert (
            result["calendar"]["cache_valid_until"]
            == (NOW + timedelta(hours=24)).isoformat()
        )
        clock[0] += timedelta(hours=1)
    assert fetch.call_count == 1


def test_bls_backup_does_not_seed_past_jobs_or_renew_uncovered_primary_dates(tmp_path):
    backup = Mock(
        return_value=html(
            ("September 2026", "Oct. 14, 2026", "08:30 AM"),
            ROW,
            ("December 2026", "Jan. 13, 2027", "08:30 AM"),
        )
    )
    value = CpiReleaseScheduler(
        None,
        tmp_path / "s",
        clock=lambda: NOW,
        calendar_fetcher=failing(),
        reference_fetcher=None,
        fallback_fetcher=backup,
    )
    result = value.refresh_calendar()
    assert [t["reference_month"] for t in result["targets"]] == ["2026-10", "2026-12"]
    value.calendar_fetcher = lambda: ics(event())
    result = value.refresh_calendar()
    future = next(t for t in result["targets"] if t["reference_month"] == "2026-12")
    assert future["calendar_execution_blocked"]
    assert future["fallback_validation"] == "awaiting_primary_coverage"
