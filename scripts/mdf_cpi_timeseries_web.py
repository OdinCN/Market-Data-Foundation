"""Explicit DEV Web preview; CPI time-series reads and persistent job interface."""

import argparse
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mdf_b3_pce_dev import PreviewHandler
from mdf_b3_pce_r5_preview import CandidateHandler
from starlette.requests import Request

from market_data_foundation.canonical.model import require
from market_data_foundation.web.claims_runtime import ClaimsOptions
from market_data_foundation.web.cpi_pg_readonly_scope import DevReadIdentity
from market_data_foundation.web.cpi_query import CpiQueryAccess
from market_data_foundation.web.cpi_timeseries_api import (
    admitted_job,
    admitted_strategy,
)
from market_data_foundation.web.cpi_timeseries_host import create_timeseries_preview
from market_data_foundation.web.financial_daily_api import (
    FinancialOptions,
    admitted_financial_job,
)
from market_data_foundation.web.gdp_runtime import GdpOptions
from market_data_foundation.web.jolts_runtime import JoltsOptions
from market_data_foundation.web.m3_runtime import M3Options
from market_data_foundation.web.pce_dev_host import CANDIDATE_LISTENER
from market_data_foundation.web.readonly_host import (
    ReadonlySettings,
    admitted_package_save,
    admitted_package_verify,
)

ROOT = Path(__file__).resolve().parents[1]


def admitted_job_resume(request):
    from market_data_foundation.web.retail_timeseries import admitted_retail_job

    return admitted_retail_job(request)


class TimeSeriesHandler(CandidateHandler):
    body_methods = {"POST", "PUT"}
    body_limit = 16384
    # Full four-series history includes provenance and derived fields.
    response_limit = 32 * 1024 * 1024
    request_deadline_seconds = 180

    def handle(self):
        def expire():
            try:
                self.request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(self.request_deadline_seconds, expire)
        timer.daemon = True
        timer.start()
        try:
            BaseHTTPRequestHandler.handle(self)
        except OSError, TimeoutError:
            pass
        finally:
            timer.cancel()

    def request_admitted(self, request):
        from market_data_foundation.historical.tdx_p0_contract import (
            ORIGIN as TDX_ORIGIN,
        )
        from market_data_foundation.web.pce_dev_host import admitted
        from market_data_foundation.web.tdx_daily_api import admitted_tdx_write

        if self.origin in {TDX_ORIGIN, "http://192.168.10.20:18767"}:
            return admitted_tdx_write(request, origin=self.origin) or (
                request.method in {"GET", "HEAD"}
                and admitted(request, origin=self.origin)
            )
        if request.method == "POST":
            from market_data_foundation.web.worker_control import admitted_worker

            return (
                admitted_worker(request)
                or admitted_financial_job(request)
                or admitted_package_verify(request)
                or admitted_job(request, prefix="/api/r5/cpi/timeseries")
                or admitted_job(request, prefix="/api/r5/ppi/timeseries")
                or admitted_job(request, prefix="/api/r5/pce/timeseries")
                or admitted_job(request, prefix="/api/r5/consumption/timeseries")
                or admitted_job(request, prefix="/api/r5/employment/timeseries")
                or admitted_job(request, prefix="/api/r5/retail/timeseries")
                or admitted_job(request, prefix="/api/r5/gdp/timeseries")
                or admitted_job(request, prefix="/api/r5/claims/timeseries")
                or admitted_job(request, prefix="/api/r5/jolts/timeseries")
                or admitted_job(request, prefix="/api/r5/m3/timeseries")
                or (
                    request.url.path == "/api/r5/m3/timeseries/jobs/resume"
                    and admitted_job(
                        Request(dict(request.scope, path="/api/r5/m3/timeseries/jobs")),
                        prefix="/api/r5/m3/timeseries",
                    )
                )
                or (
                    request.url.path == "/api/r5/retail/timeseries/strategy"
                    and admitted_strategy(
                        Request(dict(request.scope, method="PUT")),
                        prefix="/api/r5/retail/timeseries",
                    )
                )
                or (
                    request.url.path == "/api/r5/retail/timeseries/jobs/resume"
                    and admitted_job_resume(request)
                )
            )
        if request.method == "PUT":
            return (
                admitted_package_save(request)
                or admitted_strategy(request, prefix="/api/r5/cpi/timeseries")
                or admitted_strategy(request, prefix="/api/r5/ppi/timeseries")
                or admitted_strategy(request, prefix="/api/r5/pce/timeseries")
                or admitted_strategy(request, prefix="/api/r5/consumption/timeseries")
                or admitted_strategy(request, prefix="/api/r5/employment/timeseries")
                or admitted_strategy(request, prefix="/api/r5/retail/timeseries")
                or admitted_strategy(request, prefix="/api/r5/gdp/timeseries")
                or admitted_strategy(request, prefix="/api/r5/claims/timeseries")
                or admitted_strategy(request, prefix="/api/r5/jolts/timeseries")
                or admitted_strategy(request, prefix="/api/r5/m3/timeseries")
            )
        return super().request_admitted(request)

    do_POST = PreviewHandler.dispatch
    do_PUT = PreviewHandler.dispatch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opt-in-timeseries-web", action="store_true", required=True)
    parser.add_argument("--tdx-state-directory", type=Path)
    parser.add_argument("--tdx-driver", type=Path)
    parser.add_argument("--tdx-endpoint")
    parser.add_argument("--allow-tdx-collect", action="store_true")
    parser.add_argument(
        "--tdx-dev-port", type=int, choices=(18766, 18767), default=18766
    )
    parser.add_argument("--tdx-workers", type=int, choices=range(1, 9), default=4)
    parser.add_argument("--worker-window-state", type=Path)
    parser.add_argument("--config-directory", type=Path, required=True)
    parser.add_argument("--package-store-directory", type=Path, required=True)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--pg-role", required=True)
    parser.add_argument("--pce-schema-owner", required=True)
    parser.add_argument("--pce-schema-oid", type=int, required=True)
    parser.add_argument("--schedule-path", type=Path, required=True)
    parser.add_argument("--history-start", default="1999-01")
    parser.add_argument("--job-directory", type=Path, required=True)
    parser.add_argument("--allow-cpi-local-read", action="store_true")
    parser.add_argument("--allow-pce-local-read", action="store_true")
    parser.add_argument("--allow-package-config-save", action="store_true")
    parser.add_argument(
        "--allow-collect",
        action="store_true",
        help="Enable collection when the stored CPI capability and BLS key qualify",
    )
    parser.add_argument(
        "--max-collect-months",
        type=int,
        default=None,
        help=(
            "Optional operational limit on collection months (1..2400); "
            "by default the backend batches the full requested range within "
            "core range and request budgets, without manual splitting"
        ),
    )
    parser.add_argument("--enable-ppi-timeseries", action="store_true")
    parser.add_argument("--ppi-schedule-path", type=Path)
    parser.add_argument("--ppi-job-directory", type=Path)
    parser.add_argument("--ppi-history-start")
    parser.add_argument("--allow-ppi-local-read", action="store_true")
    parser.add_argument("--allow-ppi-collect", action="store_true")
    parser.add_argument("--enable-consumption-timeseries", action="store_true")
    parser.add_argument("--allow-consumption-local-read", action="store_true")
    parser.add_argument("--allow-consumption-collect", action="store_true")
    parser.add_argument("--consumption-schedule-path", type=Path)
    parser.add_argument("--consumption-job-directory", type=Path)
    parser.add_argument("--consumption-history-start")
    parser.add_argument("--enable-pce-timeseries", action="store_true")
    parser.add_argument("--pce-schedule-path", type=Path)
    parser.add_argument("--pce-job-directory", type=Path)
    parser.add_argument("--pce-history-start")
    parser.add_argument("--bea-request-control-path", type=Path)
    parser.add_argument("--allow-pce-collect", action="store_true")
    parser.add_argument("--enable-employment-timeseries", action="store_true")
    parser.add_argument("--employment-schedule-path", type=Path)
    parser.add_argument("--employment-job-directory", type=Path)
    parser.add_argument("--employment-history-start")
    parser.add_argument("--allow-employment-local-read", action="store_true")
    parser.add_argument("--allow-employment-collect", action="store_true")
    parser.add_argument("--enable-retail-timeseries", action="store_true")
    parser.add_argument("--allow-retail-local-read", action="store_true")
    parser.add_argument("--allow-retail-collect", action="store_true")
    parser.add_argument("--allow-retail-source-query", action="store_true")
    parser.add_argument("--retail-job-directory", type=Path)
    parser.add_argument("--retail-artifacts-path", type=Path)
    parser.add_argument("--retail-schedule-path", type=Path)
    parser.add_argument("--retail-request-control-path", type=Path)
    parser.add_argument("--enable-claims-timeseries", action="store_true")
    parser.add_argument("--allow-claims-local-read", action="store_true")
    parser.add_argument("--allow-claims-collect", action="store_true")
    parser.add_argument("--allow-claims-source-query", action="store_true")
    parser.add_argument("--claims-schedule-path", type=Path)
    parser.add_argument("--claims-job-directory", type=Path)
    parser.add_argument("--claims-history-start")
    parser.add_argument("--claims-artifacts-path", type=Path)
    parser.add_argument("--claims-request-control-path", type=Path)
    parser.add_argument("--enable-gdp-timeseries", action="store_true")
    parser.add_argument("--allow-gdp-local-read", action="store_true")
    parser.add_argument("--allow-gdp-collect", action="store_true")
    parser.add_argument("--allow-gdp-source-query", action="store_true")
    parser.add_argument("--gdp-schedule-path", type=Path)
    parser.add_argument("--gdp-job-directory", type=Path)
    parser.add_argument("--gdp-history-start")
    parser.add_argument("--allow-jolts-local-read", action="store_true")
    parser.add_argument("--allow-jolts-collect", action="store_true")
    parser.add_argument("--allow-jolts-source", action="store_true")
    parser.add_argument("--jolts-state-directory", type=Path)
    parser.add_argument("--allow-m3-local-read", action="store_true")
    parser.add_argument("--allow-m3-collect", action="store_true")
    parser.add_argument("--allow-m3-source-query", action="store_true")
    parser.add_argument("--m3-job-directory", type=Path)
    parser.add_argument("--m3-request-control-path", type=Path)
    parser.add_argument("--financial-job-directory", type=Path)
    parser.add_argument("--allow-financial-read", action="store_true")
    parser.add_argument("--allow-financial-collect", action="store_true")
    parser.add_argument("--financial-state-directory", type=Path)
    parser.add_argument("--allow-financial-strategy", action="store_true")
    args = parser.parse_args()
    if args.enable_claims_timeseries and (
        args.claims_schedule_path is None or args.claims_job_directory is None
    ):
        parser.error("Claims requires independent schedule/job paths")
    if args.enable_gdp_timeseries and (
        args.gdp_schedule_path is None or args.gdp_job_directory is None
    ):
        parser.error("GDP requires independent schedule/job paths")
    if args.enable_ppi_timeseries:
        if not args.ppi_history_start:
            parser.error("--enable-ppi-timeseries requires --ppi-history-start")
        if not all(
            p is not None and p.is_absolute() and ".." not in p.parts
            for p in (args.ppi_schedule_path, args.ppi_job_directory)
        ):
            parser.error("PPI requires independent absolute schedule and job paths")
        if (
            args.ppi_schedule_path.resolve() == args.schedule_path.resolve()
            or args.ppi_job_directory.resolve() == args.job_directory.resolve()
        ):
            parser.error("PPI requires independent schedule and job paths")
    require(sys.version_info[:3] == (3, 14, 8), "python_baseline_mismatch")
    require(Path.cwd().resolve() == ROOT, "timeseries_worktree_cwd_required")
    ui = ROOT / "web/dist"
    require((ui / "index.html").is_file(), "preview_ui_build_required")
    require(args.schedule_path.is_absolute(), "absolute_schedule_path_required")
    os.umask(0o077)
    identity = DevReadIdentity(args.pg_role, args.pce_schema_owner, args.pce_schema_oid)
    # The inherited settings field for old CPI identity is unused by this app.
    settings = ReadonlySettings(
        args.config_directory,
        args.package_store_directory,
        args.environment_id,
        identity,
        identity,
        package_config_save=args.allow_package_config_save,
    )

    def access(allowed):
        return CpiQueryAccess(
            metadata_permission="allowed" if allowed else "unknown",
            read_permission="allowed" if allowed else "unknown",
        )

    jolts_options = None
    if (
        args.allow_jolts_local_read
        or args.allow_jolts_collect
        or args.allow_jolts_source
    ):
        require(
            args.jolts_state_directory is not None, "jolts_independent_state_required"
        )
        jolts_options = JoltsOptions(
            state_directory=args.jolts_state_directory,
            allow_read=args.allow_jolts_local_read,
            allow_collect=args.allow_jolts_collect,
            allow_source=args.allow_jolts_source,
        )
    m3_options = None
    if args.allow_m3_local_read or args.allow_m3_collect or args.allow_m3_source_query:
        require(
            args.m3_job_directory is not None
            and args.m3_request_control_path is not None,
            "m3_independent_paths_required",
        )
        m3_options = M3Options(
            job_directory=args.m3_job_directory,
            request_control_path=args.m3_request_control_path,
            allow_read=args.allow_m3_local_read,
            allow_collect=args.allow_m3_collect,
            allow_source=args.allow_m3_source_query,
        )
    from market_data_foundation.web.worker_control import DevWorkerControl

    tdx_runtime = None
    if args.tdx_state_directory:
        from market_data_foundation.web.tdx_daily_api import DailyRuntime, TdxOptions

        require(
            args.tdx_driver is not None and args.tdx_endpoint is not None,
            "tdx_binding_required",
        )
        tdx_runtime = DailyRuntime(
            TdxOptions(
                args.tdx_state_directory,
                args.tdx_driver,
                args.tdx_endpoint,
                args.allow_tdx_collect,
                origin=f"http://192.168.10.20:{args.tdx_dev_port}",
                workers=args.tdx_workers,
            )
        )
    app = create_timeseries_preview(
        settings,
        tdx_runtime=tdx_runtime,
        dev_origin=tdx_runtime.options.origin
        if tdx_runtime
        else "http://192.168.10.20:18761",
        worker_control=DevWorkerControl(args.worker_window_state)
        if args.worker_window_state
        else None,
        m3_options=m3_options,
        jolts_options=jolts_options,
        enabled=True,
        claims_options=ClaimsOptions(
            args.claims_schedule_path,
            args.claims_job_directory,
            allow_read=args.allow_claims_local_read,
            allow_collect=args.allow_claims_collect,
            allow_source=args.allow_claims_source_query,
            history_start=args.claims_history_start,
            artifacts_path=args.claims_artifacts_path,
            request_control_path=args.claims_request_control_path,
        )
        if args.enable_claims_timeseries
        else None,
        gdp_options=GdpOptions(
            args.gdp_schedule_path,
            args.gdp_job_directory,
            allow_read=args.allow_gdp_local_read,
            allow_collect=args.allow_gdp_collect,
            allow_source=args.allow_gdp_source_query,
            history_start=args.gdp_history_start,
        )
        if args.enable_gdp_timeseries
        else None,
        ui_directory=ui,
        financial_options=FinancialOptions(
            args.financial_job_directory,
            args.allow_financial_read,
            args.allow_financial_collect,
            state_directory=args.financial_state_directory,
            window_path=args.worker_window_state,
            allow_strategy=args.allow_financial_strategy,
        )
        if args.financial_job_directory
        else None,
        schedule_path=args.schedule_path,
        history_start=args.history_start,
        job_directory=args.job_directory,
        cpi_access=access(args.allow_cpi_local_read),
        pce_access=access(args.allow_pce_local_read),
        allow_collect=args.allow_collect,
        max_collect_months=args.max_collect_months,
        employment_enabled=args.enable_employment_timeseries,
        employment_schedule_path=args.employment_schedule_path,
        employment_job_directory=args.employment_job_directory,
        employment_history_start=args.employment_history_start,
        employment_access=access(args.allow_employment_local_read),
        employment_allow_collect=args.allow_employment_collect,
        ppi_enabled=args.enable_ppi_timeseries,
        ppi_schedule_path=args.ppi_schedule_path,
        ppi_job_directory=args.ppi_job_directory,
        ppi_history_start=args.ppi_history_start,
        ppi_access=access(args.allow_ppi_local_read),
        ppi_allow_collect=args.allow_ppi_collect,
        consumption_enabled=args.enable_consumption_timeseries,
        consumption_access=access(args.allow_consumption_local_read),
        consumption_allow_collect=args.allow_consumption_collect,
        consumption_schedule_path=args.consumption_schedule_path,
        consumption_job_directory=args.consumption_job_directory,
        consumption_history_start=args.consumption_history_start,
        pce_enabled=args.enable_pce_timeseries,
        pce_schedule_path=args.pce_schedule_path,
        pce_job_directory=args.pce_job_directory,
        pce_history_start=args.pce_history_start,
        pce_request_control_path=args.bea_request_control_path,
        pce_allow_collect=args.allow_pce_collect,
        retail_enabled=args.enable_retail_timeseries,
        retail_access=access(args.allow_retail_local_read),
        retail_allow_collect=args.allow_retail_collect,
        retail_allow_source=args.allow_retail_source_query,
        retail_job_directory=args.retail_job_directory,
        retail_artifacts_path=args.retail_artifacts_path,
        retail_schedule_path=args.retail_schedule_path,
        retail_request_control_path=args.retail_request_control_path,
    )
    # Jobs own independent services and a nonblocking execution lock. Readers
    # can inspect the durable running record while one POST waits on a provider.
    listener = CANDIDATE_LISTENER
    handler = TimeSeriesHandler
    if tdx_runtime:

        class TdxHandler(TimeSeriesHandler):
            body_limit = 160000
            origin = tdx_runtime.options.origin
            listener = ("192.168.10.20", args.tdx_dev_port)

        listener, handler = TdxHandler.listener, TdxHandler
        if args.allow_tdx_collect:
            tdx_runtime.strategy.start()
    with ThreadingHTTPServer(listener, handler) as server:
        server.app = app
        server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(
            json.dumps({"status": "UNKNOWN", "exception_class": type(error).__name__})
        )
        raise SystemExit(1) from None
