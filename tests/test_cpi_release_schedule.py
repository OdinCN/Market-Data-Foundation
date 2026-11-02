import fcntl
import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from market_data_foundation.historical.bls_timeseries_profile import CPI_PROFILE
from market_data_foundation.historical.cpi_release_schedule import (
    CALENDAR_MAX_AGE,
    CpiReleaseScheduler,
    CpiScheduleError,
    fetch_official_calendar,
    parse_official_calendar,
    update_window,
)
from market_data_foundation.historical.cpi_timeseries import month_range

SERIES_IDS = CPI_PROFILE.series_ids


def calendar_html(*rows):
    body = "".join(
        "<tr>" + "".join(f"<td>{value}</td>" for value in row) + "</tr>" for row in rows
    )
    return (
        "<table><thead><tr><th>Reference Month</th><th>Release Date</th>"
        "<th>Release Time</th></tr></thead><tbody>" + body + "</tbody></table>"
    ).encode()


OCTOBER = ("October 2026", "Nov. 10, 2026", "08:30 AM")
NOVEMBER = ("November 2026", "Dec. 10, 2026", "08:30 AM")


class Clock:
    def __init__(self, value="2026-11-10T13:30:00+00:00"):
        self.at = datetime.fromisoformat(value)

    def __call__(self):
        return self.at

    def advance(self, seconds):
        self.at += timedelta(seconds=seconds)


class Service:
    def __init__(self):
        self.updates = []
        self.queries = []
        self.available = False
        self.error = None
        self.result_complete = True
        self.refresh_complete = True
        self.source_missing = []
        self.local_available = True

    def update_range(self, start, end, *, series_ids, include_buffer):
        assert include_buffer is False
        self.updates.append((start, end, tuple(series_ids)))
        if self.error:
            raise self.error
        return {
            "points": [p for m in month_range(start, end) for p in self.points(m)]
            if self.available
            else self.points("2026-09"),
            "complete": self.result_complete,
            "refresh_complete": self.refresh_complete,
            "source_missing": self.source_missing,
            "series_ids": list(series_ids),
            "persisted": True,
            "provider_requests": 1,
            "provider_attempts": 1,
        }

    def points(self, month):
        return [
            {
                "series_id": series,
                "month": month,
                "index_value": "300.000",
                "available_at": "2026-11-10T13:30:00+00:00",
            }
            for series in SERIES_IDS
        ]

    def query_range(self, start, end, *, series_ids, allow_upstream, vintage_mode):
        assert allow_upstream is False
        assert vintage_mode == "available"
        self.queries.append((start, end, tuple(series_ids)))
        return {
            "points": [p for m in month_range(start, end) for p in self.points(m)]
            if self.local_available
            else []
        }


def scheduler(tmp_path, *, clock=None, service=None, rows=None):
    clock = clock or Clock()
    service = service or Service()
    obj = CpiReleaseScheduler(
        service,
        tmp_path / "schedule.json",
        clock=clock,
        signal_fetcher=lambda: b'<h1>Errata</h1><table id="errata_datatable"></table>',
        calendar_fetcher=lambda: calendar_html(*(rows or [OCTOBER, NOVEMBER])),
        reference_fetcher=None,
    )
    return obj, service, clock


def test_parser_uses_new_york_dst_and_reference_month():
    releases = parse_official_calendar(
        calendar_html(
            ("February 2026", "Mar. 11, 2026", "08:30 AM"),
            OCTOBER,
        )
    )
    assert releases[0]["reference_month"] == "2026-02"
    assert releases[0]["release_at"] == "2026-03-11T12:30:00+00:00"
    assert releases[1]["release_at"] == "2026-11-10T13:30:00+00:00"
    assert releases[0]["timezone"] == "America/New_York"


@pytest.mark.parametrize(
    "payload",
    [
        b"<html>Access denied</html>",
        calendar_html(("October 2026", "TBA", "08:30 AM")),
        calendar_html(("October 2026", "Nov. 10, 2026", "")),
        calendar_html(("October 2026", "Nov. 10, 2026", "13:30 PM")),
        calendar_html(("October 2026", "Oct. 10, 2026", "08:30 AM")),
        calendar_html(OCTOBER, ("October 2026", "Nov. 11, 2026", "08:30 AM")),
    ],
)
def test_parser_never_guesses_missing_or_conflicting_dates(payload):
    with pytest.raises(CpiScheduleError):
        parse_official_calendar(payload)


def test_calendar_only_does_not_need_service_or_database(tmp_path):
    obj = CpiReleaseScheduler(
        None,
        tmp_path / "schedule.json",
        clock=Clock(),
        signal_fetcher=lambda: b'<h1>Errata</h1><table id="errata_datatable"></table>',
        calendar_fetcher=lambda: calendar_html(OCTOBER),
    )
    result = obj.refresh_calendar()
    assert result["calendar_status"] == "fresh"
    raw_path = Path(result["calendar"]["raw_path"])
    assert raw_path.read_bytes() == calendar_html(OCTOBER)
    assert obj.status()["targets"][0]["state"] == "pending"
    with pytest.raises(CpiScheduleError, match="schedule_service_required"):
        obj.due_check()


def test_future_only_calendar_and_release_boundary(tmp_path):
    obj, service, clock = scheduler(tmp_path, clock=Clock("2026-11-10T13:29:59+00:00"))
    assert obj.due_check()["action"] == "not_due"
    assert service.updates == []
    clock.advance(1)
    result = obj.due_check()
    assert result["target_month"] == "2026-10"
    assert result["action"] == "awaiting_provider"
    assert service.updates[0][:2] == ("2026-10", "2026-10")


def test_old_month_success_does_not_complete_target_and_retries(tmp_path):
    obj, service, clock = scheduler(tmp_path)
    result = obj.due_check()
    assert result["action"] == "awaiting_provider"
    assert result["targets"][0]["classification"] == "target_month_not_available"
    assert service.queries == []
    clock.advance(299)
    assert obj.due_check()["action"] == "not_due"
    assert len(service.updates) == 1
    service.available = True
    clock.advance(1)
    result = obj.due_check()
    assert result["action"] == "updated"
    assert result["targets"][0]["state"] == "history_pending"
    assert len(service.queries) == 1
    assert obj.due_check()["action"] == "history_updated"
    assert obj.due_check()["action"] == "not_due"


def test_target_needs_fresh_source_and_local_readback(tmp_path):
    obj, service, clock = scheduler(tmp_path)
    service.available = True
    service.refresh_complete = False
    assert obj.due_check()["action"] == "awaiting_provider"
    service.refresh_complete = True
    service.local_available = False
    clock.advance(300)
    result = obj.due_check()
    assert result["action"] == "awaiting_provider"
    assert (
        result["targets"][0]["classification"] == "schedule_local_readback_incomplete"
    )


def test_january_refreshes_fixed_configured_history(tmp_path):
    assert update_window("2027-01") == ("1999-01", "2027-01", "configured_full_history")
    obj, service, _ = scheduler(
        tmp_path,
        clock=Clock("2027-02-11T13:30:00+00:00"),
        rows=[("January 2027", "Feb. 11, 2027", "08:30 AM")],
    )
    service.available = True
    assert obj.due_check()["action"] == "updated"
    assert service.updates[0][:2] == ("2027-01", "2027-01")
    assert obj.due_check()["action"] == "history_updated"
    assert service.updates[1][:2] == ("1999-01", "2027-01")


def test_retries_are_bounded_and_survive_restart(tmp_path):
    obj, service, clock = scheduler(tmp_path)
    for _ in range(5):
        result = obj.due_check()
        assert result["action"] == "awaiting_provider"
        next_at = result["targets"][0]["target"]["next_attempt_at"]
        if next_at:
            clock.at = datetime.fromisoformat(next_at)
    assert len(service.updates) == 5
    assert result["targets"][0]["target"]["state"] == "pending_exhausted"
    service.available = True
    restarted = CpiReleaseScheduler(
        service,
        obj.state_path,
        clock=clock,
        calendar_fetcher=obj.calendar_fetcher,
        signal_fetcher=lambda: b'<h1>Errata</h1><table id="errata_datatable"></table>',
    )
    restarted.due_check()
    assert len(service.updates) == 5


def test_expired_calendar_stops_new_collection_with_warning(tmp_path):
    obj, service, clock = scheduler(tmp_path)
    obj.refresh_calendar()
    clock.advance(int(CALENDAR_MAX_AGE.total_seconds()))

    def fail():
        raise RuntimeError("password=should-never-appear")

    obj.calendar_fetcher = fail
    result = obj.due_check()
    assert result["action"] == "calendar_unavailable"
    assert result["calendar_status"] == "stale"
    assert result["calendar"]["error"] == "calendar_refresh_failed"
    assert len(service.updates) == 0
    assert "password" not in obj.state_path.read_text()


def test_calendar_retime_respects_new_release(tmp_path):
    obj, service, clock = scheduler(tmp_path)
    obj.refresh_calendar()
    obj.calendar_fetcher = lambda: calendar_html(
        ("October 2026", "Nov. 11, 2026", "08:30 AM"), NOVEMBER
    )
    result = obj.refresh_calendar()
    assert result["calendar"]["changes"]["retimed"] == ["2026-10"]
    assert obj.due_check()["action"] == "not_due"
    assert service.updates == []
    clock.advance(86400)
    assert obj.due_check()["action"] == "awaiting_provider"


def test_rolling_calendar_removal_preserves_elapsed_evidence(tmp_path):
    obj, service, clock = scheduler(tmp_path)
    obj.refresh_calendar()
    obj.calendar_fetcher = lambda: calendar_html(NOVEMBER)
    result = obj.refresh_calendar()
    target = result["targets"][0]
    assert target["absent_from_latest_calendar"] is True
    assert target["state"] == "pending"
    service.available = True
    assert obj.due_check()["action"] == "updated"


def test_future_absence_preserves_plan_without_cancellation_evidence(tmp_path):
    obj, service, _ = scheduler(tmp_path, clock=Clock("2026-11-01T00:00:00+00:00"))
    obj.refresh_calendar()
    obj.calendar_fetcher = lambda: calendar_html(NOVEMBER)
    result = obj.refresh_calendar()
    assert result["targets"][0]["state"] == "pending"
    assert result["targets"][0]["absent_from_latest_calendar"] is True
    assert result["calendar"]["changes"]["removed"] == []
    assert obj.due_check()["action"] == "not_due"
    assert service.updates == []


def test_interrupted_update_reads_local_but_does_not_claim_refresh_success(tmp_path):
    obj, service, clock = scheduler(tmp_path)
    obj.refresh_calendar()
    document = json.loads(obj.state_path.read_text())
    document["targets"]["2026-10"]["target"].update(state="running", attempts=1)
    obj.state_path.write_text(json.dumps(document))
    result = obj.due_check()
    target = result["targets"][0]
    assert result["action"] == "interrupted"
    assert target["state"] == "awaiting_provider"
    assert service.updates == []
    clock.advance(300)
    service.available = True
    assert obj.due_check()["action"] == "updated"


def test_cross_process_lock_refuses_duplicate_execution(tmp_path):
    obj, service, _ = scheduler(tmp_path)
    lock = obj.state_path.with_suffix(".json.lock")
    with lock.open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(CpiScheduleError, match="schedule_busy"):
            obj.due_check()
    assert service.updates == []


def test_source_error_is_redacted_and_attempts_recorded(tmp_path):
    obj, service, _ = scheduler(tmp_path)
    service.error = RuntimeError("credential://very-secret")
    result = obj.due_check()
    assert result["targets"][0]["classification"] == "schedule_update_failed"
    assert "very-secret" not in json.dumps(result)


def test_missing_calendar_month_is_explicit_without_invented_timestamp(tmp_path):
    obj, service, _ = scheduler(
        tmp_path,
        clock=Clock("2026-10-28T12:00:00+00:00"),
        rows=[OCTOBER, ("December 2026", "Jan. 13, 2027", "08:30 AM")],
    )
    result = obj.refresh_calendar()
    assert result["calendar"]["missing_reference_months"] == ["2026-11"]
    assert result["calendar_coverage"] == "future_dates_available"
    assert all(t["reference_month"] != "2026-11" for t in result["targets"])
    assert obj.due_check()["action"] == "not_due"
    assert service.updates == []


def test_calendar_horizon_elapsed_is_visible(tmp_path):
    obj, service, _ = scheduler(tmp_path, rows=[OCTOBER])
    service.available = True
    result = obj.due_check()
    assert result["action"] == "updated"
    assert result["calendar_coverage"] == "no_future_dates"
    assert result["next_due"] is not None
    assert obj.due_check()["action"] == "history_updated"
    assert obj.status()["next_due"] is None


def test_calendar_refresh_rejects_clock_reversal(tmp_path):
    obj, _, clock = scheduler(tmp_path)
    obj.refresh_calendar()
    clock.advance(-1)
    with pytest.raises(CpiScheduleError, match="schedule_clock_reversal"):
        obj.refresh_calendar()


def test_old_local_target_cannot_mask_incomplete_current_provider_refresh(tmp_path):
    obj, service, _ = scheduler(tmp_path)
    service.available = True
    service.refresh_complete = False
    result = obj.due_check()
    assert result["action"] == "awaiting_provider"
    assert result["targets"][0]["last_result_complete"] is True
    assert result["targets"][0]["last_refresh_complete"] is False
    assert service.queries == []


@pytest.mark.parametrize(
    ("error", "classification"),
    [
        (
            HTTPError("https://www.bls.gov", 403, "secret", {}, None),
            "calendar_http_403",
        ),
        (
            HTTPError("https://www.bls.gov", 429, "secret", {}, None),
            "calendar_http_429",
        ),
        (TimeoutError("secret"), "calendar_timeout"),
        (URLError(TimeoutError("secret")), "calendar_timeout"),
        (URLError("secret"), "calendar_transport_failed"),
    ],
)
def test_calendar_transport_error_classification_is_explicit_and_safe(
    monkeypatch, error, classification
):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(
        "market_data_foundation.historical.cpi_release_schedule.fetch_bls_page", fail
    )
    with pytest.raises(CpiScheduleError, match=classification) as raised:
        fetch_official_calendar()
    assert "secret" not in str(raised.value)


def test_calendar_import_keeps_exact_raw_provenance_and_prior_403(tmp_path):
    clock = Clock("2026-10-28T04:00:00+00:00")

    def fail():
        raise CpiScheduleError("calendar_http_403")

    obj = CpiReleaseScheduler(
        None, tmp_path / "calendar.json", clock=clock, calendar_fetcher=fail
    )
    assert obj.refresh_calendar()["calendar_status"] == "unavailable"
    raw = calendar_html(OCTOBER)
    acquired_at = clock.at - timedelta(seconds=10)
    result = obj.import_calendar(raw, acquired_at)
    evidence = result["calendar"]
    assert evidence["acquisition_method"] == "operator_import"
    assert evidence["acquired_at"] == acquired_at.isoformat()
    assert evidence["imported_at"] == clock.at.isoformat()
    assert evidence["last_refresh_error"] == "calendar_http_403"
    assert Path(evidence["raw_path"]).read_bytes() == raw
    assert result["calendar_status"] == "fresh"


def test_calendar_import_rejects_future_or_naive_acquisition_time(tmp_path):
    obj, _, clock = scheduler(tmp_path)
    with pytest.raises(CpiScheduleError, match="calendar_acquired_at_in_future"):
        obj.import_calendar(calendar_html(OCTOBER), clock.at + timedelta(seconds=1))
    with pytest.raises(CpiScheduleError, match="schedule_aware_clock_required"):
        obj.import_calendar(calendar_html(OCTOBER), clock.at.replace(tzinfo=None))
    assert not obj.state_path.exists()


def test_uncached_calendar_failure_is_unavailable_with_bounded_retry(tmp_path):
    calls = []
    obj, service, clock = scheduler(tmp_path)

    def fail():
        calls.append(1)
        raise CpiScheduleError("calendar_http_403")

    obj.calendar_fetcher = fail
    assert obj.due_check()["action"] == "calendar_unavailable"
    assert obj.due_check()["action"] == "calendar_unavailable"
    assert len(calls) == 1
    clock.advance(300)
    assert obj.due_check()["action"] == "calendar_unavailable"
    assert len(calls) == 2
    assert service.updates == []


def test_known_calendar_horizon_completed_requires_new_dates(tmp_path):
    obj, service, clock = scheduler(tmp_path, rows=[OCTOBER])
    service.available = True
    obj.due_check()
    obj.due_check()
    clock.advance(86400)
    obj.calendar_fetcher = lambda: (_ for _ in ()).throw(
        CpiScheduleError("calendar_http_403")
    )
    result = obj.due_check()
    assert result["action"] == "calendar_unavailable"
    assert result["calendar_status"] == "stale"
    assert len(service.updates) == 2


def test_past_source_gap_does_not_hide_fresh_target_release(tmp_path):
    obj, service, _ = scheduler(tmp_path)
    service.available = True
    service.result_complete = False
    service.refresh_complete = False
    service.source_missing = [{"series_id": SERIES_IDS[0], "month": "2025-10"}]
    result = obj.due_check()
    assert result["action"] == "updated"
    target = result["targets"][0]
    assert target["target_refreshed"] is True
    assert target["target_available"] is True
    assert target["source_missing"] == service.source_missing


def test_target_gap_never_uses_preexisting_target_rows(tmp_path):
    obj, service, _ = scheduler(tmp_path)
    service.available = True
    service.refresh_complete = False
    service.source_missing = [{"series_id": SERIES_IDS[0], "month": "2026-10"}]
    assert obj.due_check()["action"] == "awaiting_provider"


@pytest.mark.parametrize(
    ("value", "availability"),
    [
        (None, "source_unavailable"),
        (None, "available"),
        ("300.000", "source_unavailable"),
    ],
)
def test_explicit_unavailable_target_is_not_a_release(tmp_path, value, availability):
    obj, service, _ = scheduler(tmp_path)
    service.available = True
    original_points = service.points

    def unavailable_points(month):
        points = original_points(month)
        points[0].update(index_value=value, availability=availability)
        return points

    service.points = unavailable_points
    result = obj.due_check()
    assert result["action"] == "awaiting_provider"
    assert result["targets"][0]["target_refreshed"] is False
    assert service.queries == []


def test_older_import_cannot_revert_more_recent_calendar(tmp_path):
    obj, _, clock = scheduler(tmp_path)
    obj.import_calendar(calendar_html(OCTOBER), clock.at)
    clock.advance(10)
    with pytest.raises(CpiScheduleError, match="calendar_import_older_than_current"):
        obj.import_calendar(calendar_html(OCTOBER), clock.at - timedelta(seconds=11))


@pytest.mark.parametrize(
    ("missing_month", "expected_action"),
    [
        ("2025-10", "updated"),
        ("2027-01", "source_unavailable"),
    ],
)
def test_actual_service_explicit_unavailable_is_not_confused_with_release(
    tmp_path, monkeypatch, missing_month, expected_action
):
    from market_data_foundation.historical import cpi_timeseries_source as source
    from market_data_foundation.historical.bls_cpi_direct_live import HttpResult
    from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesService
    from market_data_foundation.historical.cpi_timeseries_store import (
        InMemoryTimeSeriesStore,
    )

    clock = Clock("2027-02-11T13:30:00+00:00")
    monkeypatch.setattr(source, "load_registration_key", lambda _: "ab" * 16)

    class Transport:
        def post(self, endpoint, body, headers, *, timeout, max_response_bytes):
            request = json.loads(body)
            values = []
            for year in range(int(request["startyear"]), int(request["endyear"]) + 1):
                for month in range(1, 13):
                    unavailable = f"{year:04d}-{month:02d}" == missing_month
                    values.append(
                        {
                            "year": str(year),
                            "period": f"M{month:02d}",
                            "value": "-" if unavailable else "300.000",
                            "footnotes": [{"code": "X", "text": "Not available"}]
                            if unavailable
                            else [],
                        }
                    )
            raw = json.dumps(
                {
                    "status": "REQUEST_SUCCEEDED",
                    "message": [],
                    "Results": {
                        "series": [
                            {"seriesID": sid, "data": values}
                            for sid in request["seriesid"]
                        ]
                    },
                }
            ).encode()
            return HttpResult(200, raw, {})

    service = CpiTimeSeriesService(
        InMemoryTimeSeriesStore(),
        source.BlsRangeSource(transport=Transport(), clock=clock),
    )
    obj = CpiReleaseScheduler(
        service,
        tmp_path / "schedule.json",
        clock=clock,
        signal_fetcher=lambda: b'<h1>Errata</h1><table id="errata_datatable"></table>',
        calendar_fetcher=lambda: calendar_html(
            ("January 2027", "Feb. 11, 2027", "08:30 AM")
        ),
    )
    result = obj.due_check()
    assert result["action"] == expected_action
    target = result["targets"][0]
    assert len(target["source_unavailable"]) == (
        len(SERIES_IDS) if missing_month == "2027-01" else 0
    )
    assert target["target_refreshed"] == (expected_action == "updated")
    if expected_action == "updated":
        assert target["last_refresh_complete"] is True
        assert obj.due_check()["action"] == "history_updated"
        assert (
            obj.status()["targets"][0]["history"]["classification"]
            == "historical_source_gaps"
        )


def test_target_success_history_failure_has_independent_bounded_budget(tmp_path):
    obj, service, clock = scheduler(tmp_path)
    service.available = True
    first = obj.run_due()
    assert first["targets"][0]["target_available"] is True
    assert first["targets"][0]["history"]["state"] == "pending"
    assert len(service.updates) == 1
    service.error = RuntimeError("private raw error")
    for _ in range(5):
        result = obj.run_due()
        history = result["targets"][0]["history"]
        assert result["targets"][0]["target"]["state"] == "complete"
        if history["next_attempt_at"]:
            clock.at = datetime.fromisoformat(history["next_attempt_at"])
    assert history["state"] == "pending_exhausted"
    assert len(service.updates) == 6
    assert all(call[:2] == ("1999-01", "2026-10") for call in service.updates[1:])
    clock.advance(86400)
    obj.run_due()
    assert len(service.updates) == 6
    assert "private" not in obj.state_path.read_text()


def test_configured_history_does_not_drift_and_ordinary_days_do_not_refresh(tmp_path):
    obj, service, clock = scheduler(tmp_path)
    service.available = True
    obj.run_due()
    obj.run_due()
    for _ in range(3):
        clock.advance(86400)
        obj.run_due()
    assert [call[:2] for call in service.updates] == [
        ("2026-10", "2026-10"),
        ("1999-01", "2026-10"),
    ]
    clock.at = datetime.fromisoformat("2026-12-10T13:30:00+00:00")
    obj.run_due()
    obj.run_due()
    assert service.updates[-1][:2] == ("1999-01", "2026-11")
    changed = CpiReleaseScheduler(service, obj.state_path, history_start="2000-01")
    with pytest.raises(CpiScheduleError, match="schedule_history_start_changed"):
        changed.status()


def test_v1_complete_preserved_without_inventing_history_verification(tmp_path):
    obj, service, _ = scheduler(tmp_path)
    obj.refresh_calendar()
    state = json.loads(obj.state_path.read_text())
    state["version"] = 1
    state.pop("signals")
    state.pop("history_start")
    for target in state["targets"].values():
        target.pop("target")
        target.pop("history")
    state["targets"]["2026-10"].update(
        state="complete",
        completed_at="2026-11-10T13:30:00+00:00",
        attempts=3,
        next_attempt_at=None,
    )
    obj.state_path.write_text(json.dumps(state))
    target = obj.run_due()["targets"][0]
    assert target["state"] == "complete"
    assert target["completed_at"] == "2026-11-10T13:30:00+00:00"
    assert target["legacy_completion_unverified"] is True
    assert target["history"]["state"] == "legacy_unverified"
    assert service.updates == []
    assert json.loads(obj.state_path.read_text())["version"] == 2


def test_failed_attempt_marker_prevents_effect_and_failed_final_marker_recovers(
    tmp_path,
):
    obj, service, clock = scheduler(tmp_path)
    obj.refresh_calendar()
    service.available = True
    original = obj._save

    def fail(_):
        raise OSError("disk unavailable")

    obj._save = fail
    with pytest.raises(OSError):
        obj.run_due()
    assert service.updates == []
    obj._save = original
    calls = []

    def fail_final(state):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("disk unavailable")
        original(state)

    obj._save = fail_final
    with pytest.raises(OSError):
        obj.run_due()
    assert len(service.updates) == 1
    assert (
        json.loads(obj.state_path.read_text())["targets"]["2026-10"]["target"]["state"]
        == "running"
    )
    obj._save = original
    assert obj.run_due()["action"] == "interrupted"
    assert len(service.updates) == 1
    clock.advance(300)
    assert obj.run_due()["action"] == "updated"
    assert len(service.updates) == 2


def test_history_running_marker_recovers_without_refetching_target(tmp_path):
    obj, service, clock = scheduler(tmp_path)
    service.available = True
    obj.run_due()
    state = json.loads(obj.state_path.read_text())
    state["targets"]["2026-10"]["history"].update(state="running", attempts=1)
    obj.state_path.write_text(json.dumps(state))
    assert obj.run_due()["action"] == "interrupted"
    assert len(service.updates) == 1
    clock.advance(300)
    assert obj.run_due()["action"] == "history_updated"
    assert service.updates[-1][:2] == ("1999-01", "2026-10")
