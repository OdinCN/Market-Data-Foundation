"""FRED daily acquisition over existing exact HTTPS and durable admission ports."""

import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal

from market_data_foundation.historical.claims_source import (
    ClaimsHttpsTransport,
    ClaimsRequest,
)
from market_data_foundation.historical.cpi_request_control import (
    CpiRequestControl,
    CpiRequestControlError,
)
from market_data_foundation.historical.financial_daily import (
    BY_ID,
    DailyBatch,
    DailyPoint,
    FinancialError,
    dates,
    day,
    instant,
    permitted_series,
    require_source_permission,
    series_ids,
)
from market_data_foundation.source_request_policy import (
    classify_source_exception,
    retry_delay_after,
    retryable_http_status,
)

ENDPOINT = "https://api.stlouisfed.org/fred/series/observations"


def document(raw, secret=None):
    try:
        value = json.loads(raw)
        if type(value) is not dict or "error_code" in value:
            raise ValueError
        if secret and secret in json.dumps(value, ensure_ascii=False):
            raise FinancialError("financial_credential_echo_rejected")
        return value
    except ValueError, TypeError, UnicodeError:
        raise FinancialError("financial_response_invalid") from None


def parse_metadata(raw, sid):
    series_ids((sid,))
    rows = document(raw).get("seriess")
    m = BY_ID[sid]
    if type(rows) is not list or len(rows) != 1 or type(rows[0]) is not dict:
        raise FinancialError("financial_metadata_invalid")
    row = rows[0]
    if (
        row.get("id") != sid
        or row.get("units") != m.unit
        or row.get("frequency_short") != "D"
        or row.get("frequency") != m.frequency
        or row.get("seasonal_adjustment_short") != m.seasonal_adjustment
    ):
        raise FinancialError("financial_metadata_mapping_invalid")
    if day(row.get("observation_start")) > day(row.get("observation_end")):
        raise FinancialError("financial_metadata_range_invalid")
    return dict(
        row,
        is_copyrighted=("copyright" in row["notes"].lower())
        if isinstance(row.get("notes"), str)
        else None,
    )


def parameters(sid, start, end):
    series_ids((sid,))
    dates(start, end)
    return dict(
        series_id=sid,
        file_type="json",
        units="lin",
        observation_start=start,
        observation_end=end,
        output_type="1",
        sort_order="asc",
        limit="100000",
    )


def parse_observations(raw, sid, start, end):
    parameters(sid, start, end)
    doc = document(raw)
    rows = doc.get("observations")
    if (
        type(rows) is not list
        or type(doc.get("count")) is not int
        or doc["count"] != len(rows)
        or doc.get("offset") != 0
        or type(doc.get("offset")) is not int
        or doc.get("units") != "lin"
        or type(doc.get("output_type")) is not int
        or doc.get("output_type") != 1
        or doc.get("observation_start") != start
        or doc.get("observation_end") != end
        or doc.get("sort_order") != "asc"
    ):
        raise FinancialError("financial_response_contract_invalid")
    points, previous = [], None
    for row in rows:
        if type(row) is not dict:
            raise FinancialError("financial_row_invalid")
        at, text = row.get("date"), row.get("value")
        day(at)
        if not start <= at <= end or (previous and at <= previous):
            raise FinancialError("financial_observation_date_invalid")
        previous = at
        if (
            type(text) is not str
            or (text != "." and not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", text))
            or len(text) > 80
        ):
            raise FinancialError("financial_value_invalid")
        if day(row.get("realtime_start")) > day(row.get("realtime_end")):
            raise FinancialError("financial_realtime_range_invalid")
        points.append(
            DailyPoint(
                sid,
                at,
                None if text == "." else Decimal(text),
                dict(
                    raw_value=text,
                    realtime_start=row["realtime_start"],
                    realtime_end=row["realtime_end"],
                    realtime_precision="date",
                    realtime_basis="FRED_date_not_publication_or_MDF_availability",
                    source_publication_at=None,
                    source_available_at=None,
                ),
            )
        )
    return tuple(points)


def validate_batch(batch):
    if isinstance(batch, DailyBatch) and batch.origin == "credit_archive":
        from market_data_foundation.historical.financial_daily_credit import (
            validate_archive,
        )

        return validate_archive(batch)
    if isinstance(batch, DailyBatch) and batch.origin in {
        "federal_reserve_board",
        "new_york_fed",
        "us_treasury",
        "mdf_derived",
    }:
        from market_data_foundation.historical.financial_daily_origin import validate

        return validate(batch)
    try:
        if not isinstance(batch, DailyBatch) or batch.origin not in {
            "fred",
            "controlled_fixture",
        }:
            raise ValueError
        instant(batch.observed_at)
        instant(batch.metadata_observed_at)
        if (
            batch.metadata_observed_at > batch.observed_at
            or type(batch.attempts) is not int
            or not 1 <= batch.attempts <= 3
        ):
            raise ValueError
        if (
            batch.request
            != dict(
                url=ENDPOINT,
                parameters=parameters(batch.series_id, batch.start, batch.end),
            )
            or parse_metadata(batch.metadata_raw, batch.series_id) != batch.metadata
            or parse_observations(batch.raw, batch.series_id, batch.start, batch.end)
            != batch.points
        ):
            raise ValueError
    except ValueError, TypeError, AttributeError, KeyError:
        raise FinancialError("financial_batch_invalid") from None


class DailyFredSource:
    def __init__(
        self,
        *,
        transport,
        request_control,
        clock=lambda: datetime.now(UTC),
        sleep=time.sleep,
        max_requests=20,
        origin="controlled_fixture",
        secret=None,
        raw_sink=None,
    ):
        if (
            origin not in {"fred", "controlled_fixture"}
            or type(max_requests) is not int
            or not 1 <= max_requests <= 40
        ):
            raise FinancialError("financial_source_contract_invalid")
        self._transport, self._control, self.clock, self.sleep = (
            transport,
            request_control,
            clock,
            sleep,
        )
        self.max_requests, self.request_count, self.origin, self._secret = (
            max_requests,
            0,
            origin,
            secret,
        )
        self._raw_sink = raw_sink

    @classmethod
    def live(
        cls,
        *,
        series,
        credential_loader,
        request_control_path,
        live_opt_in=False,
        **options,
    ):
        if live_opt_in is not True:
            raise FinancialError("financial_live_opt_in_required")
        if not permitted_series(series):
            require_source_permission(series[0])
        key = credential_loader()
        return cls(
            transport=ClaimsHttpsTransport(key),
            request_control=CpiRequestControl(
                request_control_path, min_interval_seconds=1.1
            ),
            origin="fred",
            secret=key,
            **options,
        )

    def _validate_wire(self, raw):
        document(raw, self._secret)

    def _read(self, request):
        attempts = 0
        while attempts < 3:
            if self.request_count >= self.max_requests:
                raise FinancialError(
                    "financial_request_budget_exceeded",
                    request_count=self.request_count,
                )
            try:
                for admission in range(2):
                    try:
                        with self._control.attempt() as admitted:
                            attempts += 1
                            self.request_count += 1
                            response = self._transport(request)
                            retry = admitted.record_response(
                                response.status, response.headers
                            )
                        break
                    except CpiRequestControlError as error:
                        delay = (
                            (error.retry_at - self.clock()).total_seconds()
                            if error.retry_at
                            else 0
                        )
                        if (
                            admission == 0
                            and error.classification.endswith("request_spacing")
                            and 0 < delay <= 5
                        ):
                            self.sleep(delay)
                            continue
                        raise FinancialError(
                            "financial_request_admission_blocked",
                            retry_at=error.retry_at,
                        ) from None
                if response.status == 200:
                    self._validate_wire(response.body)
                    observed = instant(self.clock())
                    if self._raw_sink:
                        try:
                            self._raw_sink(request, response, observed, attempts)
                        except Exception:
                            raise FinancialError(
                                "financial_raw_retention_failed"
                            ) from None
                    return response.body, observed, attempts
                if response.status == 429:
                    raise FinancialError(
                        "financial_http_failure", http_status=429, retry_at=retry
                    )
                if not retryable_http_status(response.status) or attempts == 3:
                    raise FinancialError(
                        "financial_http_failure", http_status=response.status
                    )
            except FinancialError:
                raise
            except Exception as error:
                classification, transient = classify_source_exception(error)
                if not transient or attempts == 0 or attempts >= 3:
                    raise FinancialError(
                        "financial_" + classification, attempts
                    ) from None
            self.sleep(retry_delay_after(attempts))
        raise AssertionError

    def fetch(self, sid, start, end):
        p = parameters(sid, start, end)
        if self.origin == "fred":
            require_source_permission(sid)
        metadata_raw, metadata_at, _ = self._read(
            ClaimsRequest("metadata", dict(series_id=sid, file_type="json"))
        )
        metadata = parse_metadata(metadata_raw, sid)
        raw, observed, attempts = self._read(ClaimsRequest("observations", p))
        return DailyBatch(
            sid,
            start,
            end,
            parse_observations(raw, sid, start, end),
            raw,
            metadata_raw,
            metadata,
            metadata_at,
            observed,
            dict(url=ENDPOINT, parameters=p),
            self.origin,
            attempts,
        )
