"""Explicit macro worker using the selected capability's Web runtime binding."""

import argparse
import fcntl
import os
import random
import sys
import time
from pathlib import Path

from market_data_foundation.historical.cpi_timeseries_cli import (
    emit,
    error_document,
    run_worker,
)
from market_data_foundation.web.claims_runtime import ClaimsOptions
from market_data_foundation.web.cpi_pg_readonly_scope import DevReadIdentity
from market_data_foundation.web.cpi_query import CpiQueryAccess
from market_data_foundation.web.cpi_timeseries_host import TimeSeriesRuntime
from market_data_foundation.web.gdp_runtime import GdpOptions
from market_data_foundation.web.readonly_host import ReadonlySettings


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--asset",
        choices=(
            "cpi",
            "ppi",
            "pce",
            "employment",
            "retail",
            "consumption",
            "gdp",
            "claims",
            "m3",
        ),
        default="cpi",
    )
    parser.add_argument("--opt-in-m3-worker", action="store_true")
    parser.add_argument("--m3-job-directory", type=Path)
    parser.add_argument("--m3-request-control-path", type=Path)
    parser.add_argument("--opt-in-claims-worker", action="store_true")
    parser.add_argument("--claims-schedule-path", type=Path)
    parser.add_argument("--claims-job-directory", type=Path)
    parser.add_argument("--claims-history-start")
    parser.add_argument("--claims-artifacts-path", type=Path)
    parser.add_argument("--claims-request-control-path", type=Path)
    parser.add_argument("--opt-in-gdp-worker", action="store_true")
    parser.add_argument("--gdp-schedule-path", type=Path)
    parser.add_argument("--gdp-job-directory", type=Path)
    parser.add_argument("--gdp-history-start")
    parser.add_argument("--once", action="store_true", help="One bounded worker tick")
    parser.add_argument("--opt-in-cpi-worker", action="store_true")
    parser.add_argument("--opt-in-ppi-worker", action="store_true")
    parser.add_argument("--opt-in-pce-worker", action="store_true")
    parser.add_argument("--opt-in-consumption-worker", action="store_true")
    parser.add_argument("--consumption-schedule-path", type=Path)
    parser.add_argument("--consumption-job-directory", type=Path)
    parser.add_argument("--consumption-history-start")
    parser.add_argument("--opt-in-employment-worker", action="store_true")
    parser.add_argument("--opt-in-retail-worker", action="store_true")
    parser.add_argument("--retail-schedule-path", type=Path)
    parser.add_argument("--retail-job-directory", type=Path)
    parser.add_argument("--retail-artifacts-path", type=Path)
    parser.add_argument("--retail-request-control-path", type=Path)
    parser.add_argument("--employment-schedule-path", type=Path)
    parser.add_argument("--employment-job-directory", type=Path)
    parser.add_argument("--employment-history-start")
    parser.add_argument("--config-directory", type=Path, required=True)
    parser.add_argument("--package-store-directory", type=Path, required=True)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--pg-role", required=True)
    parser.add_argument("--pce-schema-owner", required=True)
    parser.add_argument("--pce-schema-oid", type=int, required=True)
    parser.add_argument("--schedule-path", type=Path, required=True)
    parser.add_argument("--job-directory", type=Path, required=True)
    parser.add_argument("--history-start", default="1999-01")
    parser.add_argument("--ppi-schedule-path", type=Path)
    parser.add_argument("--ppi-job-directory", type=Path)
    parser.add_argument("--ppi-history-start")
    parser.add_argument("--pce-schedule-path", type=Path)
    parser.add_argument("--pce-job-directory", type=Path)
    parser.add_argument("--pce-history-start")
    parser.add_argument("--bea-request-control-path", type=Path)
    parser.add_argument("--poll-seconds", type=int, default=5)
    parser.add_argument("--allow-collect", action="store_true")
    return parser


def build_runtime(args):
    if not getattr(args, f"opt_in_{args.asset}_worker"):
        raise ValueError(f"explicit_{args.asset}_worker_opt_in_required")
    if args.asset == "m3" and (
        args.m3_job_directory is None or args.m3_request_control_path is None
    ):
        raise ValueError("m3_independent_paths_required")
    paths = [args.schedule_path, args.job_directory]
    if args.asset == "gdp":
        if args.gdp_schedule_path is None or args.gdp_job_directory is None:
            raise ValueError("gdp_independent_paths_required")
        paths += [args.gdp_schedule_path, args.gdp_job_directory]
    if args.asset == "claims":
        if args.claims_schedule_path is None or args.claims_job_directory is None:
            raise ValueError("claims_independent_paths_required")
        paths += [args.claims_schedule_path, args.claims_job_directory]
    ppi = args.asset == "ppi"
    pce = args.asset == "pce"
    retail = args.asset == "retail"
    consumption = args.asset == "consumption"
    if consumption:
        if (
            args.consumption_schedule_path is None
            or args.consumption_job_directory is None
        ):
            raise ValueError("consumption_independent_paths_required")
        paths += [args.consumption_schedule_path, args.consumption_job_directory]
    if retail:
        retail_paths = [
            args.retail_schedule_path,
            args.retail_job_directory,
            args.retail_artifacts_path,
            args.retail_request_control_path,
        ]
        if any(p is None for p in retail_paths):
            raise ValueError("retail_independent_paths_required")
        paths += retail_paths
    if ppi:
        if args.ppi_schedule_path is None or args.ppi_job_directory is None:
            raise ValueError("ppi_independent_paths_required")
        if not args.ppi_history_start:
            raise ValueError("ppi_history_start_evidence_required")
        paths += [args.ppi_schedule_path, args.ppi_job_directory]
    if pce:
        if args.pce_schedule_path is None or args.pce_job_directory is None:
            raise ValueError("pce_independent_paths_required")
        paths += [args.pce_schedule_path, args.pce_job_directory]
    if any(not path.is_absolute() for path in paths):
        raise ValueError("absolute_worker_paths_required")
    if len({path.resolve() for path in paths}) != len(paths):
        raise ValueError("independent_worker_paths_required")
    identity = DevReadIdentity(args.pg_role, args.pce_schema_owner, args.pce_schema_oid)
    settings = ReadonlySettings(
        args.config_directory,
        args.package_store_directory,
        args.environment_id,
        identity,
        identity,
    )
    from market_data_foundation.web.m3_runtime import M3Options

    return TimeSeriesRuntime(
        settings,
        m3_options=M3Options(
            job_directory=args.m3_job_directory,
            request_control_path=args.m3_request_control_path,
            allow_read=True,
            allow_collect=args.allow_collect,
            allow_source=args.allow_collect,
        )
        if args.asset == "m3"
        else None,
        claims_options=ClaimsOptions(
            args.claims_schedule_path,
            args.claims_job_directory,
            allow_read=True,
            allow_collect=args.allow_collect,
            history_start=args.claims_history_start,
            artifacts_path=args.claims_artifacts_path,
            request_control_path=args.claims_request_control_path,
        )
        if args.asset == "claims"
        else None,
        gdp_options=GdpOptions(
            args.gdp_schedule_path,
            args.gdp_job_directory,
            allow_read=True,
            allow_collect=args.allow_collect,
            history_start=args.gdp_history_start,
        )
        if args.asset == "gdp"
        else None,
        schedule_path=args.schedule_path,
        job_directory=args.job_directory,
        history_start=args.history_start,
        cpi_access=CpiQueryAccess(
            metadata_permission="allowed", read_permission="allowed"
        ),
        allow_collect=args.allow_collect and args.asset == "cpi",
        retail_enabled=retail,
        retail_schedule_path=args.retail_schedule_path,
        retail_job_directory=args.retail_job_directory,
        retail_artifacts_path=args.retail_artifacts_path,
        retail_request_control_path=args.retail_request_control_path,
        retail_access=CpiQueryAccess(
            metadata_permission="allowed" if retail else "denied",
            read_permission="allowed" if retail else "denied",
        ),
        retail_allow_collect=args.allow_collect and retail,
        employment_enabled=args.asset == "employment",
        employment_schedule_path=args.employment_schedule_path,
        employment_job_directory=args.employment_job_directory,
        employment_history_start=args.employment_history_start,
        employment_access=CpiQueryAccess(
            metadata_permission="allowed" if args.asset == "employment" else "denied",
            read_permission="allowed" if args.asset == "employment" else "denied",
        ),
        employment_allow_collect=args.allow_collect and args.asset == "employment",
        ppi_enabled=ppi,
        ppi_schedule_path=args.ppi_schedule_path if ppi else None,
        ppi_job_directory=args.ppi_job_directory if ppi else None,
        ppi_history_start=args.ppi_history_start if ppi else None,
        ppi_access=CpiQueryAccess(
            metadata_permission="allowed" if ppi else "denied",
            read_permission="allowed" if ppi else "denied",
        ),
        ppi_allow_collect=args.allow_collect and ppi,
        consumption_enabled=consumption,
        consumption_schedule_path=args.consumption_schedule_path,
        consumption_job_directory=args.consumption_job_directory,
        consumption_history_start=args.consumption_history_start,
        consumption_access=CpiQueryAccess(
            metadata_permission="allowed" if consumption else "denied",
            read_permission="allowed" if consumption else "denied",
        ),
        consumption_allow_collect=args.allow_collect and consumption,
        pce_enabled=pce,
        pce_schedule_path=args.pce_schedule_path if pce else None,
        pce_job_directory=args.pce_job_directory if pce else None,
        pce_history_start=args.pce_history_start if pce else None,
        pce_access=CpiQueryAccess(
            metadata_permission="allowed" if pce else "denied",
            read_permission="allowed" if pce else "denied",
        ),
        pce_allow_collect=args.allow_collect and pce,
        pce_request_control_path=args.bea_request_control_path
        if pce or consumption
        else None,
    )


def worker_sleep(seconds):
    """Spread worker wakeups without advancing due times or adding requests."""
    time.sleep(seconds + random.uniform(0.0, 2.0))


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        if sys.version_info[:3] != (3, 14, 8):
            raise ValueError("python_baseline_mismatch")
        if not 5 <= args.poll_seconds <= 3600:
            raise ValueError("poll_seconds_out_of_range")
        os.umask(0o077)
        runtime = build_runtime(args)
        # Construction and heartbeat never create/enable a strategy or collect.
        # tick rereads persistent enabled; even paused workers remain observable.
        if args.asset == "m3":
            host = runtime.m3_query_host
        elif args.asset == "claims":
            host = runtime.claims_timeseries
        elif args.asset == "gdp":
            host = runtime.gdp_timeseries
        elif args.asset == "cpi":
            host = runtime.timeseries
        elif args.asset == "consumption":
            host = runtime.consumption_timeseries
        elif args.asset == "ppi":
            host = runtime.ppi_timeseries
        elif args.asset == "retail":
            host = runtime.retail_timeseries
        elif args.asset == "employment":
            host = runtime.employment_timeseries
        else:
            host = runtime.pce_timeseries
        host.auto_strategy.strategy_path.parent.mkdir(parents=True, exist_ok=True)
        executor_lock = host.auto_strategy.strategy_path.with_suffix(
            ".executor.lock"
        ).open("a")
        try:
            fcntl.flock(executor_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("worker_already_running") from None
        if args.asset == "m3":
            import json
            from datetime import UTC, datetime

            process_path = host.auto_strategy.strategy_path.with_name("m3-worker.json")
            process_path.parent.mkdir(parents=True, exist_ok=True)
            process_path.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "state": "running",
                        "started_at": datetime.now(UTC).isoformat(),
                    }
                )
            )
        try:
            if args.once:
                emit(host.auto_strategy.tick())
            else:
                run_worker(
                    host.auto_strategy,
                    poll_seconds=args.poll_seconds,
                    sleep=worker_sleep,
                )
            return 0
        finally:
            if args.asset == "m3":
                process_path.write_text(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "state": "stopped",
                            "stopped_at": datetime.now(UTC).isoformat(),
                        }
                    )
                )
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        emit(error_document(error), stream=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
