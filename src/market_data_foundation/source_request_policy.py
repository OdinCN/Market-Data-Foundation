"""Canonical retry policy for read-only, stateless external source requests."""

from __future__ import annotations

import http.client
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceRequestPolicy:
    """One MDF-wide transport retry policy; providers do not redefine it."""

    max_transport_attempts: int = 3
    backoff_seconds: tuple[float, float] = (1.0, 2.0)
    retryable_http_statuses: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})

    def retryable_http_status(self, status: object) -> bool:
        return type(status) is int and status in self.retryable_http_statuses

    def retry_delay_after(self, attempt_number: int) -> float:
        if type(attempt_number) is not int or attempt_number not in (1, 2):
            raise ValueError("source_request_retry_attempt_invalid")
        return self.backoff_seconds[attempt_number - 1]


SOURCE_REQUEST_POLICY = SourceRequestPolicy()


def retryable_http_status(status: object) -> bool:
    return SOURCE_REQUEST_POLICY.retryable_http_status(status)


def retry_delay_after(attempt_number: int) -> float:
    return SOURCE_REQUEST_POLICY.retry_delay_after(attempt_number)


def valid_transport_attempts(attempts: object) -> bool:
    return (
        type(attempts) is int
        and 1 <= attempts <= SOURCE_REQUEST_POLICY.max_transport_attempts
    )


class SourceRequestFailed(RuntimeError):
    """Safe terminal request facts; never includes provider exception text."""

    def __init__(
        self,
        classification: str,
        attempts: int,
        *,
        http_status: int | None = None,
    ) -> None:
        super().__init__(classification)
        self.classification = classification
        self.attempts = attempts
        self.http_status = http_status


def classify_source_exception(error: Exception) -> tuple[str, bool]:
    """Return a secret-safe classification and whether it is transient."""
    if isinstance(error, TimeoutError):
        return "timeout", True
    if isinstance(error, http.client.InvalidURL):
        return "request_invalid", False
    if isinstance(error, http.client.IncompleteRead):
        return "transport_failure", True
    if isinstance(error, OSError):
        return "transport_failure", True
    if isinstance(error, http.client.HTTPException):
        return "protocol_failure", False
    return "unexpected_exception", False


def request_source[Response](
    exchange: Callable[[], Response],
    *,
    status: Callable[[Response], int],
    classify: Callable[[Exception], tuple[str, bool]] = classify_source_exception,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[Response, int]:
    """Execute one logical source read with at most three wire attempts.

    Only the transport exchange is retried. Parsing, validation, persistence,
    Garage/PG work and all other local effects must remain outside this helper.
    """
    last_observed_http_status: int | None = None
    for attempts in range(1, SOURCE_REQUEST_POLICY.max_transport_attempts + 1):
        http_status = None
        try:
            response = exchange()
            http_status = status(response)
            if type(http_status) is int and 100 <= http_status <= 599:
                last_observed_http_status = http_status
        except Exception as error:
            classification, transient = classify(error)
        else:
            if http_status == 200:
                return response, attempts
            classification = "http_failure"
            transient = retryable_http_status(http_status)

        if not transient or attempts == SOURCE_REQUEST_POLICY.max_transport_attempts:
            raise SourceRequestFailed(
                classification,
                attempts,
                # A subsequent timeout must not erase the last actual HTTP
                # response status: e.g. 503 -> Timeout -> Timeout stays 503.
                http_status=last_observed_http_status,
            ) from None
        sleep(retry_delay_after(attempts))

    raise AssertionError("source_request_attempts_exhausted")
