"""Explicit Claims runtime, independent state and short read-only connections."""

from dataclasses import dataclass
from pathlib import Path

from market_data_foundation.historical.claims_calendar import (
    BACKUP_URL,
    CURRENT_REPORT_URL,
    PRIMARY_URL,
)
from market_data_foundation.historical.claims_collection import build_service
from market_data_foundation.historical.claims_material import material_fetcher
from market_data_foundation.historical.claims_source import (
    ClaimsFredSource,
    ClaimsHttpsTransport,
)
from market_data_foundation.historical.claims_store import PostgresClaimsStore
from market_data_foundation.historical.claims_timeseries import ClaimsTimeSeriesService
from market_data_foundation.storage.postgres_runtime_config import load_pg_config
from market_data_foundation.web.claims_host import ClaimsTimeSeriesHost
from market_data_foundation.web.cpi_query import CpiQueryAccess
from market_data_foundation.web.cpi_timeseries_jobs import WebError


@dataclass(frozen=True)
class ClaimsOptions:
    schedule_path: Path
    job_directory: Path
    allow_read: bool = False
    allow_collect: bool = False
    allow_source: bool = False
    history_start: str | None = None
    artifacts_path: Path | None = None
    request_control_path: Path | None = None
    read_factory: object = None
    write_factory: object = None
    query_factory: object = None
    calendar_fetcher: object = None
    fallback_fetcher: object = None
    material_fetcher: object = None


def bind_claims(runtime, options, *, occupied_paths):
    runtime.claims_timeseries = None
    runtime.accesses["claims"] = CpiQueryAccess()
    if options is None:
        return
    if not isinstance(options, ClaimsOptions) or any(
        type(v) is not bool
        for v in (options.allow_read, options.allow_collect, options.allow_source)
    ):
        raise ValueError("claims_explicit_options_required")
    config = runtime.settings.config_directory
    control = (
        options.request_control_path or config / "claims-fred-request-control.json"
    )
    dol_control = config / "claims-dol-request-control.json"
    artifacts = (
        options.artifacts_path or options.schedule_path.parent / "claims-material"
    )
    paths = [
        options.schedule_path,
        options.job_directory,
        options.schedule_path.with_name("claims-strategy.json"),
        artifacts,
        control,
        dol_control,
    ]
    if any(not p.is_absolute() or ".." in p.parts for p in paths):
        raise ValueError("claims_independent_absolute_paths_required")
    resolved = [p.resolve() for p in paths]
    prior = [Path(p).resolve() for p in occupied_paths if p is not None]
    if any(
        a == b or a in b.parents or b in a.parents
        for i, a in enumerate(resolved)
        for b in resolved[i + 1 :] + prior
    ):
        raise ValueError("claims_independent_paths_required")

    def read():
        connection = runtime.connect(
            **runtime.profile.connection_kwargs(),
            autocommit=True,
            connect_timeout=5,
            options="-c statement_timeout=3000",
        )
        try:
            connection.execute("SET TIME ZONE 'UTC'")
            return ClaimsTimeSeriesService(
                PostgresClaimsStore(connection, None), clock=runtime.clock
            )
        except BaseException:
            connection.close()
            raise

    def query(*, credential_loader):
        service = read()
        try:
            service.source = ClaimsFredSource(
                transport=ClaimsHttpsTransport(credential_loader()),
                live_opt_in=True,
                request_control_path=control,
                max_requests=6,
                clock=runtime.clock,
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
            live_opt_in=True,
            max_requests=6,
            credential_loader=credential_loader,
            request_control_path=control,
        )

    public = material_fetcher(
        artifacts, dol_control, live_opt_in=options.allow_collect, clock=runtime.clock
    )
    host = ClaimsTimeSeriesHost(
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
        calendar_fetcher=options.calendar_fetcher or (lambda: public(PRIMARY_URL)),
        fallback_fetcher=options.fallback_fetcher or (lambda: public(BACKUP_URL)),
        material_fetcher=options.material_fetcher
        or (lambda: public(CURRENT_REPORT_URL)),
    )
    runtime.claims_timeseries = host
    runtime.packages.timeseries_hosts["claims"] = host
    runtime.accesses["claims"] = CpiQueryAccess(
        metadata_permission="allowed" if options.allow_read else "denied",
        read_permission="allowed" if options.allow_read else "denied",
    )
