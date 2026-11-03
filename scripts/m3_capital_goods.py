"""Explicit M3 DEV migration, collection/readback and current/history queries."""

import argparse
import json
from pathlib import Path

from market_data_foundation.historical.cpi_request_control import CpiRequestControl
from market_data_foundation.historical.m3_capital_goods_timeseries import build_service
from market_data_foundation.qualification.harness import classify_exception


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("prepare-schema", "query", "collect", "replay")
    )
    parser.add_argument("--material", type=Path)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--dataset", choices=("advm3", "m3"), default="m3")
    parser.add_argument("--as-of")
    parser.add_argument(
        "--basis", choices=("observed", "available"), default="available"
    )
    parser.add_argument("--allow-upstream", action="store_true")
    parser.add_argument("--confirm-dev-write", action="store_true")
    parser.add_argument("--request-control", type=Path)
    args = parser.parse_args(argv)
    if args.command != "query" and not args.confirm_dev_write:
        parser.error("--confirm-dev-write is required")
    if args.command in {"query", "collect"} and (not args.start or not args.end):
        parser.error("--start and --end are required")
    if args.command == "replay" and args.material is None:
        parser.error("--material is required for replay")
    if args.allow_upstream and args.command != "query":
        parser.error("--allow-upstream requires query")
    if (
        args.allow_upstream or args.command == "collect"
    ) and args.request_control is None:
        parser.error(
            "--request-control must select the existing Census admission state"
        )
    if args.as_of and (args.allow_upstream or args.command != "query"):
        parser.error("--as-of requires a local query")
    service = None
    try:
        service = build_service(
            runtime_path=Path("/srv/market-data-foundation/config/local/runtime.toml"),
            secret_path=Path("/srv/market-data-foundation/config/local/secret.toml"),
            package_directory=Path(
                "/srv/market-data-foundation/config/local/package-config-dev"
            ),
            request_control=None
            if args.request_control is None
            else CpiRequestControl(args.request_control),
            allow_live=args.allow_upstream or args.command == "collect",
        )
        if args.command == "prepare-schema":
            service.store.ensure_schema()
            result = {"schema_prepared": True}
        elif args.command == "replay":
            from market_data_foundation.historical.m3_capital_goods_store import M3Batch

            result = service.import_batch(
                M3Batch.from_raw(args.material.read_bytes()), confirm_write=True
            )
        elif args.command == "collect":
            result = service.collect_range(
                args.start, args.end, dataset=args.dataset, confirm_write=True
            )
        else:
            result = service.query_range(
                args.start,
                args.end,
                dataset=args.dataset,
                as_of=args.as_of,
                basis=args.basis,
                allow_upstream=args.allow_upstream,
            )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"success": False, "exception": classify_exception(error)}))
        return 1
    finally:
        if service is not None:
            service.store.connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
