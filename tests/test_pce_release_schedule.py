"""Synthetic BEA calendar contracts; no real release/data qualification."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, call

import pytest

from market_data_foundation.historical.bls_timeseries_profile import PCE_PROFILE
from market_data_foundation.historical.pce_release_schedule import (
    ICS_URL,
    PceReleaseScheduler,
    PceScheduleError,
    parse_official_ics,
)
from tests.test_cpi_timeseries_web import no_network  # noqa: F401

NOW = datetime(2026, 10, 30, 14, tzinfo=UTC)


def ics(
    title="Personal Income and Outlays, September 2026",
    *,
    at="20261029T123000Z",
    key="DTSTART",
    uid="synthetic-bea",
    status="CONFIRMED",
):
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nSUMMARY:{title}\r\n{key}:{at}\r\nSTATUS:{status}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()


def scheduler(
    tmp_path, *, fetcher=None, service=None, clock=lambda: NOW, history_start="2006-10"
):
    if service is None:
        service = Mock(profile=PCE_PROFILE)
    if isinstance(service, Mock) and isinstance(service.query_range.return_value, Mock):
        service.query_range.return_value = {"points": [], "coverage_complete": False}
    return PceReleaseScheduler(
        service,
        tmp_path / "artifacts" / "pce" / "schedule.json",
        history_start=history_start,
        calendar_fetcher=fetcher,
        clock=clock,
    )


@pytest.mark.parametrize(
    "title,months",
    [
        (
            "Personal Income and Outlays, October and November 2025",
            ["2025-10", "2025-11"],
        ),
        (
            "Personal Income and Outlays, December 2025 and January 2026",
            ["2025-12", "2026-01"],
        ),
        ("Personal Income and Outlays, September 2026", ["2026-09"]),
        ("Personal Income and Outlays", []),
        ("Personal Income and Outlays, to be announced", []),
    ],
)
def test_explicit_months_only(title, months):
    event = next(iter(parse_official_ics(ics(title)).values()))
    assert event["reference_months"] == months
    assert event["source"] == ICS_URL
    assert (event["classification"] is None) == bool(months)


@pytest.mark.parametrize(
    "at,expected",
    [
        ("20260122T083000", "2026-01-22T13:30:00+00:00"),
        ("20260730T083000", "2026-07-30T12:30:00+00:00"),
    ],
)
def test_eastern_dst_to_utc(at, expected):
    event = next(
        iter(
            parse_official_ics(ics(at=at, key="DTSTART;TZID=America/New_York")).values()
        )
    )
    assert event["release_at"] == expected


@pytest.mark.parametrize(
    "at,key",
    [
        ("20261029T083000", "DTSTART"),
        ("20261101T013000", "DTSTART;TZID=America/New_York"),
        ("20260308T023000", "DTSTART;TZID=America/New_York"),
    ],
)
def test_floating_or_ambiguous_time_is_not_guessed(at, key):
    with pytest.raises(PceScheduleError):
        parse_official_ics(ics(at=at, key=key))


def test_unmapped_event_does_not_create_target(tmp_path):
    value = scheduler(tmp_path)
    result = value.import_calendar(ics("Personal Income and Outlays"), NOW)
    assert result["targets"] == []
    assert result["calendar"]["events"]
    assert result["calendar_status"] == "fresh"


def test_two_months_cancellation_and_removal(tmp_path):
    value = scheduler(tmp_path)
    result = value.import_calendar(
        ics("Personal Income and Outlays, October and November 2025"), NOW
    )
    assert [t["reference_month"] for t in result["targets"]] == ["2025-10", "2025-11"]
    assert len(result["calendar"]["events"]) == 1
    result = value.import_calendar(ics(status="CANCELLED"), NOW)
    assert all(t["calendar_execution_blocked"] for t in result["targets"])
    assert all(t["state"] == "calendar_removed" for t in result["targets"])


def test_readonly_status_no_state_or_fetch(tmp_path):
    fetch = Mock(side_effect=AssertionError("must_not_fetch"))
    value = scheduler(tmp_path, fetcher=fetch)
    assert value.status()["calendar_status"] == "missing"
    assert not value.state_path.exists()
    assert not value.state_path.parent.exists()
    fetch.assert_not_called()


def test_calendar_failure_backs_off_without_data_or_raw_error(tmp_path):
    fetch = Mock(side_effect=RuntimeError("synthetic-sensitive-exception"))
    service = Mock(profile=PCE_PROFILE)
    value = scheduler(tmp_path, fetcher=fetch, service=service)
    assert value.due_check()["action"] == "calendar_unavailable"
    assert value.due_check()["action"] == "calendar_unavailable"
    assert fetch.call_count == 1
    service.update_range.assert_not_called()
    assert "synthetic-sensitive-exception" not in value.state_path.read_text()
    assert (
        value.status()["calendar"]["next_refresh_at"]
        == (NOW + timedelta(seconds=300)).isoformat()
    )


def test_future_release_does_not_collect(tmp_path):
    service = Mock(profile=PCE_PROFILE)
    value = scheduler(tmp_path, service=service)
    value.import_calendar(ics(at="20261125T133000Z"), NOW)
    assert value.due_check()["action"] == "not_due"
    service.update_range.assert_not_called()


def test_collection_partial_retries_are_bounded_and_truthful(tmp_path):
    clock = [NOW]
    service = Mock(profile=PCE_PROFILE)
    service.update_range.return_value = {
        "persisted": True,
        "refresh_coverage_complete": False,
    }
    value = scheduler(tmp_path, service=service, clock=lambda: clock[0])
    value.import_calendar(ics(), NOW)
    for _ in range(10):
        value.due_check()
        clock[0] += timedelta(hours=6)
        value.import_calendar(ics(), clock[0])
    stage = value.status()["targets"][0]["target"]
    assert stage["state"] == "pending_exhausted"
    assert stage["classification"] == "bea_source_range_incomplete"
    assert service.update_range.call_count == 10
    value.due_check()
    assert service.update_range.call_count == 10


@pytest.mark.parametrize(
    "history_start,ranges",
    [
        (
            "2006-10",
            [
                ("2006-10", "2011-09"),
                ("2011-10", "2016-09"),
                ("2016-10", "2021-09"),
                ("2021-10", "2026-09"),
            ],
        ),
        ("2025-08", [("2025-08", "2026-09")]),
    ],
)
def test_current_first_then_configured_history_once_across_refresh_and_restart(
    tmp_path, history_start, ranges
):
    from market_data_foundation.historical.cpi_timeseries import month_range

    clock = [NOW]
    service = real_service(lambda: clock[0])
    service.update_range = Mock(wraps=service.update_range)
    options = dict(service=service, clock=lambda: clock[0], history_start=history_start)
    value = scheduler(tmp_path, **options)
    raw = combine(
        ics("Personal Income and Outlays, January 2025", at="20250228T133000Z"),
        ics(uid="current"),
        ics(
            "Personal Income and Outlays, October 2026",
            at="20261125T133000Z",
            uid="future",
        ),
    )
    assert [
        t["reference_month"] for t in value.import_calendar(raw, NOW)["targets"]
    ] == [
        "2026-09",
        "2026-10",
    ]
    service.update_range.assert_not_called()
    assert value.due_check()["action"] == "target_complete"
    service.update_range.assert_called_once_with(
        "2026-09", "2026-09", series_ids=PCE_PROFILE.series_ids, include_buffer=False
    )
    checked = []
    for index, (start, end) in enumerate(ranges):
        before = value.status()["targets"][0]["history"]
        clock[0] += timedelta(seconds=301)
        assert value.import_calendar(raw, clock[0])["targets"][0]["history"] == before
        value = scheduler(tmp_path, **options)
        expected = "history_complete" if index == len(ranges) - 1 else "history_pending"
        assert value.due_check()["action"] == expected
        service.update_range.assert_called_with(
            start, end, series_ids=PCE_PROFILE.series_ids, include_buffer=True
        )
        checked.extend(month_range(start, end))
        stage = value.status()["targets"][0]["history"]
        assert stage["attempts"] == index + 1
        assert stage["readback_verified"] is True
        assert stage["result"]["start_month"] == start
        assert stage["result"]["end_month"] == end
        assert stage["readback"]["start_month"] < start  # Calculation buffer verified.
    assert tuple(checked) == month_range(history_start, "2026-09")
    completed = value.status()["targets"][0]["history"]
    clock[0] += timedelta(hours=6)
    assert value.import_calendar(raw, clock[0])["targets"][0]["history"] == completed
    assert scheduler(tmp_path, **options).due_check()["action"] == "not_due"
    assert service.update_range.call_count == len(ranges) + 1


def test_unattempted_reference_month_cursor_uses_configured_history(tmp_path):
    clock = [NOW]
    service = real_service(lambda: clock[0])
    service.update_range = Mock(wraps=service.update_range)
    value = scheduler(tmp_path, service=service, clock=lambda: clock[0])
    value.import_calendar(ics(), NOW)
    state = value._read()
    # Pending state written before the cursor correction has no historical progress.
    state["targets"][0]["history"]["next_start"] = "2026-09"
    value._save(state)
    assert value.due_check()["action"] == "target_complete"
    clock[0] += timedelta(seconds=61)
    value.import_calendar(ics(), clock[0])
    assert value.due_check()["action"] == "history_pending"
    assert service.update_range.call_args_list == [
        call(
            "2026-09",
            "2026-09",
            series_ids=PCE_PROFILE.series_ids,
            include_buffer=False,
        ),
        call(
            "2006-10", "2011-09", series_ids=PCE_PROFILE.series_ids, include_buffer=True
        ),
    ]


def test_history_retry_keeps_progress_and_backoff_across_calendar_refresh(tmp_path):
    from market_data_foundation.historical.cpi_timeseries_source import CpiSourceError

    clock = [NOW]
    service = real_service(lambda: clock[0])
    update = service.update_range
    service.update_range = Mock(wraps=update)
    value = scheduler(tmp_path, service=service, clock=lambda: clock[0])
    value.import_calendar(ics(), NOW)
    assert value.due_check()["action"] == "target_complete"
    clock[0] += timedelta(seconds=61)
    assert value.due_check()["action"] == "history_pending"
    clock[0] += timedelta(seconds=301)
    service.update_range.side_effect = CpiSourceError("pce_timeseries_unavailable")
    assert value.due_check()["action"] == "history_awaiting_provider"
    failed = value.status()["targets"][0]["history"]
    assert failed["next_start"] == "2011-10"
    assert failed["attempts"] == 2
    assert failed["next_attempt_at"] == (clock[0] + timedelta(seconds=15)).isoformat()
    assert value.import_calendar(ics(), clock[0])["targets"][0]["history"] == failed
    value = scheduler(tmp_path, service=service, clock=lambda: clock[0])
    assert value.due_check()["action"] == "not_due"
    assert service.update_range.call_count == 3
    clock[0] += timedelta(seconds=15)
    service.update_range.side_effect = None
    assert value.due_check()["action"] == "history_pending"
    assert [entry.args for entry in service.update_range.call_args_list] == [
        ("2026-09", "2026-09"),
        ("2006-10", "2011-09"),
        ("2011-10", "2016-09"),
        ("2011-10", "2016-09"),
    ]
    assert value.status()["targets"][0]["history"]["next_start"] == "2016-10"


def test_calendar_sequence_reversal_is_rejected(tmp_path):
    value = scheduler(tmp_path)
    newer = ics().replace(b"END:VEVENT", b"SEQUENCE:2\r\nEND:VEVENT")
    value.import_calendar(newer, NOW)
    with pytest.raises(PceScheduleError, match="bea_calendar_sequence_reversal"):
        value.import_calendar(ics(), NOW)


def test_incomplete_local_evidence_cannot_complete_target(tmp_path):
    service = Mock(profile=PCE_PROFILE)
    service.update_range.return_value = {
        "persisted": True,
        "refresh_coverage_complete": True,
    }
    service.query_range.return_value = {"coverage_complete": True, "points": []}
    value = scheduler(tmp_path, service=service)
    value.import_calendar(ics(), NOW)
    assert value.due_check()["action"] == "target_awaiting_provider"
    assert (
        value.status()["targets"][0]["target"]["classification"]
        == "schedule_local_readback_incomplete"
    )


def test_interrupted_write_is_not_replayed(tmp_path):
    service = Mock(profile=PCE_PROFILE)
    value = scheduler(tmp_path, service=service)
    value.import_calendar(ics(), NOW)
    state = value._read()
    state["targets"][0]["target"]["state"] = "running"
    value._save(state)
    assert value.due_check()["action"] == "execution_interrupted"
    assert value.status()["targets"][0]["target"]["state"] == "unknown"
    service.update_range.assert_not_called()


def test_cross_asset_state_rejected(tmp_path):
    value = scheduler(tmp_path)
    value.state_path.parent.mkdir(parents=True)
    value.state_path.write_text('{"asset":"cpi","version":1}')
    with pytest.raises(PceScheduleError, match="schedule_state_asset_mismatch"):
        value.status()


def combine(*events):
    return (
        b"BEGIN:VCALENDAR\r\n"
        + b"".join(
            raw.split(b"BEGIN:VEVENT", 1)[1]
            .split(b"END:VCALENDAR", 1)[0]
            .join([b"BEGIN:VEVENT", b""])
            for raw in events
        )
        + b"END:VCALENDAR\r\n"
    )


def real_service(clock):
    from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesService
    from market_data_foundation.historical.cpi_timeseries_store import (
        InMemoryTimeSeriesStore,
    )
    from tests.test_pce_timeseries_web import Source

    return CpiTimeSeriesService(
        InMemoryTimeSeriesStore(profile=PCE_PROFILE, clock=clock),
        Source(),
        profile=PCE_PROFILE,
    )


def test_bootstrap_retains_catalog_only_latest_missing_and_future(tmp_path):
    service = real_service(lambda: NOW)
    value = scheduler(tmp_path, service=service)
    raw = combine(
        ics(
            "Personal Income and Outlays, January 2025",
            at="20250228T133000Z",
            uid="old",
        ),
        ics(uid="latest"),
        ics(
            "Personal Income and Outlays, October 2026",
            at="20261125T133000Z",
            uid="future",
        ),
    )
    view = value.import_calendar(raw, NOW)
    assert len(view["calendar"]["events"]) == 3
    assert [t["reference_month"] for t in view["targets"]] == ["2026-09", "2026-10"]
    assert all(t["history"]["next_start"] == "2006-10" for t in view["targets"])
    service.update_range("2026-09", "2026-09", include_buffer=False)
    other = scheduler(tmp_path / "held", service=service)
    assert [
        t["reference_month"] for t in other.import_calendar(raw, NOW)["targets"]
    ] == ["2026-10"]


def test_old_unattempted_queue_migrates_but_real_unfinished_work_survives(tmp_path):
    service = real_service(lambda: NOW)
    value = scheduler(tmp_path, service=service)
    value.import_calendar(ics(), NOW)
    state = value._read()
    state.pop("automatic_target_policy_version")
    for i, month in enumerate(("2025-01", "2025-02")):
        target = {
            "reference_month": month,
            "release_at": "2025-03-01T13:30:00+00:00",
            "target": {
                "state": "pending",
                "attempts": i,
                "next_attempt_at": (NOW + timedelta(hours=1)).isoformat(),
            },
            "history": {"state": "pending", "attempts": 0},
        }
        state["targets"].append(target)
    value._save(state)
    value.due_check()
    assert {t["reference_month"] for t in value.status()["targets"]} == {
        "2025-02",
        "2026-09",
    }


def test_one_preprobe_and_dense_post_release_retries(tmp_path):
    release = NOW + timedelta(seconds=30)
    clock = [NOW]
    service = real_service(lambda: clock[0])
    service.source.skip = {("DPCCRG", "2026-09")}
    value = scheduler(tmp_path, service=service, clock=lambda: clock[0])
    value.import_calendar(ics(at=release.strftime("%Y%m%dT%H%M%SZ")), NOW)
    assert value.due_check()["action"] == "target_awaiting_provider"
    assert service.source.calls == 1
    clock[0] += timedelta(seconds=15)
    assert value.due_check()["action"] == "not_due"
    assert service.source.calls == 1
    clock[0] = release
    for delay in (15, 30, 60, 120):
        assert value.due_check()["action"] == "target_awaiting_provider"
        stage = value.status()["targets"][0]["target"]
        assert (
            stage["next_attempt_at"]
            == (clock[0] + timedelta(seconds=delay)).isoformat()
        )
        assert stage["result"]["start_month"] == "2026-09"
        clock[0] += timedelta(seconds=delay)


def test_not_due_target_does_not_starve_due_history(tmp_path):
    service = real_service(lambda: NOW)
    value = scheduler(tmp_path, service=service)
    value.import_calendar(ics(), NOW)
    state = value._read()
    target = state["targets"][0]
    target["target"].update(state="complete")
    target["history"].update(next_attempt_at=NOW.isoformat())
    newer = {
        **target,
        "reference_month": "2026-10",
        "target": {
            "state": "awaiting_provider",
            "attempts": 1,
            "next_attempt_at": (NOW + timedelta(minutes=10)).isoformat(),
        },
        "history": {"state": "pending", "attempts": 0},
    }
    state["targets"].append(newer)
    value._save(state)
    assert value.due_check()["action"] == "history_pending"


def test_auto_persists_real_result_readback_range_and_projection(tmp_path):
    from market_data_foundation.web.cpi_timeseries_policy import automatic_executions

    service = real_service(lambda: NOW)
    service.source.skip = {("DPCCRG", "2026-08")}
    value = scheduler(tmp_path, service=service)
    value.import_calendar(ics(), NOW)
    assert value.due_check()["action"] == "target_complete"
    stage = value.status()["targets"][0]["target"]
    assert stage["result"]["provider_requests"] == 1
    assert stage["result"]["provider_attempts"] == 1
    assert stage["readback"]["coverage_complete"] is True
    assert stage["result"]["source_returned_series_months"] == [
        {"series_id": s, "month": "2026-09"} for s in sorted(PCE_PROFILE.series_ids)
    ]
    record = automatic_executions(value.status(), profile=PCE_PROFILE)[0]
    assert record["start"] == record["end"] == "2026-09"
    assert record["result"]["readback"] == stage["readback"]
    assert record["result"]["saved_points"] == 2


def test_retry_after_cooldown_wins_over_release_retry(tmp_path):
    from market_data_foundation.historical.cpi_timeseries_source import CpiSourceError

    service = real_service(lambda: NOW)
    error = CpiSourceError(
        "pce_timeseries_rate_limited",
        retry_at=NOW + timedelta(minutes=20),
        http_status=429,
    )
    service.update_range = Mock(side_effect=error)
    value = scheduler(tmp_path, service=service)
    value.import_calendar(ics(), NOW)
    assert value.due_check()["action"] == "target_awaiting_provider"
    assert (
        value.status()["targets"][0]["target"]["next_attempt_at"]
        == error.retry_at.isoformat()
    )


def test_successful_preprobe_requires_post_release_real_refresh(tmp_path):
    release = NOW + timedelta(seconds=30)
    clock = [NOW]
    service = real_service(lambda: clock[0])
    value = scheduler(tmp_path, service=service, clock=lambda: clock[0])
    value.import_calendar(ics(at=release.strftime("%Y%m%dT%H%M%SZ")), NOW)
    assert value.due_check()["action"] == "target_awaiting_provider"
    assert service.source.calls == 1
    clock[0] = release
    assert value.due_check()["action"] == "target_complete"
    assert service.source.calls == 2


def test_cached_target_cannot_replace_current_source_return(tmp_path):
    service = real_service(lambda: NOW)
    service.update_range("2026-09", "2026-09", include_buffer=False)
    value = scheduler(tmp_path, service=service)
    future = NOW + timedelta(seconds=30)
    value.import_calendar(ics(at=future.strftime("%Y%m%dT%H%M%SZ")), NOW)
    service.source.skip = {("DPCCRG", "2026-09")}
    assert value.due_check()["action"] == "target_awaiting_provider"
    stage = value.status()["targets"][0]["target"]
    assert stage["classification"] == "bea_source_range_incomplete"
    assert stage["result"]["coverage_complete"] is True
    assert stage["result"]["refresh_coverage_complete"] is False


def test_history_partial_and_buffer_gaps_verify_returned_rows(tmp_path):
    service = real_service(lambda: NOW)
    value = scheduler(tmp_path, service=service, history_start="2026-08")
    value.import_calendar(ics(), NOW)
    state = value._read()
    state["targets"][0]["target"]["state"] = "complete"
    state["targets"][0]["history"].update(
        next_start="2026-08", next_attempt_at=NOW.isoformat()
    )
    value._save(state)
    service.source.skip = {("DPCCRG", "2026-08"), ("DPCERG", "2025-08")}
    assert value.due_check()["action"] == "history_complete"
    stage = value.status()["targets"][0]["history"]
    assert stage["readback_verified"] is True
    assert stage["coverage_complete"] is False
    assert stage["source_missing"] == [{"series_id": "DPCCRG", "month": "2026-08"}]
    assert stage["buffer_source_missing"] == [
        {"series_id": "DPCERG", "month": "2025-08"}
    ]


def test_official_capture_offline_when_available():
    from pathlib import Path

    capture = Path(
        "/srv/market-data-foundation-control/transactions/"
        "MDF-PCE-TEMPLATE-20261030-R1/bea-official-calendar.ics"
    )
    if not capture.is_file():
        pytest.skip("public capture is a local integration artifact")
    raw = capture.read_bytes()
    events = parse_official_ics(raw)
    assert len(events) == 35
    assert sum(len(e["reference_months"]) for e in events.values()) == 36
    multi = next(e for e in events.values() if len(e["reference_months"]) == 2)
    assert multi["reference_months"] == ["2025-10", "2025-11"]
    assert multi["release_at"] == "2026-01-22T15:00:00+00:00"
    assert any(line.startswith(b" ") for line in raw.splitlines())
    assert all(
        datetime.fromisoformat(e["release_at"]).utcoffset() == timedelta(0)
        for e in events.values()
    )


def test_target_returned_row_lost_on_readback_fails(tmp_path):
    service = real_service(lambda: NOW)
    value = scheduler(tmp_path, service=service)
    value.import_calendar(ics(), NOW)
    query = service.query_range

    def lose(*args, **kwargs):
        result = query(*args, **kwargs)
        result["points"] = [p for p in result["points"] if p["series_id"] != "DPCCRG"]
        return result

    service.query_range = lose
    assert value.due_check()["action"] == "target_awaiting_provider"
    stage = value.status()["targets"][0]["target"]
    assert stage["classification"] == "schedule_local_readback_incomplete"
    assert stage["readback_verified"] is False
    assert stage["result"]["refresh_coverage_complete"] is True
