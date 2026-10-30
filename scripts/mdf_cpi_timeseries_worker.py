"""Explicit CPI, PPI or PCE worker using the exact Web runtime bindings."""

import argparse
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
from market_data_foundation.web.cpi_pg_readonly_scope import DevReadIdentity
from market_data_foundation.web.cpi_query import CpiQueryAccess
from market_data_foundation.web.cpi_timeseries_host import TimeSeriesRuntime
from market_data_foundation.web.readonly_host import ReadonlySettings


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", choices=("cpi", "ppi", "pce"), default="cpi")
    parser.add_argument("--opt-in-cpi-worker", action="store_true")
    parser.add_argument("--opt-in-ppi-worker", action="store_true")
    parser.add_argument("--opt-in-pce-worker", action="store_true")
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
    paths = [args.schedule_path, args.job_directory]
    ppi = args.asset == "ppi"
    pce = args.asset == "pce"
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
    return TimeSeriesRuntime(
        settings,
        schedule_path=args.schedule_path,
        job_directory=args.job_directory,
        history_start=args.history_start,
        cpi_access=CpiQueryAccess(
            metadata_permission="allowed", read_permission="allowed"
        ),
        allow_collect=args.allow_collect and args.asset == "cpi",
        ppi_enabled=ppi,
        ppi_schedule_path=args.ppi_schedule_path if ppi else None,
        ppi_job_directory=args.ppi_job_directory if ppi else None,
        ppi_history_start=args.ppi_history_start if ppi else None,
        ppi_access=CpiQueryAccess(
            metadata_permission="allowed" if ppi else "denied",
            read_permission="allowed" if ppi else "denied",
        ),
        ppi_allow_collect=args.allow_collect and ppi,
        pce_enabled=pce,
        pce_schedule_path=args.pce_schedule_path if pce else None,
        pce_job_directory=args.pce_job_directory if pce else None,
        pce_history_start=args.pce_history_start if pce else None,
        pce_access=CpiQueryAccess(
            metadata_permission="allowed" if pce else "denied",
            read_permission="allowed" if pce else "denied",
        ),
        pce_allow_collect=args.allow_collect and pce,
        pce_request_control_path=args.bea_request_control_path if pce else None,
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
        if args.asset == "cpi":
            host = runtime.timeseries
        elif args.asset == "ppi":
            host = runtime.ppi_timeseries
        else:
            host = runtime.pce_timeseries
        run_worker(
            host.auto_strategy,
            poll_seconds=args.poll_seconds,
            sleep=worker_sleep,
        )
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        emit(error_document(error), stream=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
