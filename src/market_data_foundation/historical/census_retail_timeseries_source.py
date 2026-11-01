"""Bounded MARTS input snapshots; opt-in transport, no storage or service binding."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from market_data_foundation.historical.census_retail import (
    CATEGORY_CODES,
    ENDPOINT,
    FIELDS,
    HEADERS,
)
from market_data_foundation.historical.census_retail_live import (
    ExactCensusTransport,
    HttpResult,
)
from market_data_foundation.historical.cpi_request_control import retry_after_at
from market_data_foundation.historical.cpi_timeseries import month_at, month_number
from market_data_foundation.source_request_policy import (
    SOURCE_REQUEST_POLICY,
    classify_source_exception,
    retry_delay_after,
    retryable_http_status,
)

BATCH_MONTHS = 252
MAX_REQUESTS = 10
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_INLINE_WAIT = 5.0
_NUMBER = re.compile(r"-?[0-9]{1,18}(?:\.[0-9]{1,9})?")


class RetailSourceError(RuntimeError):
    """Safe facts only; never provider text or wire parameters."""

    def __init__(self, classification, attempts=0, *, http_status=None, retry_at=None):
        self.classification, self.attempts = classification, attempts
        self.http_status, self.retry_at = http_status, retry_at
        super().__init__(classification)


def _require(condition, classification):
    if not condition:
        raise RetailSourceError("census_retail_" + classification)


def _range(start, end):
    try:
        first, last = month_number(start), month_number(end)
    except Exception:
        raise RetailSourceError("census_retail_month_invalid") from None
    _require(first <= last, "range_invalid")
    return first, last


def _now(clock):
    value = clock()
    _require(
        isinstance(value, datetime) and value.utcoffset() is not None,
        "aware_clock_required",
    )
    return value.astimezone(UTC)


@dataclass(frozen=True)
class RetailRangeRequest:
    start_month: str
    end_month: str

    def __post_init__(self):
        first, last = _range(self.start_month, self.end_month)
        _require(last - first < BATCH_MONTHS, "batch_too_large")

    def persisted(self):
        return {
            "endpoint": ENDPOINT,
            "method": "GET",
            "parameters": {
                "get": ",".join(FIELDS),
                "seasonally_adj": "yes",
                "error_data": "no",
                "for": "us:*",
                "time": f"from {self.start_month} to {self.end_month}",
            },
        }

    @property
    def identity(self):
        raw = json.dumps(self.persisted(), sort_keys=True).encode()
        return "census-marts-range:" + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class RetailRangePlan:
    start_month: str
    end_month: str
    input_start_month: str
    requests: tuple[RetailRangeRequest, ...]

    @property
    def transport_attempt_budget(self):
        return len(self.requests) * SOURCE_REQUEST_POLICY.max_transport_attempts


def plan_range(start_month, end_month, *, buffer_months=12, max_requests=MAX_REQUESTS):
    """Budget all inclusive windows before IO; 240 target months + 12 fit one GET."""
    first, last = _range(start_month, end_month)
    _require(type(buffer_months) is int and 0 <= buffer_months <= 12, "buffer_invalid")
    _require(
        type(max_requests) is int and 1 <= max_requests <= MAX_REQUESTS,
        "request_budget_invalid",
    )
    first -= buffer_months
    _range(month_at(first), end_month)
    count = (last - first) // BATCH_MONTHS + 1
    _require(count <= max_requests, "request_budget_exceeded")
    requests = tuple(
        RetailRangeRequest(month_at(n), month_at(min(n + BATCH_MONTHS - 1, last)))
        for n in range(first, last + 1, BATCH_MONTHS)
    )
    return RetailRangePlan(start_month, end_month, month_at(first), requests)


@dataclass(frozen=True)
class RetailCell:
    month: str
    category_code: str
    data_type_code: str
    value: Decimal | None
    status: str
    reason: str | None
    source_value: str | None
    source_pointer: str | None
    raw_sha256: str

    @property
    def key(self):
        return self.month, self.category_code, self.data_type_code

    @property
    def unit(self):
        return "million_current_usd" if self.data_type_code == "SM" else "percent"


def _cell(month, category, kind, value, pointer, digest):
    if value is not None and _NUMBER.fullmatch(value):
        number, status, reason = Decimal(value), "available", None
    elif value in {"(S)", "(D)"}:
        number, status, reason = None, "suppressed", "source_suppression_marker"
    else:
        number, status = None, "unavailable"
        reason = "source_null" if value is None else "source_non_numeric_marker"
    return RetailCell(
        month, category, kind, number, status, reason, value, pointer, digest
    )


def parse_range_response(raw, start_month, end_month, *, forbidden_secret=None):
    """Project six source cells/month, retaining absent and suppressed cells."""
    first, last = _range(start_month, end_month)
    _require(last - first < BATCH_MONTHS, "batch_too_large")
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_RESPONSE_BYTES, "body_invalid")
    if forbidden_secret is not None:
        _require(
            type(forbidden_secret) is str and bool(forbidden_secret),
            "credential_invalid",
        )
        _require(forbidden_secret.encode() not in raw, "credential_echo_forbidden")
    try:
        table = json.loads(raw)
    except Exception:
        raise RetailSourceError("census_retail_json_invalid") from None
    _require(type(table) is list and bool(table), "table_invalid")
    # Inspect decoded strings too: JSON unicode escapes must not bypass the guard.
    if forbidden_secret:
        _require(
            all(
                forbidden_secret not in value
                for row in table
                if type(row) is list
                for value in row
                if type(value) is str
            ),
            "credential_echo_forbidden",
        )
    header = table[0]
    _require(
        type(header) is list
        and all(type(name) is str for name in header)
        and set(header) == set(HEADERS),
        "header_invalid",
    )
    positions = {
        name: [n for n, item in enumerate(header) if item == name] for name in HEADERS
    }
    _require(
        all(
            len(indices)
            in ((1, 2) if name in {"seasonally_adj", "error_data"} else (1,))
            for name, indices in positions.items()
        ),
        "header_invalid",
    )
    months = tuple(month_at(n) for n in range(first, last + 1))
    expected = {(m, c, "SM") for m in months for c in CATEGORY_CODES} | {
        (m, "44X72", "MPCSM") for m in months
    }
    digest, cells = hashlib.sha256(raw).hexdigest(), {}
    for n, values in enumerate(table[1:], 1):
        _require(
            type(values) is list
            and len(values) == len(header)
            and all(
                type(value) is str or (value is None and header[i] == "cell_value")
                for i, value in enumerate(values)
            ),
            "row_invalid",
        )
        _require(
            all(
                values[ix[0]] == values[ix[1]]
                for ix in positions.values()
                if len(ix) == 2
            ),
            "duplicate_dimension_mismatch",
        )
        row = {name: values[ix[0]] for name, ix in positions.items()}
        _require(
            row["us"] == "1"
            and row["seasonally_adj"] == "yes"
            and row["error_data"] == "no",
            "row_dimensions_invalid",
        )
        _require(row["time"] in months, "row_range_invalid")
        key = row["time"], row["category_code"], row["data_type_code"]
        if key not in expected:
            continue
        _require(key not in cells, "duplicate_target")
        value = row["cell_value"]
        _require(value is None or len(value) <= 128, "source_value_invalid")
        cells[key] = _cell(*key, value, f"/{n}/{positions['cell_value'][0]}", digest)
    return tuple(
        cells[key]
        if key in cells
        else RetailCell(*key, None, "missing", "source_row_absent", None, None, digest)
        for key in sorted(expected)
    )


@dataclass(frozen=True)
class RetailBatch:
    request: RetailRangeRequest
    cells: tuple[RetailCell, ...]
    raw: bytes = field(repr=False)
    observed_at: datetime
    attempts: int


@dataclass(frozen=True)
class RetailInputSnapshot:
    plan: RetailRangePlan
    batches: tuple[RetailBatch, ...]
    requested_at: datetime

    @property
    def identity(self):
        material = [self.requested_at.isoformat()] + [
            batch.request.identity + ":" + hashlib.sha256(batch.raw).hexdigest()
            for batch in self.batches
        ]
        return (
            "census-marts-snapshot:"
            + hashlib.sha256("|".join(material).encode()).hexdigest()
        )


class CensusRetailRangeSource:
    """Explicit credential only; no file loading, persistence or binding changes."""

    def __init__(
        self,
        *,
        enabled=False,
        credential=None,
        transport=None,
        clock=lambda: datetime.now(UTC),
        sleep=time.sleep,
        max_requests=MAX_REQUESTS,
    ):
        self._enabled, self._credential = enabled, credential
        self._transport, self._clock, self._sleep = transport, clock, sleep
        self._max_requests = max_requests

    def plan(self, start_month, end_month, *, buffer_months=12):
        return plan_range(
            start_month,
            end_month,
            buffer_months=buffer_months,
            max_requests=self._max_requests,
        )

    def _request(self, client, parameters):
        last_status = None
        for attempt in range(1, SOURCE_REQUEST_POLICY.max_transport_attempts + 1):
            try:
                response = client.get(
                    ENDPOINT,
                    parameters,
                    timeout=20.0,
                    max_response_bytes=MAX_RESPONSE_BYTES,
                )
            except RetailSourceError as error:
                raise RetailSourceError(
                    error.classification,
                    max(error.attempts, attempt - 1),
                    http_status=error.http_status,
                    retry_at=error.retry_at,
                ) from None
            except Exception as error:
                classification, transient = classify_source_exception(error)
            else:
                if type(response) is not HttpResult or type(response.status) is not int:
                    raise RetailSourceError(
                        "census_retail_http_response_invalid", attempt
                    ) from None
                last_status = response.status
                if last_status == 200:
                    return response, attempt
                classification, transient = (
                    "http_failure",
                    retryable_http_status(last_status),
                )
                try:
                    if transient and (
                        last_status == 429
                        or any(k.lower() == "retry-after" for k in response.headers)
                    ):
                        now = _now(self._clock)
                        retry_at = retry_after_at(
                            response.headers,
                            now,
                            minimum_seconds=300
                            if last_status == 429
                            else retry_delay_after(min(attempt, 2)),
                        )
                        delay = (retry_at - now).total_seconds()
                        if delay > MAX_INLINE_WAIT or attempt == 3:
                            raise RetailSourceError(
                                "census_retail_http_failure",
                                attempt,
                                http_status=last_status,
                                retry_at=retry_at,
                            ) from None
                        self._sleep(delay)
                        continue
                except RetailSourceError as error:
                    raise RetailSourceError(
                        error.classification,
                        attempt,
                        http_status=last_status,
                        retry_at=error.retry_at,
                    ) from None
                except Exception:
                    raise RetailSourceError(
                        "census_retail_local_failure", attempt, http_status=last_status
                    ) from None
            if not transient or attempt == 3:
                raise RetailSourceError(
                    "census_retail_" + classification, attempt, http_status=last_status
                ) from None
            try:
                self._sleep(retry_delay_after(attempt))
            except Exception:
                raise RetailSourceError(
                    "census_retail_local_failure", attempt, http_status=last_status
                ) from None
        raise AssertionError

    def fetch(
        self, start_month, end_month, *, buffer_months=12, completed=(), on_batch=None
    ):
        _require(self._enabled is True, "explicit_opt_in_required")
        plan = self.plan(start_month, end_month, buffer_months=buffer_months)
        key = self._credential
        _require(
            type(key) is str and re.fullmatch(r"[A-Za-z0-9]{16,128}", key) is not None,
            "credential_invalid",
        )
        retained = {batch.request: batch for batch in completed}
        _require(
            len(retained) == len(completed) and set(retained) <= set(plan.requests),
            "resume_batches_mismatch",
        )
        total, batches = 0, []
        try:
            requested_at = _now(self._clock)
            client = self._transport or ExactCensusTransport()
            for request in plan.requests:
                if request in retained:
                    batches.append(retained[request])
                    continue
                try:
                    response, attempts = self._request(
                        client, request.persisted()["parameters"] | {"key": key}
                    )
                except RetailSourceError as error:
                    total += error.attempts
                    raise
                total += attempts
                # All parsing, clocks and local work follow the successful exchange.
                _require(
                    response.headers.get("content-encoding", "identity").lower()
                    == "identity"
                    and response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .lower()
                    == "application/json",
                    "http_headers_invalid",
                )
                cells = parse_range_response(
                    response.body,
                    request.start_month,
                    request.end_month,
                    forbidden_secret=key,
                )
                observed_at = _now(self._clock)
                _require(observed_at >= requested_at, "clock_regressed")
                batch = RetailBatch(
                    request, cells, response.body, observed_at, attempts
                )
                if on_batch is not None:
                    on_batch(batch)
                batches.append(batch)
            return RetailInputSnapshot(plan, tuple(batches), requested_at)
        except RetailSourceError as error:
            raise RetailSourceError(
                error.classification,
                total,
                http_status=error.http_status,
                retry_at=error.retry_at,
            ) from None
        except Exception:
            raise RetailSourceError("census_retail_local_failure", total) from None
