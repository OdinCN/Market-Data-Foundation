"""Explicit DEV local-data preview with optional package configuration saves."""

import argparse
import json
import sys
from http.server import HTTPServer
from pathlib import Path

from mdf_b3_pce_dev import PreviewHandler
from mdf_b3_pce_r5_preview import CandidateHandler

from market_data_foundation.canonical.model import require
from market_data_foundation.web.cpi_pg_readonly_scope import DevReadIdentity
from market_data_foundation.web.cpi_query import CpiQueryAccess
from market_data_foundation.web.pce_dev_host import CANDIDATE_LISTENER
from market_data_foundation.web.readonly_host import (
    ReadonlySettings,
    admitted_package_save,
    create_readonly_preview,
)

ROOT = Path(__file__).resolve().parents[1]


class ConfigurationHandler(CandidateHandler):
    """Only same-origin package configuration PUT, with no job dispatch."""

    body_methods = {"PUT"}
    body_limit = 16384

    def request_admitted(self, request):
        if request.method == "PUT":
            return admitted_package_save(request)
        return super().request_admitted(request)

    do_PUT = PreviewHandler.dispatch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opt-in-readonly-preview", action="store_true", required=True)
    parser.add_argument("--config-directory", type=Path, required=True)
    parser.add_argument("--package-store-directory", type=Path, required=True)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--cpi-schema-owner", required=True)
    parser.add_argument("--cpi-schema-oid", type=int, required=True)
    parser.add_argument("--pce-schema-owner", required=True)
    parser.add_argument("--pce-schema-oid", type=int, required=True)
    parser.add_argument("--pg-role", required=True)
    parser.add_argument("--allow-cpi-local-read", action="store_true")
    parser.add_argument("--allow-pce-local-read", action="store_true")
    parser.add_argument("--allow-package-config-save", action="store_true")
    args = parser.parse_args()
    require(sys.version_info[:3] == (3, 14, 8), "python_baseline_mismatch")
    require(Path.cwd().resolve() == ROOT, "readonly_worktree_cwd_required")
    ui = ROOT / "web/dist"
    require((ui / "index.html").is_file(), "preview_ui_build_required")
    settings = ReadonlySettings(
        args.config_directory,
        args.package_store_directory,
        args.environment_id,
        DevReadIdentity(args.pg_role, args.cpi_schema_owner, args.cpi_schema_oid),
        DevReadIdentity(args.pg_role, args.pce_schema_owner, args.pce_schema_oid),
        package_config_save=args.allow_package_config_save,
    )

    def access(allowed):
        return CpiQueryAccess(
            metadata_permission="allowed" if allowed else "unknown",
            read_permission="allowed" if allowed else "unknown",
        )

    app = create_readonly_preview(
        settings,
        enabled=True,
        ui_directory=ui,
        cpi_access=access(args.allow_cpi_local_read),
        pce_access=access(args.allow_pce_local_read),
    )
    handler = (
        ConfigurationHandler if args.allow_package_config_save else CandidateHandler
    )
    with HTTPServer(CANDIDATE_LISTENER, handler) as server:
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
