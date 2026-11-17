"""Explicit quarterly GDP routes with the existing operator/write boundary."""

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from market_data_foundation.historical.cpi_timeseries_cli import (
    reject_json_constant,
    unique_json_object,
)
from market_data_foundation.historical.gdp_contract import GdpSourceError
from market_data_foundation.historical.gdp_store import GdpStorageError
from market_data_foundation.historical.gdp_timeseries import GdpTimeSeriesError
from market_data_foundation.web.cpi_timeseries_api import (
    admitted_job,
    admitted_strategy,
)
from market_data_foundation.web.cpi_timeseries_dispatch import dispatch_job
from market_data_foundation.web.cpi_timeseries_jobs import WebError
from market_data_foundation.web.gdp_host import PREFIX
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN, admitted


def failure(error):
    code = getattr(error, "classification", "gdp_local_failure")
    status = (
        error.status
        if isinstance(error, WebError)
        else (
            503
            if isinstance(error, GdpStorageError)
            else 400
            if isinstance(error, (GdpSourceError, GdpTimeSeriesError))
            else 503
        )
    )
    return JSONResponse({"status": "ERROR", "classification": code}, status)


async def body(request, *, max_size=4096):
    raw = bytearray()
    async for part in request.stream():
        raw.extend(part)
        if len(raw) > max_size:
            raise WebError("request_body_too_large", 413)
    try:
        return json.loads(
            raw,
            object_pairs_hook=unique_json_object,
            parse_constant=reject_json_constant,
        )
    except Exception:
        raise WebError("invalid_json_body") from None


def request_schema(properties, required):
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": properties,
                        "required": required,
                    }
                }
            },
        }
    }


QUARTER_SCHEMA = {"type": "string", "pattern": "^[0-9]{4}-Q[1-4]$"}


def timeseries_router(
    host,
    *,
    prefix=PREFIX,
    family="gdp",
    period_schema=QUARTER_SCHEMA,
    period_description="YYYY-Qn inclusive",
    series_description="native SeriesCode CSV; all eight by default",
    basis_description="observed/available; history defaults available",
    release_fields=(
        "reference_quarter",
        "event_id",
        "source_version",
        "read_mode",
        "as_of",
    ),
    error_handler=failure,
):
    router = APIRouter(prefix=prefix)

    def endpoint(operation):
        def get(request: Request):
            try:
                if not admitted(request, origin=CANDIDATE_ORIGIN):
                    raise WebError("operator_not_authorized", 403)
                if (
                    request.headers.get("content-length", "0") != "0"
                    or "transfer-encoding" in request.headers
                ):
                    raise WebError("get_body_not_supported")
                if host is None:
                    raise WebError(family + "_timeseries_not_enabled", 503)
                pairs = request.query_params.multi_items()
                if len(pairs) != len(dict(pairs)):
                    raise WebError("duplicate_parameter")
                if operation not in {"query", "releases"} and pairs:
                    raise WebError("unsupported_parameters")
                if operation == "strategy":
                    from market_data_foundation.web.macro_runtime_facts import (
                        read_strategy,
                    )

                    return JSONResponse(read_strategy(host, request))
                if operation == "jobs":
                    host._read_allowed()
                    result = {"jobs": host.journal.list()}
                else:
                    method = getattr(host, operation.replace("-", "_"))
                    result = (
                        method(dict(pairs))
                        if operation in {"query", "releases"}
                        else method()
                    )
                return JSONResponse(result)
            except Exception as error:
                return error_handler(error)

        return get

    query_parameters = {
        "start": (period_description, True),
        "end": (period_description, True),
        "series": (series_description, False),
        "local_only": (
            "true default; false allows nonpersisted current gap fetch",
            False,
        ),
        "query_mode": ("current/history; history inferred with as_of", False),
        "as_of": ("aware ISO8601 cutoff required in history", False),
        "as_of_basis": (basis_description, False),
    }
    release_parameters = {k: ("optional " + k, False) for k in release_fields}
    for operation in (
        "status",
        "query",
        "api-help",
        "releases",
        "jobs",
        "executions",
        "strategy",
        "schedule",
    ):
        parameters = (
            query_parameters
            if operation == "query"
            else release_parameters
            if operation == "releases"
            else {}
        )
        router.add_api_route(
            "/" + operation,
            endpoint(operation),
            methods=["GET"],
            name=family + "_timeseries_" + operation,
            openapi_extra={
                "parameters": [
                    {
                        "name": k,
                        "in": "query",
                        "required": required,
                        "schema": {"type": "string"},
                        "description": description,
                    }
                    for k, (description, required) in parameters.items()
                ]
            },
        )

    @router.post(
        "/jobs",
        openapi_extra=request_schema(
            {
                "request_id": {"type": "string", "format": "uuid"},
                "action": {"type": "string", "enum": ["init", "update"]},
                "start": period_schema,
                "end": period_schema,
            },
            ["request_id", "action", "start", "end"],
        ),
    )
    async def submit(request: Request):
        try:
            if not admitted_job(request, prefix=prefix):
                raise WebError("operator_not_authorized", 403)
            if host is None:
                raise WebError(family + "_timeseries_not_enabled", 503)
            command = await body(request)
            result = await run_in_threadpool(dispatch_job, host.submit, command)
            return JSONResponse(result, 202 if result["state"] == "running" else 200)
        except Exception as error:
            return error_handler(error)

    @router.put(
        "/strategy",
        openapi_extra=request_schema(
            {"enabled": {"type": "boolean"}, "history_start": period_schema},
            ["enabled"],
        ),
    )
    async def strategy(request: Request):
        try:
            if not admitted_strategy(request, prefix=prefix):
                raise WebError("operator_not_authorized", 403)
            if host is None:
                raise WebError(family + "_timeseries_not_enabled", 503)
            result = await run_in_threadpool(host.update_strategy, await body(request))
            return JSONResponse(result)
        except Exception as error:
            return error_handler(error)

    return router
