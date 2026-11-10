"""Offline auto-worker policy tests; no BLS, database, Garage or service IO."""

import copy
import fcntl
import json
import socket
from datetime import UTC, datetime, timedelta

import pytest
from test_cpi_release_schedule import OCTOBER, Clock, Service, calendar_html

from market_data_foundation.historical.bls_cpi_direct_live import HttpResult
from market_data_foundation.historical.cpi_auto_strategy import CpiAutoStrategy
from market_data_foundation.historical.cpi_release_schedule import (
    AUTO_HTTP_CAP,
    CpiReleaseScheduler,
    CpiScheduleError,
    release_probe_at,
)
from market_data_foundation.historical.cpi_timeseries_source import (
    SERIES_IDS,
    BlsRangeSource,
    CpiSourceError,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("auto strategy tests must never open network connections")

    monkeypatch.setattr(socket, "create_connection", forbidden)


class ProviderError(RuntimeError):
    def __init__(self, classification, attempts=1, status=None, retry_at=None):
        self.classification = classification
        self.provider_attempts = attempts
        self.http_status = status
        self.retry_at = retry_at
        super().__init__(classification)


class AutoService(Service):
    def __init__(self):
        super().__init__()
        self.count = 1
        self.explicit_missing = False
        self.with_count = True

    def update_range(self, start, end, **kwargs):
        result = super().update_range(start, end, **kwargs)
        result["provider_attempts"] = self.count
        result["source_request_count"] = self.count
        if not self.with_count:
            result.pop("source_request_count")
            result.pop("provider_attempts")
        if self.explicit_missing:
            result.update(
                points=[],
                refreshed_series_months=[],
                source_unavailable=[
                    {"series_id": s, "month": end} for s in result["series_ids"]
                ],
                refresh_coverage_complete=True,
            )
        return result


def make(tmp_path, at="2026-11-10T13:30:00+00:00", *, rows=None):
    service, clock = AutoService(), Clock(at)
    obj = CpiReleaseScheduler(
        service,
        tmp_path / "schedule.json",
        clock=clock,
        calendar_fetcher=lambda: calendar_html(*(rows or [OCTOBER])),
        reference_fetcher=None,
        signal_fetcher=lambda: b'<h1>Errata</h1><table id="errata_datatable"></table>',
        auto_mode=True,
    )
    obj.refresh_calendar()
    return obj, service, clock


def target(result, month="2026-10"):
    return next(t for t in result["targets"] if t["reference_month"] == month)


def strategy(obj, clock, tmp_path):
    return CpiAutoStrategy(obj, tmp_path / "strategy.json", clock=clock)


def test_uncreated_get_does_not_write_or_collect(tmp_path):
    obj, service, clock = make(tmp_path)
    auto = strategy(obj, clock, tmp_path)
    assert auto.get() == {
        "exists": False,
        "enabled": False,
        "history_start": "1999-01",
        "history_start_locked": True,
        "worker": {"state": "not_started", "last_heartbeat_at": None},
        "next_due": None,
        "policy": auto.get()["policy"],
    }
    assert not auto.strategy_path.exists()
    assert service.updates == []
    assert service.queries == []


def test_strategy_save_pause_restart_and_history_change_are_durable(tmp_path):
    obj, service, clock = make(tmp_path)
    auto = strategy(obj, clock, tmp_path)
    before = obj.state_path.read_bytes()
    assert auto.update(enabled=True, history_start="1999-01")["enabled"]
    assert obj.state_path.read_bytes() == before
    assert service.updates == []
    auto.update(enabled=False, history_start="1999-01")
    restarted = strategy(obj, clock, tmp_path)
    assert restarted.tick()["action"] == "paused"
    assert service.updates == []
    assert obj.state_path.read_bytes() == before
    with pytest.raises(
        CpiScheduleError, match="schedule_history_start_changed"
    ) as error:
        restarted.update(enabled=True, history_start="2000-01")
    assert "单独扩展历史" in error.value.message
    assert restarted.get()["history_start"] == "1999-01"
    assert restarted.get()["enabled"] is False


def test_real_heartbeat_survives_restart_and_get_never_advances_it(tmp_path):
    obj, service, clock = make(tmp_path)
    auto = strategy(obj, clock, tmp_path)
    # A real worker may heartbeat before a user creates a strategy.
    first = auto.heartbeat("paused")
    assert first["exists"] is False
    beat = first["worker"]["last_heartbeat_at"]
    clock.advance(60)
    assert strategy(obj, clock, tmp_path).get()["worker"]["last_heartbeat_at"] == beat
    clock.advance(61)
    assert auto.get()["worker"]["state"] == "stale"
    assert auto.get()["worker"]["last_heartbeat_at"] == beat
    assert auto.update(enabled=True, history_start="1999-01")["exists"]
    service.available = True
    result = auto.tick()
    assert {k: result["worker"][k] for k in ("state", "last_heartbeat_at")} == {
        "state": "idle",
        "last_heartbeat_at": clock().isoformat(),
    }
    assert result["enabled"]


@pytest.mark.parametrize(
    "offset,next_offset",
    [
        (-31, -30),
        (-30, -15),
        (-15, 0),
        (0, 15),
        (105, 120),
        (120, 180),
        (540, 600),
        (600, 900),
        (1500, 1800),
        (1800, 3600),
        (5400, 7200),
        (7200, None),
    ],
)
def test_official_probe_windows(offset, next_offset):
    release = datetime(2026, 11, 10, 13, 30, tzinfo=UTC)
    expected = None if next_offset is None else release + timedelta(seconds=next_offset)
    assert release_probe_at(release, release + timedelta(seconds=offset)) == expected


def test_early_success_must_be_rechecked_after_official_time(tmp_path):
    obj, service, clock = make(tmp_path, "2026-11-10T13:29:30+00:00")
    service.available = True
    result = obj.due_check()
    item = target(result)
    assert result["action"] == "early_detected"
    assert item["release_at"] == "2026-11-10T13:30:00+00:00"
    assert item["auto"]["state"] == "early_detected"
    assert item["target"]["state"] != "complete"
    clock.advance(15)
    assert obj.due_check()["action"] == "not_due"
    assert len(service.updates) == 1
    clock.advance(15)
    result = obj.due_check()
    assert result["action"] == "updated"
    assert target(result)["auto"]["actual_http"] == 2
    assert target(result)["target_available"]
    assert obj.due_check()["action"] == "not_due"
    clock.advance(7205)
    assert obj.due_check()["action"] == "history_batch_updated"
    assert service.updates[2][:2] == ("1999-01", "2018-12")


def test_early_explicit_missing_and_http200_continue_observing(tmp_path):
    obj, service, clock = make(tmp_path, "2026-11-10T13:29:30+00:00")
    service.explicit_missing = True
    for expected in (
        "2026-11-10T13:29:45+00:00",
        "2026-11-10T13:30:00+00:00",
        "2026-11-10T13:30:15+00:00",
    ):
        result = obj.due_check()
        item = target(result)
        assert result["action"] == "awaiting_provider"
        assert item["auto"]["next_attempt_at"] == expected
        assert item["target"]["state"] != "source_unavailable"
        assert not item["target_available"]
        clock.at = datetime.fromisoformat(expected)


def test_actual_http_cap_counts_transport_not_logical_rounds_and_survives_restart(
    tmp_path,
):
    obj, service, clock = make(tmp_path)
    service.count = 10
    for index in range(3):
        result = obj.due_check()
        assert target(result)["auto"]["actual_http"] == (index + 1) * 10
        clock.advance(15)
    assert len(service.updates) == 3
    assert target(result)["auto"]["state"] == "stopped"
    assert target(result)["auto"]["actual_http"] == AUTO_HTTP_CAP
    restarted = CpiReleaseScheduler(
        service,
        obj.state_path,
        clock=clock,
        calendar_fetcher=obj.calendar_fetcher,
        reference_fetcher=None,
        auto_mode=True,
    )
    restarted.due_check()
    assert len(service.updates) == 3
    assert target(restarted.status())["auto"]["actual_http"] == 30


def test_spacing_and_busy_locks_do_not_spend_http_allowance(tmp_path):
    obj, service, clock = make(tmp_path)
    retry = clock() + timedelta(seconds=3)
    service.error = ProviderError(
        "cpi_timeseries_request_spacing", attempts=0, retry_at=retry
    )
    result = obj.due_check()
    assert target(result)["auto"]["actual_http"] == 0
    assert target(result)["auto"]["next_attempt_at"] == retry.isoformat()
    lock_path = obj.state_path.with_suffix(".json.lock")
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(CpiScheduleError, match="schedule_busy"):
            obj.due_check()
    assert len(service.updates) == 1
    assert target(obj.status())["auto"]["actual_http"] == 0


@pytest.mark.parametrize("status", [401, 403])
def test_auth_stops_without_retry(tmp_path, status):
    obj, service, clock = make(tmp_path)
    service.error = ProviderError("cpi_timeseries_http_rejected", status=status)
    result = obj.due_check()
    assert target(result)["auto"]["state"] == "stopped"
    clock.advance(60)
    obj.due_check()
    assert len(service.updates) == 1


@pytest.mark.parametrize("status", [400, 404, 409, 422])
def test_deterministic_http4xx_stops_and_preserves_diagnostics(tmp_path, status):
    obj, service, clock = make(tmp_path)
    obj.due_check()  # An earlier valid response missed the target month.
    clock.advance(15)
    service.error = ProviderError("bls_timeseries_http_failure", status=status)
    result = obj.due_check()
    item = target(result)
    assert item["auto"]["state"] == "stopped"
    assert item["auto"]["next_attempt_at"] is None
    assert item["auto"]["classification"] == "bls_timeseries_http_failure"
    assert item["auto"]["http_status"] == status
    assert item["auto"]["actual_http"] == 2
    assert not item["target_available"]
    clock.advance(60)
    obj.due_check()
    assert len(service.updates) == 2
    assert target(obj.status())["auto"]["actual_http"] == 2


@pytest.mark.parametrize(
    "suffix",
    [
        "provider_rejected",
        "response_invalid",
        "series_response_invalid",
        "index_invalid",
    ],
)
def test_real_source_http200_payload_errors_stop_auto_observation(tmp_path, suffix):
    obj, service, clock = make(tmp_path)
    document = {
        "status": "REQUEST_SUCCEEDED",
        "Results": {
            "series": [
                {
                    "seriesID": series,
                    "data": [{"year": "2026", "period": "M10", "value": "bad"}],
                }
                for series in SERIES_IDS
            ]
        },
    }
    if suffix == "provider_rejected":
        document["status"] = "REQUEST_FAILED"
    elif suffix == "response_invalid":
        document["Results"] = None
    elif suffix == "series_response_invalid":
        document["Results"]["series"] = [None]

    class Transport:
        def post(self, *args, **kwargs):
            return HttpResult(200, json.dumps(document).encode(), {})

    source = BlsRangeSource(registered=False, transport=Transport(), clock=clock)
    with pytest.raises(CpiSourceError) as error:
        source.fetch(SERIES_IDS, "2026-10", "2026-10")
    assert error.value.classification == "cpi_timeseries_" + suffix
    service.error = error.value
    item = target(obj.due_check())
    assert item["auto"]["state"] == "stopped"
    assert item["auto"]["next_attempt_at"] is None
    assert item["auto"]["classification"] == error.value.classification
    assert item["auto"]["http_status"] == 200
    assert item["auto"]["actual_http"] == 1
    assert not item["target_available"]
    assert item["target"]["state"] != "source_unavailable"
    clock.advance(60)
    obj.due_check()
    assert len(service.updates) == 1


@pytest.mark.parametrize(
    "classification",
    [
        "cpi_timeseries_duplicate_json_key",
        "cpi_timeseries_invalid_json",
        "cpi_timeseries_response_size_invalid",
        "cpi_timeseries_credential_echo_forbidden",
        "cpi_timeseries_point_invalid",
        "cpi_timeseries_period_invalid",
        "cpi_timeseries_duplicate_point",
        "cpi_timeseries_metadata_invalid",
        "cpi_timeseries_series_missing",
        "cpi_timeseries_content_encoding_rejected",
        "cpi_timeseries_observed_at_invalid",
        "bls_live_response_too_large",
    ],
)
def test_other_deterministic_source_validation_errors_stop(tmp_path, classification):
    obj, service, clock = make(tmp_path)
    service.error = ProviderError(classification, status=200)
    item = target(obj.due_check())
    assert item["auto"]["state"] == "stopped"
    assert item["auto"]["next_attempt_at"] is None
    assert item["auto"]["classification"] == classification
    assert item["auto"]["actual_http"] == 1
    clock.advance(60)
    obj.due_check()
    assert len(service.updates) == 1


@pytest.mark.parametrize(
    "classification,status",
    [
        ("bls_timeseries_http_failure", 408),
        ("bls_timeseries_http_failure", 503),
        ("bls_timeseries_transport_failure", None),
        ("cpi_timeseries_timeout", None),
    ],
)
def test_transient_errors_keep_30_60_120_second_backoff(
    tmp_path, classification, status
):
    obj, service, clock = make(tmp_path)
    service.error = ProviderError(classification, status=status)
    for count, delay in enumerate((30, 60, 120), 1):
        item = target(obj.due_check())
        assert item["auto"]["state"] == "awaiting_provider"
        assert (
            item["auto"]["next_attempt_at"]
            == (clock() + timedelta(seconds=delay)).isoformat()
        )
        assert item["auto"]["actual_http"] == count
        clock.advance(delay)
    assert len(service.updates) == 3


def test_http200_daily_quota_remains_terminal(tmp_path):
    obj, service, clock = make(tmp_path)
    service.error = ProviderError(
        "cpi_timeseries_provider_daily_limit",
        status=200,
        retry_at=clock() + timedelta(hours=24),
    )
    item = target(obj.due_check())
    assert item["auto"]["state"] == "stopped"
    assert item["auto"]["next_attempt_at"] is None
    assert item["auto"]["classification"] == "cpi_timeseries_provider_daily_limit"
    assert item["auto"]["actual_http"] == 1
    clock.advance(60)
    obj.due_check()
    assert len(service.updates) == 1


def test_5xx_exponential_retry_then_429_minimum_and_retry_after(tmp_path):
    obj, service, clock = make(tmp_path)
    for delay in (30, 60, 120):
        service.error = ProviderError("cpi_timeseries_provider_http_error", status=503)
        result = obj.due_check()
        next_at = datetime.fromisoformat(target(result)["auto"]["next_attempt_at"])
        assert next_at == clock() + timedelta(seconds=delay)
        clock.at = next_at
    retry = clock() + timedelta(seconds=600)
    service.error = ProviderError(
        "cpi_timeseries_rate_limited", status=429, retry_at=retry
    )
    result = obj.due_check()
    assert target(result)["auto"]["next_attempt_at"] == retry.isoformat()
    assert target(result)["auto"]["actual_http"] == 4


def test_continuous_429_beyond_thirty_minutes_uses_shared_retry_at(tmp_path):
    obj, service, clock = make(tmp_path)
    for delay in (300, 1800, 1800, 1800):
        retry = clock() + timedelta(seconds=delay)
        service.error = ProviderError(
            "cpi_timeseries_rate_limited", status=429, retry_at=retry
        )
        result = obj.due_check()
        assert target(result)["auto"]["state"] == "awaiting_provider"
        assert target(result)["auto"]["next_attempt_at"] == retry.isoformat()
        clock.at = retry
    assert target(result)["auto"]["actual_http"] == 4


def test_unknown_request_accounting_never_reports_success(tmp_path):
    obj, service, clock = make(tmp_path)
    service.available = True
    service.with_count = False
    result = obj.due_check()
    item = target(result)
    assert not item["target_available"]
    assert item["auto"]["state"] == "stopped"
    assert item["auto"]["classification"] == "schedule_actual_http_unknown"


def test_interruption_keeps_known_counts_and_recovers_without_reset(tmp_path):
    obj, service, clock = make(tmp_path)
    obj.due_check()
    state = json.loads(obj.state_path.read_text())
    state["targets"]["2026-10"]["auto"].update(state="running", actual_http=4)
    obj._save(state)
    clock.advance(15)
    service.local_available = False
    result = obj.due_check()
    item = target(result)
    assert result["action"] == "interrupted"
    assert item["auto"]["actual_http"] == 4
    assert item["auto"]["uncertain_http"] == 1
    assert len(service.updates) == 1
    clock.advance(300)
    service.available = True
    service.local_available = True
    assert obj.due_check()["action"] == "updated"
    assert target(obj.status())["auto"]["actual_http"] == 5


def test_catch_up_collects_targets_before_one_batched_history_pass_and_keeps_old_state(
    tmp_path,
):
    rows = [("September 2026", "Oct. 13, 2026", "08:30 AM"), OCTOBER]
    obj, service, clock = make(tmp_path, "2026-11-11T13:30:00+00:00", rows=rows)
    state = obj._read()
    seed = copy.deepcopy(state["targets"]["2026-09"])
    for year in range(2012, 2026):
        old = copy.deepcopy(seed)
        old.update(state="complete", custom_progress={"preserved": year})
        old["target"].update(state="complete", attempts=3)
        old["history"].update(state="complete", attempts=2)
        state["targets"][f"{year}-09"] = old
    obj._save(state)
    service.available = True
    assert obj.due_check()["target_month"] == "2026-10"
    assert obj.due_check()["target_month"] == "2026-09"
    assert [item[:2] for item in service.updates] == [
        ("2026-10", "2026-10"),
        ("2026-09", "2026-09"),
    ]
    assert obj.due_check()["action"] == "history_batch_updated"
    assert service.updates[-1][:2] == ("1999-01", "2018-12")
    clock.advance(300)
    assert obj.due_check()["action"] == "history_updated"
    assert service.updates[-1][:2] == ("2019-01", "2026-10")
    assert obj.due_check()["action"] == "not_due"
    assert len(service.updates) == 4
    final = obj._read()
    for year in range(2012, 2026):
        old = final["targets"][f"{year}-09"]
        assert old["custom_progress"] == {"preserved": year}
        assert old["target"]["attempts"] == 3
        assert old["history"]["attempts"] == 2
    assert final["targets"]["2026-09"]["history"]["state"] == "complete"
    assert (
        final["targets"]["2026-09"]["history"]["satisfied_by_monthly_history"]
        == "2026-10"
    )
    assert final["targets"]["2026-10"]["collection_kind"] == "catch_up"


def test_next_due_reports_the_pre_release_probe_without_mutating_state(tmp_path):
    obj, service, clock = make(tmp_path, "2026-11-10T12:00:00+00:00")
    before = obj.state_path.read_bytes()
    assert obj.status()["next_due"] == "2026-11-10T13:29:30+00:00"
    auto = strategy(obj, clock, tmp_path)
    auto.update(enabled=True, history_start="1999-01")
    assert auto.get()["next_due"] == "2026-11-10T13:29:30+00:00"
    assert obj.state_path.read_bytes() == before
    assert service.updates == []


def test_auto_retime_retains_actual_budget_and_rechecks_calendar_once(tmp_path):
    obj, service, clock = make(tmp_path, "2026-11-10T13:29:30+00:00")
    obj.due_check()
    changed = calendar_html(("October 2026", "Nov. 11, 2026", "08:30 AM"))
    obj.calendar_fetcher = lambda: changed
    obj.refresh_calendar()
    assert obj.due_check()["action"] == "not_due"
    item = target(obj.status())
    assert item["auto"]["actual_http"] == 1
    assert item["auto"]["next_attempt_at"] == "2026-11-11T13:29:30+00:00"
    assert len(service.updates) == 1
    calls = []

    def counted_calendar():
        calls.append(1)
        return changed

    obj.calendar_fetcher = counted_calendar
    clock.at = datetime.fromisoformat(item["auto"]["next_attempt_at"])
    obj.due_check()
    assert len(calls) == 1
    clock.advance(15)
    obj.due_check()
    assert len(calls) == 1
    assert target(obj.status())["auto"]["actual_http"] == 3


def test_auto_calendar_cancellation_prevents_a_previously_early_recheck(tmp_path):
    from test_cpi_release_schedule_ics import event, ics

    obj, service, clock = make(tmp_path, "2026-11-10T13:29:30+00:00")
    obj.import_calendar(ics(event()), clock())
    obj.calendar_fetcher = lambda: ics(event())
    service.available = True
    assert obj.due_check()["action"] == "early_detected"
    obj.import_calendar(ics(event(sequence=2, extra="STATUS:CANCELLED\r\n")), clock())
    clock.advance(30)
    assert obj.due_check()["action"] == "not_due"
    assert target(obj.status())["state"] == "calendar_removed"
    assert target(obj.status())["auto"]["actual_http"] == 1
    assert len(service.updates) == 1


def test_retry_after_beyond_thirty_minutes_remains_observable(tmp_path):
    obj, service, clock = make(tmp_path)
    retry = clock() + timedelta(hours=1)
    service.error = ProviderError(
        "cpi_timeseries_rate_limited", status=429, retry_at=retry
    )
    result = obj.due_check()
    assert target(result)["auto"]["next_attempt_at"] == retry.isoformat()
    clock.advance(1800)
    assert obj.due_check()["action"] == "not_due"
    assert len(service.updates) == 1
    clock.advance(1800)
    service.error = ProviderError(
        "cpi_timeseries_rate_limited",
        status=429,
        retry_at=clock() + timedelta(minutes=30),
    )
    result = obj.due_check()
    assert target(result)["auto"]["state"] == "awaiting_provider"
    assert len(service.updates) == 2


def test_budget_refusal_with_zero_count_never_finishes_target(tmp_path):
    obj, service, clock = make(tmp_path)
    service.error = ProviderError(
        "cpi_timeseries_request_budget_exhausted",
        attempts=0,
        retry_at=clock() + timedelta(hours=24),
    )
    result = obj.due_check()
    assert target(result)["auto"]["actual_http"] == 0
    assert not target(result)["target_available"]
    assert target(result)["auto"]["state"] == "awaiting_provider"
    assert target(result)["auto"]["next_attempt_at"] == "2026-11-10T15:30:05+00:00"
    clock.advance(7205)
    obj.due_check()
    assert target(obj.status())["auto"]["state"] == "stopped"
    assert len(service.updates) == 1


def test_auto_rejects_lower_transport_retry_multiplication_before_calendar_io(tmp_path):
    from types import SimpleNamespace

    obj, service, clock = make(tmp_path)
    service.source = SimpleNamespace(_max_transport_attempts=3)
    obj.calendar_fetcher = lambda: pytest.fail(
        "must reject configuration before any network attempt"
    )
    with pytest.raises(
        CpiScheduleError, match="schedule_single_transport_attempt_required"
    ):
        obj.due_check()
    assert service.updates == []


def test_history_control_refusals_do_not_exhaust_history_attempts_or_restart_batches(
    tmp_path,
):
    obj, service, clock = make(tmp_path)
    service.available = True
    obj.due_check()
    clock.advance(7205)
    obj.due_check()
    first = target(obj.status())["history"]
    assert first["next_start"] == "2019-01"
    assert first["attempts"] == 1
    for _ in range(6):
        clock.advance(300)
        service.error = ProviderError(
            "cpi_timeseries_request_spacing",
            attempts=0,
            retry_at=clock() + timedelta(seconds=3),
        )
        obj.due_check()
        stage = target(obj.status())["history"]
        assert stage["attempts"] == 1
        assert stage["state"] == "awaiting_provider"
        assert stage["next_start"] == "2019-01"
    service.error = None
    clock.advance(300)
    assert obj.due_check()["action"] == "history_updated"
    assert service.updates[-1][:2] == ("2019-01", "2026-10")


def test_persisted_reservation_recovers_by_strict_local_readback_without_http(tmp_path):
    obj, service, clock = make(tmp_path)
    state = obj._read()
    item = state["targets"]["2026-10"]
    auto = obj._auto_target_state(item, clock())
    auto.update(
        state="running",
        actual_http=5,
        reserved_http=1,
        last_attempt_at=clock().isoformat(),
    )
    obj._save(state)
    result = obj.due_check()
    assert result["action"] == "updated"
    assert target(result)["auto"]["actual_http"] == 5
    assert target(result)["auto"].get("uncertain_http", 0) == 0
    assert service.updates == []
    assert service.queries == [
        ("2026-10", "2026-10", tuple(target(result)["verified_target_series"]))
    ]


def test_interrupted_early_request_readback_still_requires_post_release_check(tmp_path):
    obj, service, clock = make(tmp_path, "2026-11-10T13:29:45+00:00")
    state = obj._read()
    item = state["targets"]["2026-10"]
    auto = obj._auto_target_state(item, clock())
    auto.update(
        state="running",
        actual_http=1,
        reserved_http=1,
        last_attempt_at="2026-11-10T13:29:30+00:00",
    )
    obj._save(state)
    result = obj.due_check()
    assert result["action"] == "early_detected"
    assert not target(result)["target_available"]
    assert target(result)["auto"]["next_attempt_at"] == "2026-11-10T13:30:00+00:00"
    assert service.updates == []
    clock.advance(15)
    service.available = True
    assert obj.due_check()["action"] == "updated"
    assert target(obj.status())["auto"]["actual_http"] == 2


def test_wire_reservation_is_durable_and_zero_admission_returns_it(tmp_path):
    obj, service, clock = make(tmp_path)
    original = service.update_range

    def inspect_reserved(*args, **kwargs):
        auto = json.loads(obj.state_path.read_text())["targets"]["2026-10"]["auto"]
        assert auto["state"] == "running"
        assert auto["actual_http"] == 1
        assert auto["reserved_http"] == 1
        raise ProviderError(
            "cpi_timeseries_request_in_flight",
            attempts=0,
            retry_at=clock() + timedelta(seconds=5),
        )

    service.update_range = inspect_reserved
    result = obj.due_check()
    assert target(result)["auto"]["actual_http"] == 0
    assert target(result)["target"]["attempts"] == 0
    service.update_range = original


def test_dense_window_defers_history_and_recovery_until_after_two_hours(tmp_path):
    obj, service, clock = make(tmp_path)
    service.available = True
    obj.due_check()
    state = obj._read()
    state["signals"]["recovery"] = {
        "state": "pending",
        "attempts": 0,
        "next_attempt_at": clock().isoformat(),
        "end_month": "2026-10",
    }
    obj._save(state)
    for advance in (0, 15, 105, 480, 1200, 1800):
        clock.advance(advance)
        assert obj.due_check()["action"] == "not_due"
        assert len(service.updates) == 1
        assert datetime.fromisoformat(obj.status()["next_due"]) > clock()
    clock.at = datetime.fromisoformat("2026-11-10T15:30:05+00:00")
    assert obj.due_check()["action"] == "history_batch_updated"
    clock.advance(300)
    assert obj.due_check()["action"] == "history_updated"
    assert obj.status()["signals"]["recovery"]["state"] == "complete"


def test_auto_recovery_without_monthly_history_executes_twenty_year_batches(tmp_path):
    obj, service, clock = make(tmp_path, "2026-11-11T13:30:00+00:00")
    state = obj._read()
    item = state["targets"]["2026-10"]
    item.update(state="complete")
    item["target"].update(state="complete")
    item["history"].update(state="complete", next_attempt_at=None)
    state["signals"]["recovery"] = {
        "state": "pending",
        "attempts": 0,
        "next_attempt_at": clock().isoformat(),
        "end_month": "2026-10",
    }
    obj._save(state)
    service.available = True
    assert obj.due_check()["action"] == "recovery_history_batch_updated"
    assert service.updates[-1][:2] == ("1999-01", "2018-12")
    clock.advance(300)
    assert obj.due_check()["action"] == "recovery_history_updated"
    assert service.updates[-1][:2] == ("2019-01", "2026-10")


def test_existing_correction_signal_runs_after_dense_window(tmp_path):
    from decimal import Decimal

    from test_cpi_revision_signals import EMPTY, Source, notice

    from market_data_foundation.historical.bls_cpi_direct import SERIES_IDS
    from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesService
    from market_data_foundation.historical.cpi_timeseries_store import (
        InMemoryTimeSeriesStore,
    )

    clock = Clock("2026-11-10T15:31:00+00:00")
    source = Source(clock)
    service = CpiTimeSeriesService(InMemoryTimeSeriesStore(clock=clock), source)
    obj = CpiReleaseScheduler(
        service,
        tmp_path / "real-service-schedule.json",
        clock=clock,
        calendar_fetcher=lambda: calendar_html(OCTOBER),
        signal_fetcher=lambda: EMPTY,
        reference_fetcher=None,
        history_start="2026-01",
        auto_mode=True,
    )
    assert obj.due_check()["action"] == "updated"
    source.values[SERIES_IDS[0], "2026-09"] = Decimal("301")
    obj.import_signal(notice())
    assert obj.due_check()["action"] == "signal_completed"
    assert next(iter(obj.status()["signals"]["events"].values()))["state"] == "complete"
    future = clock() + timedelta(hours=1)
    state = obj._read()
    state["signals"]["next_refresh_at"] = future.isoformat()
    for target_state in state["targets"].values():
        target_state["history"].update(state="complete", next_attempt_at=None)
    obj._save(state)
    assert obj.status()["next_due"] == future.isoformat()


def test_fresh_strategy_first_history_choice_and_default_constructor_restart(tmp_path):
    clock = Clock()
    obj = CpiReleaseScheduler(
        None, tmp_path / "fresh-schedule.json", clock=clock, auto_mode=True
    )
    auto = strategy(obj, clock, tmp_path)
    assert auto.get()["history_start_locked"] is False
    assert auto.heartbeat("paused")["history_start_locked"] is False
    result = auto.update(enabled=True, history_start="2000-02")
    assert result["history_start"] == "2000-02"
    assert result["history_start_locked"] is True
    assert obj.history_start == "2000-02"
    assert not obj.state_path.exists()
    restarted = CpiReleaseScheduler(None, obj.state_path, clock=clock, auto_mode=True)
    restored = strategy(restarted, clock, tmp_path)
    assert restored.get()["history_start"] == "2000-02"
    assert restarted.history_start == "2000-02"
    with pytest.raises(CpiScheduleError, match="schedule_history_start_changed"):
        restored.update(enabled=False, history_start="2001-01")
    assert not obj.state_path.exists()


def test_existing_fourteen_targets_lock_first_strategy_choice_and_preserve_state(
    tmp_path,
):
    obj, service, clock = make(tmp_path)
    state = obj._read()
    for year in range(2012, 2025):
        state["targets"][f"{year}-10"] = copy.deepcopy(state["targets"]["2026-10"])
    obj._save(state)
    before = obj.state_path.read_bytes()
    auto = strategy(obj, clock, tmp_path)
    assert auto.get()["exists"] is False
    assert auto.get()["history_start_locked"] is True
    with pytest.raises(CpiScheduleError, match="schedule_history_start_changed"):
        auto.update(enabled=True, history_start="2000-02")
    assert obj.state_path.read_bytes() == before
    assert len(obj._read()["targets"]) == 14
    assert service.updates == []


def test_late_resume_expires_existing_event_before_any_new_http(tmp_path):
    obj, service, clock = make(tmp_path)
    obj.due_check()
    previous = target(obj.status())["auto"]["actual_http"]
    clock.advance(7201)
    service.available = True
    obj.due_check()
    item = target(obj.status())
    assert item["auto"]["classification"] == "schedule_release_window_expired"
    assert item["auto"]["catch_up"] is False
    assert item["auto"]["actual_http"] == previous
    assert len(service.updates) == 1


def test_late_running_recovery_can_finish_using_only_local_available(tmp_path):
    obj, service, clock = make(tmp_path)
    state = obj._read()
    auto = obj._auto_target_state(state["targets"]["2026-10"], clock())
    auto.update(
        state="running",
        actual_http=1,
        reserved_http=1,
        last_attempt_at=clock().isoformat(),
    )
    obj._save(state)
    clock.advance(7201)
    assert obj.due_check()["action"] == "updated"
    assert target(obj.status())["auto"]["actual_http"] == 1
    assert service.updates == []
    assert len(service.queries) == 1


def test_history_guard_starts_two_minutes_before_future_release(tmp_path):
    obj, service, clock = make(tmp_path, "2026-11-10T13:29:29+00:00")
    state = obj._read()
    old = copy.deepcopy(state["targets"]["2026-10"])
    old.update(state="history_pending", release_at="2026-10-13T12:30:00+00:00")
    old["target"].update(state="complete")
    old["history"].update(state="pending", next_attempt_at=clock().isoformat())
    state["targets"]["2026-09"] = old
    obj._save(state)
    assert obj.due_check()["action"] == "not_due"
    assert service.updates == []
    assert obj.status()["next_due"] == "2026-11-10T13:29:30+00:00"
