"""Normal R5 daily query, collection, execution, strategy and API help routes."""

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from market_data_foundation.canonical.model import ContractError, require
from market_data_foundation.historical.tdx_p0_contract import HELP, ORIGIN, PREFIX
from market_data_foundation.historical.tdx_p0_jobs import (
    DailyJobs,
    coverage,
    latest_day,
    strategy_slot_id,
)
from market_data_foundation.historical.tdx_p0_source import GoDailySource, TdxError
from market_data_foundation.historical.tdx_p0_strategy import DailyStrategy
from market_data_foundation.historical.tdx_roster import PREFIX as ROSTER_PREFIX
from market_data_foundation.historical.tdx_roster import published
from market_data_foundation.storage.tdx_p0 import PgStore
from market_data_foundation.web.gdp_api import body
from market_data_foundation.web.pce_dev_host import admitted


@dataclass(frozen=True)
class TdxOptions:
    directory: Path
    binary: Path
    endpoint: str
    allow_collect: bool = False
    allow_read: bool = True
    origin: str = ORIGIN
    workers: int = 4


def admitted_tdx_write(request, *, origin=ORIGIN):
    return (
        request.method == "POST"
        and request.url.path
        in {
            PREFIX + "/jobs",
            PREFIX + "/jobs/resume",
            PREFIX + "/strategy",
            ROSTER_PREFIX + "/jobs",
            ROSTER_PREFIX + "/jobs/resume",
        }
        and not request.url.query
        and request.headers.getlist("origin") == [origin]
        and request.headers.getlist("content-type") == ["application/json"]
        and "transfer-encoding" not in request.headers
        and admitted(Request(dict(request.scope, method="GET")), origin=origin)
    )


class DailyRuntime:
    def __init__(self, options, store=None, source_factory=None):
        self.options = options
        self.store = store or PgStore()
        self.jobs = DailyJobs(
            options.directory / "jobs",
            self.store,
            source_factory
            or (
                lambda identity: GoDailySource(
                    options.binary,
                    options.endpoint,
                    options.directory / "raw" / identity,
                    live_opt_in=options.allow_collect,
                )
            ),
            workers=options.workers,
        )
        self.strategy = DailyStrategy(
            self.jobs,
            directory=options.directory
            / "strategies"
            / options.origin.rsplit(":", 1)[-1],
        )


def tdx_failure(error):
    classification = getattr(error, "classification", None) or (
        str(error)
        if isinstance(error, ContractError)
        else "tdx_missing_parameter"
        if isinstance(error, KeyError)
        else "tdx_invalid_parameter"
        if isinstance(error, (ValueError, TypeError))
        else type(error).__name__
    )
    return JSONResponse(
        dict(status="NOT_AVAILABLE", classification=classification),
        400
        if isinstance(error, (ContractError, ValueError, KeyError, TypeError))
        else 409
        if classification == "job_running"
        else 404
        if classification == "job_not_found"
        else 403
        if classification.endswith(("not_enabled", "not_authorized"))
        else 503,
    )


def present_tdx_job(job):
    command = job["command"]
    action = command["action"]
    day = command["end"]
    slot = None
    if action in {"universe", "update"}:
        if job["request_id"] == strategy_slot_id(day, action):
            slot = action
        elif action == "update" and job["request_id"] == strategy_slot_id(
            day, "review"
        ):
            slot = "review"
    origin = job.get("origin")
    if origin not in {"manual", "periodic"}:
        origin = "periodic" if slot else "unknown"
    return {**job, "origin": origin, "strategy_slot": slot}


def tdx_router(runtime=None):
    fail = tdx_failure
    router = APIRouter(prefix=PREFIX, tags=["tdx-stock-daily"])

    def ready(write=False):
        if (
            runtime is None
            or not runtime.options.allow_read
            or write
            and not runtime.options.allow_collect
        ):
            raise TdxError("tdx_capability_not_enabled")

    def parameters(request, allowed):
        require(
            all(
                k in allowed and len(request.query_params.getlist(k)) == 1
                for k in request.query_params
            ),
            "tdx_query_parameters",
        )

    @router.get("/help")
    def help():
        return HELP

    @router.get("/status")
    def status():
        try:
            roster = (
                published(runtime.store, latest_day())[1]
                if runtime and runtime.options.allow_read
                else None
            )
            return dict(
                dataset_id=HELP["dataset_id"],
                enabled=bool(runtime and runtime.options.allow_read),
                collect=bool(runtime and runtime.options.allow_collect),
                defaults=dict(start="1990-01-01", end=latest_day().isoformat()),
                price_basis="native_projection",
                return_qualification="NOT_QUALIFIED",
                provider_requests=0,
                roster=roster,
            )
        except Exception as error:
            return fail(error)

    @router.get("/summary")
    def summary():
        try:
            ready()
            return runtime.store.summary()
        except Exception as error:
            return fail(error)

    @router.get("/universe")
    def universe(request: Request):
        try:
            ready()
            parameters(request, {"as_of"})
            return runtime.store.universe(
                as_of=datetime.fromisoformat(request.query_params["as_of"])
                if "as_of" in request.query_params
                else None
            ) or dict(snapshot_id=None, members=[], provider_requests=0)
        except Exception as error:
            return fail(error)

    @router.get("/query")
    def query(request: Request):
        try:
            ready()
            parameters(
                request,
                {
                    "day",
                    "start",
                    "end",
                    "market",
                    "instrument",
                    "as_of",
                    "offset",
                    "limit",
                    "local_only",
                    "basis",
                    "period",
                    "security_type",
                    "order",
                },
            )
            q = request.query_params
            require(
                q.get("local_only", "true") == "true"
                and q.get("basis", "native_projection") == "native_projection"
                and q.get("period", "1d") == "1d"
                and q.get("security_type", "stock") == "stock",
                "tdx_query_contract",
            )
            require(
                ("day" in q and "start" not in q and "end" not in q)
                or "day" not in q
                and "start" in q
                and "end" in q,
                "tdx_query_direction",
            )
            return runtime.store.query(
                start=date.fromisoformat(q.get("day") or q["start"]),
                end=date.fromisoformat(q.get("day") or q["end"]),
                market=int(q["market"]) if "market" in q else None,
                instrument=q.get("instrument"),
                as_of=datetime.fromisoformat(q["as_of"]) if "as_of" in q else None,
                offset=int(q.get("offset", "0")),
                limit=int(q.get("limit", "100")),
                order=q.get("order", "asc"),
            )
        except Exception as error:
            return fail(error)

    @router.get("/coverage")
    def report(request: Request):
        try:
            ready()
            parameters(request, {"day", "as_of", "request_id", "offset", "limit"})
            q = request.query_params
            value = coverage(
                runtime.store,
                date.fromisoformat(q["day"]),
                as_of=datetime.fromisoformat(q["as_of"]) if "as_of" in q else None,
                job=runtime.jobs.read(q["request_id"]) if "request_id" in q else None,
            )
            offset = int(q.get("offset", "0"))
            limit = int(q.get("limit", "100"))
            require(offset >= 0 and 1 <= limit <= 10000, "tdx_coverage_page")
            value["missing_total"] = len(value["missing"])
            value["missing"] = value["missing"][offset : offset + limit]
            value.update(offset=offset, limit=limit)
            return value
        except Exception as error:
            return fail(error)

    @router.get("/jobs")
    def jobs():
        try:
            ready()
            return dict(jobs=[present_tdx_job(job) for job in runtime.jobs.summaries()])
        except Exception as error:
            return fail(error)

    @router.get("/jobs/{identity}")
    def detail(identity: str):
        try:
            ready()
            job = runtime.jobs.read(identity)
            return present_tdx_job(job)
        except Exception as error:
            return fail(error)

    @router.post("/jobs", status_code=202)
    async def submit(request: Request):
        try:
            ready(True)
            if not admitted_tdx_write(request, origin=runtime.options.origin):
                raise TdxError("operator_not_authorized")
            return runtime.jobs.submit(
                await body(request, max_size=160000), origin="manual"
            )
        except Exception as error:
            return fail(error)

    @router.post("/jobs/resume", status_code=202)
    async def resume(request: Request):
        try:
            ready(True)
            if not admitted_tdx_write(request, origin=runtime.options.origin):
                raise TdxError("operator_not_authorized")
            command = await body(request)
            require(set(command) == {"request_id"}, "tdx_resume_shape")
            return runtime.jobs.resume(command["request_id"])
        except Exception as error:
            return fail(error)

    @router.get("/strategy")
    def strategy():
        try:
            ready()
            return runtime.strategy.read()
        except Exception as error:
            return fail(error)

    @router.post("/strategy")
    async def set_strategy(request: Request):
        try:
            ready(True)
            if not admitted_tdx_write(request, origin=runtime.options.origin):
                raise TdxError("operator_not_authorized")
            command = await body(request)
            require(
                set(command) == {"enabled"} and type(command["enabled"]) is bool,
                "tdx_strategy_shape",
            )
            return runtime.strategy.set(command["enabled"])
        except Exception as error:
            return fail(error)

    return router
