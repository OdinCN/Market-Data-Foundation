"""Bounded FRED claims API; no DOL/CSV fallback, credentials only on wire."""

from __future__ import annotations

import http.client
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

from market_data_foundation.historical.claims_contract import (
    BY_ID,
    ClaimsError,
    day,
    instant,
    validate_series,
    weeks,
)
from market_data_foundation.historical.cpi_request_control import (
    CpiRequestControl,
    CpiRequestControlError,
)
from market_data_foundation.historical.fred_alfred_vintage_live import load_api_key
from market_data_foundation.source_request_policy import (
    classify_source_exception,
    retry_delay_after,
    retryable_http_status,
)

MAX_RESPONSE_BYTES = 2_000_000
DEFAULT_CONTROL = Path("artifacts/claims/fred-request-control.json")


@dataclass(frozen=True)
class ClaimsRequest:
    operation: str
    parameters: dict

    @property
    def url(self):
        return "https://api.stlouisfed.org/fred/series" + (
            "/observations" if self.operation == "observations" else ""
        )


@dataclass(frozen=True)
class ClaimsResponse:
    status: int
    body: bytes = field(repr=False)
    headers: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ClaimsPoint:
    series_id: str
    week_ending: str
    value: Decimal | None
    provider_extra: dict


@dataclass(frozen=True)
class ClaimsBatch:
    points: tuple[ClaimsPoint, ...]
    raw: bytes = field(repr=False)
    request: dict
    observed_at: datetime
    attempts: int


class ClaimsHttpsTransport:
    def __init__(self, key):
        self._key = key

    def __call__(self, request):
        if request.operation not in {"metadata", "observations"}:
            raise ClaimsError("claims_operation_invalid")
        path = "/fred/series" + (
            "/observations" if request.operation == "observations" else ""
        )
        connection = http.client.HTTPSConnection("api.stlouisfed.org", timeout=12)
        try:
            connection.request(
                "GET",
                path + "?" + urlencode({**request.parameters, "api_key": self._key}),
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            )
            response = connection.getresponse()
            encoding = response.getheader("Content-Encoding", "identity")
            if encoding.lower() != "identity":
                raise ClaimsError("claims_content_encoding_rejected")
            length = response.getheader("Content-Length")
            if length is not None and (
                not length.isascii()
                or not length.isdecimal()
                or len(length) > 8
                or int(length) > MAX_RESPONSE_BYTES
            ):
                raise ClaimsError("claims_response_size_invalid")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ClaimsError("claims_response_too_large")
            if self._key.encode() in raw:
                raise ClaimsError("claims_credential_echo_rejected")
            if length is not None and len(raw) != int(length):
                raise http.client.IncompleteRead(raw, int(length) - len(raw))
            return ClaimsResponse(response.status, raw, dict(response.getheaders()))
        finally:
            connection.close()


def document(raw):
    try:
        result = json.loads(raw)
        if type(result) is not dict or "error_code" in result:
            raise ValueError
        return result
    except ValueError, TypeError, UnicodeError:
        raise ClaimsError("claims_response_invalid") from None


def parse_observations(raw, series_id, start, end):
    validate_series((series_id,))
    weeks(start, end)
    doc = document(raw)
    rows = doc.get("observations")
    if (
        type(rows) is not list
        or type(doc.get("count")) is not int
        or doc["count"] != len(rows)
        or type(doc.get("offset")) is not int
        or doc.get("offset") != 0
        or doc.get("units") != "lin"
        or type(doc.get("output_type")) is not int
        or doc.get("output_type") != 1
        or doc.get("observation_start") != start
        or doc.get("observation_end") != end
        or doc.get("sort_order") != "asc"
    ):
        raise ClaimsError("claims_response_contract_invalid")
    points, previous = [], None
    for row in rows:
        if type(row) is not dict:
            raise ClaimsError("claims_row_invalid")
        week = row.get("date")
        if (
            day(week).weekday() != 5
            or not start <= week <= end
            or (previous is not None and week <= previous)
        ):
            raise ClaimsError("claims_week_ending_invalid")
        previous = week
        first, last = row.get("realtime_start"), row.get("realtime_end")
        if day(first) > day(last):
            raise ClaimsError("claims_realtime_range_invalid")
        text = row.get("value")
        if type(text) is not str or (
            text != "." and not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text)
        ):
            raise ClaimsError("claims_value_invalid")
        points.append(
            ClaimsPoint(
                series_id,
                week,
                None if text == "." else Decimal(text),
                {
                    "raw_value": text,
                    "realtime_start": first,
                    "realtime_end": last,
                    "realtime_precision": "date",
                    "realtime_date_basis": "fred_not_disambiguated",
                    "source_missing_reason": "provider_missing"
                    if text == "."
                    else None,
                    "event_binding_status": "unconfirmed",
                },
            )
        )
    return tuple(points)


def parse_metadata(raw, series_id):
    validate_series((series_id,))
    rows = document(raw).get("seriess")
    metric = BY_ID[series_id]
    if type(rows) is not list or len(rows) != 1 or type(rows[0]) is not dict:
        raise ClaimsError("claims_metadata_invalid")
    row = rows[0]
    if (
        row.get("id") != series_id
        or row.get("frequency_short") != "W"
        or row.get("frequency") != "Weekly, Ending Saturday"
        or row.get("units") != metric.unit
        or row.get("seasonal_adjustment_short") != metric.seasonal_adjustment
    ):
        raise ClaimsError("claims_metadata_mapping_invalid")
    return row


class ClaimsFredSource:
    """Each series is one logical read; all retries share durable admission.

    The caller supplies a raw sink for evidence. Capture precedes parsing, so a
    parser failure cannot erase a credential-safe HTTP response or trigger retries.
    """

    def __init__(
        self,
        *,
        transport=None,
        live_opt_in=False,
        request_control=None,
        request_control_path=DEFAULT_CONTROL,
        raw_sink=None,
        clock=lambda: datetime.now(UTC),
        sleep=time.sleep,
        max_requests=20,
    ):
        if type(max_requests) is not int or not 1 <= max_requests <= 20:
            raise ClaimsError("claims_request_budget_invalid")
        if transport is None:
            if live_opt_in is not True:
                raise ClaimsError("claims_live_opt_in_required")
            transport = ClaimsHttpsTransport(load_api_key())
        self._transport, self._sink = transport, raw_sink
        self._clock, self._sleep = clock, sleep
        self._control = request_control or CpiRequestControl(
            request_control_path, clock=clock, min_interval_seconds=1.1
        )
        self.max_requests = max_requests
        self.request_count = self.attempts = 0

    def _read(self, request):
        if self.request_count >= self.max_requests:
            raise ClaimsError("claims_request_budget_exceeded")
        self.request_count += 1
        attempts, last_status = 0, None
        while attempts < 3:
            try:
                # At most one short spacing wait, never poll cooldown or quota.
                for admission in range(2):
                    try:
                        with self._control.attempt() as control:
                            attempts += 1
                            self.attempts += 1
                            response = self._transport(request)
                            last_status = response.status
                            retry_at = control.record_response(
                                response.status, response.headers
                            )
                        break
                    except CpiRequestControlError as error:
                        delay = (
                            (error.retry_at - self._clock()).total_seconds()
                            if error.retry_at
                            else 0
                        )
                        if (
                            admission == 0
                            and error.classification.endswith("request_spacing")
                            and 0 < delay <= 5
                        ):
                            self._sleep(delay)
                            continue
                        raise ClaimsError(
                            error.classification.replace("cpi_timeseries_", "claims_"),
                            http_status=error.http_status,
                            retry_at=error.retry_at,
                        ) from None
                observed = instant(self._clock())
                if self._sink:
                    try:
                        self._sink(request, response, observed, attempts)
                    except Exception:
                        raise ClaimsError("claims_raw_retention_failed") from None
                if response.status == 200:
                    return response.body, observed, attempts
                if response.status == 429:
                    raise ClaimsError(
                        "claims_http_failure", http_status=429, retry_at=retry_at
                    )
                classification = "claims_http_failure"
                transient = retryable_http_status(response.status)
            except ClaimsError as error:
                raise ClaimsError(
                    error.classification,
                    self.attempts,
                    request_count=self.request_count,
                    http_status=error.http_status or last_status,
                    retry_at=error.retry_at,
                ) from None
            except Exception as error:
                classification, transient = classify_source_exception(error)
                classification = "claims_" + classification
                if isinstance(error, PermissionError):
                    transient = False
            if not transient or attempts == 0 or attempts >= 3:
                raise ClaimsError(
                    classification,
                    self.attempts,
                    request_count=self.request_count,
                    http_status=last_status,
                ) from None
            self._sleep(retry_delay_after(attempts))
        raise AssertionError

    def metadata(self, series_id):
        validate_series((series_id,))
        raw, _, _ = self._read(
            ClaimsRequest("metadata", {"series_id": series_id, "file_type": "json"})
        )
        try:
            return parse_metadata(raw, series_id)
        except ClaimsError as error:
            raise ClaimsError(
                error.classification,
                self.attempts,
                request_count=self.request_count,
            ) from None

    def fetch(self, series_ids, start, end):
        validate_series(series_ids)
        weeks(start, end)
        if self.request_count + len(series_ids) > self.max_requests:
            raise ClaimsError("claims_request_budget_exceeded")
        batches = []
        for sid in series_ids:
            params = {
                "series_id": sid,
                "file_type": "json",
                "units": "lin",
                "observation_start": start,
                "observation_end": end,
                "output_type": "1",
                "sort_order": "asc",
                "limit": "100000",
            }
            request = ClaimsRequest("observations", params)
            raw, observed, attempts = self._read(request)
            try:
                points = parse_observations(raw, sid, start, end)
            except ClaimsError as error:
                raise ClaimsError(
                    error.classification,
                    self.attempts,
                    request_count=self.request_count,
                ) from None
            batches.append(
                ClaimsBatch(
                    points,
                    raw,
                    {"url": request.url, "parameters": params, "request_count": 1},
                    observed,
                    attempts,
                )
            )
        return tuple(batches)
