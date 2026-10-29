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

from market_data_foundation.canonical.model import require
from market_data_foundation.web.cpi_pg_readonly_scope import DevReadIdentity
from market_data_foundation.web.cpi_query import CpiQueryAccess
from market_data_foundation.web.cpi_timeseries_api import (
    admitted_job,
    admitted_strategy,
)
from market_data_foundation.web.cpi_timeseries_host import create_timeseries_preview
from market_data_foundation.web.pce_dev_host import CANDIDATE_LISTENER
from market_data_foundation.web.readonly_host import (
    ReadonlySettings,
    admitted_package_save,
)

ROOT = Path(__file__).resolve().parents[1]


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
        if request.method == "POST":
            return admitted_job(request)
        if request.method == "PUT":
            return admitted_package_save(request) or admitted_strategy(request)
        return super().request_admitted(request)

    do_POST = PreviewHandler.dispatch
    do_PUT = PreviewHandler.dispatch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opt-in-timeseries-web", action="store_true", required=True)
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
    args = parser.parse_args()
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

    app = create_timeseries_preview(
        settings,
        enabled=True,
        ui_directory=ui,
        schedule_path=args.schedule_path,
        history_start=args.history_start,
        job_directory=args.job_directory,
        cpi_access=access(args.allow_cpi_local_read),
        pce_access=access(args.allow_pce_local_read),
        allow_collect=args.allow_collect,
        max_collect_months=args.max_collect_months,
    )
    # Jobs own independent services and a nonblocking execution lock. Readers
    # can inspect the durable running record while one POST waits on a provider.
    with ThreadingHTTPServer(CANDIDATE_LISTENER, TimeSeriesHandler) as server:
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
