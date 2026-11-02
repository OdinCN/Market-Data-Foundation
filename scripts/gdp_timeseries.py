"""DEV GDP quarterly CLI. Schema and collection writes require explicit opt-in."""

import argparse
import json

from market_data_foundation.historical.gdp_contract import SERIES_IDS, GdpSourceError
from market_data_foundation.historical.gdp_store import GdpStorageError
from market_data_foundation.historical.gdp_timeseries import (
    GdpTimeSeriesError,
    build_service,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("status", "query", "prepare", "init", "update")
    )
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--series", default=",".join(SERIES_IDS))
    parser.add_argument("--as-of")
    parser.add_argument("--as-of-basis", choices=("observed", "available"))
    parser.add_argument("--allow-upstream", action="store_true")
    parser.add_argument("--confirm-dev-write", action="store_true")
    args = parser.parse_args(argv)
    if args.command in ("prepare", "init", "update") and not args.confirm_dev_write:
        parser.error("--confirm-dev-write is required")
    if args.command in ("query", "init", "update") and (not args.start or not args.end):
        parser.error("--start and --end use YYYY-Qn and are required")
    if args.command != "query" and (
        args.as_of or args.as_of_basis or args.allow_upstream
    ):
        parser.error("query options require the query command")
    service = None
    try:
        service = build_service()
        series = tuple(args.series.split(","))
        if args.command == "status":
            result = service.status()
        elif args.command == "prepare":
            service.store.ensure_schema()
            with service.store.connection.transaction():
                role = service.store.connection.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname='odin_dba'"
                ).fetchone()
                if role != (1,):
                    raise GdpStorageError("gdp_timeseries_readonly_role_missing")
                schema = service.store.schema
                for sql in (
                    f"GRANT USAGE ON SCHEMA {schema} TO odin_dba",
                    f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO odin_dba",
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
                    "GRANT SELECT ON TABLES TO odin_dba",
                ):
                    service.store.connection.execute(sql)
            result = {"schema": schema, "prepared": True, "readonly_role": "odin_dba"}
        elif args.command == "query":
            result = service.query_range(
                args.start,
                args.end,
                series_ids=series,
                as_of=args.as_of,
                as_of_basis=args.as_of_basis,
                allow_upstream=args.allow_upstream,
            )
        else:
            operation = (
                service.init_range if args.command == "init" else service.update_range
            )
            result = operation(args.start, args.end, series_ids=series)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (GdpTimeSeriesError, GdpStorageError, GdpSourceError) as error:
        print(
            json.dumps(
                {
                    "error": error.classification,
                    "provider_requests": getattr(error, "provider_requests", 0),
                    "provider_attempts": getattr(error, "provider_attempts", 0),
                }
            )
        )
        return 1
    finally:
        if service is not None:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
