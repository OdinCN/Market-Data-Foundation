"""Explicit employment operations; no inflation basket or implicit live work."""

import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from market_data_foundation.historical.bls_cpi_direct_live import load_registration_key
from market_data_foundation.historical.bls_timeseries_profile import EMPLOYMENT_PROFILE
from market_data_foundation.historical.cpi_timeseries import month_at, month_number
from market_data_foundation.historical.cpi_timeseries_cli import (
    DEFAULT_CONFIG,
    InterfaceError,
    JsonParser,
    emit,
    error_document,
    execute,
    validate_range,
)
from market_data_foundation.historical.employment_timeseries import build_service
from market_data_foundation.historical.macro_release_records import ReleaseRecords


def build_parser():
    parser = JsonParser(
        description="Employment DEV continuous ranges; explicit BLS binding"
    )
    parser.add_argument("--runtime", type=Path, default=DEFAULT_CONFIG / "runtime.toml")
    parser.add_argument("--secret", type=Path, default=DEFAULT_CONFIG / "secret.toml")
    parser.add_argument("--live-provider", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "update", "query"):
        command = commands.add_parser(name)
        command.add_argument("--start", required=name != "init")
        command.add_argument("--end", required=name != "init")
        if name == "init":
            command.add_argument("--months", type=int, default=240)
        command.add_argument(
            "--series", action="append", choices=EMPLOYMENT_PROFILE.series_ids
        )
        if name == "query":
            command.add_argument("--local-only", action="store_true")
            command.add_argument("--as-of")
            command.add_argument("--columns")
            command.add_argument("--as-of-basis", choices=("observed", "available"))
            command.add_argument("--query-mode", choices=("current", "history"))
    commands.add_parser("status")
    commands.add_parser("prepare-schema")
    commands.add_parser("prepare-release-schema")
    release = commands.add_parser("import-release")
    release.add_argument("--file", type=Path, required=True)
    release.add_argument("--source-url", required=True)
    release.add_argument(
        "--material-format",
        choices=(
            "retained_document",
            "original_http_body",
            "official_summary_projection",
        ),
        default="retained_document",
    )
    records = commands.add_parser("releases")
    records.add_argument("--event-id")
    records.add_argument("--as-of")
    records.add_argument(
        "--read-mode", choices=("event", "source_history"), default="event"
    )
    records.add_argument("--source-version")
    return parser


def main(argv=None, *, service_factory=build_service, clock=lambda: datetime.now(UTC)):
    service = None
    try:
        args = build_parser().parse_args(argv)
        if args.command in {"init", "update", "query"}:
            start, end = args.start, args.end
            if args.command == "init":
                if not 1 <= args.months < 2400:
                    raise InterfaceError("employment_initialization_months_invalid")
                if start is not None and end is None:
                    raise InterfaceError("start_and_end_required")
                now = clock().astimezone(ZoneInfo("Asia/Shanghai"))
                end = end or month_at(now.year * 12 + now.month - 2)
                start = start or month_at(month_number(end) - args.months + 1)
            validate_range(start, end)
            if getattr(args, "as_of", None) is not None:
                try:
                    if datetime.fromisoformat(args.as_of).utcoffset() is None:
                        raise ValueError
                except ValueError:
                    raise InterfaceError("invalid_as_of") from None

        def credential_loader():
            # Standalone CLI opt-in is independent of the public Host package
            # binding; neither silently grants authorization to the other.
            if not args.live_provider:
                raise InterfaceError("employment_live_opt_in_required")
            return load_registration_key(args.secret)

        service = service_factory(
            runtime_path=args.runtime,
            secret_path=args.secret,
            credential_loader=credential_loader,
        )
        if getattr(service, "profile", None) is not EMPLOYMENT_PROFILE:
            raise InterfaceError("bls_timeseries_profile_mismatch")
        if args.command == "status":
            result = service.status()
        elif args.command == "prepare-schema":
            with service.store.operation():
                service.store.ensure_schema()
            result = {"schema": EMPLOYMENT_PROFILE.schema, "prepared": True}
        elif args.command == "prepare-release-schema":
            ReleaseRecords(service.store).prepare()
            result = {
                "schema": EMPLOYMENT_PROFILE.schema,
                "release_schema_prepared": True,
            }
        elif args.command == "import-release":
            with args.file.open("rb") as material:
                raw = material.read(2_000_001)
            result = ReleaseRecords(service.store).import_employment(
                raw, args.source_url, material_format=args.material_format
            )
        elif args.command == "releases":
            result = ReleaseRecords(service.store).read(
                event_id=args.event_id,
                as_of=args.as_of,
                read_mode=args.read_mode,
                source_version=args.source_version,
            )
        else:
            options = {
                "series_ids": tuple(
                    args.series
                    or (
                        EMPLOYMENT_PROFILE.default_query_series_ids
                        if args.command == "query"
                        else EMPLOYMENT_PROFILE.series_ids
                    )
                )
            }
            if args.command == "query":
                result = execute(
                    service,
                    "query",
                    {
                        "start": start,
                        "end": end,
                        "series": options["series_ids"],
                        "local_only": args.local_only,
                        **{
                            key: value
                            for key, value in {
                                "as_of": args.as_of,
                                "as_of_basis": args.as_of_basis,
                                "query_mode": args.query_mode,
                                "columns": args.columns,
                            }.items()
                            if value is not None
                        },
                    },
                )
            else:
                result = getattr(service, args.command + "_range")(
                    start, end, **options
                )
        emit(result)
        return 0
    except Exception as error:
        emit(error_document(error), stream=sys.stderr)
        return 2
    finally:
        if service is not None:
            try:
                service.close()
            except Exception as error:
                emit(error_document(error), stream=sys.stderr)
