"""Local entrypoint; caller explicitly injects a retail service, never
discovers secrets."""

import argparse
import importlib
import json
import sys

from market_data_foundation.web.retail_timeseries import json_value


def main(argv=None, *, service_factory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("initialize", "init", "update", "query", "status")
    )
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--collection-id")
    parser.add_argument(
        "--service-factory", help="explicit trusted local module:callable binding"
    )
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--as-of")
    parser.add_argument("--query-mode", choices=("current", "history"))
    parser.add_argument("--as-of-basis", choices=("observed", "available"))
    args = parser.parse_args(argv)
    try:
        if service_factory is None and args.service_factory:
            module, name = args.service_factory.split(":", 1)
            service_factory = getattr(importlib.import_module(module), name)
        if service_factory is None:
            raise RuntimeError
        service = service_factory()
        if args.command == "status":
            result = service.status()
        elif args.command == "query":
            result = service.query_range(
                args.start,
                args.end,
                local_only=args.local_only,
                as_of=args.as_of,
                query_mode=args.query_mode,
                as_of_basis=args.as_of_basis,
            )
        else:
            method = service.update if args.command == "update" else service.initialize
            result = method(args.start, args.end, collection_id=args.collection_id)
        print(json.dumps(json_value(result)))
        return 2 if result["status"] in {"ERROR", "PARTIAL"} else 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "classification": getattr(
                        error, "classification", "retail_service_not_bound"
                    ),
                }
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
