"""PCE CLI: shared dispatch with BEA credentials; no implicit release scheduling."""

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from market_data_foundation.historical.bls_timeseries_profile import PCE_PROFILE
from market_data_foundation.historical.cpi_timeseries_cli import (
    InterfaceError,
    emit,
    error_document,
    execute,
    package_credential_loader,
    selected_series,
    validate_query,
    validate_range,
)
from market_data_foundation.historical.cpi_timeseries_cli import (
    build_parser as shared_parser,
)


def build_parser():
    parser = shared_parser(profile=PCE_PROFILE)
    parser.add_argument(
        "--bea-request-control-path",
        type=Path,
        help="Trusted absolute BEA control path; defaults beside --runtime",
    )
    end = previous_calendar_month()
    year, month = map(int, end.split("-"))
    number = year * 12 + month - 1 - 239
    parser.set_defaults(history_start=f"{number // 12:04d}-{number % 12 + 1:02d}")
    return parser


def previous_calendar_month(now=None):
    now = (now or datetime.now(ZoneInfo("Asia/Shanghai"))).astimezone(
        ZoneInfo("Asia/Shanghai")
    )
    number = now.year * 12 + now.month - 2
    return f"{number // 12:04d}-{number % 12 + 1:02d}"


def main(argv=None, *, service_factory=None, scheduler_factory=None):
    service = None
    try:
        args = build_parser().parse_args(argv)
        if args.command not in {"init", "update", "query", "status"}:
            raise InterfaceError("pce_command_unsupported")
        if args.command == "update" and (args.start is None or args.end is None):
            raise InterfaceError("pce_command_unsupported")
        if args.command == "init":
            args.start = args.start or args.history_start
            args.end = args.end or previous_calendar_month()
        params = {}
        if args.command != "status":
            validate_range(args.start, args.end)
            selected_series(
                vars(args), query=args.command == "query", profile=PCE_PROFILE
            )
            if args.command == "query":
                validate_query(vars(args))
            params = {
                k: v
                for k, v in vars(args).items()
                if k
                in {
                    "start",
                    "end",
                    "series",
                    "basket",
                    "adjustment",
                    "measure",
                    "columns",
                    "local_only",
                    "as_of",
                    "vintage_mode",
                    "query_mode",
                    "as_of_basis",
                }
                and v is not None
            }
        control_path = args.bea_request_control_path
        if control_path is None:
            control_path = (
                args.runtime.parent.resolve() / "bea-timeseries-request-control.json"
            )
        elif not control_path.is_absolute() or ".." in control_path.parts:
            raise InterfaceError("bea_absolute_request_control_path_required")
        control_path = control_path.resolve()
        if service_factory is None:
            from market_data_foundation.historical.pce_timeseries import build_service

            def load_key():
                return package_credential_loader(
                    args.package_store_directory,
                    args.environment_id,
                    secret_path=args.secret,
                    profile=PCE_PROFILE,
                )()

            def service_factory(**options):
                return build_service(
                    **options,
                    credential_loader=load_key,
                    max_transport_attempts=1,
                    request_control_path=control_path,
                )

        service = service_factory(
            runtime_path=args.runtime, secret_path=args.secret, profile=PCE_PROFILE
        )
        if getattr(service, "profile", None) is not PCE_PROFILE:
            raise InterfaceError("bls_timeseries_profile_mismatch")
        emit(execute(service, args.command, params))
        return 0
    except KeyboardInterrupt:
        emit({"status": "STOPPED"})
        return 130
    except Exception as error:
        emit(error_document(error), stream=sys.stderr)
        return 2
    finally:
        if service is not None:
            try:
                service.close()
            except Exception as error:
                emit(error_document(error), stream=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
