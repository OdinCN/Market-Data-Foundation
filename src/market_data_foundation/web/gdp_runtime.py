"""Opt-in GDP composition, independent paths and read-only query connections."""

from dataclasses import dataclass
from pathlib import Path

from market_data_foundation.historical.bea_shared_material import calendar_fetcher
from market_data_foundation.historical.gdp_calendar import parse_official_ics
from market_data_foundation.historical.gdp_source import GdpRangeSource
from market_data_foundation.historical.gdp_store import PostgresGdpStore
from market_data_foundation.historical.gdp_timeseries import (
    GdpTimeSeriesService,
    build_service,
)
from market_data_foundation.storage.postgres_runtime_config import load_pg_config
from market_data_foundation.web.cpi_query import CpiQueryAccess
from market_data_foundation.web.cpi_timeseries_jobs import WebError
from market_data_foundation.web.gdp_host import GdpTimeSeriesHost


@dataclass(frozen=True)
class GdpOptions:
    schedule_path: Path
    job_directory: Path
    allow_read: bool = False
    allow_collect: bool = False
    allow_source: bool = False
    history_start: str | None = None
    read_factory: object = None
    write_factory: object = None
    query_factory: object = None
    calendar_fetcher: object = None


def bind_gdp(runtime, options, *, occupied_paths):
    runtime.gdp_timeseries = None
    runtime.accesses["gdp"] = CpiQueryAccess()
    if options is None:
        return
    if not isinstance(options, GdpOptions) or any(
        type(x) is not bool
        for x in (options.allow_read, options.allow_collect, options.allow_source)
    ):
        raise ValueError("gdp_explicit_options_required")
    paths = [
        options.schedule_path,
        options.job_directory,
        options.schedule_path.with_name("gdp-strategy.json"),
    ]
    if any(not p.is_absolute() or ".." in p.parts for p in paths):
        raise ValueError("gdp_independent_absolute_paths_required")
    resolved = [p.resolve() for p in paths]
    prior = [Path(p).resolve() for p in occupied_paths if p is not None]
    if any(
        a == b or a in b.parents or b in a.parents
        for i, a in enumerate(resolved)
        for b in resolved[i + 1 :] + prior
    ):
        raise ValueError("gdp_independent_paths_required")
    config = runtime.settings.config_directory
    control = (
        runtime.bea_request_control.state_path
        if hasattr(runtime, "bea_request_control")
        else config / "bea-timeseries-request-control.json"
    )
    if any(p == control.resolve() or p in control.resolve().parents for p in resolved):
        raise ValueError("gdp_independent_paths_required")

    class LocalOnly:
        def fetch(self, *args, **kwargs):
            raise WebError("gdp_local_only_source_required", 503)

    def read():
        connection = runtime.connect(
            **runtime.profile.connection_kwargs(),
            autocommit=True,
            connect_timeout=5,
            options="-c statement_timeout=3000",
        )
        try:
            connection.execute("SET TIME ZONE 'UTC'")
            return GdpTimeSeriesService(PostgresGdpStore(connection, None), LocalOnly())
        except BaseException:
            connection.close()
            raise

    def query(*, credential_loader):
        service = read()
        try:
            service.source = GdpRangeSource(
                credential_loader=credential_loader,
                request_control_path=control,
                max_transport_attempts=1,
            )
            return service
        except BaseException:
            service.close()
            raise

    def write(*, credential_loader):
        if load_pg_config(config / "runtime.toml") != runtime.profile:
            raise WebError("postgres_profile_changed", 503)
        return build_service(
            runtime_path=config / "runtime.toml",
            secret_path=config / "secret.toml",
            request_control_path=control,
            credential_loader=credential_loader,
            max_transport_attempts=1,
        )

    host = GdpTimeSeriesHost(
        packages=runtime.packages,
        read_service_factory=options.read_factory or read,
        write_service_factory=(options.write_factory or write)
        if options.allow_collect
        else None,
        query_service_factory=(options.query_factory or query)
        if options.allow_source
        else None,
        query_allowed=options.allow_read,
        allow_collect=options.allow_collect,
        allow_source=options.allow_source,
        schedule_path=options.schedule_path,
        job_directory=options.job_directory,
        history_start=options.history_start,
        clock=runtime.clock,
        calendar_fetcher=options.calendar_fetcher
        or calendar_fetcher(
            control.parent / "bea-gdp-calendar.json",
            parser=parse_official_ics,
            clock=runtime.clock,
        ),
    )
    runtime.gdp_timeseries = host
    runtime.packages.timeseries_hosts["gdp"] = host
    runtime.accesses["gdp"] = CpiQueryAccess(
        metadata_permission="allowed" if options.allow_read else "denied",
        read_permission="allowed" if options.allow_read else "denied",
    )
