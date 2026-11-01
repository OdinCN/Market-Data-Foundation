"""Composable retail read API. No shared UI, worker, credentials or implicit binding."""

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from market_data_foundation.historical.census_retail_timeseries_source import (
    RetailSourceError,
)
from market_data_foundation.historical.retail_timeseries import RetailServiceError
from market_data_foundation.historical.retail_timeseries_store import RetailStorageError

PREFIX = "/api/r5/retail/timeseries"


def json_value(value):
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


class RetailTimeSeriesHost:
    prefix = PREFIX

    def __init__(self, service=None, *, query_allowed=False, allow_source=False):
        self.service, self.query_allowed, self.allow_source = (
            service,
            query_allowed,
            allow_source,
        )

    def _service(self):
        if self.service is None or self.query_allowed is not True:
            raise RetailServiceError("retail_timeseries_not_enabled")
        return self.service

    def status(self):
        result = self._service().status()
        return {
            **result,
            "source_allowed": result["source_allowed"] and self.allow_source is True,
        }

    def query(self, params):
        allowed = {
            "start",
            "end",
            "local_only",
            "as_of",
            "query_mode",
            "as_of_basis",
            "vintage_mode",
        }
        if set(params) - allowed:
            raise RetailServiceError("unsupported_parameters")
        if not params.get("start") or not params.get("end"):
            raise RetailServiceError("retail_range_required")
        local = params.get("local_only", "false")
        if local not in {"true", "false"}:
            raise RetailServiceError("retail_local_only_invalid")
        service = self._service()
        options = {
            k: v for k, v in params.items() if k not in {"start", "end", "local_only"}
        }
        # Host and service must independently admit Census access. A read host
        # with source disabled still serves existing data and visible gaps.
        return service.query_range(
            params["start"],
            params["end"],
            local_only=local == "true" or self.allow_source is not True,
            **options,
        )

    def api_help(self):
        self._service()
        return {
            "prefix": "/api/r5/retail/timeseries",
            "provider": "Census",
            "dataset": "MARTS",
            "routes": ["status", "query", "api-help"],
            "parameters": {
                "start/end": "required inclusive YYYY-MM; max 2520 input "
                "months including 12 month buffer",
                "local_only": "true/false; default false; host and service "
                "must both permit Census; queries never "
                "write",
                "query_mode": "current/history; inferred history with "
                "as_of or available basis; history always "
                "local",
                "as_of": "aware ISO8601 UTC or offset datetime; one "
                "inclusive cutoff for every input and comparison "
                "base; history without cutoff uses current UTC",
                "as_of_basis/vintage_mode": "observed/available aliases; "
                "must agree; current defaults "
                "observed, history defaults "
                "available",
            },
            "fields": {
                "points": "months descending, including missing months",
                "source_cells": "five SM inputs plus official 44X72 MPCSM; "
                "exact Decimal string or null",
                "status/reason/source_value": "available, suppressed, "
                "unavailable, missing (official "
                "row absent), not_provided (no "
                "local evidence); original "
                "text/null retained",
                "headline_level": "44X72 SM; million_current_usd, SA, not annualized",
                "control_level": "MDF formula SM44X72-SM441-SM444-SM447-SM722; "
                "never official Census control",
                "headline_rates/control_rates": "1/3/6/12 month level ratios; "
                "Decimal precision 28 "
                "half-even, unannualized; "
                "0.0312=3.12%; zero "
                "denominator is null with "
                "reason",
                "official_headline_mpcsm": "official Census percent; separate "
                "from derived ratio, no "
                "substitution",
                "inputs/input_versions": "all current and comparison operands: "
                "local revision, batch, raw "
                "hash/pointer, observed_at and "
                "nullable available_at; upstream is "
                "unpersisted",
                "local_revision": "local change sequence; 1 does not establish "
                "official first release",
                "observed_at": "source response acquisition; not calendar or "
                "official release time",
                "available_at": "post-commit local confirmation; unknown "
                "commits remain null until collection "
                "reconcile; never backdated",
                "snapshot_identity": "query projection identity; not atomic "
                "official vintage",
                "provider_attempts": "wire attempts during this query only",
                "units": "million_current_usd / ratio / percent; Decimal JSON "
                "strings retain precision",
            },
            "collection": "CLI/injected service initialize/update; default 240 target "
            "months plus 12 buffer; resume with same collection_id and "
            "range; no GET initialization",
            "boundary": "shared runtime, UI, calendar and periodic strategy unbound; "
            "no official as-published history assertion",
            "examples": [
                "/api/r5/retail/timeseries/query?start=2026-08&end=2026-09&local_only=true",
                (
                    "/api/r5/retail/timeseries/query?start=2026-08&end=2026-09"
                    "&query_mode=history&as_of_basis=available"
                    "&as_of=2026-11-01T08%3A00%3A00%2B08%3A00"
                ),
                "/api/r5/retail/timeseries/status",
            ],
        }


def timeseries_router(host=None, *, authorize=None):
    router = APIRouter(prefix=PREFIX)

    def endpoint(operation):
        def get(request: Request):
            try:
                if host is None:
                    return JSONResponse(
                        {
                            "status": "ERROR",
                            "classification": "retail_timeseries_not_enabled",
                        },
                        503,
                    )
                if authorize is None or authorize(request) is not True:
                    return JSONResponse(
                        {
                            "status": "ERROR",
                            "classification": "operator_not_authorized",
                        },
                        403,
                    )
                if (
                    request.headers.get("content-length", "0") != "0"
                    or "transfer-encoding" in request.headers
                ):
                    raise RetailServiceError("get_body_not_supported")
                pairs = request.query_params.multi_items()
                if len(dict(pairs)) != len(pairs):
                    raise RetailServiceError("duplicate_parameter")
                if operation != "query" and pairs:
                    raise RetailServiceError("unsupported_parameters")
                result = (
                    host.query(dict(pairs))
                    if operation == "query"
                    else getattr(host, operation.replace("-", "_"))()
                )
                return JSONResponse(json_value(result))
            except Exception as error:
                if isinstance(
                    error, (RetailServiceError, RetailSourceError, RetailStorageError)
                ):
                    classification = error.classification
                    status = (
                        503
                        if isinstance(error, RetailStorageError)
                        or classification.endswith(("not_enabled", "not_bound"))
                        else 502
                        if isinstance(error, RetailSourceError) and error.attempts
                        else 400
                    )
                else:
                    classification, status = "retail_local_failure", 503
                return JSONResponse(
                    {"status": "ERROR", "classification": classification}, status
                )

        return get

    for operation in ("status", "query", "api-help"):
        router.add_api_route(
            "/" + operation,
            endpoint(operation),
            methods=["GET"],
            name="retail_timeseries_" + operation,
        )
    return router


def create_app(host=None, *, authorize=None):
    app = FastAPI()
    app.include_router(timeseries_router(host, authorize=authorize))
    return app
