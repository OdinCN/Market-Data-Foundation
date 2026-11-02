"""Explicit DEV Claims prepare-schema, collection and local-only read CLI."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from market_data_foundation.historical.claims_collection import build_service
from market_data_foundation.historical.claims_contract import (
    SERIES_IDS,
    twenty_year_range,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "prepare-schema",
            "status",
            "query",
            "init",
            "update",
            "import-release",
            "releases",
        ),
    )
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--series", default=",".join(SERIES_IDS))
    parser.add_argument("--as-of")
    parser.add_argument("--include-releases", action="store_true")
    parser.add_argument(
        "--as-of-basis", choices=("available", "observed"), default="available"
    )
    parser.add_argument("--confirm-dev-write", action="store_true")
    parser.add_argument("--live-provider", action="store_true")
    parser.add_argument("--material")
    parser.add_argument("--source-url")
    parser.add_argument("--obtained-at")
    args = parser.parse_args(argv)
    if (
        args.command in {"prepare-schema", "init", "update", "import-release"}
        and not args.confirm_dev_write
    ):
        parser.error("--confirm-dev-write is required")
    if args.command in {"init", "update"} and not args.live_provider:
        parser.error("--live-provider is required")
    if args.live_provider and args.command not in {"init", "update"}:
        parser.error("--live-provider is only valid for collection")
    if args.as_of and args.command not in {"query", "releases"}:
        parser.error("--as-of is only valid for reads")
    if args.include_releases and args.command != "query":
        parser.error("--include-releases is only valid for query")
    service = None
    try:
        service = build_service(live_opt_in=args.live_provider)
        end = args.end or datetime.now(UTC).date().isoformat()
        start = args.start or (
            twenty_year_range(end)[0]
            if args.command != "update"
            else (datetime.fromisoformat(end) - timedelta(weeks=8)).date().isoformat()
        )
        if args.command == "prepare-schema":
            service.store.ensure_schema()
            result = dict(
                schema=service.store.schema, prepared=True, readonly_role="odin_dba"
            )
        elif args.command == "status":
            result = service.store.status()
        elif args.command in {"releases", "import-release"}:
            from market_data_foundation.historical.claims_release import (
                ClaimsReleaseRecords,
            )

            records = ClaimsReleaseRecords(service.store)
            if args.command == "releases":
                result = records.read(as_of=args.as_of)
            else:
                if not all((args.material, args.source_url, args.obtained_at)):
                    parser.error("material, source-url and obtained-at required")
                result = records.import_material(
                    Path(args.material).read_bytes(),
                    args.source_url,
                    obtained_at=datetime.fromisoformat(args.obtained_at),
                )
        else:
            options = dict(series_ids=tuple(args.series.split(",")))
            if args.command == "query":
                options.update(as_of=args.as_of, as_of_basis=args.as_of_basis)
            result = getattr(service, args.command + "_range")(start, end, **options)
            if args.include_releases:
                from market_data_foundation.historical.claims_release import (
                    ClaimsReleaseRecords,
                )

                result = ClaimsReleaseRecords(service.store).link_result(result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "error": getattr(
                        error, "classification", "claims_" + type(error).__name__
                    )
                }
            )
        )
        return 1
    finally:
        if service is not None:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
