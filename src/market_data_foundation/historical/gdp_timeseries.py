"""Quarterly GDP collection/readback and local-first, same-cutoff queries."""

from __future__ import annotations

import tomllib
from datetime import datetime
from pathlib import Path

from market_data_foundation.historical.bls_cpi_material_store import (
    BUCKET,
    GARAGE_ENDPOINT,
    GarageCredentials,
)
from market_data_foundation.historical.cpi_timeseries import (
    DEFAULT_SECRET_PATH,
    _ratio,
)
from market_data_foundation.historical.gdp_contract import (
    METRICS,
    SERIES_IDS,
    GdpSourceError,
    shift_quarter,
    validate_range,
    validate_series,
)
from market_data_foundation.historical.gdp_source import GdpRangeSource
from market_data_foundation.historical.gdp_store import (
    GarageGdpRawStore,
    GdpStorageError,
    PostgresGdpStore,
    instant,
)
from market_data_foundation.storage.market_dataset_postgres import connect_dev
from market_data_foundation.storage.postgres_runtime_config import DEFAULT_RUNTIME_PATH

_METADATA = {m.series_id: m for m in METRICS}


class GdpTimeSeriesError(RuntimeError):
    def __init__(
        self, classification, *, requests=0, attempts=0, http_status=None, retry_at=None
    ):
        super().__init__(classification)
        self.classification = classification
        self.provider_requests, self.provider_attempts = requests, attempts
        self.http_status, self.retry_at = http_status, retry_at


def quarters(start, end):
    validate_range(start, end)
    count = (int(end[:4]) - int(start[:4])) * 4 + int(end[-1]) - int(start[-1]) + 1
    if count > 800:
        raise GdpTimeSeriesError("gdp_timeseries_range_too_large")
    return tuple(shift_quarter(start, n) for n in range(count))


def _cutoff(value):
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return instant(value)
    except TypeError, ValueError, GdpStorageError:
        raise GdpTimeSeriesError("gdp_timeseries_cutoff_invalid") from None


def _request_args(start, end, series):
    series = validate_series(
        tuple(series) if isinstance(series, (tuple, list)) else series
    )
    quarters(start, end)
    buffer = (
        shift_quarter(start, -4)
        if any(_METADATA[s].statistic == "SAAR_level" for s in series)
        else start
    )
    return series, buffer


def _source_error(error):
    return GdpTimeSeriesError(
        error.classification,
        requests=error.request_count,
        attempts=error.attempts,
        http_status=error.http_status,
        retry_at=error.retry_at,
    )


class GdpTimeSeriesService:
    def __init__(self, store, source):
        self.store, self.source = store, source

    def query_range(
        self,
        start,
        end,
        *,
        series_ids=SERIES_IDS,
        allow_upstream=False,
        as_of=None,
        query_mode=None,
        as_of_basis=None,
    ):
        if type(allow_upstream) is not bool:
            raise GdpTimeSeriesError("gdp_timeseries_upstream_flag_invalid")
        cutoff = _cutoff(as_of)
        mode = query_mode or ("history" if cutoff is not None else "current")
        if mode not in ("current", "history"):
            raise GdpTimeSeriesError("gdp_timeseries_query_mode_invalid")
        if (mode == "history") != (cutoff is not None):
            raise GdpTimeSeriesError("gdp_timeseries_history_cutoff_required")
        basis = as_of_basis or ("available" if mode == "history" else "observed")
        if basis == "official":
            raise GdpTimeSeriesError("gdp_timeseries_official_vintage_unsupported")
        if basis not in ("available", "observed"):
            raise GdpTimeSeriesError("gdp_timeseries_cutoff_basis_invalid")
        series, buffer = _request_args(start, end, series_ids)
        with self.store.read_snapshot():
            local = self.store.read_range(series, buffer, end, cutoff, basis)
        indexed = {
            (d["series_id"], d["quarter"]): dict(d, origin="local") for d in local
        }
        needed = {(s, q) for s in series for q in quarters(buffer, end)}
        missing = needed - indexed.keys()
        requests = attempts = 0
        failure = None
        if allow_upstream and mode == "current" and missing:
            now = instant(self.store.clock())
            current_q = f"{now.year:04d}-Q{(now.month - 1) // 3 + 1}"
            missing = {(s, q) for s, q in missing if q < current_q}
            if missing:
                selected = tuple(s for s in series if any(k[0] == s for k in missing))
                try:
                    batches = self.source.fetch(
                        selected, min(q for _, q in missing), max(q for _, q in missing)
                    )
                    requests = sum(b.request["request_count"] for b in batches)
                    attempts = sum(b.attempts for b in batches)
                    for b in batches:
                        for p in b.points:
                            if (p.series_id, p.quarter) in missing:
                                indexed[p.series_id, p.quarter] = {
                                    "series_id": p.series_id,
                                    "quarter": p.quarter,
                                    "value": None if p.value is None else str(p.value),
                                    "revision": None,
                                    "batch_id": None,
                                    "available_at": None,
                                    "observed_at": b.observed_at.isoformat(),
                                    "provider_extra": p.provider_extra,
                                    "origin": "upstream",
                                }
                except GdpSourceError as error:
                    failure = error.classification
                    requests, attempts = error.request_count, error.attempts
        result = self._result(start, end, series, indexed)
        result.update(
            query_mode=mode,
            as_of=None if cutoff is None else cutoff.isoformat(),
            as_of_basis=basis,
            local_only=not (allow_upstream and mode == "current"),
            provider_requests=requests,
            provider_attempts=attempts,
            source_failure=failure,
        )
        if failure:
            result["status"] = (
                "partial" if result["coverage"]["available"] else "unavailable"
            )
        return result

    def init_range(self, start, end, *, series_ids=SERIES_IDS):
        return self._collect(start, end, series_ids, refresh=False)

    def update_range(self, start, end, *, series_ids=SERIES_IDS):
        return self._collect(start, end, series_ids, refresh=True)

    def _collect(self, start, end, series_ids, *, refresh):
        series, buffer = _request_args(start, end, series_ids)
        requests = attempts = 0
        returned = set()
        with self.store.operation():
            before = self.store.read_range(series, buffer, end)
            have = {(d["series_id"], d["quarter"]) for d in before}
            needed = {(s, q) for s in series for q in quarters(buffer, end)}
            missing = needed if refresh else needed - have
            saved = {"saved_points": 0, "saved_revisions": 0}
            if missing:
                selected = tuple(s for s in series if any(k[0] == s for k in missing))
                try:
                    batches = self.source.fetch(
                        selected, min(q for _, q in missing), max(q for _, q in missing)
                    )
                except GdpSourceError as error:
                    raise _source_error(error) from None
                requests = sum(b.request["request_count"] for b in batches)
                attempts = sum(b.attempts for b in batches)
                returned = {(p.series_id, p.quarter) for b in batches for p in b.points}
                try:
                    saved = self.store.save_batches(batches, missing_only=not refresh)
                except GdpStorageError as error:
                    raise GdpTimeSeriesError(
                        error.classification, requests=requests, attempts=attempts
                    ) from None
                after = self.store.read_range(series, buffer, end, basis="available")
                checked = {(d["series_id"], d["quarter"]): d["value"] for d in after}
                for b in batches:
                    for p in b.points:
                        if not refresh and (p.series_id, p.quarter) in have:
                            continue
                        actual = checked.get((p.series_id, p.quarter), "absent")
                        expected = None if p.value is None else str(p.value)
                        # Numeric text may differ only in insignificant trailing zeros.
                        from decimal import Decimal

                        if actual == "absent" or (
                            (actual is None) != (expected is None)
                            or (
                                actual is not None
                                and Decimal(actual) != Decimal(expected)
                            )
                        ):
                            raise GdpTimeSeriesError(
                                "gdp_timeseries_readback_mismatch",
                                requests=requests,
                                attempts=attempts,
                            )
            rows = self.store.read_range(series, buffer, end, basis="available")
        result = self._result(
            start,
            end,
            series,
            {(d["series_id"], d["quarter"]): dict(d, origin="local") for d in rows},
        )
        result.update(
            **saved,
            provider_requests=requests,
            provider_attempts=attempts,
            source_returned_series_quarters=[
                {"series_id": s, "quarter": q} for s, q in sorted(returned)
            ],
            source_missing=[
                {"series_id": s, "quarter": q}
                for s, q in sorted(missing - returned)
                if start <= q <= end
            ],
            collection_mode="update" if refresh else "initialize",
            buffer_start_quarter=buffer,
            query_mode="current",
            as_of=None,
            as_of_basis="available",
        )
        return result

    def _result(self, start, end, series, indexed):
        points = []
        available = nulls = 0
        for q in reversed(quarters(start, end)):
            for sid in series:
                doc = indexed.get((sid, q))
                metric = _METADATA[sid]
                value = None if doc is None else doc["value"]
                confirmed = doc is not None and (
                    doc["origin"] == "upstream" or doc["available_at"] is not None
                )
                reason = (
                    "local_record_missing"
                    if doc is None
                    else "local_availability_unconfirmed"
                    if not confirmed
                    else "official_null"
                    if value is None
                    else None
                )
                available += int(value is not None and confirmed)
                nulls += int(doc is not None and value is None)
                row = {
                    **(doc or {}),
                    "series_id": sid,
                    "quarter": q,
                    "value": value,
                    "indicator": metric.indicator,
                    "value_unit": metric.value_unit,
                    "statistic": metric.statistic,
                    "seasonal_adjustment": metric.seasonal_adjustment,
                    "chained_base": metric.chained_base,
                    "availability": "available" if reason is None else "missing",
                    "missing_reason": reason,
                    "estimate_kind": "unknown",
                    "event_binding_status": "unconfirmed",
                    "event_binding_reason": "current_nipa_table_has_no_release_vintage",
                    "change_1q_ratio": None,
                    "change_4q_ratio": None,
                    "calculation_missing": {},
                }
                if metric.statistic == "SAAR_level":
                    for k in (1, 4):
                        previous = indexed.get((sid, shift_quarter(q, -k)))
                        valid_previous = (
                            previous
                            and previous["value"] is not None
                            and (
                                previous["origin"] == "upstream"
                                or previous["available_at"] is not None
                            )
                        )
                        if value is not None and confirmed and valid_previous:
                            row[f"change_{k}q_ratio"] = _ratio(value, previous["value"])
                        else:
                            row["calculation_missing"][f"change_{k}q_ratio"] = (
                                "same_cutoff_endpoint_unavailable"
                            )
                points.append(row)
        return {
            "start_quarter": start,
            "end_quarter": end,
            "series_ids": list(series),
            "points": points,
            "status": "complete" if available == len(points) else "partial",
            "coverage": {
                "expected": len(points),
                "available": available,
                "official_null": nulls,
                "missing": len(points) - available - nulls,
            },
        }

    def status(self):
        return self.store.status()

    def close(self):
        self.store.close()


def build_service(
    *,
    runtime_path=DEFAULT_RUNTIME_PATH,
    secret_path=DEFAULT_SECRET_PATH,
    request_control_path=None,
    max_transport_attempts=1,
    credential_loader=None,
):
    """Fixed DEV binding. No DDL or provider request during construction."""
    connection = None
    try:
        runtime = tomllib.loads(Path(runtime_path).read_text())
        secret = tomllib.loads(Path(secret_path).read_text())
        s3 = runtime["s3"]
        if not (
            runtime["storage"]["backend"] == "s3"
            and s3["endpoint_url"] == GARAGE_ENDPOINT
            and s3["bucket"] == BUCKET
            and s3["region"] == "garage"
            and s3["key_prefix"] == ""
            and s3["addressing_style"] == "path"
        ):
            raise GdpTimeSeriesError("gdp_timeseries_storage_binding_invalid")
        credentials = GarageCredentials(
            secret["s3"]["access_key_id"], secret["s3"]["secret_access_key"]
        )
        connection = connect_dev(
            secret["postgres"]["password"], runtime_path=runtime_path
        )

        def loader():
            return tomllib.loads(Path(secret_path).read_text())["bea"]["api_key"]

        return GdpTimeSeriesService(
            PostgresGdpStore(connection, GarageGdpRawStore(credentials)),
            GdpRangeSource(
                credential_loader=credential_loader or loader,
                max_transport_attempts=max_transport_attempts,
                request_control_path=request_control_path
                or (
                    "/srv/market-data-foundation/config/local/"
                    "bea-timeseries-request-control.json"
                ),
            ),
        )
    except GdpTimeSeriesError:
        if connection is not None:
            connection.close()
        raise
    except Exception:
        if connection is not None:
            connection.close()
        raise GdpTimeSeriesError("gdp_timeseries_configuration_failed") from None
