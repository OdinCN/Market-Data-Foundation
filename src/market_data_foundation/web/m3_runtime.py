"""Explicit M3 host binding; no source, database write or scheduler on construction."""

from dataclasses import dataclass
from pathlib import Path

from market_data_foundation.historical.cpi_request_control import CpiRequestControl
from market_data_foundation.historical.m3_capital_goods_source import M3CandidateSource
from market_data_foundation.historical.m3_capital_goods_store import PostgresM3Store
from market_data_foundation.historical.m3_capital_goods_timeseries import (
    M3TimeSeriesService,
    build_service,
)
from market_data_foundation.storage.postgres_runtime_config import load_pg_config
from market_data_foundation.web.cpi_query import CpiQueryAccess
from market_data_foundation.web.cpi_timeseries_jobs import WebError
from market_data_foundation.web.m3_product_host import M3ProductHost


@dataclass(frozen=True)
class M3Options:
    job_directory: Path
    request_control_path: Path
    allow_read: bool = False
    allow_collect: bool = False
    allow_source: bool = False
    read_factory: object = None
    query_factory: object = None
    write_factory: object = None


def bind_m3(runtime, options, *, occupied_paths):
    if options is None:
        return
    if not isinstance(options, M3Options) or any(
        type(v) is not bool
        for v in (options.allow_read, options.allow_collect, options.allow_source)
    ):
        raise ValueError("m3_explicit_options_required")
    job = Path(options.job_directory)
    control = Path(options.request_control_path)
    if any(not p.is_absolute() or ".." in p.parts for p in (job, control)):
        raise ValueError("m3_absolute_paths_required")
    target = job.resolve()
    prior = [Path(p).resolve() for p in occupied_paths if p is not None]
    if any(
        target == p or target in p.parents or p in target.parents
        for p in prior + [control.resolve()]
    ):
        raise ValueError("m3_independent_job_path_required")
    request_control = CpiRequestControl(control, clock=runtime.clock)
    config = runtime.settings.config_directory

    def read():
        connection = runtime.connect(
            **runtime.profile.connection_kwargs(),
            autocommit=True,
            connect_timeout=5,
            options="-c statement_timeout=3000",
        )
        try:
            connection.execute("SET TIME ZONE 'UTC'")
            return M3TimeSeriesService(
                PostgresM3Store(connection, None, clock=runtime.clock)
            )
        except BaseException:
            connection.close()
            raise

    def query(*, credential_loader):
        service = read()
        try:
            service.source = M3CandidateSource(
                credential_loader=credential_loader,
                request_control=request_control,
                enabled=True,
                clock=runtime.clock,
            )
            return service
        except BaseException:
            service.store.connection.close()
            raise

    def write(*, credential_loader):
        if load_pg_config(config / "runtime.toml") != runtime.profile:
            raise WebError("postgres_profile_changed", 503)
        return build_service(
            runtime_path=config / "runtime.toml",
            secret_path=config / "secret.toml",
            package_directory=runtime.settings.package_store_directory,
            request_control=request_control,
            allow_live=True,
            credential_loader=credential_loader,
        )

    host = None

    def query_bound():
        return (options.query_factory or query)(
            credential_loader=host.effective_credential
        )

    def write_bound(*, credential_loader):
        return (options.write_factory or write)(credential_loader=credential_loader)

    from market_data_foundation.historical.m3_calendar import fetch_calendar

    host = M3ProductHost(
        packages=runtime.packages,
        calendar_fetch=(lambda: fetch_calendar(request_control))
        if options.allow_source
        else None,
        read_factory=options.read_factory or read,
        query_factory=query_bound if options.allow_source else None,
        write_factory=write_bound if options.allow_collect else None,
        job_directory=job,
        allow_read=options.allow_read,
        allow_source=options.allow_source,
        allow_collect=options.allow_collect,
        clock=runtime.clock,
    )
    runtime.m3_query_host = host
    runtime.packages.timeseries_hosts["m3"] = host
    runtime.accesses["m3"] = CpiQueryAccess(
        metadata_permission="allowed" if options.allow_read else "denied",
        read_permission="allowed" if options.allow_read else "denied",
    )
