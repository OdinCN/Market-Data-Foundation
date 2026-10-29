"""Explicit independent CPI worker using the exact Web runtime bindings."""

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
    parser.add_argument("--opt-in-cpi-worker", action="store_true", required=True)
    parser.add_argument("--config-directory", type=Path, required=True)
    parser.add_argument("--package-store-directory", type=Path, required=True)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--pg-role", required=True)
    parser.add_argument("--pce-schema-owner", required=True)
    parser.add_argument("--pce-schema-oid", type=int, required=True)
    parser.add_argument("--schedule-path", type=Path, required=True)
    parser.add_argument("--job-directory", type=Path, required=True)
    parser.add_argument("--history-start", default="1999-01")
    parser.add_argument("--poll-seconds", type=int, default=5)
    parser.add_argument("--allow-collect", action="store_true")
    return parser


def build_runtime(args):
    if not args.schedule_path.is_absolute():
        raise ValueError("absolute_schedule_path_required")
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
        allow_collect=args.allow_collect,
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
        run_worker(
            runtime.timeseries.auto_strategy,
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
