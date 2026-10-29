"""Bounded BLS CPI range reads; no storage or historical-vintage inference."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from market_data_foundation.historical.bls_cpi_direct import ENDPOINT, SERIES_IDS
from market_data_foundation.historical.bls_cpi_direct_live import (
    _KEY_SHAPE,
    DEFAULT_SECRET_PATH,
    TIMEOUT_SECONDS,
    ExactOneHttpsTransport,
    HttpResult,
    LiveExecutionError,
    Transport,
    load_registration_key,
)
from market_data_foundation.historical.cpi_request_control import (
    CpiRequestControl,
    CpiRequestControlError,
    retry_after_at,
)
from market_data_foundation.source_request_policy import (
    classify_source_exception,
    retry_delay_after,
    retryable_http_status,
    valid_transport_attempts,
)

MAX_RANGE_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_RANGE_REQUESTS = 20
MAX_SPACING_WAIT_SECONDS = 5.0
MAX_SPACING_REACQUISITIONS = 2
_MONTH = re.compile(r"(?:19[0-9]{2}|[2-9][0-9]{3})-(?:0[1-9]|1[0-2])")
_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_SENSITIVE_KEY = re.compile(
    r"registration.?key|api.?key|credential|secret|token", re.IGNORECASE
)


class CpiSourceError(RuntimeError):
    """A classification and bounded transport count, never raw provider text."""

    def __init__(
        self,
        classification: str,
        attempts: int = 0,
        *,
        http_status: int | None = None,
        retry_at: datetime | None = None,
    ) -> None:
        super().__init__(classification)
        self.classification = classification
        self.attempts = attempts
        self.http_status = http_status
        self.retry_at = retry_at


@dataclass(frozen=True)
class SourcePoint:
    series_id: str
    month: str
    index_value: Decimal | None
    provider_calculations: dict
    footnotes: tuple


@dataclass(frozen=True)
class SourceBatch:
    points: tuple[SourcePoint, ...]
    raw: bytes = field(repr=False)
    request: dict
    observed_at: datetime
    attempts: int


def validate_month(month: str) -> str:
    if type(month) is not str or _MONTH.fullmatch(month) is None:
        raise CpiSourceError("cpi_timeseries_month_invalid")
    return month


def _series(series_ids: tuple[str, ...]) -> tuple[str, ...]:
    if (
        type(series_ids) is not tuple
        or not series_ids
        or any(type(item) is not str or item not in SERIES_IDS for item in series_ids)
        or len(set(series_ids)) != len(series_ids)
    ):
        raise CpiSourceError("cpi_timeseries_series_invalid")
    return series_ids


def year_windows(
    start_month: str,
    end_month: str,
    *,
    registered: bool = True,
    max_requests: int = MAX_RANGE_REQUESTS,
) -> tuple[tuple[int, int], ...]:
    """Plan inclusive, nonoverlapping BLS year windows before any wire request."""
    validate_month(start_month)
    validate_month(end_month)
    if start_month > end_month:
        raise CpiSourceError("cpi_timeseries_range_invalid")
    if (
        type(registered) is not bool
        or type(max_requests) is not int
        or not 1 <= max_requests <= MAX_RANGE_REQUESTS
    ):
        raise CpiSourceError("cpi_timeseries_request_budget_invalid")
    first, last = int(start_month[:4]), int(end_month[:4])
    width = 20 if registered else 10
    count = (last - first) // width + 1
    if count > max_requests:
        raise CpiSourceError("cpi_timeseries_request_budget_exceeded")
    return tuple(
        (year, min(year + width - 1, last)) for year in range(first, last + 1, width)
    )


def _json(raw: bytes, forbidden_secret: str | None = None) -> dict:
    def pairs(items):
        if len(dict(items)) != len(items):
            raise CpiSourceError("cpi_timeseries_duplicate_json_key")
        return dict(items)

    def finite_constant(_):
        raise CpiSourceError("cpi_timeseries_invalid_json")

    if type(raw) is not bytes or not 0 < len(raw) <= MAX_RANGE_RESPONSE_BYTES:
        raise CpiSourceError("cpi_timeseries_response_size_invalid")
    try:
        document = json.loads(
            raw, object_pairs_hook=pairs, parse_constant=finite_constant
        )
    except UnicodeError, ValueError, TypeError, RecursionError:
        raise CpiSourceError("cpi_timeseries_invalid_json") from None
    if type(document) is not dict:
        raise CpiSourceError("cpi_timeseries_response_invalid")
    _no_credentials(document, forbidden_secret)
    return document


def _no_credentials(value: object, forbidden_secret: str | None = None) -> None:
    # Provider-original material is retained only after this credential-echo guard.
    pending = [value]
    while pending:
        item = pending.pop()
        if type(item) is dict:
            if any(
                _SENSITIVE_KEY.search(key)
                or (forbidden_secret and forbidden_secret in key)
                for key in item
            ):
                raise CpiSourceError("cpi_timeseries_credential_echo_forbidden")
            pending.extend(item.values())
        elif type(item) is list:
            pending.extend(item)
        elif type(item) is str and forbidden_secret and forbidden_secret in item:
            raise CpiSourceError("cpi_timeseries_credential_echo_forbidden")
        elif type(item) is float and not math.isfinite(item):
            raise CpiSourceError("cpi_timeseries_invalid_json")


def parse_response(
    raw: bytes,
    series_ids: tuple[str, ...],
    start_month: str,
    end_month: str,
    *,
    forbidden_secret: str | None = None,
) -> tuple[SourcePoint, ...]:
    """Preserve original index/calculations and report actual monthly coverage.

    Omitted months remain gaps. The observed BLS "-" marker is retained as an
    explicit missing point with index_value=None and original metadata. Neither
    is interpolated or evidence of knowledge before its observation timestamp.
    """
    _series(series_ids)
    validate_month(start_month)
    validate_month(end_month)
    if start_month > end_month:
        raise CpiSourceError("cpi_timeseries_range_invalid")
    document = _json(raw, forbidden_secret)
    if _daily_limit(document):
        raise CpiSourceError("cpi_timeseries_provider_daily_limit")
    if document.get("status") != "REQUEST_SUCCEEDED":
        raise CpiSourceError("cpi_timeseries_provider_rejected")
    results = document.get("Results")
    if type(results) is not dict or type(results.get("series")) is not list:
        raise CpiSourceError("cpi_timeseries_response_invalid")
    seen_series: set[str] = set()
    seen_points: set[tuple[str, str]] = set()
    points: list[SourcePoint] = []
    for series in results["series"]:
        if type(series) is not dict:
            raise CpiSourceError("cpi_timeseries_series_response_invalid")
        identity = series.get("seriesID")
        if (
            type(identity) is not str
            or identity not in series_ids
            or identity in seen_series
            or type(series.get("data")) is not list
        ):
            raise CpiSourceError("cpi_timeseries_series_response_invalid")
        seen_series.add(identity)
        for row in series["data"]:
            if type(row) is not dict:
                raise CpiSourceError("cpi_timeseries_point_invalid")
            year, period = row.get("year"), row.get("period")
            if (
                type(year) is not str
                or re.fullmatch(r"[0-9]{4}", year) is None
                or not start_month[:4] <= year <= end_month[:4]
                or type(period) is not str
                or re.fullmatch(r"M(?:0[1-9]|1[0-3])", period) is None
            ):
                raise CpiSourceError("cpi_timeseries_period_invalid")
            if period == "M13":
                continue
            month = year + "-" + period[1:]
            key = (identity, month)
            if key in seen_points:
                raise CpiSourceError("cpi_timeseries_duplicate_point")
            seen_points.add(key)
            value = row.get("value")
            if value == "-":
                # BLS explicitly uses this marker for unavailable observations.
                # Keep its month/footnotes so persistence can distinguish an
                # observed absence from an unfetched gap and avoid fetch loops.
                index = None
            else:
                if type(value) is not str or _NUMBER.fullmatch(value) is None:
                    raise CpiSourceError("cpi_timeseries_index_invalid")
                try:
                    index = Decimal(value)
                except InvalidOperation:
                    raise CpiSourceError("cpi_timeseries_index_invalid") from None
                if not index.is_finite() or index <= 0:
                    raise CpiSourceError("cpi_timeseries_index_invalid")
            calculations, footnotes = (
                row.get("calculations", {}),
                row.get("footnotes", []),
            )
            if (
                type(calculations) is not dict
                or type(footnotes) is not list
                or any(type(note) is not dict for note in footnotes)
            ):
                raise CpiSourceError("cpi_timeseries_metadata_invalid")
            if start_month <= month <= end_month:
                points.append(
                    SourcePoint(identity, month, index, calculations, tuple(footnotes))
                )
    if seen_series != set(series_ids):
        raise CpiSourceError("cpi_timeseries_series_missing")
    return tuple(sorted(points, key=lambda point: (point.series_id, point.month)))


def _daily_limit(document: dict) -> bool:
    if document.get("status") == "REQUEST_SUCCEEDED":
        return False
    messages = document.get("message", [])
    if isinstance(messages, str):
        messages = [messages]
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, str):
            continue
        text = message.lower()
        if (
            ("daily" in text or "per day" in text or "24 hour" in text)
            and any(word in text for word in ("limit", "quota", "threshold", "maximum"))
            and any(word in text for word in ("request", "api", "query", "queries"))
        ):
            return True
    return False


class BlsRangeSource:
    """One request per allowed year window using the shared MDF retry policy."""

    def __init__(
        self,
        *,
        secret_path: Path = DEFAULT_SECRET_PATH,
        credential_loader: Callable[[], str] | None = None,
        registered: bool = True,
        transport: Transport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        max_requests: int = MAX_RANGE_REQUESTS,
        request_control: CpiRequestControl | None = None,
        max_transport_attempts: int = 3,
    ) -> None:
        if not valid_transport_attempts(max_transport_attempts):
            raise CpiSourceError("cpi_timeseries_transport_attempts_invalid")
        self._secret_path = secret_path
        self._credential_loader = credential_loader
        self.registered = registered
        self._transport = transport or ExactOneHttpsTransport()
        self._clock = clock
        self._sleep = sleep
        self._max_requests = max_requests
        self._request_control = request_control
        self._max_transport_attempts = max_transport_attempts

    def _request(self, body: bytes, key: str | None) -> tuple[HttpResult, int]:
        attempts = 0
        last_status = None
        spacing_reacquisitions = 0
        while attempts < self._max_transport_attempts:
            try:
                admission = (
                    self._request_control.attempt()
                    if self._request_control is not None
                    else nullcontext(None)
                )
                with admission as ticket:
                    attempts += 1
                    response = self._transport.post(
                        ENDPOINT,
                        body,
                        {
                            "Accept": "application/json",
                            "Accept-Encoding": "identity",
                            "Content-Type": "application/json",
                        },
                        timeout=TIMEOUT_SECONDS,
                        max_response_bytes=MAX_RANGE_RESPONSE_BYTES,
                    )
                    if type(response.status) is int and 100 <= response.status <= 599:
                        last_status = response.status
                    daily_limit = False
                    if response.status == 200:
                        try:
                            daily_limit = _daily_limit(_json(response.body, key))
                        except CpiSourceError:
                            # Full response validation remains outside wire retries.
                            pass
                    retry_at = (
                        ticket.record_response(
                            response.status,
                            response.headers,
                            provider_daily_limit=daily_limit,
                        )
                        if ticket is not None
                        else None
                    )
                    if daily_limit:
                        raise CpiSourceError(
                            "cpi_timeseries_provider_daily_limit",
                            attempts,
                            http_status=200,
                            retry_at=retry_at or self._clock() + timedelta(hours=24),
                        )
                    if response.status == 429:
                        raise CpiSourceError(
                            "bls_timeseries_http_failure",
                            attempts,
                            http_status=429,
                            retry_at=retry_at
                            or retry_after_at(response.headers, self._clock()),
                        )
            except CpiRequestControlError as error:
                # One fetch can span several year windows. A short admission
                # gap between its own exchanges must not discard earlier work.
                # Long cooldowns/busy still return immediately. A frozen clock
                # cannot turn admission into an unbounded polling loop.
                if (
                    error.classification == "cpi_timeseries_request_spacing"
                    and error.retry_at is not None
                    and spacing_reacquisitions < MAX_SPACING_REACQUISITIONS
                ):
                    delay = (error.retry_at - self._clock()).total_seconds()
                    if 0 < delay <= MAX_SPACING_WAIT_SECONDS:
                        spacing_reacquisitions += 1
                        self._sleep(delay)
                        continue
                raise CpiSourceError(
                    error.classification,
                    attempts,
                    http_status=error.http_status or last_status,
                    retry_at=error.retry_at,
                ) from None
            except CpiSourceError:
                raise
            except LiveExecutionError as error:
                raise CpiSourceError(
                    error.classification,
                    attempts,
                    http_status=error.http_status or last_status,
                ) from None
            except Exception as error:
                classification, transient = classify_source_exception(error)
                classification = (
                    "bls_timeseries_transport_failure"
                    if classification in {"timeout", "transport_failure"}
                    else "bls_timeseries_request_failed"
                )
            else:
                if response.status == 200:
                    return response, attempts
                classification = (
                    "bls_timeseries_redirect_rejected"
                    if type(response.status) is int and 300 <= response.status < 400
                    else "bls_timeseries_http_failure"
                )
                transient = retryable_http_status(response.status)
            if not transient or attempts >= self._max_transport_attempts:
                raise CpiSourceError(
                    classification, attempts, http_status=last_status
                ) from None
            self._sleep(retry_delay_after(attempts))
        raise AssertionError("cpi_timeseries_attempts_exhausted")

    def fetch(
        self,
        series_ids: tuple[str, ...],
        start_month: str,
        end_month: str,
    ) -> tuple[SourceBatch, ...]:
        _series(series_ids)
        windows = year_windows(
            start_month,
            end_month,
            registered=self.registered,
            max_requests=self._max_requests,
        )
        try:
            key = (
                (
                    self._credential_loader()
                    if self._credential_loader is not None
                    else load_registration_key(self._secret_path)
                )
                if self.registered
                else None
            )
        except LiveExecutionError as error:
            raise CpiSourceError(error.classification, 0) from None
        except Exception:
            raise CpiSourceError("bls_live_credential_invalid", 0) from None
        if self.registered and (
            type(key) is not str or _KEY_SHAPE.fullmatch(key) is None
        ):
            raise CpiSourceError("bls_live_credential_invalid", 0)
        batches: list[SourceBatch] = []
        total_attempts = 0
        for first, last in windows:
            request = {
                "seriesid": list(series_ids),
                "startyear": str(first),
                "endyear": str(last),
                "calculations": True,
                "catalog": True,
                "annualaverage": False,
            }
            wire = dict(request)
            if key is not None:
                wire["registrationkey"] = key
            body = json.dumps(wire, sort_keys=True, separators=(",", ":")).encode()
            try:
                response, attempts = self._request(body, key)
            except CpiSourceError as error:
                raise CpiSourceError(
                    error.classification,
                    total_attempts + error.attempts,
                    http_status=error.http_status,
                    retry_at=error.retry_at,
                ) from None
            total_attempts += attempts
            try:
                if response.headers.get("content-encoding", "identity").lower() != (
                    "identity"
                ):
                    raise CpiSourceError("cpi_timeseries_content_encoding_rejected")
                if key is not None and key.encode() in response.body:
                    raise CpiSourceError("cpi_timeseries_credential_echo_forbidden")
                observed_at = self._clock()
                if (
                    not isinstance(observed_at, datetime)
                    or observed_at.tzinfo is None
                    or observed_at.utcoffset() is None
                ):
                    raise CpiSourceError("cpi_timeseries_observed_at_invalid")
                points = parse_response(
                    response.body,
                    series_ids,
                    max(start_month, f"{first:04d}-01"),
                    min(end_month, f"{last:04d}-12"),
                    forbidden_secret=key,
                )
            except CpiSourceError as error:
                raise CpiSourceError(
                    error.classification,
                    total_attempts,
                    http_status=response.status,
                    retry_at=error.retry_at,
                ) from None
            batches.append(
                SourceBatch(points, response.body, request, observed_at, attempts)
            )
        return tuple(batches)
