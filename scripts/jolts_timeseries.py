"""Explicit JOLTS bootstrap and local query; collection uses the manual Host."""

import sys
from pathlib import Path

from market_data_foundation.historical.cpi_timeseries_cli import (
    JsonParser,
    emit,
    error_document,
    execute,
)
from market_data_foundation.historical.jolts_timeseries import (
    build_read_service,
    build_service,
)


def main(argv=None):
    parser = JsonParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--secret", required=True, type=Path)
    parser.add_argument("--state-directory", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare-schema")
    commands.add_parser("status")
    query = commands.add_parser("query")
    query.add_argument("--start", required=True)
    query.add_argument("--end", required=True)
    query.add_argument("--series")
    query.add_argument("--columns")
    query.add_argument("--as-of")
    query.add_argument("--as-of-basis", choices=("observed", "available"))
    service = None
    try:
        args = parser.parse_args(argv)
        if args.command == "prepare-schema":
            service = build_service(
                runtime_path=args.runtime,
                secret_path=args.secret,
                credential_loader=lambda: None,
            )
            with service.store.operation():
                service.store.ensure_schema()
            result = {"schema": service.profile.schema, "prepared": True}
        else:
            service = build_read_service(
                runtime_path=args.runtime, secret_path=args.secret
            )
            params = (
                {
                    **{
                        k: v
                        for k, v in vars(args).items()
                        if k
                        in {"start", "end", "series", "columns", "as_of", "as_of_basis"}
                        and v is not None
                    },
                    "local_only": True,
                }
                if args.command == "query"
                else {}
            )
            result = execute(service, args.command, params)
        emit(result)
        return 0
    except Exception as error:
        emit(error_document(error), stream=sys.stderr)
        return 2
    finally:
        if service is not None:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
