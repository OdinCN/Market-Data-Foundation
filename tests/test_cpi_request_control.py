"""Offline persistent request admission; all state is confined to tmp_path."""

import json
import multiprocessing
import os
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from threading import Event, Thread

import pytest

from market_data_foundation.historical import cpi_request_control as control_module
from market_data_foundation.historical.cpi_request_control import (
    CpiRequestControl,
    CpiRequestControlError,
)

NOW = datetime(2026, 10, 29, 8, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def admit(control, status=200, headers=None, **kwargs):
    with control.attempt() as attempt:
        return attempt.record_response(status, headers or {}, **kwargs)


def make_control(tmp_path, clock, interval=0):
    return CpiRequestControl(
        tmp_path / "bls-requests.json", clock=clock, min_interval_seconds=interval
    )


def test_400_budget_survives_recreation_and_rolls_at_24_hours(tmp_path):
    clock = Clock()
    control = make_control(tmp_path, clock)
    for _ in range(400):
        admit(control)
    reloaded = make_control(tmp_path, clock)
    with pytest.raises(CpiRequestControlError, match="budget_exhausted") as error:
        admit(reloaded)
    assert error.value.retry_at == NOW + timedelta(hours=24)
    clock.advance(86400 - 1)
    with pytest.raises(CpiRequestControlError, match="budget_exhausted"):
        admit(reloaded)
    clock.advance(1)
    admit(reloaded)
    assert len(json.loads(reloaded.state_path.read_text())["requests"]) == 1


def test_rolling_budget_releases_only_expired_actual_requests(tmp_path):
    clock = Clock()
    control = make_control(tmp_path, clock)
    admit(control)
    clock.advance(60)
    for _ in range(399):
        admit(control)
    clock.advance(86400 - 60)
    admit(control)
    with pytest.raises(CpiRequestControlError, match="budget_exhausted") as error:
        admit(control)
    assert error.value.retry_at == NOW + timedelta(hours=24, minutes=1)


def test_failures_are_counted_before_http_and_lock_is_released(tmp_path):
    clock = Clock()
    control = make_control(tmp_path, clock)
    for _ in range(3):
        with pytest.raises(TimeoutError):
            with control.attempt():
                stored = json.loads(control.state_path.read_text())
                assert stored["in_flight_at"] is not None
                raise TimeoutError("private provider diagnostic")
    reloaded = make_control(tmp_path, clock)
    admit(reloaded)
    state = json.loads(control.state_path.read_text())
    assert len(state["requests"]) == 4
    assert state["in_flight_at"] is None
    assert "private" not in control.state_path.read_text()


def test_minimum_spacing_is_persistent_and_does_not_wait(tmp_path):
    clock = Clock()
    control = make_control(tmp_path, clock, interval=15)
    admit(control)
    with pytest.raises(CpiRequestControlError, match="spacing") as error:
        admit(make_control(tmp_path, clock, interval=15))
    assert error.value.retry_at == NOW + timedelta(seconds=15)
    assert len(json.loads(control.state_path.read_text())["requests"]) == 1
    clock.advance(15)
    admit(control)


def test_single_flight_between_threads_and_distinct_controllers(tmp_path):
    clock = Clock()
    entered, release = Event(), Event()
    errors = []

    def hold():
        try:
            with make_control(tmp_path, clock).attempt():
                entered.set()
                assert release.wait(5)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=hold)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(CpiRequestControlError, match="in_flight") as error:
            admit(make_control(tmp_path, clock))
        assert error.value.retry_at > NOW
    finally:
        release.set()
        thread.join(5)
    assert not errors and not thread.is_alive()
    admit(make_control(tmp_path, clock))


def _process_hold(path, entered, release):
    with CpiRequestControl(path, clock=lambda: NOW).attempt():
        entered.set()
        if not release.wait(5):
            raise AssertionError("parent did not release")


def test_single_flight_is_cross_process(tmp_path):
    context = multiprocessing.get_context("fork")
    entered, release = context.Event(), context.Event()
    path = tmp_path / "bls-requests.json"
    process = context.Process(target=_process_hold, args=(path, entered, release))
    process.start()
    try:
        assert entered.wait(5)
        with pytest.raises(CpiRequestControlError, match="in_flight"):
            admit(CpiRequestControl(path, clock=lambda: NOW))
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("10", 300), ("900", 900), ("invalid", 300), ("-3", 300)],
)
def test_429_delta_and_minimum_shared_after_reload(tmp_path, value, seconds):
    clock = Clock()
    control = make_control(tmp_path, clock)
    until = admit(control, 429, {"Retry-After": value})
    assert until == NOW + timedelta(seconds=seconds)
    with pytest.raises(CpiRequestControlError, match="cooldown") as error:
        admit(make_control(tmp_path, clock))
    assert error.value.retry_at == until
    assert error.value.http_status == 429
    assert len(json.loads(control.state_path.read_text())["requests"]) == 1


def test_429_http_date_and_consecutive_30_minute_escalation(tmp_path):
    clock = Clock()
    control = make_control(tmp_path, clock)
    date = format_datetime(NOW + timedelta(minutes=10), usegmt=True)
    assert admit(control, 429, {"retry-after": date}) == NOW + timedelta(minutes=10)
    clock.advance(600)
    until = admit(make_control(tmp_path, clock), 429, {"retry-after": "2"})
    assert until == clock.now + timedelta(minutes=30)
    clock.advance(1800)
    admit(control, 200)
    assert admit(control, 429) == clock.now + timedelta(minutes=5)


def test_daily_quota_is_shared_and_persistent_for_full_24_hours(tmp_path):
    clock = Clock()
    control = make_control(tmp_path, clock)
    until = admit(control, provider_daily_limit=True)
    assert until == NOW + timedelta(hours=24)
    with pytest.raises(CpiRequestControlError, match="provider_daily_limit") as error:
        admit(make_control(tmp_path, clock))
    assert error.value.http_status == 200
    assert error.value.retry_at == until
    clock.advance(86400)
    admit(make_control(tmp_path, clock))


@pytest.mark.parametrize(
    "raw",
    [b"", b"broken", b"{}", b"[]", b'{"version":1,"version":1}', b"\xff"],
)
def test_corrupt_state_fails_closed_without_rewriting(tmp_path, raw):
    control = make_control(tmp_path, Clock())
    control.state_path.write_bytes(raw)
    with pytest.raises(CpiRequestControlError, match="state_unknown"):
        admit(control)
    assert control.state_path.read_bytes() == raw


def test_deleted_initialized_state_cannot_reset_budget(tmp_path):
    control = make_control(tmp_path, Clock())
    admit(control)
    control.state_path.unlink()
    with pytest.raises(CpiRequestControlError, match="state_unknown"):
        admit(make_control(tmp_path, Clock()))
    assert not control.state_path.exists()


def test_valid_json_corruption_cannot_silently_reduce_budget(tmp_path):
    control = make_control(tmp_path, Clock())
    admit(control)
    state = json.loads(control.state_path.read_text())
    state["requests"] = []
    control.state_path.write_text(json.dumps(state))
    with pytest.raises(CpiRequestControlError, match="state_unknown"):
        admit(control)


def _crash_after_admission(path):
    with CpiRequestControl(path, clock=lambda: NOW).attempt():
        os._exit(0)


def test_interrupted_http_retains_count_and_recovers_after_fixed_30_seconds(tmp_path):
    path = tmp_path / "bls-requests.json"
    context = multiprocessing.get_context("fork")
    process = context.Process(target=_crash_after_admission, args=(path,))
    process.start()
    process.join(5)
    assert process.exitcode == 0
    clock = Clock()
    assert len(json.loads(path.read_text())["requests"]) == 1
    with pytest.raises(CpiRequestControlError, match="request_interrupted") as error:
        admit(make_control(tmp_path, clock))
    assert error.value.retry_at == NOW + timedelta(seconds=30)
    assert json.loads(path.read_text())["in_flight_at"] is None
    clock.advance(10)
    with pytest.raises(CpiRequestControlError, match="request_interrupted") as again:
        admit(make_control(tmp_path, clock))
    assert again.value.retry_at == error.value.retry_at
    clock.advance(20)
    admit(make_control(tmp_path, clock))
    assert len(json.loads(path.read_text())["requests"]) == 2


def test_clock_is_refreshed_after_flock_succeeds(tmp_path, monkeypatch):
    clock = Clock()
    clock.advance(1)
    admit(make_control(tmp_path, clock))
    clock.advance(-1)
    flock = control_module.fcntl.flock

    def release_and_advance(descriptor, operation):
        flock(descriptor, operation)
        clock.advance(1)

    monkeypatch.setattr(control_module.fcntl, "flock", release_and_advance)
    admit(make_control(tmp_path, clock))
    assert (
        len(json.loads((tmp_path / "bls-requests.json").read_text())["requests"]) == 2
    )


def test_legacy_400_consecutive_429_saturates_and_remains_readable(tmp_path):
    clock = Clock()
    control = make_control(tmp_path, clock)
    admit(control)
    state = json.loads(control.state_path.read_text())
    state["consecutive_429"] = 400
    state.pop("recovery_until")  # Existing schema remains valid on restart.
    control._save(state)
    clock.advance(86400)
    until = admit(make_control(tmp_path, clock), 429)
    assert until == clock.now + timedelta(minutes=30)
    assert json.loads(control.state_path.read_text())["consecutive_429"] == 2
    with pytest.raises(CpiRequestControlError, match="cooldown"):
        admit(make_control(tmp_path, clock))
    clock.advance(1800)
    admit(make_control(tmp_path, clock), 429)
    assert json.loads(control.state_path.read_text())["consecutive_429"] == 2


def _crash_after_response(path, daily):
    with CpiRequestControl(path, clock=lambda: NOW).attempt() as attempt:
        attempt.record_response(200 if daily else 429, {}, provider_daily_limit=daily)
        os._exit(0)


@pytest.mark.parametrize("daily", [False, True])
def test_crash_recovery_never_shortens_persisted_cooldown_or_quota(tmp_path, daily):
    path = tmp_path / "bls-requests.json"
    context = multiprocessing.get_context("fork")
    process = context.Process(target=_crash_after_response, args=(path, daily))
    process.start()
    process.join(5)
    assert process.exitcode == 0
    clock = Clock()
    classification = "provider_daily_limit" if daily else "request_cooldown"
    until = NOW + (timedelta(hours=24) if daily else timedelta(minutes=5))
    for seconds in (0, 30):
        clock.advance(seconds)
        with pytest.raises(CpiRequestControlError, match=classification) as error:
            admit(make_control(tmp_path, clock))
        assert error.value.retry_at == until
        assert len(json.loads(path.read_text())["requests"]) == 1


def test_clock_rollback_fails_closed(tmp_path):
    clock = Clock()
    control = make_control(tmp_path, clock)
    admit(control)
    clock.advance(-1)
    with pytest.raises(CpiRequestControlError, match="state_unknown"):
        admit(control)


def test_incomplete_initialization_can_recover_without_budget_reset(tmp_path):
    control = make_control(tmp_path, Clock())
    control.lock_path.touch()
    admit(control)
    assert control.lock_path.read_bytes() == b"1"


def test_preaccounting_storage_failure_never_admits_http(tmp_path, monkeypatch):
    control = make_control(tmp_path, Clock())

    def fail(_state):
        raise CpiRequestControlError("cpi_timeseries_request_state_unknown")

    monkeypatch.setattr(control, "_save", fail)
    entered = False
    with pytest.raises(CpiRequestControlError, match="state_unknown"):
        with control.attempt():
            entered = True
    assert not entered


@pytest.mark.parametrize("fail_at", [2, 3])
def test_response_or_completion_storage_failure_retains_uncertainty(
    tmp_path, monkeypatch, fail_at
):
    clock = Clock()
    control = make_control(tmp_path, clock)
    save = control._save
    calls = 0

    def failing_save(state):
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise CpiRequestControlError("cpi_timeseries_request_state_unknown")
        save(state)

    monkeypatch.setattr(control, "_save", failing_save)
    with pytest.raises(CpiRequestControlError, match="state_unknown"):
        admit(control)
    state = json.loads(control.state_path.read_text())
    assert len(state["requests"]) == 1 and state["in_flight_at"] is not None
    with pytest.raises(CpiRequestControlError, match="request_interrupted") as error:
        admit(make_control(tmp_path, clock))
    assert error.value.retry_at == NOW + timedelta(seconds=30)
    clock.advance(30)
    admit(make_control(tmp_path, clock))
    assert len(json.loads(control.state_path.read_text())["requests"]) == 2


@pytest.mark.parametrize("value", [-1, True, float("inf"), float("nan"), "1"])
def test_invalid_interval_is_rejected(tmp_path, value):
    with pytest.raises(ValueError, match="interval_invalid"):
        make_control(tmp_path, Clock(), interval=value)
