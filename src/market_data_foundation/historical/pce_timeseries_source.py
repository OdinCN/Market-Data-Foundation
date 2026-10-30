"""BEA monthly index ranges; only credential-free Results projections leave here."""

from __future__ import annotations

import fcntl
import http.client
import json
import math
import os
import re
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

from market_data_foundation.historical.bls_cpi_direct_live import HttpResult
from market_data_foundation.historical.bls_timeseries_profile import (
    PCE_PROFILE,
    validate_profile,
)
from market_data_foundation.historical.cpi_request_control import retry_after_at
from market_data_foundation.historical.cpi_timeseries_source import (
    CpiSourceError,
    SourceBatch,
    SourcePoint,
    _series,
    validate_month,
)
from market_data_foundation.source_request_policy import (
    classify_source_exception,
    retry_delay_after,
    retryable_http_status,
    valid_transport_attempts,
)

SERIES_IDS = PCE_PROFILE.series_ids
PceSourceError = CpiSourceError
ENDPOINT = "https://apps.bea.gov/api/data/"
MAX_RANGE_REQUESTS = 48
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_REQUEST_CONTROL_PATH = Path("artifacts/pce-timeseries/bea-request-control.json")
_LINES = {"1": "DPCERG", "25": "DPCCRG"}
_SENSITIVE = re.compile(
    r"userid|api.?key|registration.?key|credential|secret|token", re.I
)


def year_windows(start_month, end_month, *, max_requests=MAX_RANGE_REQUESTS):
    """Conservative five-year BEA batches; not the BLS twenty-year rule."""
    validate_month(start_month)
    validate_month(end_month)
    if start_month > end_month:
        raise PceSourceError("pce_timeseries_range_invalid")
    if type(max_requests) is not int or not 1 <= max_requests <= MAX_RANGE_REQUESTS:
        raise PceSourceError("pce_timeseries_request_budget_invalid")
    first, last = int(start_month[:4]), int(end_month[:4])
    windows = tuple((year, min(year + 4, last)) for year in range(first, last + 1, 5))
    if len(windows) > max_requests:
        raise PceSourceError("pce_timeseries_request_budget_exceeded")
    return windows


def _decode(raw):
    def pairs(items):
        if len(dict(items)) != len(items):
            raise ValueError
        return dict(items)

    def constant(_):
        raise ValueError

    if type(raw) is not bytes or not 0 < len(raw) <= MAX_RESPONSE_BYTES:
        raise PceSourceError("pce_timeseries_response_size_invalid")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant
        )
        if type(value) is not dict:
            raise ValueError
        return value
    except ValueError, UnicodeError, TypeError, RecursionError:
        raise PceSourceError("pce_timeseries_invalid_json") from None


def _results(raw, forbidden_secret=None):
    document = _decode(raw)
    envelope = document.get("BEAAPI")
    if type(envelope) is not dict:
        raise PceSourceError("pce_timeseries_response_invalid")
    results = envelope.get("Results")
    if "Error" in envelope or (isinstance(results, dict) and "Error" in results):
        raise PceSourceError("pce_timeseries_provider_rejected")
    if type(results) is not dict or type(results.get("Data")) is not list:
        raise PceSourceError("pce_timeseries_response_invalid")
    # Request.RequestParam may legitimately echo UserID. Discard the entire
    # Request envelope; never hash, log, archive or return authenticated bytes.
    pending, strings = [results], set()
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if any(_SENSITIVE.search(k) for k in value):
                raise PceSourceError("pce_timeseries_credential_echo_forbidden")
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str):
            strings.add(value)
            if forbidden_secret and forbidden_secret.casefold() in value.casefold():
                raise PceSourceError("pce_timeseries_credential_echo_forbidden")
    if forbidden_secret:
        # Reuse the established BEA edge's encoded credential echo guard, not
        # its fixed-date qualification or dual-table execution path.
        from market_data_foundation.historical.bea_pce_live import _credential_scan

        try:
            for value in strings:
                _credential_scan(value, forbidden_secret)
        except Exception:
            raise PceSourceError("pce_timeseries_credential_echo_forbidden") from None
    return results


def _parse_results(results, series_ids, start_month, end_month):
    points, seen = [], set()
    for row in results["Data"]:
        if type(row) is not dict or row.get("TableName") != "T20804":
            raise PceSourceError("pce_timeseries_table_invalid")
        line, series = row.get("LineNumber"), row.get("SeriesCode")
        if type(line) is not str or not line.isdigit() or type(series) is not str:
            raise PceSourceError("pce_timeseries_series_invalid")
        if (line in _LINES and series != _LINES[line]) or (
            series in SERIES_IDS and _LINES.get(line) != series
        ):
            raise PceSourceError("pce_timeseries_series_mapping_invalid")
        period = row.get("TimePeriod")
        if (
            type(period) is not str
            or re.fullmatch(r"[0-9]{4}M(?:0[1-9]|1[0-2])", period) is None
        ):
            raise PceSourceError("pce_timeseries_period_invalid")
        month = period[:4] + "-" + period[5:]
        if not start_month[:4] <= month[:4] <= end_month[:4]:
            raise PceSourceError("pce_timeseries_period_invalid")
        key = (line, month)
        if key in seen:
            raise PceSourceError("pce_timeseries_duplicate_point")
        seen.add(key)
        if line not in _LINES:
            continue  # BEA returns the full NIPA table, including unrelated rows.
        if (
            row.get("CL_UNIT") != "Level"
            or row.get("UNIT_MULT") != "0"
            or row.get("METRIC_NAME") != "Fisher Price Index"
        ):
            raise PceSourceError("pce_timeseries_unit_invalid")
        if "DataValue" not in row:
            raise PceSourceError("pce_timeseries_point_invalid")
        value = row["DataValue"]
        if isinstance(value, str):
            value = value.strip()
        if value in (None, "", "(NA)", "NA", "---", "--", "..."):
            index = None
        else:
            if (
                type(value) is not str
                or re.fullmatch(
                    r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?", value
                )
                is None
            ):
                raise PceSourceError("pce_timeseries_index_invalid")
            try:
                index = Decimal(value.replace(",", ""))
            except InvalidOperation:
                raise PceSourceError("pce_timeseries_index_invalid") from None
            if not index.is_finite() or index <= 0:
                raise PceSourceError("pce_timeseries_index_invalid")
        if series in series_ids and start_month <= month <= end_month:
            metadata = {
                k: row[k]
                for k in (
                    "TableName",
                    "LineNumber",
                    "SeriesCode",
                    "CL_UNIT",
                    "UNIT_MULT",
                    "METRIC_NAME",
                    "LineDescription",
                    "NoteRef",
                )
                if k in row
            }
            points.append(SourcePoint(series, month, index, {"bea": metadata}, ()))
    return tuple(sorted(points, key=lambda p: (p.series_id, p.month)))


def parse_response(
    raw,
    series_ids,
    start_month,
    end_month,
    *,
    forbidden_secret=None,
    profile=PCE_PROFILE,
):
    if profile is not PCE_PROFILE:
        raise PceSourceError("pce_timeseries_profile_mismatch")
    _series(series_ids, profile=profile)
    year_windows(start_month, end_month)
    return _parse_results(
        _results(raw, forbidden_secret), series_ids, start_month, end_month
    )


class BeaRequestControl:
    """BEA-only cross-process pacing and durable UTC Retry-After deadlines.

    The stable state file is also the exchange lock. All timestamps are Unix
    seconds, so cooldown survives process and machine restarts. Hosts may supply
    a trusted fixed path; defaults share artifacts within the candidate CWD.
    """

    provider = "BEA"

    def __init__(
        self,
        state_path=DEFAULT_REQUEST_CONTROL_PATH,
        *,
        sleep=time.sleep,
        clock=lambda: datetime.now(UTC),
    ):
        self.state_path, self.sleep = Path(state_path).resolve(), sleep
        self.clock = clock

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError
        return value.timestamp()

    @staticmethod
    def _write(stream, state):
        stream.seek(0)
        stream.truncate()
        json.dump(state, stream, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())

    @contextmanager
    def attempt(self):
        stream = None
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            stream = self.state_path.open("a+")
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            stream.seek(0)
            text = stream.read(4097)
            now = self._now()
            state = json.loads(text) if text else {}
            # Upgrade the previous UTC admission-only format.
            if type(state) in (float, int):
                state = {"last_request_at": state}
            if type(state) is not dict or set(state) - {
                "last_request_at",
                "cooldown_until",
            }:
                raise ValueError
            for value in state.values():
                if (
                    type(value) not in (float, int)
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise ValueError
            until = state.get("cooldown_until", 0)
            if until > now:
                raise PceSourceError(
                    "pce_timeseries_request_cooldown",
                    http_status=429,
                    retry_at=datetime.fromtimestamp(until, UTC),
                )
            # A wall-clock rollback must never turn pacing into an unbounded wait.
            previous = min(state.get("last_request_at", now - 3.1), now)
            delay = max(0, previous + 3.1 - now)
            if delay:
                self.sleep(delay)
            state = {"last_request_at": self._now()}
            self._write(stream, state)
        except PceSourceError:
            if stream is not None:
                stream.close()
            raise
        except Exception:
            if stream is not None:
                stream.close()
            raise PceSourceError("pce_timeseries_request_control_failed") from None

        def record_retry(retry_at):
            try:
                state["cooldown_until"] = retry_at.timestamp()
                self._write(stream, state)
            except Exception:
                raise PceSourceError("pce_timeseries_request_control_failed") from None

        try:
            yield record_retry
        finally:
            stream.close()


class BeaHttpsTransport:
    def post(self, endpoint, body, headers, *, timeout, max_response_bytes):
        # BEA accepts URL-encoded API parameters; no redirects or host fallback.
        if endpoint != ENDPOINT:
            raise PceSourceError("pce_timeseries_endpoint_invalid")
        connection = http.client.HTTPSConnection("apps.bea.gov", timeout=timeout)
        try:
            connection.request(
                "GET", "/api/data/?" + body.decode("ascii"), headers=headers
            )
            response = connection.getresponse()
            length = response.getheader("Content-Length")
            declared = int(length) if length is not None else None
            if declared is not None and declared > max_response_bytes:
                raise PceSourceError("pce_timeseries_response_size_invalid")
            raw = (
                response.read(max_response_bytes + 1) if response.status == 200 else b""
            )
            if len(raw) > max_response_bytes:
                raise PceSourceError("pce_timeseries_response_size_invalid")
            if response.status == 200 and declared is not None and len(raw) < declared:
                raise http.client.IncompleteRead(raw, declared - len(raw))
            return HttpResult(response.status, raw, dict(response.getheaders()))
        finally:
            connection.close()


class BeaRangeSource:
    max_range_requests = MAX_RANGE_REQUESTS

    def __init__(
        self,
        *,
        credential_loader=None,
        transport=None,
        clock=lambda: datetime.now(UTC),
        sleep=time.sleep,
        max_requests=MAX_RANGE_REQUESTS,
        max_transport_attempts=3,
        request_control=None,
        request_control_path=None,
        profile=PCE_PROFILE,
    ):
        self.profile = validate_profile(profile)
        if profile is not PCE_PROFILE:
            raise PceSourceError("pce_timeseries_profile_mismatch")
        if not valid_transport_attempts(max_transport_attempts):
            raise PceSourceError("pce_timeseries_transport_attempts_invalid")
        if (
            request_control is not None
            and getattr(request_control, "provider", None) != "BEA"
        ):
            raise PceSourceError("pce_timeseries_request_control_binding_invalid")
        self._loader, self._transport = (
            credential_loader,
            transport or BeaHttpsTransport(),
        )
        self._clock, self._sleep = clock, sleep
        self._max_requests, self._max_attempts = max_requests, max_transport_attempts
        if request_control is not None and request_control_path is not None:
            raise PceSourceError("pce_timeseries_request_control_binding_invalid")
        self._control = request_control or BeaRequestControl(
            request_control_path or DEFAULT_REQUEST_CONTROL_PATH,
            sleep=sleep,
            clock=clock,
        )

    def plan_windows(self, start_month, end_month):
        return year_windows(start_month, end_month, max_requests=self._max_requests)

    def _request(self, body):
        last_status, attempts = None, 0
        while attempts < self._max_attempts:
            try:
                with self._control.attempt() as record_retry:
                    attempts += 1
                    response = self._transport.post(
                        ENDPOINT,
                        body,
                        {"Accept": "application/json", "Accept-Encoding": "identity"},
                        timeout=20.0,
                        max_response_bytes=MAX_RESPONSE_BYTES,
                    )
                    if response.status == 429:
                        retry_at = retry_after_at(response.headers, self._clock())
                        record_retry(retry_at)
                        raise PceSourceError(
                            "pce_timeseries_http_failure",
                            attempts,
                            http_status=429,
                            retry_at=retry_at,
                        )
                last_status = response.status
                if response.status == 200:
                    return response, attempts
                classification, transient = (
                    "pce_timeseries_http_failure",
                    retryable_http_status(response.status),
                )
            except PceSourceError as error:
                raise PceSourceError(
                    error.classification,
                    attempts,
                    http_status=error.http_status or last_status,
                    retry_at=error.retry_at,
                ) from None
            except Exception as error:
                _, transient = classify_source_exception(error)
                classification = "pce_timeseries_transport_failure"
            if not transient or attempts == self._max_attempts:
                raise PceSourceError(
                    classification, attempts, http_status=last_status
                ) from None
            self._sleep(retry_delay_after(attempts))
        raise AssertionError

    def fetch(self, series_ids, start_month, end_month):
        _series(series_ids, profile=self.profile)
        windows = self.plan_windows(start_month, end_month)
        try:
            key = self._loader() if self._loader else None
            if (
                type(key) is not str
                or not key.strip()
                or len(key) > 256
                or any(ord(c) < 33 or ord(c) > 126 for c in key)
            ):
                raise ValueError
        except Exception:
            raise PceSourceError("pce_timeseries_bea_credential_invalid") from None
        batches, total_attempts = [], 0
        for first, last in windows:
            params = {
                "method": "GetData",
                "DataSetName": "NIPA",
                "TableName": "T20804",
                "Frequency": "M",
                "Year": ",".join(map(str, range(first, last + 1))),
                "ResultFormat": "JSON",
            }
            try:
                response, attempts = self._request(
                    urlencode({**params, "UserID": key}).encode("ascii")
                )
                total_attempts += attempts
                if any(
                    k.lower() == "content-encoding" and v.lower() != "identity"
                    for k, v in response.headers.items()
                ):
                    raise PceSourceError("pce_timeseries_content_encoding_rejected")
                results = _results(response.body, key)
                points = _parse_results(
                    results,
                    series_ids,
                    max(start_month, f"{first:04d}-01"),
                    min(end_month, f"{last:04d}-12"),
                )
                observed = self._clock()
                if not isinstance(observed, datetime) or observed.utcoffset() is None:
                    raise PceSourceError("pce_timeseries_observed_at_invalid")
                projection = json.dumps(
                    {
                        "retention_class": "credential_free_bea_results_projection",
                        "exact_original_retained": False,
                        "Results": results,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
                request = {
                    **params,
                    "provider": "BEA",
                    "seriesid": list(series_ids),
                    "retention_class": "credential_free_bea_results_projection",
                    "exact_original_retained": False,
                }
                batches.append(
                    SourceBatch(points, projection, request, observed, attempts)
                )
            except PceSourceError as error:
                raise PceSourceError(
                    error.classification,
                    total_attempts + (error.attempts if error.attempts else 0),
                    http_status=error.http_status,
                    retry_at=error.retry_at,
                ) from None
            except Exception:
                raise PceSourceError(
                    "pce_timeseries_response_invalid", total_attempts
                ) from None
        return tuple(batches)
