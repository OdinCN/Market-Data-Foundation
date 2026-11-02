"""Quarter-native GDP reads over the shared, bounded BEA transport.

Only current NIPA table values are available here. A table timestamp or calendar
match is not evidence that a value belongs to an advance/second/third estimate.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlencode

from market_data_foundation.historical.gdp_contract import (
    METRICS,
    TABLES,
    GdpSourceError,
    requested_tables,
    source_quarter,
    validate_range,
    validate_series,
    year_windows,
)
from market_data_foundation.historical.pce_timeseries_source import (
    BeaRangeSource,
    PceSourceError,
    _results,
)

_PROJECTION_CLASS = "credential_free_bea_results_projection"


@dataclass(frozen=True)
class GdpSourcePoint:
    series_id: str
    quarter: str
    value: Decimal | None
    provider_extra: dict
    estimate_kind: str = "unknown"
    event_binding_status: str = "unconfirmed"
    event_binding_reason: str = "current_nipa_table_has_no_release_vintage"


@dataclass(frozen=True)
class GdpSourceBatch:
    points: tuple[GdpSourcePoint, ...]
    raw: bytes = field(repr=False)
    request: dict
    observed_at: datetime
    attempts: int


def _gdp_error(error, *, attempts=None, request_count=None):
    return GdpSourceError(
        error.classification.replace("pce_timeseries_", "gdp_timeseries_", 1),
        error.attempts if attempts is None else attempts,
        http_status=error.http_status,
        retry_at=error.retry_at,
        request_count=error.request_count if request_count is None else request_count,
        transient=error.transient,
    )


def _number(raw, *, positive):
    if isinstance(raw, str):
        raw = raw.strip()
    if raw in (None, "", "(NA)", "NA", "---", "--", "..."):
        return None
    if type(raw) is not str or not re.fullmatch(
        r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?", raw
    ):
        raise GdpSourceError("gdp_timeseries_value_invalid")
    value = Decimal(raw.replace(",", ""))
    if not value.is_finite() or (positive and value <= 0):
        raise GdpSourceError("gdp_timeseries_value_invalid")
    return value


def _parse_results(results, series_ids, start, end, *, table):
    validate_series(series_ids)
    validate_range(start, end)
    if table not in TABLES:
        raise GdpSourceError("gdp_timeseries_table_invalid")
    if type(results) is not dict or type(results.get("Data")) is not list:
        raise GdpSourceError("gdp_timeseries_response_invalid")
    notes = results.get("Notes", [])
    if type(notes) is not list or any(type(n) is not dict for n in notes):
        raise GdpSourceError("gdp_timeseries_notes_invalid")
    metrics = {m.series_id: m for m in METRICS if m.table == table}
    lines = {m.line: m.series_id for m in metrics.values()}
    points, seen = [], set()
    for row in results["Data"]:
        if type(row) is not dict or row.get("TableName") != table:
            raise GdpSourceError("gdp_timeseries_table_invalid")
        line, sid = row.get("LineNumber"), row.get("SeriesCode")
        if (
            type(line) is not str
            or not re.fullmatch(r"[1-9][0-9]*", line)
            or type(sid) is not str
            or not sid
        ):
            raise GdpSourceError("gdp_timeseries_series_invalid")
        # Some tables legitimately repeat a series belonging to another table.
        # Validate identities within this canonical table only.
        if (line in lines and lines[line] != sid) or (
            sid in metrics and metrics[sid].line != line
        ):
            raise GdpSourceError("gdp_timeseries_series_mapping_invalid")
        quarter = source_quarter(row.get("TimePeriod"))
        if not start[:4] <= quarter[:4] <= end[:4]:
            raise GdpSourceError("gdp_timeseries_source_quarter_out_of_range")
        if (line, quarter) in seen:
            raise GdpSourceError("gdp_timeseries_duplicate_point")
        seen.add((line, quarter))
        if line not in lines:
            continue
        metric = metrics[sid]
        if (
            row.get("METRIC_NAME") != metric.metric_name
            or row.get("CL_UNIT") != metric.cl_unit
            or row.get("UNIT_MULT") != metric.unit_mult
        ):
            raise GdpSourceError("gdp_timeseries_unit_invalid")
        if metric.chained_base:
            descriptions = [row.get("LineDescription", "")]
            descriptions.extend(n.get("NoteText", "") for n in notes)
            bases = {
                year
                for text in descriptions
                if isinstance(text, str)
                for year in re.findall(r"chained\s*\((\d{4})\)", text, re.I)
            }
            if bases != {metric.chained_base}:
                raise GdpSourceError("gdp_timeseries_chained_base_invalid")
        if "DataValue" not in row:
            raise GdpSourceError("gdp_timeseries_value_missing")
        value = _number(row["DataValue"], positive=metric.statistic == "SAAR_level")
        if sid in series_ids and start <= quarter <= end:
            points.append(
                GdpSourcePoint(
                    sid,
                    quarter,
                    value,
                    {
                        "bea": dict(row),
                        "retained_notes": notes,
                        "source_missing_reason": "official_null"
                        if value is None
                        else None,
                    },
                )
            )
    return tuple(sorted(points, key=lambda p: (p.series_id, p.quarter)))


def parse_response(raw, series_ids, start, end, *, table, forbidden_secret=None):
    """Parse a wire response; ignore Request and reject credential echoes."""
    try:
        return _parse_results(
            _results(raw, forbidden_secret), series_ids, start, end, table=table
        )
    except PceSourceError as error:
        raise _gdp_error(error) from None


class GdpRangeSource:
    """One retained batch per table/window, with its own capture timestamp.

    Reuses only the credential-safe BEA exchange, pacing and retry machinery.
    The monthly fetch/planner/profile are never called.
    """

    max_range_requests = 48

    def __init__(
        self,
        *,
        credential_loader=None,
        transport=None,
        clock=lambda: datetime.now(UTC),
        sleep=time.sleep,
        max_requests=48,
        max_transport_attempts=3,
        request_control=None,
        request_control_path=None,
    ):
        if type(max_requests) is not int or not 1 <= max_requests <= 48:
            raise GdpSourceError("gdp_timeseries_request_budget_invalid")
        self._loader, self._clock = credential_loader, clock
        self._max_requests = max_requests
        try:
            self._edge = BeaRangeSource(
                transport=transport,
                clock=clock,
                sleep=sleep,
                max_transport_attempts=max_transport_attempts,
                request_control=request_control,
                request_control_path=request_control_path,
            )
        except PceSourceError as error:
            raise _gdp_error(error) from None

    def plan_windows(self, series_ids, start, end):
        return year_windows(
            start,
            end,
            table_count=len(requested_tables(series_ids)),
            max_requests=self._max_requests,
        )

    def fetch(self, series_ids, start, end):
        tables = requested_tables(series_ids)
        windows = self.plan_windows(series_ids, start, end)
        try:
            key = self._loader() if self._loader else None
            if (
                type(key) is not str
                or not key
                or len(key) > 256
                or any(ord(c) < 33 or ord(c) > 126 for c in key)
            ):
                raise ValueError
        except Exception:
            raise GdpSourceError("gdp_timeseries_bea_credential_invalid") from None
        batches, total_requests, total_attempts = [], 0, 0
        for first, last in windows:
            for table in tables:
                params = {
                    "method": "GetData",
                    "DataSetName": "NIPA",
                    "TableName": table,
                    "Frequency": "Q",
                    "Year": ",".join(map(str, range(first, last + 1))),
                    "ResultFormat": "JSON",
                }
                total_requests += 1
                try:
                    try:
                        response, attempts = self._edge._request(
                            urlencode({**params, "UserID": key}).encode("ascii")
                        )
                    except PceSourceError as error:
                        total_attempts += error.attempts
                        raise
                    total_attempts += attempts
                    observed = self._clock()
                    if (
                        not isinstance(observed, datetime)
                        or observed.utcoffset() is None
                    ):
                        raise GdpSourceError("gdp_timeseries_observed_at_invalid")
                    if response.status == 204 and response.body:
                        raise GdpSourceError("gdp_timeseries_nonempty_no_content")
                    if any(
                        k.lower() == "content-encoding" and v.lower() != "identity"
                        for k, v in response.headers.items()
                    ):
                        raise GdpSourceError("gdp_timeseries_content_encoding_rejected")
                    results = (
                        {"Data": [], "no_content": True}
                        if response.status == 204
                        else _results(response.body, key)
                    )
                    points = _parse_results(
                        results,
                        series_ids,
                        max(start, f"{first:04d}-Q1"),
                        min(end, f"{last:04d}-Q4"),
                        table=table,
                    )
                    raw = json.dumps(
                        {
                            "retention_class": _PROJECTION_CLASS,
                            "exact_original_retained": False,
                            "Results": results,
                        },
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                    batches.append(
                        GdpSourceBatch(
                            points,
                            raw,
                            {
                                **params,
                                "provider": "BEA",
                                "start_quarter": max(start, f"{first:04d}-Q1"),
                                "end_quarter": min(end, f"{last:04d}-Q4"),
                                "seriesid": [
                                    s
                                    for s in series_ids
                                    if any(
                                        m.series_id == s and m.table == table
                                        for m in METRICS
                                    )
                                ],
                                "request_count": 1,
                                "retention_class": _PROJECTION_CLASS,
                                "exact_original_retained": False,
                            },
                            observed,
                            attempts,
                        )
                    )
                except PceSourceError as error:
                    raise _gdp_error(
                        error, attempts=total_attempts, request_count=total_requests
                    ) from None
        return tuple(batches)
