"""Explicit standalone JOLTS composition; never touches the shared preview."""

from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI

from market_data_foundation.historical import cpi_timeseries as _core
from market_data_foundation.historical.cpi_request_control import CpiRequestControl
from market_data_foundation.historical.jolts_timeseries import (
    BlsJoltsRangeSource,
    build_read_service,
    build_service,
)
from market_data_foundation.web.cpi_timeseries_api import timeseries_router
from market_data_foundation.web.jolts_timeseries import JoltsTimeSeriesHost


def create_jolts_app(
    *,
    packages,
    runtime_path,
    secret_path,
    state_directory,
    request_control_path=None,
    allow_read=False,
    allow_collect=False,
    allow_source=False,
    read_factory=None,
    write_factory=None,
    query_factory=None,
    **host_options,
):
    if any(type(v) is not bool for v in (allow_read, allow_collect, allow_source)):
        raise ValueError("jolts_explicit_permissions_required")
    state = Path(state_directory)
    if not state.is_absolute() or ".." in state.parts:
        raise ValueError("jolts_absolute_state_directory_required")
    control = (
        _core.DEFAULT_REQUEST_CONTROL_PATH
        if request_control_path is None
        else Path(request_control_path)
    )

    def read():
        return build_read_service(runtime_path=runtime_path, secret_path=secret_path)

    def query(*, credential_loader):
        service = read()
        try:
            service.source = BlsJoltsRangeSource(
                allow_live=True,
                credential_loader=credential_loader,
                request_control=CpiRequestControl(control),
                max_requests=8,
            )
            return service
        except BaseException:
            service.close()
            raise

    def write(*, credential_loader):
        return build_service(
            runtime_path=runtime_path,
            secret_path=secret_path,
            allow_live=True,
            credential_loader=credential_loader,
            request_control_path=control,
        )

    host = JoltsTimeSeriesHost(
        packages=packages,
        state_path=state / "jolts-state.json",
        job_directory=state / "jolts-jobs",
        query_allowed=allow_read,
        allow_collect=allow_collect,
        read_service_factory=read_factory or read,
        write_service_factory=(write_factory or write) if allow_collect else None,
        query_service_factory=(query_factory or query) if allow_source else None,
        **host_options,
    )
    app = FastAPI(title="MDF JOLTS DEV API")
    app.include_router(timeseries_router(host))
    app.state.jolts_host = host
    return app


# Shared preview integration is optional; construction never starts a worker.


@dataclass(frozen=True)
class JoltsOptions:
    state_directory: Path
    allow_read: bool = False
    allow_collect: bool = False
    allow_source: bool = False
    read_factory: object = None
    write_factory: object = None
    query_factory: object = None


def bind_jolts(runtime, options, *, occupied_paths):
    from market_data_foundation.web.cpi_query import CpiQueryAccess

    runtime.jolts_timeseries = None
    runtime.accesses["jolts"] = CpiQueryAccess()
    if options is None:
        return
    if not isinstance(options, JoltsOptions):
        raise ValueError("jolts_explicit_options_required")
    state = Path(options.state_directory)
    if not state.is_absolute() or ".." in state.parts:
        raise ValueError("jolts_independent_absolute_paths_required")
    root = state.resolve()
    if any(
        root == p or root in p.parents or p in root.parents
        for value in occupied_paths
        if value is not None
        for p in [Path(value).resolve()]
    ):
        raise ValueError("jolts_independent_paths_required")
    config = runtime.settings.config_directory
    app = create_jolts_app(
        packages=runtime.packages,
        runtime_path=config / "runtime.toml",
        secret_path=config / "secret.toml",
        state_directory=state,
        allow_read=options.allow_read,
        allow_collect=options.allow_collect,
        allow_source=options.allow_source,
        read_factory=options.read_factory,
        write_factory=options.write_factory,
        query_factory=options.query_factory,
        clock=runtime.clock,
    )
    runtime.jolts_timeseries = app.state.jolts_host
    runtime.packages.timeseries_hosts["jolts"] = runtime.jolts_timeseries
    runtime.accesses["jolts"] = CpiQueryAccess(
        metadata_permission="allowed" if options.allow_read else "denied",
        read_permission="allowed" if options.allow_read else "denied",
    )
