"""Opt-in DEV composition: new CPI storage, existing packages and read-only PCE."""

from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

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
    PREFIX,
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
        definition = next(
            c.definition_ref for c in view.capabilities if c.capability_id == "cpi"
        )
        source = self.timeseries.source(record)
        capabilities = self.timeseries.capabilities(source)
        upstream = self.timeseries.upstream_access(source)
        assets = []
        for asset in view.assets:
            revisions = []
            for revision in asset.revisions:
                if revision.definition_ref == definition:
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
                                    "cpi_timeseries_local_coverage"
                                    if readable
                                    else "cpi_timeseries_read_unavailable"
                                ],
                            ),
                            "upstream_read": Eligibility(
                                allowed=upstream["allowed"],
                                reasons=[
                                    upstream["reason"]
                                    or "cpi_local_first_gap_passthrough"
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
    ):
        super().__init__(
            settings,
            connector=connector,
            clock=clock,
            cpi_access=cpi_access,
            pce_access=pce_access,
        )
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

    def worker_service(self):
        capabilities = self.timeseries.capabilities(self.timeseries.source())
        if not capabilities["collect"]:
            raise WebError(capabilities["reason"], 403)
        return self.timeseries.write_factory(
            credential_loader=self.timeseries.effective_credential
        )

    def query_service(self, *, credential_loader):
        service = self.read_service()
        try:
            service.source = build_source(
                credential_loader=credential_loader,
                request_control=self.request_control,
                max_transport_attempts=1,
            )
            return service
        except BaseException:
            service.close()
            raise

    def write_service(self, *, credential_loader):
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
        )

    def read_service(self):
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
                PostgresTimeSeriesStore(connection, None), LocalOnlySource()
            )
        except BaseException:
            connection.close()
            raise

    def _history(self, capability, definition, at):
        if capability != "cpi":
            return super()._history(capability, definition, at)
        access = self.accesses["cpi"]
        holding = "unknown"
        if access.metadata_permission == "allowed":
            try:
                with self.timeseries.service() as service:
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
    app.state.timeseries_runtime = runtime

    @app.middleware("http")
    async def boundary(request, call_next):
        config_put = settings.package_config_save and admitted_package_save(request)
        job_post = admitted_job(request)
        strategy_put = admitted_strategy(request)
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
                "cpi"
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
                PREFIX
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
