"""Opt-in DEV composition: new CPI storage, existing packages and read-only PCE."""

from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.historical.cpi_release_schedule import CpiReleaseScheduler
from market_data_foundation.historical.cpi_timeseries import (
    DEFAULT_REQUEST_CONTROL_PATH,
    CpiTimeSeriesService,
    build_service,
    build_source,
)
from market_data_foundation.historical.cpi_timeseries_cli import (
    WorkerService,
    persisted_history_start,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    PostgresTimeSeriesStore,
)
from market_data_foundation.storage.postgres_runtime_config import load_pg_config
from market_data_foundation.web.cpi_api import create_app
from market_data_foundation.web.cpi_query import CpiQueryAccess
from market_data_foundation.web.cpi_timeseries_api import (
    PREFIXES,
    TimeSeriesHost,
    admitted_job,
    admitted_strategy,
    timeseries_router,
)
from market_data_foundation.web.cpi_timeseries_jobs import WebError
from market_data_foundation.web.package_catalog import Eligibility, HistoryEvidence
from market_data_foundation.web.packages import PackageHost
from market_data_foundation.web.pce_api import add_read_routes
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN, admitted
from market_data_foundation.web.readonly_host import (
    ReadonlyRuntime,
    _clock,
    admitted_package_save,
)


class LocalOnlySource:
    def fetch(self, *_args, **_kwargs):
        raise RuntimeError("local_only_source_required")


class TimeSeriesPackages(PackageHost):
    """CPI eligibility uses time-series evidence without inventing Publication."""

    def project(self, package_id, record, access):
        view = super().project(package_id, record, access)
        if package_id != "macro":
            return view
        hosts = self.timeseries_hosts
        definitions = {
            (c.definition_ref.identity, c.definition_ref.revision): hosts[
                c.capability_id
            ]
            for c in view.capabilities
            if c.capability_id in hosts
        }
        assets = []
        for asset in view.assets:
            revisions = []
            for revision in asset.revisions:
                host = definitions.get(
                    (revision.definition_ref.identity, revision.definition_ref.revision)
                )
                if host is not None:
                    source = host.source(record)
                    capabilities = host.capabilities(source)
                    upstream = host.upstream_access(source)
                    readable = (
                        revision.local_holding == "PRESENT"
                        and revision.metadata_view_permission == "allowed"
                        and capabilities["query"]
                    )
                    revision = revision.model_copy(
                        update={
                            "local_read": Eligibility(
                                allowed=readable,
                                reasons=[
                                    host.profile.key + "_timeseries_local_coverage"
                                    if readable
                                    else host.profile.key
                                    + "_timeseries_read_unavailable"
                                ],
                            ),
                            "upstream_read": Eligibility(
                                allowed=upstream["allowed"],
                                reasons=[
                                    upstream["reason"]
                                    or host.profile.key + "_local_first_gap_passthrough"
                                ],
                            ),
                            "collect": Eligibility(
                                allowed=capabilities["collect"],
                                reasons=[
                                    capabilities["reason"]
                                    or "host_and_package_authorized"
                                ],
                            ),
                            "plan": Eligibility(
                                allowed=False, reasons=["web_host_has_no_worker"]
                            ),
                        }
                    )
                revisions.append(revision)
            assets.append(asset.model_copy(update={"revisions": revisions}))
        return view.model_copy(update={"assets": assets})


class TimeSeriesRuntime(ReadonlyRuntime):
    def __init__(
        self,
        settings,
        *,
        schedule_path,
        job_directory,
        connector=None,
        clock=_clock,
        cpi_access=CpiQueryAccess(),
        pce_access=CpiQueryAccess(),
        allow_collect=False,
        read_service_factory=None,
        write_service_factory=None,
        max_collect_months=None,
        history_start="1999-01",
        query_service_factory=None,
        ppi_enabled=False,
        ppi_read_service_factory=None,
        ppi_write_service_factory=None,
        ppi_query_service_factory=None,
        ppi_schedule_path=None,
        ppi_job_directory=None,
        ppi_access=CpiQueryAccess(),
        ppi_allow_collect=False,
        ppi_history_start=None,
    ):
        super().__init__(
            settings,
            connector=connector,
            clock=clock,
            cpi_access=cpi_access,
            pce_access=pce_access,
        )
        if not isinstance(ppi_access, CpiQueryAccess):
            raise ValueError("readonly_explicit_access_required")
        if type(ppi_enabled) is not bool or type(ppi_allow_collect) is not bool:
            raise ValueError("explicit_timeseries_permissions_required")
        self.accesses["ppi"] = ppi_access
        self.ppi_timeseries = None
        self.packages = TimeSeriesPackages(
            self.packages.store,
            settings.environment_id,
            authorize=self.authorize_package,
            credentials=self.credential_states,
            credential_loader=self.load_legacy_key,
            history=self.history,
            credential_replace_enabled=settings.package_config_save,
        )
        from market_data_foundation.historical.cpi_request_control import (
            CpiRequestControl,
        )

        self.request_control = CpiRequestControl(
            DEFAULT_REQUEST_CONTROL_PATH, clock=clock
        )
        self.timeseries = TimeSeriesHost(
            packages=self.packages,
            read_service_factory=read_service_factory or self.read_service,
            write_service_factory=write_service_factory or self.write_service,
            scheduler=CpiReleaseScheduler(
                WorkerService(self.worker_service),
                schedule_path,
                clock=clock,
                history_start=persisted_history_start(schedule_path, history_start),
                auto_mode=True,
            ),
            query_service_factory=query_service_factory or self.query_service,
            job_directory=job_directory,
            allow_collect=allow_collect,
            query_allowed=(
                cpi_access.metadata_permission == "allowed"
                and cpi_access.read_permission == "allowed"
            ),
            max_collect_months=max_collect_months,
            clock=clock,
        )
        self.packages.timeseries = self.timeseries
        self.packages.timeseries_hosts = {"cpi": self.timeseries}
        configured = ppi_enabled or any(
            factory is not None
            for factory in (
                ppi_read_service_factory,
                ppi_write_service_factory,
                ppi_query_service_factory,
            )
        )
        if configured:
            if ppi_schedule_path is None or ppi_job_directory is None:
                raise ValueError("ppi_independent_paths_required")
            if not all(
                Path(p).is_absolute() and ".." not in Path(p).parts
                for p in (ppi_schedule_path, ppi_job_directory)
            ):
                raise ValueError("ppi_independent_absolute_paths_required")
            if (
                Path(ppi_schedule_path).resolve() == Path(schedule_path).resolve()
                or Path(ppi_job_directory).resolve() == Path(job_directory).resolve()
            ):
                raise ValueError("ppi_independent_paths_required")
            baseline = ppi_history_start
            if baseline is None:
                raise ValueError("ppi_history_start_evidence_required")
            self.ppi_timeseries = TimeSeriesHost(
                profile=PPI_PROFILE,
                packages=self.packages,
                read_service_factory=ppi_read_service_factory or self.ppi_read_service,
                write_service_factory=ppi_write_service_factory
                or (self.ppi_write_service if ppi_enabled else None),
                query_service_factory=ppi_query_service_factory
                or (self.ppi_query_service if ppi_enabled else None),
                scheduler=CpiReleaseScheduler(
                    WorkerService(self.ppi_worker_service, profile=PPI_PROFILE),
                    ppi_schedule_path,
                    profile=PPI_PROFILE,
                    clock=clock,
                    history_start=persisted_history_start(ppi_schedule_path, baseline),
                    auto_mode=True,
                ),
                job_directory=ppi_job_directory,
                allow_collect=ppi_allow_collect,
                query_allowed=ppi_access.metadata_permission == "allowed"
                and ppi_access.read_permission == "allowed",
                max_collect_months=max_collect_months,
                clock=clock,
            )
            self.packages.timeseries_hosts["ppi"] = self.ppi_timeseries

    def worker_service(self):
        capabilities = self.timeseries.capabilities(self.timeseries.source())
        if not capabilities["collect"]:
            raise WebError(capabilities["reason"], 403)
        return self.timeseries.write_factory(
            credential_loader=self.timeseries.effective_credential
        )

    def ppi_worker_service(self):
        host = self.ppi_timeseries
        capabilities = host.capabilities(host.source())
        if not capabilities["collect"]:
            raise WebError(capabilities["reason"], 403)
        return host.write_factory(credential_loader=host.effective_credential)

    def ppi_query_service(self, *, credential_loader):
        return self.query_service(
            credential_loader=credential_loader, profile=PPI_PROFILE
        )

    def ppi_write_service(self, *, credential_loader):
        return self.write_service(
            credential_loader=credential_loader, profile=PPI_PROFILE
        )

    def ppi_read_service(self):
        return self.read_service(profile=PPI_PROFILE)

    def query_service(self, *, credential_loader, profile=CPI_PROFILE):
        service = (
            self.read_service()
            if profile is CPI_PROFILE
            else self.read_service(profile=profile)
        )
        try:
            service.source = build_source(
                credential_loader=credential_loader,
                request_control=self.request_control,
                max_transport_attempts=1,
                profile=profile,
            )
            return service
        except BaseException:
            service.close()
            raise

    def write_service(self, *, credential_loader, profile=CPI_PROFILE):
        config = self.settings.config_directory
        if load_pg_config(config / "runtime.toml") != self.profile:
            raise WebError("postgres_profile_changed", 503)
        # Same core factory/configuration as CLI, but BLS authentication is
        # exclusively the effective PackageHost key. A replacement reference
        # takes precedence over legacy secret.toml and failure never falls back.
        return build_service(
            runtime_path=config / "runtime.toml",
            secret_path=config / "secret.toml",
            registered=True,
            credential_loader=credential_loader,
            request_control=self.request_control,
            max_transport_attempts=1,
            profile=profile,
        )

    def read_service(self, *, profile=CPI_PROFILE):
        # Dedicated connection with server-enforced session READ ONLY. Never call
        # build_service: GET does not require Garage/BLS credentials or clients.
        connection = self.connect(
            **self.profile.connection_kwargs(),
            autocommit=True,
            connect_timeout=5,
            options="-c statement_timeout=3000",
        )
        try:
            connection.execute("SET TIME ZONE 'UTC'")
            return CpiTimeSeriesService(
                PostgresTimeSeriesStore(connection, None, profile=profile),
                LocalOnlySource(),
                profile=profile,
            )
        except BaseException:
            connection.close()
            raise

    def _history(self, capability, definition, at):
        if capability not in {"cpi", "ppi"}:
            return super()._history(capability, definition, at)
        access = self.accesses[capability]
        host = self.timeseries if capability == "cpi" else self.ppi_timeseries
        holding = "unknown"
        if access.metadata_permission == "allowed" and host is not None:
            try:
                with host.service() as service:
                    holding = (
                        "present" if service.status()["observation_count"] else "absent"
                    )
            except Exception:
                pass
        return HistoryEvidence(
            definition_ref=definition,
            holding=holding,
            metadata_view_permission=access.metadata_permission,
            read_permission=access.read_permission,
        )

    def history(self, environment_id, package_id, record):
        result = super().history(environment_id, package_id, record)
        if package_id != "macro":
            return result
        ppi = next(
            c
            for c in self.packages.catalog.package("macro").capabilities
            if c.capability_id == "ppi"
        )
        return result.model_copy(
            update={
                "history": [
                    self._history("ppi", ppi.definition_ref, self.clock())
                    if evidence.definition_ref == ppi.definition_ref
                    else evidence
                    for evidence in result.history
                ]
            }
        )


def create_timeseries_preview(settings, *, enabled=False, ui_directory=None, **options):
    if enabled is not True:
        raise ValueError("cpi_timeseries_web_opt_in_required")
    runtime = TimeSeriesRuntime(settings, **options)
    # No legacy CPI scope is installed. Its old endpoint remains unconfigured;
    # new data never traverses the old canonical schema or publication model.
    app = create_app(package_host=runtime.packages)
    add_read_routes(app, runtime.scopes["pce"], runtime.clock, write_eligible=False)

    def prune(application):
        router = getattr(application, "router", None)
        if router is None:
            return
        router.routes[:] = [
            r
            for r in router.routes
            if getattr(r, "methods", None) is None
            or r.methods <= {"GET", "HEAD"}
            or (
                settings.package_config_save
                and r.methods == {"PUT"}
                and r.path == "/api/r5/packages/{package_id}"
            )
        ]
        for route in router.routes:
            prune(getattr(route, "app", None))

    prune(app)
    app.include_router(timeseries_router(runtime.timeseries))
    app.include_router(timeseries_router(runtime.ppi_timeseries, profile=PPI_PROFILE))
    app.state.timeseries_runtime = runtime

    @app.middleware("http")
    async def boundary(request, call_next):
        config_put = settings.package_config_save and admitted_package_save(request)
        prefixes = PREFIXES
        job_post = any(admitted_job(request, prefix=prefix) for prefix in prefixes)
        strategy_put = any(
            admitted_strategy(request, prefix=prefix) for prefix in prefixes
        )
        error = None
        if request.method not in {"GET", "HEAD"} and not (
            config_put or job_post or strategy_put
        ):
            error = (405, "method_not_allowed")
        elif not (config_put or job_post or strategy_put) and not admitted(
            request, origin=CANDIDATE_ORIGIN
        ):
            error = (403, "operator_not_authorized")
        else:
            capability = (
                "ppi"
                if request.url.path.startswith("/api/r5/ppi/")
                else "cpi"
                if request.url.path.startswith("/api/r5/cpi")
                else (
                    "pce"
                    if request.url.path in {"/api/pce/assets", "/api/pce/read"}
                    else None
                )
            )
            if capability:
                access = runtime.accesses[capability]
                if access.metadata_permission != "allowed":
                    error = (403, "metadata_permission_" + access.metadata_permission)
                elif (
                    request.url.path.endswith(("/read", "/query"))
                    and access.read_permission != "allowed"
                ):
                    error = (403, "read_permission_" + access.read_permission)
            if request.method in {"GET", "HEAD"} and request.url.path.startswith(
                prefixes
            ):
                if (
                    request.headers.get("content-length", "0") != "0"
                    or "transfer-encoding" in request.headers
                ):
                    error = (400, "get_body_not_supported")
        if error:
            response = JSONResponse(
                {"status": "NOT_AVAILABLE", "classification": error[1]}, error[0]
            )
        else:
            try:
                response = await call_next(request)
            except Exception:
                response = JSONResponse(
                    {
                        "status": "UNKNOWN",
                        "classification": "timeseries_host_unavailable",
                    },
                    503,
                )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    if ui_directory is not None:
        ui = Path(ui_directory)
        app.mount("/assets", StaticFiles(directory=ui / "assets"))
        app.get("/")(lambda: RedirectResponse("/pce"))
        app.get("/pce")(lambda: FileResponse(ui / "index.html"))
        app.get("/mdf-data-hub.png")(lambda: FileResponse(ui / "mdf-data-hub.png"))
    return app
