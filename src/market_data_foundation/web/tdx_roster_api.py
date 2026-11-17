"""Base roster API over the same publications and durable executor as daily."""

from datetime import UTC, date, datetime

from fastapi import APIRouter, Request

from market_data_foundation.canonical.model import instant, require
from market_data_foundation.historical import tdx_roster
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
                "search支持简称、连续拼音片段和首字母；准确输入六位代码可查看名称记录。matched_changes来自深交所简称变更或通达信沪北股票更名，matched_observed_names为曾观察到的名称。",
                "GET只读已发布名册，零来源请求；刷新必须显式POST并进入共享执行记录。",
                "自动和手动刷新共用GetCount/GetCode/GetBJDirectory逐页核实；roster_membership_state=IN_DIRECTORY仅依据该证券真实返回记录，roster_verified_at为原目录页received_at，同名未变也重新保存。NOT_IN_DIRECTORY表示本次完整枚举未包含，不能据此推断退市；已知退市事实优先保留。",
                "membership_state及evidence继续表示独立交易所生命周期，valid_through不限制新目录成员核验。新出现证券在独立名册查询可见，不因此升级交易所在市或改变日线既有范围策略。旧快照缺producer时仅按精确snapshot_id关联唯一COMPLETE原作业，再以完整步骤、原目录页摘要和实际时间只读核实；歧义、缺失或损坏保留未核实原因，不回写历史。",
                "翻页同时固定day、snapshot_id、directory_snapshot_id和as_of；后续刷新不混页。失败刷新不替换已有完整名册，latest_refresh单列真实作业及失败，核验时间不取查询时间或失败时间。",
                "缺省day采用as_of前可见的最近完整TDX目录实际观测日，上午发布后即可查询；无目录刷新凭据时采用已有交易所证据日期，不使用日线盘后日期规则。",
                "日期选定后，与显式同日查询按同一as_of选择已发布的交易所成员证据；目录独立取不晚于请求日且截至as_of可见的完整目录，同日核验不得借用后来日期。snapshot_id固定实际返回的成员快照，directory_snapshot_id单列目录凭据，交易所原件晚到不改目录观测日。",
                "directory_observed_day是整批TDX目录的观测日，valid_through是交易所上市/退市证据截至日；membership_state仅由该证券原始生命周期证据判定，完整目录或名称差异不能将UNKNOWN升级为在市或证明单证券在目录出现。",
                "深沪GetCount/GetCode分页，北交所GetBJDirectory；官方在市/退市原件补生命周期，二者不是可互换来源。",
                "北交所官方分页与新旧代码映射独立留证；已知退市区间保留，未知历史代码沿革不推造。",
                "刷新来源目录不会延长交易所证据有效期；缺失、过期和历史未知独立报告，失败不替换完整发布。",
                "日线复用同一发布snapshot_id；旧daily/universe与action=universe作业继续兼容。",
                "交易日北京时间09:00刷新行情目录；15:35自动日线绑定本交易日晨间周期名册作业及其实际快照，同号恢复不随手动刷新更换。",
            ],
        )

    @router.get("/status")
    def status(request: Request):
        try:
            ready()
            q = request.query_params
            cutoff = datetime.fromisoformat(q["as_of"]) if "as_of" in q else None
            if cutoff is not None:
                require(len(q.getlist("as_of")) == 1, "tdx_roster_parameters")
                instant(cutoff)
            cutoff = cutoff or datetime.now(UTC)
            snapshot, value, directory = tdx_roster.query_publication(
                runtime.store, as_of=cutoff, with_directory=True
            )
            _, _, verification = tdx_roster.publication_verification(
                directory, getattr(runtime, "jobs", None), cutoff
            )
            verification["applies_to_query_day"] = (
                value["directory_observed_day"] == value["requested_day"]
            )
            return dict(
                value,
                enabled=True,
                collect=runtime.options.allow_collect,
                total=len((snapshot or {}).get("members", [])),
                directory_verification=verification,
                latest_refresh=tdx_roster.latest_refresh(
                    getattr(runtime, "jobs", None), cutoff
                ),
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
                        "directory_snapshot_id",
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
                date.fromisoformat(q["day"]) if "day" in q else None,
                as_of=cutoff,
                snapshot_id=q.get("snapshot_id"),
                directory_snapshot_id=q.get("directory_snapshot_id"),
                jobs=getattr(runtime, "jobs", None),
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
            from market_data_foundation.web.tdx_daily_api import tdx_job_listing

            return tdx_job_listing(runtime.jobs, roster=True)
        except Exception as error:
            return fail(error)

    def roster_job(identity):
        job = runtime.jobs.read(identity)
        if job["command"]["action"] != "universe":
            raise TdxError("job_not_found")
        return present_tdx_job(job)

    @router.get("/jobs/{identity}")
    def detail(identity: str, request: Request):
        try:
            ready()
            from market_data_foundation.web.tdx_daily_api import read_tdx_job

            return read_tdx_job(runtime.jobs, identity, request, roster=True)
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
