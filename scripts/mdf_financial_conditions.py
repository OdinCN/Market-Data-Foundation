"""Operator data-layer CLI. No task scheduler, process control or Web mutation."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from market_data_foundation.historical.financial_daily import (
    SERIES_IDS,
    FinancialError,
    permitted_series,
    registry,
    require_source_permission,
)  # noqa: E402
from market_data_foundation.historical.financial_daily_runtime import (
    bound_fred_credential,
    build_service,  # noqa: E402
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation", choices=["registry", "migrate", "collect", "query", "recover"]
    )
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument(
        "--series", nargs="+", choices=SERIES_IDS, default=list(SERIES_IDS)
    )
    parser.add_argument("--as-of")
    parser.add_argument(
        "--basis", choices=["available", "observed"], default="available"
    )
    parser.add_argument("--batch-id")
    parser.add_argument("--opt-in-dev-write", action="store_true")
    args = parser.parse_args()
    service = None
    try:
        if args.operation == "registry":
            result = registry()
        else:
            if (
                args.operation in {"migrate", "collect", "recover"}
                and not args.opt_in_dev_write
            ):
                raise FinancialError("financial_dev_write_opt_in_required")
            if args.operation == "collect":
                # Permission is checked before credentials, source HTTP or PG.
                if not permitted_series(tuple(args.series)):
                    require_source_permission(args.series[0])
            service = build_service(
                write=args.operation in {"collect", "recover"},
                live_opt_in=args.operation == "collect",
                credential_loader=bound_fred_credential,
                selected=tuple(args.series),
            )
            if args.operation == "migrate":
                result = service.store.migrate()
            elif args.operation == "recover":
                result = service.recover(args.batch_id)
            elif args.operation == "query":
                result = service.query(
                    args.start,
                    args.end,
                    selected=tuple(args.series),
                    as_of=datetime.fromisoformat(args.as_of) if args.as_of else None,
                    basis=args.basis,
                )
            else:
                result = service.collect(
                    args.start, args.end, selected=tuple(args.series)
                )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except FinancialError as error:
        print(
            json.dumps(
                {
                    "state": "blocked"
                    if "permission" in error.classification
                    else "failed",
                    "classification": error.classification,
                },
                ensure_ascii=False,
            )
        )
        return 2
    except ValueError, TypeError:
        print(
            json.dumps(
                {"state": "failed", "classification": "financial_cli_arguments_invalid"}
            )
        )
        return 2
    finally:
        if service:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
