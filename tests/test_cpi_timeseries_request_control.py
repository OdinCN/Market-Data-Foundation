"""Offline source integration: count each actual HTTP, never persist secrets."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from market_data_foundation.historical.bls_cpi_direct_live import HttpResult
from market_data_foundation.historical.cpi_request_control import CpiRequestControl
from market_data_foundation.historical.cpi_timeseries_source import (
    SERIES_IDS,
    BlsRangeSource,
    CpiSourceError,
)

NOW = datetime(2026, 10, 29, 8, tzinfo=UTC)
KEY = "d" * 32


class Clock:
    def __init__(self):
        self.now = NOW
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


class Transport:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.calls = 0

    def post(self, _endpoint, body, _headers, **_kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else 200
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, HttpResult):
            return outcome
        if outcome != 200:
            return HttpResult(outcome, b"private provider error " + KEY.encode(), {})
        request = json.loads(body)
        payload = {
            "status": "REQUEST_SUCCEEDED",
            "Results": {
                "series": [
                    {
                        "seriesID": series,
                        "data": [
                            {
                                "year": request["startyear"],
                                "period": "M09",
                                "value": "123.45",
                                "footnotes": [],
                            }
                        ],
                    }
                    for series in request["seriesid"]
                ]
            },
        }
        return HttpResult(200, json.dumps(payload).encode(), {})


def client(tmp_path, clock, transport, **kwargs):
    return BlsRangeSource(
        credential_loader=lambda: KEY,
        clock=clock,
        sleep=clock.sleep,
        transport=transport,
        request_control=CpiRequestControl(
            tmp_path / "requests.json", clock=clock, min_interval_seconds=0
        ),
        **kwargs,
    )


def fetch(source, start="2026-09", end="2026-09"):
    return source.fetch(SERIES_IDS, start, end)


@pytest.mark.parametrize("outcome", [503, TimeoutError(KEY), ConnectionError(KEY)])
def test_one_transport_attempt_disables_retry_multiplication(tmp_path, outcome):
    clock, transport = Clock(), Transport([outcome])
    with pytest.raises(CpiSourceError) as error:
        fetch(client(tmp_path, clock, transport, max_transport_attempts=1))
    assert error.value.attempts == transport.calls == 1
    assert error.value.http_status == (503 if outcome == 503 else None)
    assert clock.sleeps == []
    assert KEY not in str(error.value)
    assert len(json.loads((tmp_path / "requests.json").read_text())["requests"]) == 1
    fetch(client(tmp_path, clock, transport, max_transport_attempts=1))
    assert len(json.loads((tmp_path / "requests.json").read_text())["requests"]) == 2


def test_default_retry_contract_still_counts_every_actual_request(tmp_path):
    clock = Clock()
    transport = Transport([503, TimeoutError(KEY), 200])
    batches = fetch(client(tmp_path, clock, transport))
    assert transport.calls == batches[0].attempts == 3
    assert clock.sleeps == [1.0, 2.0]
    assert len(json.loads((tmp_path / "requests.json").read_text())["requests"]) == 3


@pytest.mark.parametrize("status", [401, 403])
def test_auth_status_is_terminal_and_preserved(tmp_path, status):
    clock, transport = Clock(), Transport([status])
    with pytest.raises(CpiSourceError, match="http_failure") as error:
        fetch(client(tmp_path, clock, transport))
    assert error.value.http_status == status
    assert error.value.attempts == transport.calls == 1
    assert clock.sleeps == []


def test_429_source_returns_shared_retry_at_without_sleeping(tmp_path):
    clock = Clock()
    transport = Transport([HttpResult(429, b"", {"Retry-After": "900"})])
    with pytest.raises(CpiSourceError) as error:
        fetch(client(tmp_path, clock, transport))
    assert error.value.attempts == transport.calls == 1
    assert error.value.http_status == 429
    assert error.value.retry_at == NOW + timedelta(minutes=15)
    assert clock.sleeps == []
    with pytest.raises(CpiSourceError, match="cooldown") as blocked:
        fetch(client(tmp_path, clock, transport))
    assert blocked.value.attempts == 0
    assert blocked.value.retry_at == error.value.retry_at
    assert transport.calls == 1


@pytest.mark.parametrize(
    "message",
    [
        "Request could not be serviced, as the daily threshold for total number "
        "of requests allocated to the API key provided has been reached.",
        "The daily request limit has been exceeded.",
        "Maximum queries per day exceeded.",
    ],
)
def test_http200_provider_daily_quota_stops_and_persists(tmp_path, message):
    clock = Clock()
    payload = {"status": "REQUEST_NOT_PROCESSED", "message": [message]}
    transport = Transport([HttpResult(200, json.dumps(payload).encode(), {})])
    with pytest.raises(CpiSourceError, match="provider_daily_limit") as error:
        fetch(client(tmp_path, clock, transport))
    assert error.value.http_status == 200
    assert error.value.retry_at == NOW + timedelta(hours=24)
    assert error.value.attempts == transport.calls == 1
    with pytest.raises(CpiSourceError, match="provider_daily_limit") as blocked:
        fetch(client(tmp_path, clock, transport))
    assert blocked.value.attempts == 0
    assert blocked.value.retry_at == error.value.retry_at
    assert transport.calls == 1 and clock.sleeps == []
    state = (tmp_path / "requests.json").read_text()
    assert KEY not in state and message not in state


def test_other_provider_rejection_is_not_classified_as_daily_quota(tmp_path):
    clock = Clock()
    payload = {"status": "REQUEST_FAILED", "message": ["Invalid series requested."]}
    transport = Transport([HttpResult(200, json.dumps(payload).encode(), {})])
    with pytest.raises(CpiSourceError, match="provider_rejected") as error:
        fetch(client(tmp_path, clock, transport))
    assert error.value.retry_at is None
    fetch(client(tmp_path, clock, transport))
    assert transport.calls == 2


def test_failure_attempts_accumulate_across_year_windows(tmp_path):
    clock = Clock()
    transport = Transport([200, 503, TimeoutError(KEY), TimeoutError(KEY)])
    with pytest.raises(CpiSourceError) as error:
        fetch(client(tmp_path, clock, transport), "1999-09", "2026-09")
    assert error.value.attempts == transport.calls == 4
    assert error.value.http_status == 503
    assert len(json.loads((tmp_path / "requests.json").read_text())["requests"]) == 4


def test_successful_windows_report_per_batch_actual_attempts(tmp_path):
    clock = Clock()
    transport = Transport([503, 200, 503, 200])
    batches = fetch(client(tmp_path, clock, transport), "1999-09", "2026-09")
    assert [batch.attempts for batch in batches] == [2, 2]
    assert transport.calls == sum(batch.attempts for batch in batches) == 4


def test_multiwindow_fetch_honors_default_spacing_and_succeeds(tmp_path):
    clock, transport = Clock(), Transport()
    source = BlsRangeSource(
        credential_loader=lambda: KEY,
        transport=transport,
        clock=clock,
        sleep=clock.sleep,
        request_control=CpiRequestControl(tmp_path / "requests.json", clock=clock),
        max_transport_attempts=1,
    )
    batches = fetch(source, "1999-09", "2026-09")
    assert [batch.attempts for batch in batches] == [1, 1]
    assert transport.calls == 2 and clock.sleeps == [1.0]


def test_initial_short_spacing_waits_without_spending_http_attempt(tmp_path):
    clock, transport = Clock(), Transport()
    source = BlsRangeSource(
        credential_loader=lambda: KEY,
        transport=transport,
        clock=clock,
        sleep=clock.sleep,
        request_control=CpiRequestControl(tmp_path / "requests.json", clock=clock),
        max_transport_attempts=1,
    )
    fetch(source)
    batches = fetch(source)
    assert batches[0].attempts == 1 and transport.calls == 2
    assert clock.sleeps == [1.0]


def test_frozen_clock_exits_after_two_spacing_reacquisitions(tmp_path):
    clock, transport = Clock(), Transport()
    sleeps = []
    source = BlsRangeSource(
        credential_loader=lambda: KEY,
        transport=transport,
        clock=clock,
        sleep=sleeps.append,
        request_control=CpiRequestControl(tmp_path / "requests.json", clock=clock),
        max_transport_attempts=1,
    )
    with pytest.raises(CpiSourceError, match="spacing") as error:
        fetch(source, "1999-09", "2026-09")
    assert error.value.attempts == transport.calls == 1
    assert sleeps == [1.0, 1.0]
    assert len(json.loads((tmp_path / "requests.json").read_text())["requests"]) == 1


def test_spacing_reacquisition_rechecks_shared_cooldown(tmp_path):
    clock, transport = Clock(), Transport()
    control = CpiRequestControl(tmp_path / "requests.json", clock=clock)

    def sleep_and_throttle(seconds):
        clock.sleep(seconds)
        with control.attempt() as attempt:
            attempt.record_response(429, {"retry-after": "900"})

    source = BlsRangeSource(
        credential_loader=lambda: KEY,
        transport=transport,
        clock=clock,
        sleep=sleep_and_throttle,
        request_control=control,
        max_transport_attempts=1,
    )
    with pytest.raises(CpiSourceError, match="cooldown") as error:
        fetch(source, "1999-09", "2026-09")
    assert error.value.attempts == transport.calls == 1
    assert clock.sleeps == [1.0]
    assert error.value.retry_at == clock.now + timedelta(minutes=15)


def test_long_spacing_returns_promptly_with_prior_window_count(tmp_path):
    clock, transport = Clock(), Transport()
    source = BlsRangeSource(
        credential_loader=lambda: KEY,
        transport=transport,
        clock=clock,
        sleep=clock.sleep,
        request_control=CpiRequestControl(
            tmp_path / "requests.json", clock=clock, min_interval_seconds=30
        ),
        max_transport_attempts=1,
    )
    with pytest.raises(CpiSourceError, match="spacing") as error:
        fetch(source, "1999-09", "2026-09")
    assert error.value.attempts == transport.calls == 1
    assert error.value.retry_at == NOW + timedelta(seconds=30)
    assert clock.sleeps == []


def test_budget_block_before_http_is_not_reported_as_success(tmp_path):
    clock, transport = Clock(), Transport()
    control = CpiRequestControl(
        tmp_path / "requests.json", clock=clock, min_interval_seconds=0
    )
    for _ in range(400):
        with control.attempt() as attempt:
            attempt.record_response(200, {})
    with pytest.raises(CpiSourceError, match="budget_exhausted") as error:
        fetch(client(tmp_path, clock, transport))
    assert error.value.attempts == transport.calls == 0
    assert error.value.retry_at == NOW + timedelta(hours=24)


def test_state_contains_only_control_facts_no_key_body_or_provider_text(tmp_path):
    clock, transport = Clock(), Transport()
    fetch(client(tmp_path, clock, transport))
    for path in tmp_path.iterdir():
        raw = path.read_bytes()
        assert KEY.encode() not in raw
        assert b"registrationkey" not in raw
        assert b"seriesid" not in raw
        assert b"Results" not in raw


@pytest.mark.parametrize("value", [0, 4, True, 1.0, None])
def test_invalid_max_transport_attempts_never_calls_transport(value):
    with pytest.raises(CpiSourceError, match="transport_attempts_invalid"):
        BlsRangeSource(max_transport_attempts=value)
