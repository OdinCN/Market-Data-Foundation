import http.client
from dataclasses import dataclass

import pytest

from market_data_foundation.source_request_policy import (
    SOURCE_REQUEST_POLICY,
    SourceRequestFailed,
    request_source,
    retry_delay_after,
    retryable_http_status,
)


@dataclass(frozen=True)
class Response:
    status: int


def test_canonical_policy_is_one_plus_two_attempts():
    assert SOURCE_REQUEST_POLICY.max_transport_attempts == 3
    assert SOURCE_REQUEST_POLICY.backoff_seconds == (1.0, 2.0)
    assert SOURCE_REQUEST_POLICY.retryable_http_statuses == frozenset(
        {408, 429, 500, 502, 503, 504}
    )
    assert retry_delay_after(1) == 1.0
    assert retry_delay_after(2) == 2.0
    for value in (0, 3, True, 1.0):
        with pytest.raises(ValueError):
            retry_delay_after(value)


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_retryable_http_statuses_are_canonical(status):
    assert retryable_http_status(status)


@pytest.mark.parametrize("status", [200, 201, 301, 302, 401, 403, 404, 409, 422, 501])
def test_other_http_statuses_are_not_retryable(status):
    assert not retryable_http_status(status)


def test_success_first_attempt_has_no_wait():
    calls, waits = [], []

    def exchange():
        calls.append(1)
        return Response(200)

    response, attempts = request_source(
        exchange,
        status=lambda value: value.status,
        sleep=waits.append,
    )
    assert response.status == 200
    assert attempts == 1
    assert len(calls) == 1
    assert waits == []


def test_transport_failure_then_success_retries_once():
    outcomes = [OSError("private provider detail"), Response(200)]
    waits = []

    def exchange():
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    response, attempts = request_source(
        exchange,
        status=lambda value: value.status,
        sleep=waits.append,
    )
    assert response.status == 200
    assert attempts == 2
    assert waits == [1.0]


def test_retryable_http_uses_full_three_attempt_budget_then_success():
    outcomes = [Response(503), Response(503), Response(200)]
    waits = []

    def exchange():
        return outcomes.pop(0)

    response, attempts = request_source(
        exchange,
        status=lambda value: value.status,
        sleep=waits.append,
    )
    assert response.status == 200
    assert attempts == 3
    assert waits == [1.0, 2.0]


def test_retryable_http_exhaustion_is_terminal_after_three():
    calls, waits = [], []

    def exchange():
        calls.append(1)
        return Response(503)

    with pytest.raises(SourceRequestFailed) as caught:
        request_source(
            exchange,
            status=lambda value: value.status,
            sleep=waits.append,
        )
    assert caught.value.classification == "http_failure"
    assert caught.value.http_status == 503
    assert caught.value.attempts == 3
    assert len(calls) == 3
    assert waits == [1.0, 2.0]


@pytest.mark.parametrize("status", [201, 204, 301, 302, 401, 403, 404, 409, 422])
def test_nonretryable_http_is_one_attempt(status):
    calls, waits = [], []

    def exchange():
        calls.append(1)
        return Response(status)

    with pytest.raises(SourceRequestFailed) as caught:
        request_source(
            exchange,
            status=lambda value: value.status,
            sleep=waits.append,
        )
    assert caught.value.attempts == 1
    assert caught.value.http_status == status
    assert len(calls) == 1
    assert waits == []


def test_http_protocol_parse_failure_is_terminal_without_retry():
    calls, waits = [], []

    def exchange():
        calls.append(1)
        raise http.client.LineTooLong("synthetic header line")

    with pytest.raises(SourceRequestFailed) as caught:
        request_source(
            exchange,
            status=lambda value: value.status,
            sleep=waits.append,
        )
    assert caught.value.classification == "protocol_failure"
    assert caught.value.attempts == 1
    assert len(calls) == 1
    assert waits == []


def test_incomplete_read_remains_transient():
    calls, waits = [], []

    def exchange():
        calls.append(1)
        if len(calls) == 1:
            raise http.client.IncompleteRead(b"partial", 1)
        return Response(200)

    response, attempts = request_source(
        exchange,
        status=lambda value: value.status,
        sleep=waits.append,
    )
    assert response.status == 200
    assert attempts == 2
    assert waits == [1.0]


def test_unknown_exception_is_terminal_and_message_is_not_forwarded():
    waits = []

    with pytest.raises(SourceRequestFailed) as caught:
        request_source(
            lambda: (_ for _ in ()).throw(
                RuntimeError("credential-or-url-must-not-cross")
            ),
            status=lambda value: value.status,
            sleep=waits.append,
        )
    assert caught.value.classification == "unexpected_exception"
    assert caught.value.attempts == 1
    assert "credential-or-url" not in str(caught.value)
    assert waits == []


def test_mixed_503_timeout_timeout_keeps_latest_seen_http_status():
    outcomes = [
        Response(503),
        TimeoutError("secret-provider-detail"),
        TimeoutError("secret-provider-detail"),
    ]
    delays = []

    def exchange():
        value = outcomes.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    with pytest.raises(SourceRequestFailed) as caught:
        request_source(
            exchange, status=lambda response: response.status, sleep=delays.append
        )
    assert caught.value.classification == "timeout"
    assert caught.value.attempts == 3
    assert caught.value.http_status == 503
    assert delays == [1.0, 2.0]
    assert not outcomes
    assert "secret" not in str(caught.value)


def test_last_observed_http_status_replaced_by_latest_actual_response():
    outcomes = [Response(503), TimeoutError("private"), Response(429)]
    delays = []

    def exchange():
        value = outcomes.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    with pytest.raises(SourceRequestFailed) as caught:
        request_source(
            exchange, status=lambda response: response.status, sleep=delays.append
        )
    assert caught.value.classification == "http_failure"
    assert caught.value.attempts == 3
    assert caught.value.http_status == 429
    assert delays == [1.0, 2.0]
