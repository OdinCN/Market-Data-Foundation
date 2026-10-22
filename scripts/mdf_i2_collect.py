"""Plan or explicitly collect one bounded I-2 research observation."""

import argparse
import json
import uuid
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from market_data_foundation.canonical.model import ContractError, require
from market_data_foundation.qualification.tdx_market_collect import ArtifactRun, collect
from market_data_foundation.qualification.tdx_market_plan import (
    Plan,
    Request,
    bounded_read,
    encoded,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("plan")
    create.add_argument("--sdk-checkout", type=Path, required=True)
    create.add_argument("--endpoint", required=True)
    create.add_argument("--collector-sha256", required=True)
    create.add_argument(
        "--symbols", required=True, help="explicit comma separated SH/SZ stocks"
    )
    create.add_argument("--quote-only", action="store_true")
    create.add_argument(
        "--day", default=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    )
    create.add_argument("--session-window", choices=("AM", "PM"), default="")
    create.add_argument("--directory-pages", type=int, default=2)
    create.add_argument("--daily-count", type=int, default=32)
    create.add_argument(
        "--mode", choices=("after_close", "session"), default="after_close"
    )
    create.add_argument(
        "--output", type=Path, required=True, help="new artifacts directory"
    )
    execute = commands.add_parser("collect")
    execute.add_argument("--live", action="store_true")
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--sdk-checkout", type=Path, required=True)
    execute.add_argument("--candidate", type=Path, required=True)
    execute.add_argument("--host-reviewed-collector-sha256", default="")
    execute.add_argument("--driver", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--calendar", type=Path)
    execute.add_argument("--host-reviewed-calendar-sha256", default="")
    intake = commands.add_parser("intake-quotes")
    intake.add_argument("--source-directory", type=Path, required=True)
    intake.add_argument("--manifest", type=Path, required=True)
    intake.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "intake-quotes":
        from market_data_foundation.qualification.tdx_market_retained import (
            intake_quotes,
        )
        from market_data_foundation.runtime.tdx_watchlist import decode_json

        summary = intake_quotes(
            args.source_directory,
            args.output,
            expected_files=decode_json(bounded_read(args.manifest, 16384)),
        )
        print(
            json.dumps(
                {
                    key: summary[key]
                    for key in (
                        "classification",
                        "original_source_run_id",
                        "new_provider_requests",
                        "original_getquote_calls",
                        "original_ack_receipts",
                        "status_at_inspection",
                    )
                }
            )
        )
        return 0
    if args.command == "plan":
        require(0 <= args.directory_pages <= 25, "directory_page_budget")
        symbols = tuple(args.symbols.split(","))
        requests = []
        for market in (0, 1):
            requests.append(Request("GetCount", market))
            requests.extend(
                Request("GetCode", market, start=1000 * i, count=1000)
                for i in range(args.directory_pages)
            )
            requests.append(Request("GetCount", market))
        requests.extend(
            Request(
                "GetKline",
                1 if s.startswith("sh") else 0,
                s[2:],
                count=args.daily_count,
            )
            for s in symbols
        )
        plan = Plan(
            uuid.uuid4().hex,
            args.endpoint,
            sha256(
                bounded_read(args.sdk_checkout / "hosts.go", 1024 * 1024)
            ).hexdigest(),
            args.collector_sha256,
            tuple(requests),
            symbols,
            source_enabled=not args.quote_only and args.mode != "session",
            mode=args.mode,
            observation_day=args.day,
            session_window=args.session_window,
            quote_rounds=3 if args.mode == "session" else 1,
        )
        from market_data_foundation.qualification.tdx_market_plan import verify_endpoint

        verify_endpoint(plan, args.sdk_checkout)
        run = ArtifactRun(args.output)
        run.write("plan.json", encoded(asdict(plan)))
        print(
            json.dumps(
                {
                    "plan_revision": plan.revision,
                    "run_id": plan.run_id,
                    "source_requests": plan.budgets["source_requests"],
                    "quote_rounds": plan.quote_rounds,
                }
            )
        )
        return 0
    ledger = collect(
        Plan.load(bounded_read(args.plan, 65536)),
        sdk=args.sdk_checkout,
        candidate=args.candidate,
        driver=args.driver,
        output=args.output,
        allow_live=args.live,
        reviewed_collector_sha256=args.host_reviewed_collector_sha256,
        calendar_raw=bounded_read(args.calendar, 16384) if args.calendar else None,
        calendar_sha256=args.host_reviewed_calendar_sha256,
    )
    print(json.dumps({key: ledger[key] for key in ("terminal", "source", "quote")}))
    return 0 if ledger["terminal"] == "COMPLETED_BOUNDED_OBSERVATION" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContractError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit("input_or_host_preflight_rejected") from None
