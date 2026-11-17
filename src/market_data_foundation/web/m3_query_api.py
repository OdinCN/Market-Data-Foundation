"""Explicitly bound M3 query endpoint; no job, strategy or asset activation."""

from contextlib import contextmanager
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from market_data_foundation.canonical.model import ContractError
from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError
from market_data_foundation.historical.m3_capital_goods import M3Request
from market_data_foundation.historical.m3_capital_goods_store import SERIES_IDS
from market_data_foundation.historical.m3_capital_goods_timeseries import (
    M3TimeSeriesService,
    _months,
)
from market_data_foundation.web.cpi_timeseries_api import (
    admitted_job,
    admitted_strategy,
)
from market_data_foundation.web.cpi_timeseries_dispatch import dispatch_job
from market_data_foundation.web.cpi_timeseries_jobs import WebError
from market_data_foundation.web.gdp_api import body, request_schema
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN, admitted

PREFIX = "/api/r5/m3/timeseries"


class M3QueryHost:
    """Host-owned read and source permissions; no network during construction."""

    def __init__(
        self,
        *,
        read_factory,
        query_factory=None,
        allow_read=False,
        allow_source=False,
        upstream_access=lambda: False,
    ):
        if type(allow_read) is not bool or type(allow_source) is not bool:
            raise ValueError("m3_explicit_permissions_required")
        if (
            not callable(read_factory)
            or not callable(upstream_access)
            or (query_factory is not None and not callable(query_factory))
        ):
            raise ValueError("m3_explicit_factories_required")
        self.read_factory, self.query_factory = read_factory, query_factory
        self.allow_read, self.allow_source = allow_read, allow_source
        self._source_admission = upstream_access

    @contextmanager
    def service(self, upstream=False):
        factory = self.query_factory if upstream else self.read_factory
        if factory is None:
            raise WebError("m3_service_not_bound", 503)
        service = factory()
        try:
            if not isinstance(service, M3TimeSeriesService):
                raise WebError("m3_service_binding_mismatch", 503)
            yield service
        finally:
            connection = getattr(service.store, "connection", None)
            if connection is not None:
                connection.close()

    def query(self, params):
        if not self.allow_read:
            raise WebError("local_read_not_authorized", 403)
        if set(params) - {
            "start",
            "end",
            "dataset",
            "series",
            "local_only",
            "query_mode",
            "as_of",
            "as_of_basis",
            "include_mom",
        }:
            raise WebError("unsupported_parameters")
        if not params.get("start") or not params.get("end"):
            raise WebError("m3_range_required")
        local = params.get("local_only", "true")
        if local not in {"true", "false"}:
            raise WebError("invalid_local_only")
        mode = params.get("query_mode", "history" if params.get("as_of") else "current")
        if mode not in {"current", "history"} or (mode == "history") != bool(
            params.get("as_of")
        ):
            raise WebError("m3_history_cutoff_required")
        basis = params.get("as_of_basis", "available")
        if basis not in {"observed", "available"}:
            raise WebError("m3_cutoff_basis_invalid")
        series = (
            tuple(params["series"].split(",")) if "series" in params else SERIES_IDS
        )
        try:
            M3Request(
                "m3"
                if params.get("dataset") == "both"
                else params.get("dataset", "m3"),
                "MDM",
                params["start"],
                params["start"],
            )
            _months(params["start"], params["end"])
            if (
                not series
                or len(set(series)) != len(series)
                or not set(series) <= set(SERIES_IDS)
            ):
                raise ValueError
            if mode == "history":
                stamp = datetime.fromisoformat(params["as_of"].replace("Z", "+00:00"))
                if stamp.utcoffset() is None:
                    raise ValueError
        except ValueError, TypeError, CpiTimeSeriesError:
            raise WebError("m3_request_invalid") from None
        mom = params.get("include_mom", "false")
        if mom not in {"true", "false"}:
            raise WebError("m3_invalid_mom_flag")
        upstream = local == "false" and mode == "current"
        if upstream and not (
            self.allow_source
            and self.query_factory is not None
            and self._source_admission() is True
        ):
            raise WebError("m3_source_not_bound", 403)
        with self.service(upstream) as service:
            from market_data_foundation.historical.m3_reports import query_reports

            joint = params.get("dataset") == "both"
            query = (
                (lambda *args, **kwargs: query_reports(service, *args, **kwargs))
                if joint
                else service.query_range
            )
            result = query(
                params["start"],
                params["end"],
                **({} if joint else {"dataset": params.get("dataset", "m3")}),
                as_of=params.get("as_of"),
                basis=basis,
                allow_upstream=upstream,
                series_ids=series,
                include_mom=mom == "true",
            )
        return result | {"local_only": not upstream}


def timeseries_router(host):
    router = APIRouter(prefix=PREFIX)

    def guard(request):
        if not admitted(request, origin=CANDIDATE_ORIGIN):
            raise WebError("operator_not_authorized", 403)
        if (
            request.headers.get("content-length", "0") != "0"
            or "transfer-encoding" in request.headers
        ):
            raise WebError("get_body_not_supported")
        if host is None:
            raise WebError("m3_timeseries_not_enabled", 503)

    def failure(error):
        if isinstance(error, WebError):
            return JSONResponse(
                {"status": "ERROR", "classification": error.classification},
                error.status,
            )
        if isinstance(error, CpiTimeSeriesError) and error.classification in {
            "release_as_of_invalid",
            "release_read_mode_unsupported",
            "release_source_version_invalid",
            "release_reference_month_invalid",
        }:
            return JSONResponse(
                {"status": "ERROR", "classification": error.classification}, 400
            )
        if isinstance(error, ContractError):
            return JSONResponse(
                {"status": "ERROR", "classification": "m3_request_invalid"}, 400
            )
        return JSONResponse(
            {"status": "ERROR", "classification": "m3_local_failure"}, 503
        )

    @router.get(
        "/query",
        openapi_extra={
            "parameters": [
                {
                    "name": name,
                    "in": "query",
                    "required": name in {"start", "end"},
                    "schema": {"type": "string"},
                    "description": description,
                }
                for name, description in (
                    ("start", "inclusive YYYY-MM"),
                    ("end", "inclusive YYYY-MM"),
                    (
                        "dataset",
                        "both compare; m3/advm3 single; legacy default m3",
                    ),
                    (
                        "series",
                        "comma-separated M3 series identities; defaults all three",
                    ),
                    (
                        "local_only",
                        "true by default; false permits current nonpersistent fill",
                    ),
                    ("query_mode", "current or history"),
                    ("as_of", "aware history cutoff"),
                    ("as_of_basis", "observed or available"),
                    (
                        "include_mom",
                        "false default; true adds same-stage MoM and previous month",
                    ),
                )
            ]
        },
    )
    def query(request: Request):
        try:
            guard(request)
            pairs = request.query_params.multi_items()
            if len(pairs) != len(dict(pairs)):
                raise WebError("duplicate_parameter")
            return JSONResponse(host.query(dict(pairs)))
        except Exception as error:
            return failure(error)

    def read_host_operation(operation):
        def endpoint(request: Request):
            try:
                guard(request)
                if request.query_params:
                    raise WebError("unsupported_parameters")
                method = getattr(host, operation, None)
                if method is None:
                    raise WebError("m3_operation_not_enabled", 503)
                if operation == "strategy":
                    from market_data_foundation.web.macro_runtime_facts import (
                        read_strategy,
                    )

                    return JSONResponse(read_strategy(host, request))
                return JSONResponse(method())
            except Exception as error:
                return failure(error)

        return endpoint

    for operation in ("status", "jobs", "executions", "strategy", "schedule"):
        router.add_api_route(
            "/" + operation,
            read_host_operation(operation),
            methods=["GET"],
            name="m3_" + operation,
        )

    @router.get("/releases")
    def releases(request: Request):
        try:
            guard(request)
            pairs = request.query_params.multi_items()
            if len(pairs) != len(dict(pairs)):
                raise WebError("duplicate_parameter")
            return JSONResponse(host.releases(dict(pairs)))
        except Exception as error:
            return failure(error)

    @router.put("/strategy")
    async def strategy(request: Request):
        try:
            if not admitted_strategy(request, prefix=PREFIX):
                raise WebError("operator_not_authorized", 403)
            if host is None:
                raise WebError("m3_timeseries_not_enabled", 503)
            command = await body(request)
            return JSONResponse(await run_in_threadpool(host.update_strategy, command))
        except Exception as error:
            return failure(error)

    async def submit_job(request: Request, *, resume=False):
        try:
            expected = PREFIX + ("/jobs/resume" if resume else "/jobs")
            scoped = Request(dict(request.scope, path=PREFIX + "/jobs"))
            if request.url.path != expected or not admitted_job(scoped, prefix=PREFIX):
                raise WebError("operator_not_authorized", 403)
            if host is None or not hasattr(host, "submit"):
                raise WebError("m3_operation_not_enabled", 503)
            command = await body(request)

            def execute(value, on_started=None):
                return host.submit(value, on_started=on_started, resume=resume)

            result = await run_in_threadpool(dispatch_job, execute, command)
            return JSONResponse(result, 202 if result["state"] == "running" else 200)
        except Exception as error:
            return failure(error)

    @router.post(
        "/jobs",
        openapi_extra=request_schema(
            {
                "request_id": {"type": "string", "format": "uuid"},
                "action": {"type": "string", "enum": ["init", "initialize", "update"]},
                "start": {"type": "string", "pattern": "^[0-9]{4}-(0[1-9]|1[0-2])$"},
                "end": {"type": "string", "pattern": "^[0-9]{4}-(0[1-9]|1[0-2])$"},
            },
            ["request_id", "action", "start", "end"],
        ),
    )
    async def submit(request: Request):
        return await submit_job(request)

    @router.post("/jobs/resume")
    async def resume(request: Request):
        return await submit_job(request, resume=True)

    @router.get("/api-help")
    def api_help(request: Request):
        try:
            guard(request)
            if request.query_params:
                raise WebError("unsupported_parameters")
            return {
                "asset": "美国耐用品与核心资本品",
                "frequency": "monthly",
                "unit": "million_current_usd",
                "seasonal_adjustment": "SA",
                "datasets": ["m3", "advm3"],
                "series": list(SERIES_IDS),
                "current_default": "local_only=true",
                "history": "local only; aware as_of; observed or available",
                "source_snapshot": "event attribution unconfirmed",
                "jobs": "explicit host-bound manual collection with persisted receipts"
                if hasattr(host, "submit")
                else "not yet connected",
                "parameters": {
                    "start/end": "inclusive YYYY-MM",
                    "series": "three named identities",
                    "dataset": "both compare; m3/advm3 single; default m3",
                    "local_only": "true default",
                    "query_mode": "current / history",
                    "as_of": "aware inclusive cutoff",
                    "as_of_basis": "observed / available",
                    "include_mom": "false default; same-stage prior month if true",
                },
                "fields": {
                    "points[].value": "decimal string or null, million current USD",
                    "points[].source": "dataset-specific capture evidence",
                    "points[].mom_percent/mom_reason": (
                        "decimal percent or null; current_missing, "
                        "missing_denominator or zero_denominator"
                    ),
                    "missing/source_failure": "explicit gaps and safe source failures",
                    "provider_requests/provider_attempts": (
                        "bounded logical and HTTP counts"
                    ),
                    "persisted": "false for every query",
                },
                "coverage": "Complete counts observed values; missing and zero remain distinct.",  # noqa: E501
                "history_semantics": (
                    "Local only; MDF confirmation is not an official event release."
                ),
                "release_records": {
                    "path": PREFIX + "/releases",
                    "local_only": True,
                    "empty_evidence": "explicit missing; no current-value substitution",
                },
                "periodic": {
                    "strategy": PREFIX + "/strategy",
                    "schedule": PREFIX + "/schedule",
                    "default_enabled": False,
                    "worker": "explicit CLI process only",
                },
                "comparison": {
                    "direction": "m3 minus advm3",
                    "basis": "source snapshots; not official event revision/surprise",
                    "missing": "difference null if either side missing",
                },
                "query_budget_seconds": 150,
                "web_timeout_seconds": 180,
                "source_request_timeout_seconds": 20,
                "mom": "percent; same dataset/cutoff; missing/zero denominator null; "
                "half-even 6 decimals; previous month included",
                "collection": (
                    "New manual jobs jointly collect both independent reports. "
                    "Initialization may adopt proved confirmed source fragments. "
                    "Background historical windows up to 240 months per indicator; "
                    "progress is durable. Scheduled work is not failure. "
                    "Recovery retains the same collection ID. "
                    "Source gaps and unrequested targets are distinct."
                ),
            }
        except Exception as error:
            return failure(error)

    return router
