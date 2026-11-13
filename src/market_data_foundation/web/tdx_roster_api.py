"""Base roster API over the same publications and durable executor as daily."""

from datetime import date, datetime

from fastapi import APIRouter, Request

from market_data_foundation.canonical.model import instant, require
from market_data_foundation.historical import tdx_roster
from market_data_foundation.historical.tdx_p0_jobs import latest_day
from market_data_foundation.historical.tdx_p0_source import TdxError
from market_data_foundation.web.gdp_api import body
from market_data_foundation.web.tdx_daily_api import (
    admitted_tdx_write,
    present_tdx_job,
    tdx_failure,
)


def roster_router(runtime):
    router = APIRouter(prefix=tdx_roster.PREFIX, tags=["tdx-stock-roster"])

    def ready(write=False):
        if (
            not runtime
            or not runtime.options.allow_read
            or (write and not runtime.options.allow_collect)
        ):
            raise TdxError("tdx_capability_not_enabled")

    fail = tdx_failure

    @router.get("/help")
    def help():
        return dict(
            dataset_id=tdx_roster.DEFINITION.identity,
            revision="r1",
            endpoints={
                name: tdx_roster.PREFIX + path
                for name, path in dict(
                    query="/query",
                    status="/status",
                    jobs="/jobs",
                    resume="/jobs/resume",
                    help="/help",
                ).items()
            },
            notes=[
                "GET只读已发布名册，零来源请求；刷新必须显式POST并进入共享执行记录。",
                "深沪GetCount/GetCode分页，北交所GetBJDirectory；官方在市/退市原件补生命周期，二者不是可互换来源。",
                "北交所官方分页与新旧代码映射独立留证；已知退市区间保留，未知历史代码沿革不推造。",
                "刷新来源目录不会延长交易所证据有效期；缺失、过期和历史未知独立报告，失败不替换完整发布。",
                "日线复用同一发布snapshot_id；旧daily/universe与action=universe作业继续兼容。",
                "周期刷新复用现有日线15:10 Asia/Shanghai执行器，不另起worker或策略。",
            ],
        )

    @router.get("/status")
    def status():
        try:
            ready()
            snapshot, value = tdx_roster.published(runtime.store, latest_day())
            return dict(
                value,
                enabled=True,
                collect=runtime.options.allow_collect,
                total=len((snapshot or {}).get("members", [])),
            )
        except Exception as error:
            return fail(error)

    @router.get("/query")
    def query(request: Request):
        try:
            ready()
            q = request.query_params
            require(
                all(
                    k
                    in {
                        "day",
                        "as_of",
                        "snapshot_id",
                        "market",
                        "search",
                        "offset",
                        "limit",
                    }
                    and len(q.getlist(k)) == 1
                    for k in q
                ),
                "tdx_roster_parameters",
            )
            cutoff = datetime.fromisoformat(q["as_of"]) if "as_of" in q else None
            if cutoff is not None:
                instant(cutoff)
            return tdx_roster.query(
                runtime.store,
                date.fromisoformat(q["day"]) if "day" in q else latest_day(),
                as_of=cutoff,
                snapshot_id=q.get("snapshot_id"),
                market=int(q["market"]) if "market" in q else None,
                search=q.get("search", ""),
                offset=int(q.get("offset", "0")),
                limit=int(q.get("limit", "100")),
            )
        except Exception as error:
            return fail(error)

    @router.get("/jobs")
    def jobs():
        try:
            ready()
            return dict(
                jobs=[
                    present_tdx_job(j)
                    for j in runtime.jobs.summaries()
                    if j["command"]["action"] == "universe"
                ]
            )
        except Exception as error:
            return fail(error)

    def roster_job(identity):
        job = runtime.jobs.read(identity)
        if job["command"]["action"] != "universe":
            raise TdxError("job_not_found")
        return present_tdx_job(job)

    @router.get("/jobs/{identity}")
    def detail(identity: str):
        try:
            ready()
            return roster_job(identity)
        except Exception as error:
            return fail(error)

    @router.post("/jobs", status_code=202)
    async def submit(request: Request):
        try:
            ready(True)
            if not admitted_tdx_write(request, origin=runtime.options.origin):
                raise TdxError("operator_not_authorized")
            command = await body(request)
            require(set(command) == {"request_id"}, "tdx_roster_refresh_shape")
            if runtime.jobs.path(command["request_id"]).exists():
                return roster_job(command["request_id"])
            return runtime.jobs.submit(
                dict(command, action="universe"), origin="manual"
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
            roster_job(command["request_id"])
            return runtime.jobs.resume(command["request_id"])
        except Exception as error:
            return fail(error)

    return router
