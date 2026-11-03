"""Direct CLI and shared, JSON-only CPI time-series operations.

No import-time configuration reads, network access, DDL, or old Job/Web routing.
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    PCE_PROFILE,
    validate_profile,
)

DEFAULT_CONFIG = Path("/srv/market-data-foundation/config/local")
DEFAULT_STATE = Path("artifacts/cpi-timeseries/schedule.json")
SERIES_IDS = CPI_PROFILE.series_ids
SERIES = {
    (metadata["indicator"], metadata["seasonal_adjustment"].lower()): series
    for series, metadata in CPI_PROFILE.series_metadata.items()
}
MEASURES = {
    "index": ("index_value", "index"),
    "native": ("value", "per_series_value_unit"),
    "mom": ("mom_pct", "percent"),
    "yoy": ("yoy_pct", "percent"),
    **{
        f"change_{months}m_ratio": (f"change_{months}m_ratio", "ratio")
        for months in (1, 3, 6, 12)
    },
}
MEASURES.update(
    {f"annualized_{k}m_ratio": (f"annualized_{k}m_ratio", "ratio") for k in (3, 6)}
)
VINTAGE_MODES = ("observed", "available", "official")


class InterfaceError(ValueError):
    def __init__(self, classification):
        self.classification = classification
        super().__init__(classification)


class JsonParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse messages may contain user input, so use a fixed classification.
        raise InterfaceError("invalid_arguments")


def selected_series(params, *, query=False, profile=CPI_PROFILE):
    profile = validate_profile(profile)
    explicit = params.get("series")
    basket = params.get("basket")
    adjustment = params.get("adjustment")
    if explicit is not None:
        if basket is not None or adjustment is not None:
            raise InterfaceError("series_and_basket_are_mutually_exclusive")
        if isinstance(explicit, str):
            explicit = explicit.split(",")
        if not isinstance(explicit, (tuple, list)):
            raise InterfaceError("invalid_series")
        values = []
        for item in explicit:
            if not isinstance(item, str):
                raise InterfaceError("invalid_series")
            values.extend(item.split(","))
        values = tuple(dict.fromkeys(value.strip() for value in values))
        if not values or not set(values) <= set(profile.series_ids):
            raise InterfaceError("unsupported_series")
        return values
    if profile.key in {"employment", "consumption", "jolts"}:
        if basket is not None or adjustment is not None:
            if basket != "all" or adjustment != "all":
                raise InterfaceError(profile.key + "_series_filter_required")
        return (
            profile.default_query_series_ids
            if query and basket is None
            else profile.series_ids
        )
    basket = ("headline" if query else "all") if basket is None else basket
    adjustment = ("sa" if query else "all") if adjustment is None else adjustment
    if not isinstance(basket, str) or not isinstance(adjustment, str):
        raise InterfaceError("invalid_series_selection")
    basket, adjustment = basket.lower(), adjustment.lower()
    if basket == "overall":
        basket = "headline"
    baskets = basket.split(",")
    if (
        not baskets
        or not set(baskets) <= {*profile.baskets, "all"}
        or ("all" in baskets and len(baskets) != 1)
    ):
        raise InterfaceError("invalid_basket")
    if adjustment not in {"sa", "nsa", "all"}:
        raise InterfaceError("invalid_adjustment")
    selected = tuple(
        series
        for series, metadata in profile.series_metadata.items()
        if ("all" in baskets or metadata["indicator"] in baskets)
        and adjustment in {"all", metadata["seasonal_adjustment"].lower()}
    )
    if not selected:
        raise InterfaceError("unsupported_series_selection")
    return selected


def require_parameters(params, allowed):
    if not isinstance(params, dict) or not set(params) <= set(allowed):
        raise InterfaceError("unsupported_parameters")


def validate_range(start, end):
    from market_data_foundation.historical.cpi_timeseries import month_number

    if not isinstance(start, str) or not isinstance(end, str):
        raise InterfaceError("start_and_end_required")
    begin, finish = month_number(start), month_number(end)
    if begin > finish or finish - begin >= 2400 or begin < 1901 * 12:
        raise InterfaceError("cpi_timeseries_range_invalid")


def validate_query(params):
    measure = params.get("measure", "index")
    if not isinstance(measure, str) or measure not in MEASURES:
        raise InterfaceError("invalid_measure")
    if type(params.get("local_only", False)) is not bool:
        raise InterfaceError("invalid_local_only")
    for key in ("vintage_mode", "as_of_basis", "query_mode"):
        if params.get(key) is not None:
            choices = {"current", "history"} if key == "query_mode" else VINTAGE_MODES
            if not isinstance(params[key], str) or params[key] not in choices:
                raise InterfaceError("invalid_" + key)
    query_mode = params.get("query_mode") or (
        "history"
        if params.get("as_of") is not None
        or params.get("vintage_mode") == "available"
        or params.get("as_of_basis") == "available"
        else "current"
    )
    if query_mode not in {"current", "history"}:
        raise InterfaceError("invalid_query_mode")
    mode = (
        params.get("as_of_basis")
        or params.get("vintage_mode")
        or (
            "available"
            if query_mode == "history" or params.get("as_of") is not None
            else "observed"
        )
    )
    if not isinstance(mode, str) or mode not in VINTAGE_MODES:
        raise InterfaceError("invalid_vintage_mode")
    if mode == "official":
        raise InterfaceError("cpi_timeseries_official_vintage_unsupported")
    if (
        params.get("as_of_basis")
        and params.get("vintage_mode")
        and params["as_of_basis"] != params["vintage_mode"]
    ):
        raise InterfaceError("conflicting_as_of_basis")
    as_of = params.get("as_of")
    if as_of is not None:
        try:
            if not isinstance(as_of, str):
                raise ValueError
            if datetime.fromisoformat(as_of).utcoffset() is None:
                raise ValueError
        except TypeError, ValueError, OverflowError:
            raise InterfaceError("invalid_as_of") from None


def validate_signal_shapes(notices):
    """Reject malformed JSON types before the scheduler validates source evidence."""
    if not isinstance(notices, list) or not 1 <= len(notices) <= 100:
        raise InterfaceError("invalid_signal_notices")
    strings = {
        "evidence_url",
        "kind",
        "scope",
        "start_month",
        "end_month",
        "effective_at",
        "evidence_text",
    }
    expected_fields = {"series_id", "month", "previous_value", "index_value"}
    for notice in notices:
        require_parameters(
            notice,
            strings
            | {
                "verified",
                "series_ids",
                "expected_points",
                "discovered_signal_id",
                "scope_mapping",
            },
        )
        if "discovered_signal_id" in notice and not isinstance(
            notice["discovered_signal_id"], str
        ):
            raise InterfaceError("invalid_signal_notice")
        if any(not isinstance(notice.get(key), str) for key in strings):
            raise InterfaceError("invalid_signal_notice")
        if type(notice.get("verified")) is not bool:
            raise InterfaceError("invalid_signal_notice")
        if "scope_mapping" in notice:
            mapping = notice["scope_mapping"]
            require_parameters(
                mapping, {"verified", "series_ids", "start_month", "end_month"}
            )
            if (
                type(mapping.get("verified")) is not bool
                or not isinstance(mapping.get("series_ids"), list)
                or any(not isinstance(item, str) for item in mapping["series_ids"])
                or not isinstance(mapping.get("start_month"), str)
                or not isinstance(mapping.get("end_month"), str)
            ):
                raise InterfaceError("invalid_signal_scope_mapping")
        series = notice.get("series_ids")
        if not isinstance(series, list) or any(
            not isinstance(item, str) for item in series
        ):
            raise InterfaceError("invalid_signal_notice")
        expected = notice.get("expected_points", [])
        if not isinstance(expected, list):
            raise InterfaceError("invalid_signal_notice")
        for point in expected:
            require_parameters(point, expected_fields)
            if any(not isinstance(point.get(key), str) for key in expected_fields):
                raise InterfaceError("invalid_signal_notice")


def execute(service, action, params=None, *, scheduler=None):
    """Shared dispatcher. The injected service owns all source/storage behavior."""
    params = {} if params is None else params
    common = {"start", "end", "series", "basket", "adjustment"}
    if action == "status":
        require_parameters(params, set())
        return service.status()
    if action in {"schedule/status", "schedule/refresh", "schedule/due"}:
        require_parameters(params, set())
        if scheduler is None:
            raise InterfaceError("schedule_not_configured")
        return {
            "schedule/status": scheduler.status,
            "schedule/refresh": scheduler.refresh_calendar,
            "schedule/due": scheduler.due_check,
        }[action]()
    if action in {
        "signals/status",
        "signals/refresh",
        "signals/import",
        "signals/dismiss",
    }:
        allowed = (
            {"notices"}
            if action == "signals/import"
            else ({"signal_id", "reason"} if action == "signals/dismiss" else set())
        )
        require_parameters(params, allowed)
        if scheduler is None:
            raise InterfaceError("schedule_not_configured")
        if action == "signals/status":
            return scheduler.status()
        if action == "signals/refresh":
            return scheduler.refresh_signals()
        if action == "signals/dismiss":
            for key in ("signal_id", "reason"):
                if not isinstance(params.get(key), str) or not params[key].strip():
                    raise InterfaceError("invalid_signal_dismiss_parameters")
            return scheduler.dismiss_signal(params["signal_id"], params["reason"])
        notices = params.get("notices")
        validate_signal_shapes(notices)
        return scheduler.import_signals(notices)
    if action not in {"init", "update", "query", "confirm-availability"}:
        raise InterfaceError("unknown_operation")
    allowed = common | (
        {
            "measure",
            "columns",
            "local_only",
            "as_of",
            "vintage_mode",
            "query_mode",
            "as_of_basis",
        }
        if action == "query"
        else set()
    )
    require_parameters(params, allowed)
    start, end = params.get("start"), params.get("end")
    if action == "update" and start is None and end is None:
        if set(params) - {"start", "end"}:
            raise InterfaceError("scheduled_update_does_not_accept_series_filter")
        return execute(service, "schedule/due", scheduler=scheduler)
    validate_range(start, end)
    selected = selected_series(
        params,
        query=action == "query",
        profile=getattr(service, "profile", CPI_PROFILE),
    )
    if action == "init":
        return service.init_range(start, end, series_ids=selected)
    if action == "update":
        return service.update_range(start, end, series_ids=selected)
    if action == "confirm-availability":
        return service.confirm_current_availability(start, end, series_ids=selected)
    measure = params.get(
        "measure",
        "native"
        if getattr(service, "profile", CPI_PROFILE).key == "consumption"
        else "index",
    )
    validate_query(dict(params, measure=measure))
    profile = getattr(service, "profile", CPI_PROFILE)
    if measure == "native" and profile.key != "consumption":
        raise InterfaceError("invalid_measure")
    if profile.key == "consumption" and measure != "native":
        raise InterfaceError("consumption_measure_not_supported")
    if measure.startswith("annualized_") and profile is not PCE_PROFILE:
        raise InterfaceError("invalid_measure")
    from market_data_foundation.historical.macro_research_fields import (
        project_columns,
        selected_columns,
    )

    columns = selected_columns(params.get("columns"), profile.key)
    if profile.key == "jolts" and measure != "index":
        raise InterfaceError("jolts_measure_not_supported")
    local_only = params.get("local_only", profile.key == "jolts")
    as_of = params.get("as_of")
    query_mode = params.get("query_mode") or (
        "history"
        if as_of is not None
        or params.get("vintage_mode") == "available"
        or params.get("as_of_basis") == "available"
        else "current"
    )
    basis = params.get("as_of_basis") or params.get("vintage_mode")
    options = {"query_mode": query_mode, "as_of_basis": basis}
    result = service.query_range(
        start,
        end,
        series_ids=selected,
        allow_upstream=not local_only and as_of is None and query_mode == "current",
        as_of=as_of,
        vintage_mode=basis
        or ("available" if query_mode == "history" or as_of is not None else None),
        **options,
    )
    if getattr(service, "profile", CPI_PROFILE).key in {
        "employment",
        "consumption",
        "jolts",
    }:
        if measure != ("native" if profile.key == "consumption" else "index"):
            raise InterfaceError(profile.key + "_measure_not_supported")
        if profile.key == "consumption":
            result.update(measure=measure, unit="per_series_value_unit")
        return project_columns(result, columns, profile.key)
    field, unit = MEASURES[measure]
    points = [{**point, "value": point.get(field)} for point in result["points"]]
    return project_columns(
        {
            **result,
            "points": points,
            "measure": measure,
            "query_mode": query_mode,
            "as_of_basis": result.get(
                "as_of_basis",
                basis
                or (
                    "available"
                    if query_mode == "history" or as_of is not None
                    else "observed"
                ),
            ),
            "unit": unit,
            "measure_complete": result["complete"]
            and all(point["value"] is not None for point in points),
        },
        columns,
        profile.key,
    )


def error_document(error):
    """Never expose arbitrary exception messages, config values, or credentials."""
    from market_data_foundation.historical.cpi_release_schedule import CpiScheduleError
    from market_data_foundation.historical.cpi_revision_signals import CpiSignalError
    from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError

    trusted = isinstance(
        error, (InterfaceError, CpiTimeSeriesError, CpiScheduleError, CpiSignalError)
    )
    classification = error.classification if trusted else "internal_error"
    return {
        "status": "ERROR",
        "classification": classification,
        "provider_attempts": getattr(error, "provider_attempts", 0) if trusted else 0,
    }


def build_parser(*, profile=CPI_PROFILE):
    profile = validate_profile(profile)
    parser = JsonParser(
        description=(
            "MDF-DEV CPI time series: direct service and loopback HTTP"
            if profile is CPI_PROFILE
            else "MDF-DEV PCE time series: init/update/query/status; "
            "serve/schedule/signals/confirm-availability unsupported"
            if profile is PCE_PROFILE
            else f"MDF-DEV {profile.key.upper()} time series: "
            "init/update/query/status/confirm-availability; "
            "serve/schedule/signals unsupported"
        )
    )
    parser.add_argument("--runtime", type=Path, default=DEFAULT_CONFIG / "runtime.toml")
    parser.add_argument("--secret", type=Path, default=DEFAULT_CONFIG / "secret.toml")
    parser.add_argument(
        "--schedule-state",
        type=Path,
        default=Path(f"artifacts/{profile.key}-timeseries/schedule.json"),
    )
    parser.add_argument("--package-store-directory", type=Path)
    parser.add_argument("--environment-id")
    now = datetime.now()
    first_month = now.year * 12 + now.month - 1 - 239
    parser.add_argument(
        "--history-start",
        default=(
            "1999-01"
            if profile is CPI_PROFILE
            else f"{first_month // 12:04d}-{first_month % 12 + 1:02d}"
        ),
        help="Requested history start; PPI/PCE default to a 240-month request window",
    )
    parser.add_argument("--signal-poll-seconds", type=int, default=3600)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "update", "query", "confirm-availability"):
        command = commands.add_parser(name)
        required = name != "update" and not (profile is PCE_PROFILE and name == "init")
        command.add_argument("--start", required=required)
        command.add_argument("--end", required=required)
        command.add_argument("--series", action="append")
        command.add_argument(
            "--basket",
            help="comma-separated baskets: " + ",".join(profile.baskets) + ",all",
        )
        command.add_argument(
            "--adjustment", type=str.lower, choices=("sa", "nsa", "all")
        )
        if name == "query":
            command.add_argument("--measure", choices=tuple(MEASURES), default="index")
            command.add_argument(
                "--columns",
                help="comma-separated response columns; omitted preserves all fields",
            )
            command.add_argument("--local-only", action="store_true")
            command.add_argument("--as-of")
            command.add_argument("--query-mode", choices=("current", "history"))
            command.add_argument("--as-of-basis", choices=("available", "observed"))
            command.add_argument("--vintage-mode", choices=VINTAGE_MODES)
    commands.add_parser("status")
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=18765)
    schedule = commands.add_parser("schedule")
    schedule_commands = schedule.add_subparsers(dest="schedule_command", required=True)
    for name in ("refresh", "due", "status"):
        schedule_commands.add_parser(name)
    imported = schedule_commands.add_parser("import")
    imported.add_argument("source_file", type=Path)
    imported.add_argument("--acquired-at", required=True)
    worker = schedule_commands.add_parser("worker")
    worker.add_argument("--poll-seconds", type=int, default=5)
    signals = commands.add_parser("signals")
    signal_commands = signals.add_subparsers(dest="signals_command", required=True)
    for name in ("refresh", "status"):
        signal_commands.add_parser(name)
    signal_import = signal_commands.add_parser("import")
    signal_import.add_argument("source_file", type=Path)
    signal_dismiss = signal_commands.add_parser("dismiss")
    signal_dismiss.add_argument("--signal-id", required=True)
    signal_dismiss.add_argument("--reason", required=True)
    return parser


def emit(document, stream=None):
    print(
        json.dumps(document, ensure_ascii=False, sort_keys=True),
        file=stream,
        flush=True,
    )


class WorkerService:
    """Open a fresh service only for actual work; recover from idle PG disconnects."""

    def __init__(self, factory, *, profile=CPI_PROFILE):
        self.factory = factory
        self.profile = validate_profile(profile)

    def _call(self, operation, *args, **kwargs):
        service = self.factory()
        try:
            if getattr(service, "profile", CPI_PROFILE) is not self.profile:
                from market_data_foundation.historical.cpi_timeseries import (
                    CpiTimeSeriesError,
                )

                raise CpiTimeSeriesError("bls_timeseries_profile_mismatch")
            result = getattr(service, operation)(*args, **kwargs)
            if operation in {"update_range", "init_range"} and hasattr(
                self, "material_capture"
            ):
                result["release_material"] = self.material_capture(
                    service, args[0], args[1], result
                )
            return result
        finally:
            service.close()

    def update_range(self, *args, **kwargs):
        return self._call("update_range", *args, **kwargs)

    def init_range(self, *args, **kwargs):
        return self._call("init_range", *args, **kwargs)

    def query_range(self, *args, **kwargs):
        return self._call("query_range", *args, **kwargs)

    def withdraw_range(self, *args, **kwargs):
        return self._call("withdraw_range", *args, **kwargs)

    def confirm_current_availability(self, *args, **kwargs):
        return self._call("confirm_current_availability", *args, **kwargs)

    def status(self):
        return self._call("status")


def run_worker(scheduler, *, poll_seconds=5, sleep=time.sleep, emit_result=emit):
    """Continue across monthly releases; retry timing belongs to durable scheduler."""
    if type(poll_seconds) is not int or not 5 <= poll_seconds <= 3600:
        raise InterfaceError("poll_seconds_out_of_range")
    while True:
        try:
            emit_result(
                scheduler.tick()
                if hasattr(scheduler, "tick")
                else scheduler.due_check()
            )
        except Exception as error:
            emit_result(error_document(error))
        sleep(min(poll_seconds, 5))


def import_calendar_file(scheduler, path, acquired_at):
    from market_data_foundation.historical.cpi_release_schedule import (
        MAX_CALENDAR_BYTES,
    )

    try:
        acquired = datetime.fromisoformat(acquired_at)
        if acquired.tzinfo is None:
            raise ValueError
    except TypeError, ValueError:
        raise InterfaceError("calendar_acquired_at_aware_timestamp_required") from None
    try:
        with Path(path).open("rb") as source:
            raw = source.read(MAX_CALENDAR_BYTES + 1)
    except OSError:
        raise InterfaceError("calendar_import_file_unavailable") from None
    if len(raw) > MAX_CALENDAR_BYTES:
        raise InterfaceError("calendar_payload_invalid")
    return scheduler.import_calendar(raw, acquired_at=acquired)


def read_signal_file(path):
    """Bounded JSON only; never accept arbitrary HTTP filesystem paths."""
    try:
        with Path(path).open("rb") as source:
            raw = source.read(65537)
    except OSError:
        raise InterfaceError("signal_import_file_unavailable") from None
    if len(raw) > 65536:
        raise InterfaceError("signal_payload_too_large")
    try:
        document = json.loads(
            raw,
            object_pairs_hook=unique_json_object,
            parse_constant=reject_json_constant,
        )
    except InterfaceError:
        raise
    except ValueError, UnicodeError, RecursionError:
        raise InterfaceError("invalid_json") from None
    require_parameters(document, {"notices"})
    return document


def unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InterfaceError("duplicate_parameter")
        result[key] = value
    return result


def reject_json_constant(value):
    raise InterfaceError("invalid_json")


def package_credential_loader(
    directory,
    environment_id,
    *,
    secret_path=DEFAULT_CONFIG / "secret.toml",
    credential_loader=None,
    profile=CPI_PROFILE,
):
    """Reload the binding each fetch; explicit refs precede inherited keys."""
    profile = validate_profile(profile)
    from market_data_foundation.historical.bls_cpi_direct_live import (
        load_registration_key,
    )
    from market_data_foundation.web.package_catalog import PackageCatalog
    from market_data_foundation.web.package_store import LocalPackageStore
    from market_data_foundation.web.packages import PackageHost

    if directory is None or not environment_id:
        raise InterfaceError(f"{profile.key}_package_credential_binding_required")

    def inherited_key(current_environment, package_id, source_id):
        if (
            current_environment != environment_id
            or package_id != "macro"
            or source_id != profile.provider
        ):
            return None
        if profile.provider == "BEA":
            import tomllib

            try:
                with Path(secret_path).open("rb") as stream:
                    value = tomllib.load(stream)["bea"]["api_key"]
                if type(value) is not str or not value.strip():
                    raise ValueError
                return value
            except Exception:
                raise InterfaceError("bea_credential_missing") from None
        if profile.provider == "BLS":
            return load_registration_key(Path(secret_path))
        return None

    packages = PackageHost(
        LocalPackageStore(directory, PackageCatalog()),
        environment_id,
        credential_loader=(
            inherited_key if credential_loader is None else credential_loader
        ),
    )

    def load():
        record = packages.store.read(environment_id, "macro")
        if record is None or profile.key not in record.enabled_capability_ids:
            raise InterfaceError(f"{profile.key}_capability_disabled")
        value = packages.load_credential("macro", profile.provider, record=record)
        if value is None:
            raise InterfaceError(f"{profile.provider.lower()}_credential_missing")
        return value

    return load


def persisted_history_start(schedule_path, default="1999-01"):
    """Keep an existing scheduler's baseline without resetting its progress."""
    path = Path(schedule_path)
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text())["history_start"]
        validate_range(value, value)
        return value
    except OSError, ValueError, KeyError, TypeError:
        raise InterfaceError("schedule_state_invalid") from None


def main(
    argv=None, *, service_factory=None, scheduler_factory=None, profile=CPI_PROFILE
):
    if profile is PCE_PROFILE:
        from market_data_foundation.historical.pce_timeseries_cli import (
            main as pce_main,
        )

        return pce_main(
            argv, service_factory=service_factory, scheduler_factory=scheduler_factory
        )
    service = None
    try:
        profile = validate_profile(profile)
        args = build_parser(profile=profile).parse_args(argv)
        if profile is not CPI_PROFILE:
            if args.command in {"serve", "schedule", "signals"} or (
                args.command == "update" and args.start is None and args.end is None
            ):
                raise InterfaceError("ppi_command_unsupported")
            if args.schedule_state.resolve() == DEFAULT_STATE.resolve():
                raise InterfaceError("ppi_schedule_state_conflicts_with_cpi")
        validate_range(args.history_start, args.history_start)
        if not 60 <= args.signal_poll_seconds <= 86400:
            raise InterfaceError("signal_poll_seconds_out_of_range")
        if args.command in {"init", "update", "query", "confirm-availability"}:
            if (
                args.command != "update"
                or args.start is not None
                or args.end is not None
            ):
                validate_range(args.start, args.end)
            selected_series(vars(args), query=args.command == "query", profile=profile)
            if args.command == "update" and args.start is None and args.end is None:
                if any(
                    getattr(args, key) is not None
                    for key in ("series", "basket", "adjustment")
                ):
                    raise InterfaceError(
                        "scheduled_update_does_not_accept_series_filter"
                    )
            if args.command == "query":
                validate_query(vars(args))
        if args.command == "serve" and not 1024 <= args.port <= 65535:
            raise InterfaceError("port_out_of_range")
        if args.command == "schedule" and args.schedule_command == "worker":
            if not 5 <= args.poll_seconds <= 3600:
                raise InterfaceError("poll_seconds_out_of_range")
        if service_factory is None:
            from market_data_foundation.historical.cpi_timeseries import build_service

            def service_factory(**options):
                def load_key():
                    return package_credential_loader(
                        args.package_store_directory,
                        args.environment_id,
                        secret_path=args.secret,
                        profile=profile,
                    )()

                options.setdefault("profile", profile)
                return build_service(
                    **options,
                    credential_loader=load_key,
                    max_transport_attempts=1,
                )

        default_scheduler = scheduler_factory is None
        if default_scheduler:
            from market_data_foundation.historical.cpi_release_schedule import (
                CpiReleaseScheduler,
            )

            scheduler_factory = CpiReleaseScheduler
        # Calendar/signal-only commands need neither credentials nor a connection.
        calendar_only = args.command == "signals" or (
            args.command == "schedule"
            and args.schedule_command
            in {
                "refresh",
                "status",
                "import",
            }
        )
        worker_mode = args.command == "schedule" and args.schedule_command == "worker"
        service_options = {"runtime_path": args.runtime, "secret_path": args.secret}
        if profile is not CPI_PROFILE:
            service_options["profile"] = profile
        if not calendar_only and not worker_mode:
            service = service_factory(**service_options)
            if (
                profile is not CPI_PROFILE
                and getattr(service, "profile", None) is not profile
            ):
                raise InterfaceError("bls_timeseries_profile_mismatch")
        scheduler_service = (
            WorkerService(lambda: service_factory(**service_options), profile=profile)
            if worker_mode
            else service
        )
        scheduler = scheduler_factory(
            scheduler_service,
            args.schedule_state,
            history_start=(
                persisted_history_start(args.schedule_state, args.history_start)
                if profile is CPI_PROFILE
                else args.history_start
            ),
            signal_poll_seconds=args.signal_poll_seconds,
            **({"auto_mode": True} if worker_mode else {}),
            **(
                {"profile": profile}
                if default_scheduler or profile is not CPI_PROFILE
                else {}
            ),
        )
        if args.command == "serve":
            from market_data_foundation.historical.cpi_timeseries_http import serve

            serve(service, scheduler=scheduler, port=args.port)
            return 0
        if args.command == "signals":
            if args.signals_command == "import":
                params = read_signal_file(args.source_file)
            elif args.signals_command == "dismiss":
                params = {"signal_id": args.signal_id, "reason": args.reason}
            else:
                params = {}
            result = execute(
                service, "signals/" + args.signals_command, params, scheduler=scheduler
            )
        elif args.command == "schedule":
            if args.schedule_command == "worker":
                from market_data_foundation.historical.cpi_auto_strategy import (
                    CpiAutoStrategy,
                )

                scheduler.auto_mode = True
                strategy = CpiAutoStrategy(
                    scheduler, args.schedule_state.with_name("strategy.json")
                )
                run_worker(strategy, poll_seconds=args.poll_seconds)
                return 0
            if args.schedule_command == "import":
                result = import_calendar_file(
                    scheduler, args.source_file, args.acquired_at
                )
            else:
                result = execute(
                    service, "schedule/" + args.schedule_command, scheduler=scheduler
                )
        else:
            params = {
                key: value
                for key, value in vars(args).items()
                if key
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
                and value is not None
            }
            result = execute(service, args.command, params, scheduler=scheduler)
        emit(result)
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
