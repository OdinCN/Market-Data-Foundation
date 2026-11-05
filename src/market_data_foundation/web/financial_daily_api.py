"""Explicit DEV daily read/manual-job interface, with the normal private-write gate."""

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from market_data_foundation.historical.financial_daily import (
    DATASET_ID,
    SERIES_IDS,
    FinancialError,
    day,
    series_ids,
)
from market_data_foundation.historical.financial_daily_runtime import (
    bound_fred_credential,
    build_service,
)
from market_data_foundation.web.cpi_timeseries_api import admitted_job
from market_data_foundation.web.cpi_timeseries_dispatch import dispatch_job
from market_data_foundation.web.cpi_timeseries_jobs import WebError
from market_data_foundation.web.financial_daily_jobs import (
    FinancialJobs,
    coverage,
    identifier,
    local_summary,
    stamp,
    status,
)
from market_data_foundation.web.financial_daily_schedule import FinancialSchedule
from market_data_foundation.web.gdp_api import body
from market_data_foundation.web.worker_control import DevWorkerControl

PREFIX = "/api/r5/financial/timeseries"


@dataclass(frozen=True)
class FinancialOptions:
    job_directory: Path
    allow_read: bool = False
    allow_collect: bool = False
    state_directory: Path | None = None
    window_path: Path | None = None
    allow_strategy: bool = False


def admitted_financial_job(request):
    if request.url.path in {PREFIX + "/jobs/resume", PREFIX + "/strategy"}:
        request = Request(dict(request.scope, path=PREFIX + "/jobs"))
    return admitted_job(request, prefix=PREFIX)


HELP = dict(
    dataset_id=DATASET_ID,
    endpoints=dict(
        status=PREFIX + "/status",
        strategy=PREFIX + "/strategy",
        summary=PREFIX + "/summary",
        query=PREFIX
        + "/query?start=YYYY-MM-DD&end=YYYY-MM-DD&series=DGS10"
        + "&as_of=ISO8601&offset=0&limit=200",
        jobs=PREFIX + "/jobs",
        detail=PREFIX + "/jobs/{request_id}",
        resume=PREFIX + "/jobs/resume",
    ),
    job_body=dict(
        request_id="canonical UUID; same request_id replays the original job",
        action="init | update",
        start="YYYY-MM-DD",
        end="YYYY-MM-DD",
    ),
    resume_body=dict(request_id="existing job UUID; resumes only incomplete steps"),
    notes=[
        "GET/查询/刷新零上游请求；POST 仅已准入私网 Host/Origin，公开 Host 只读。",
        "默认初始化为当天往前二十日历年，可编辑；增量更新所选日期区间并保留真实修订。",
        "as_of 是 MDF 确认可用截止，不代表官方历史首发；无历史发布时间证据则未知。",
        "信用原值为百分比，basis_points=percent×100；保留月末周末值。",
        (
            "信用历史为指定 GitHub 提交归档，FRED 只接尾段/显式增量；"
            "非商业个人 DEV，非对外分发授权。"
        ),
        "任务源请求失败/冷却/确认未知独立留痕；续跑保留已确认数值及首次可用时刻。",
    ],
)


def financial_router(options=None, *, service_factory=build_service, jobs=None):
    router = APIRouter(prefix=PREFIX, tags=["financial-daily"])
    host = jobs or (FinancialJobs(options.job_directory) if options else None)
    schedule = (
        FinancialSchedule(options.state_directory)
        if options and options.state_directory
        else None
    )
    control = (
        DevWorkerControl(options.window_path, assets=("financial",))
        if options and options.window_path
        else None
    )
    adapter = SimpleNamespace(
        auto_strategy=schedule,
        journal=host.journal if host else None,
        scheduler=schedule,
        source=lambda: None,
        capabilities=lambda _: dict(collect=bool(options and options.allow_collect)),
    )

    def require(write=False):
        if (
            not options
            or not options.allow_read
            or (write and not options.allow_collect)
        ):
            raise WebError("financial_capability_not_enabled", 403)

    def fail(error):
        return JSONResponse(
            dict(
                status="UNKNOWN",
                classification=getattr(
                    error, "classification", "financial_local_unavailable"
                ),
            ),
            status_code=error.status
            if isinstance(error, WebError)
            else 400
            if isinstance(error, FinancialError)
            else 503,
        )

    @router.get("/status")
    def read_status():
        try:
            require()
            return dict(
                status(),
                resident_worker=bool(schedule and control and options.allow_strategy),
            )
        except Exception as error:
            return fail(error)

    def strategy():
        if not schedule or not control or not options.allow_strategy:
            raise WebError("financial_strategy_not_enabled", 403)
        value = schedule.get()
        try:
            value["control"] = control.inspect("financial", adapter)
        except Exception as error:
            value["control"] = dict(
                online=False,
                registered=False,
                reason=getattr(
                    error, "classification", "financial_worker_not_verified"
                ),
            )
        return value

    @router.get("/strategy")
    def read_strategy():
        try:
            require()
            return strategy()
        except Exception as error:
            return fail(error)

    @router.post("/strategy")
    async def write_strategy(request: Request):
        try:
            require(True)
            if not admitted_financial_job(request):
                raise WebError("operator_not_authorized", 403)
            command = await body(request)
            if (
                type(command) is not dict
                or set(command) not in ({"enabled"}, {"enabled", "recover"})
                or type(command.get("enabled")) is not bool
                or (
                    "recover" in command
                    and (command["recover"] is not True or not command["enabled"])
                )
            ):
                raise WebError("financial_strategy_invalid")
            strategy()  # Require an explicitly configured, bounded DEV control.
            if command["enabled"]:
                await run_in_threadpool(bound_fred_credential)
                result = await run_in_threadpool(
                    control.act,
                    "financial",
                    adapter,
                    "recover" if command.get("recover") else "start",
                )
                if not result["online"]:
                    raise WebError("financial_worker_awaiting_heartbeat", 503)
            schedule.enabled(command["enabled"])
            return strategy()
        except Exception as error:
            return fail(error)

    @router.get("/api-help")
    def help_():
        try:
            require()
            return dict(status="OK", **HELP)
        except Exception as error:
            return fail(error)

    @router.get("/summary")
    def summary():
        service = None
        try:
            require()
            service = service_factory()
            return local_summary(service.store)
        except Exception as error:
            return fail(error)
        finally:
            if service:
                service.store.close()

    @router.get("/query")
    def query(request: Request):
        service = None
        try:
            require()
            params = request.query_params
            if set(params) - {
                "start",
                "end",
                "series",
                "as_of",
                "offset",
                "limit",
            } or any(len(params.getlist(k)) != 1 for k in params):
                raise WebError("financial_query_invalid")
            start, end = day(params["start"]), day(params["end"])
            if start > end or (end - start).days > 366 * 30:
                raise WebError("financial_query_range_invalid")
            selected = tuple(params.get("series", ",".join(SERIES_IDS)).split(","))
            series_ids(selected)
            offset, limit = (
                int(params.get("offset", "0")),
                int(params.get("limit", "200")),
            )
            if offset < 0 or not 1 <= limit <= 1000:
                raise WebError("financial_query_page_invalid")
            service = service_factory()
            cutoff = (
                stamp(params.get("as_of")) if params.get("as_of") else service.clock()
            )
            days = (end - start).days + 1
            total = days * len(selected)
            rows = []
            # Read the page's dates at one cutoff; no provider is constructed.
            for n, sid in enumerate(selected):
                lo, hi = max(offset, n * days), min(offset + limit, (n + 1) * days)
                if lo < hi:
                    a = (start + timedelta(days=lo - n * days)).isoformat()
                    b = (start + timedelta(days=hi - 1 - n * days)).isoformat()
                    rows.extend(
                        service.query(a, b, selected=(sid,), as_of=cutoff)["rows"]
                    )
            return dict(
                status="OK",
                rows=rows,
                as_of=cutoff.isoformat(),
                total=total,
                offset=offset,
                next_offset=offset + limit if offset + limit < total else None,
                provider_requests=0,
                coverage=coverage(
                    service.store, start.isoformat(), end.isoformat(), as_of=cutoff
                ),
            )
        except KeyError, ValueError, TypeError:
            return fail(WebError("financial_query_invalid"))
        except Exception as error:
            return fail(error)
        finally:
            if service:
                service.store.close()

    @router.get("/jobs")
    def list_jobs():
        try:
            require()
            return dict(status="OK", jobs=host.journal.list(), provider_requests=0)
        except Exception as error:
            return fail(error)

    @router.get("/jobs/{request_id}")
    def read_job(request_id: str):
        try:
            require()
            record = next(
                (
                    r
                    for r in host.journal.list()
                    if r["request_id"] == identifier(request_id)
                ),
                None,
            )
            if record is None:
                raise WebError("financial_job_not_found", 404)
            return dict(status="OK", job=record, provider_requests=0)
        except Exception as error:
            return fail(error)

    @router.post("/jobs")
    @router.post("/jobs/resume")
    async def submit(request: Request):
        try:
            require(True)
            if not admitted_financial_job(request):
                raise WebError("operator_not_authorized", 403)
            command = await body(request)
            if request.url.path.endswith("/resume"):
                if type(command) is not dict or set(command) != {"request_id"}:
                    raise WebError("financial_invalid_resume_request")
                record = await run_in_threadpool(
                    dispatch_job, host.resume, identifier(command["request_id"])
                )
            else:
                record = await run_in_threadpool(dispatch_job, host.submit, command)
            return dict(status="OK", job=record)
        except Exception as error:
            return fail(error)

    return router
