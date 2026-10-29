"""Host-only R5: read-only 18761 or bounded PCE / CPI-successor DEV 18760."""

import argparse
import json
import os
import sys
from http.server import HTTPServer
from pathlib import Path

from mdf_b3_pce_dev import PreviewHandler

from market_data_foundation.canonical.model import require
from market_data_foundation.historical.cpi_pg_journal import PgJournalIdentity
from market_data_foundation.historical.pce_pg_journal import (
    PgJournalIdentity as PceJournalIdentity,
)
from market_data_foundation.web.cpi_dev_host import DevHost as CpiHost
from market_data_foundation.web.observability import LinuxSampler, ObservabilityHost
from market_data_foundation.web.pce_dev_host import (
    CANDIDATE_LISTENER,
    CANDIDATE_ORIGIN,
    LISTENER,
    ORIGIN,
)
from market_data_foundation.web.pce_dev_host import (
    DevHost as PceHost,
)
from market_data_foundation.web.pce_r5_host import create_native_preview

ROOT = Path(__file__).resolve().parents[1]


class CandidateHandler(PreviewHandler):
    origin = CANDIDATE_ORIGIN
    listener = CANDIDATE_LISTENER

    # A QA preview may READ the actual CPI/PCE Host but may not accept jobs.
    # Deny mutations at the socket boundary, independent of forged HTTP
    # Origin/Host values and without delegating to ASGI journaling.
    def deny_mutation(self):
        return self.respond(403)

    do_POST = deny_mutation
    do_PUT = deny_mutation
    do_PATCH = deny_mutation
    do_DELETE = deny_mutation
    do_OPTIONS = deny_mutation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--opt-in-preview", action="store_true")
    mode.add_argument("--opt-in-dev-pce", action="store_true")
    parser.add_argument("--pce-schema-owner", required=True)
    parser.add_argument("--pce-schema-oid", type=int, required=True)
    parser.add_argument("--cpi-schema-owner", required=True)
    parser.add_argument("--cpi-schema-oid", type=int, required=True)
    parser.add_argument("--opt-in-local-resources", action="store_true")
    args = parser.parse_args()
    require(
        args.opt_in_preview or args.opt_in_dev_pce, "pce_native_host_opt_in_required"
    )
    require(sys.version_info[:3] == (3, 14, 8), "python_baseline_mismatch")
    require(Path.cwd().resolve() == ROOT, "pce_worktree_cwd_required")
    ui = ROOT / "web/dist"
    require((ui / "index.html").is_file(), "preview_ui_build_required")
    os.umask(0o077)
    pce_host = PceHost(
        PceJournalIdentity(args.pce_schema_owner, args.pce_schema_oid), enabled=True
    )
    cpi_host = CpiHost(
        PgJournalIdentity(args.cpi_schema_owner, args.cpi_schema_oid), enabled=True
    )
    observation = (
        ObservabilityHost(sampler=LinuxSampler("mdf-dev-r5-candidate"))
        if args.opt_in_local_resources
        else None
    )
    app = create_native_preview(
        pce_host,
        cpi_host,
        ui_directory=ui,
        enabled=True,
        observability_host=observation,
        origin=ORIGIN if args.opt_in_dev_pce else CANDIDATE_ORIGIN,
    )
    listener = LISTENER if args.opt_in_dev_pce else CANDIDATE_LISTENER
    handler = PreviewHandler if args.opt_in_dev_pce else CandidateHandler
    with HTTPServer(listener, handler) as server:
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
        sys.exit(1)
