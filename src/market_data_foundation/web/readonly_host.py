"""Read-only data composition with separately opted-in package configuration saves."""

import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    ReadFailure,
)
from market_data_foundation.canonical.model import DatasetVersion, require
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from market_data_foundation.historical.bea_pce import PCE_DEFINITION, REFERENCE
from market_data_foundation.historical.cpi_local_read import (
    CpiReadRejected,
    read_local_cpi,
)
from market_data_foundation.historical.cpi_pg_binding import SCHEMA as CPI_SCHEMA
from market_data_foundation.historical.pce_local_read import (
    PceReadRejected,
    read_local_pce,
)
from market_data_foundation.historical.pce_pg_binding import SCHEMA as PCE_SCHEMA
from market_data_foundation.storage.postgres_runtime_config import load_pg_config
from market_data_foundation.web.cpi_api import create_app
from market_data_foundation.web.cpi_pg_readonly_scope import (
    DevReadIdentity,
    _readonly_scope_factory,
)
from market_data_foundation.web.cpi_query import CpiQueryAccess, CpiQueryHost
from market_data_foundation.web.package_catalog import (
    CredentialState,
    HistoryEvidence,
    PackageCatalog,
    PackageError,
    PackageHistory,
)
from market_data_foundation.web.package_store import LocalPackageStore
from market_data_foundation.web.packages import PackageAccess, PackageHost
from market_data_foundation.web.pce_api import add_read_routes
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN, admitted


@dataclass(frozen=True)
class ReadonlySettings:
    config_directory: Path
    package_store_directory: Path
    environment_id: str
    cpi_identity: DevReadIdentity
    pce_identity: DevReadIdentity
    package_config_save: bool = False

    def __post_init__(self):
        if type(self.package_config_save) is not bool:
            raise ValueError("explicit_package_config_save_required")
        for path in (self.config_directory, self.package_store_directory):
            if (
                not isinstance(path, Path)
                or not path.is_absolute()
                or ".." in path.parts
            ):
                raise ValueError("readonly_absolute_host_paths_required")


def _clock():
    return datetime.now(UTC)


def admitted_package_save(request):
    """A separate config-only trust check; never expands legacy job writes."""
    return (
        request.method == "PUT"
        and request.url.path
        in {"/api/r5/packages/macro", "/api/r5/packages/tdx", "/api/r5/packages/kd"}
        and not request.url.query
        and request.headers.getlist("origin") == [CANDIDATE_ORIGIN]
        and request.headers.getlist("content-type") == ["application/json"]
        and admitted(
            Request(dict(request.scope, method="GET")), origin=CANDIDATE_ORIGIN
        )
    )


def admitted_package_verify(request):
    parts = request.url.path.split("/")
    if (
        request.method != "POST"
        or len(parts) != 8
        or parts[:4] != ["", "api", "r5", "packages"]
        or parts[5] != "sources"
        or parts[7] != "verify"
        or parts[6] not in {"BLS", "BEA", "Census", "FRED"}
    ):
        return False
    scope = dict(request.scope, method="PUT", path="/".join(parts[:5]))
    return admitted_package_save(Request(scope))


class ReadonlyRuntime:
    def __init__(
        self,
        settings,
        *,
        connector=None,
        clock=_clock,
        cpi_access=CpiQueryAccess(),
        pce_access=CpiQueryAccess(),
    ):
        self.settings = settings
        self.profile = load_pg_config(settings.config_directory / "runtime.toml")
        self.connector = psycopg.connect if connector is None else connector
        self.clock = clock
        self.accesses = {"cpi": cpi_access, "pce": pce_access}
        for access in self.accesses.values():
            if not isinstance(access, CpiQueryAccess):
                raise ValueError("readonly_explicit_access_required")
        if any(
            identity.role != self.profile.user
            for identity in (settings.cpi_identity, settings.pce_identity)
        ):
            raise ValueError("readonly_role_profile_mismatch")
        self.scopes = {
            "cpi": _readonly_scope_factory(
                self.connect,
                settings.cpi_identity,
                enabled=True,
                pg_profile=self.profile,
                schema=CPI_SCHEMA,
            ),
            "pce": _readonly_scope_factory(
                self.connect,
                settings.pce_identity,
                enabled=True,
                pg_profile=self.profile,
                schema=PCE_SCHEMA,
            ),
        }
        catalog = PackageCatalog()
        # Opening an existing store never initializes, repairs or seeds it.
        self.packages = PackageHost(
            LocalPackageStore(settings.package_store_directory, catalog),
            settings.environment_id,
            authorize=self.authorize_package,
            credentials=self.credential_states,
            credential_loader=self.load_legacy_key,
            history=self.history,
            credential_replace_enabled=settings.package_config_save,
        )
        self.query = CpiQueryHost(
            self.scopes["cpi"],
            self.authorize_cpi,
            self.packages,
            bindings=(),
            clock=clock,
        )

    def _secret(self, section, name):
        try:
            with (self.settings.config_directory / "secret.toml").open("rb") as stream:
                document = tomllib.load(stream)
            value = document.get(section, {}).get(name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError()
            return value
        except Exception:
            raise PackageError("credential_storage_unavailable") from None

    def connect(self, **options):
        if (
            load_pg_config(self.settings.config_directory / "runtime.toml")
            != self.profile
        ):
            raise PackageError("postgres_profile_changed")
        expected = dict(
            self.profile.connection_kwargs(),
            autocommit=True,
            connect_timeout=5,
            options="-c statement_timeout=3000",
        )
        if options != expected:
            raise PackageError("readonly_connection_options_mismatch")
        password = self._secret("postgres", "password")
        if password is None:
            raise PackageError("postgres_credential_missing")
        # The complete session defaults to READ ONLY, before the explicit
        # REPEATABLE READ READ ONLY transaction used by the shared snapshot.
        return self.connector(
            **(
                options
                | {
                    "options": options["options"]
                    + " -c default_transaction_read_only=on",
                }
            ),
            password=password,
        )

    def load_legacy_key(self, environment_id, package_id, source_id):
        if environment_id != self.settings.environment_id or package_id != "macro":
            return None
        section = {"BLS": "bls", "BEA": "bea", "FRED": "fred"}.get(source_id)
        return self._secret(section, "api_key") if section is not None else None

    def credential_states(self, environment_id, package_id, record):
        sources = self.packages.catalog.credential_sources(package_id)
        states = []
        for source in sorted(sources):
            if source not in {"BLS", "BEA", "FRED"}:
                value = "unknown"
            else:
                try:
                    value = (
                        "configured"
                        if self.load_legacy_key(environment_id, package_id, source)
                        is not None
                        else "missing"
                    )
                except PackageError:
                    value = "unknown"
            states.append(CredentialState(source_id=source, credential_state=value))
        return states

    def authorize_package(self, request, package_id):
        allowed = (
            request.method in {"GET", "HEAD"}
            and admitted(request, origin=CANDIDATE_ORIGIN)
        ) or (
            self.settings.package_config_save
            and (admitted_package_save(request) or admitted_package_verify(request))
        )
        return PackageAccess(
            metadata_view_permission="allowed" if allowed else "denied",
            write_eligible=allowed and self.settings.package_config_save,
            write_reasons=[
                "dev_package_config_only"
                if self.settings.package_config_save
                else "readonly_host"
            ],
        )

    def authorize_cpi(self, request):
        return (
            self.accesses["cpi"]
            if admitted(request, origin=CANDIDATE_ORIGIN)
            else (
                CpiQueryAccess(metadata_permission="denied", read_permission="denied")
            )
        )

    def _history(self, capability, definition, at):
        access = self.accesses[capability]
        unknown = HistoryEvidence(
            definition_ref=definition,
            holding="unknown",
            metadata_view_permission=access.metadata_permission,
            read_permission=access.read_permission,
        )
        if access.metadata_permission != "allowed":
            return unknown
        try:
            with self.scopes[capability]() as scope:
                key = (
                    BLS_CPI_HEADLINES.key if capability == "cpi" else PCE_DEFINITION.key
                )
                versions = scope.versions_for_definition(key)
                if (
                    not isinstance(versions, tuple)
                    or len(versions) > 128
                    or any(
                        not isinstance(v, DatasetVersion) or v.definition != key
                        for v in versions
                    )
                ):
                    raise ValueError()
                readable = None if access.read_permission == "unknown" else False
                if access.read_permission == "allowed":
                    for version in versions:
                        try:
                            if capability == "cpi":
                                months = {
                                    m.split(":", 1)[0]
                                    for m in version.finite_scope.members
                                }
                                if len(months) != 1:
                                    raise CpiReadRejected(
                                        "coverage_partial_or_mismatched"
                                    )
                                read_local_cpi(
                                    scope,
                                    reference=next(iter(months)),
                                    as_of=at,
                                    version_identity=version.identity,
                                )
                            else:
                                read_local_pce(
                                    scope,
                                    reference=REFERENCE,
                                    as_of=at,
                                    version_identity=version.identity,
                                )
                            readable = True
                        except CpiReadRejected:
                            pass  # Read denial does not erase retained history.
                        except ReadContractError as error:
                            if error.classification == ReadFailure.STORAGE_UNAVAILABLE:
                                raise
                        except PceReadRejected as error:
                            if error.classification == "pce_storage_unavailable":
                                raise
                result = unknown.model_copy(
                    update={
                        "holding": "present" if versions else "absent",
                        "publication_readable": readable,
                    }
                )
            return result
        except Exception:
            # Failed/aborted scopes cannot prove absence or readable publication.
            return unknown

    def history(self, environment_id, package_id, record):
        if environment_id != self.settings.environment_id:
            raise PackageError("package_environment_mismatch")
        at = self.clock()
        if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
            raise PackageError("decision_clock_invalid")
        history = []
        for cap in self.packages.catalog.package(package_id).capabilities:
            if package_id == "macro" and cap.capability_id in self.scopes:
                history.append(self._history(cap.capability_id, cap.definition_ref, at))
            else:
                history.append(
                    HistoryEvidence(
                        definition_ref=cap.definition_ref, holding="unknown"
                    )
                )
        # PPI/TDX/KD have no installed local reader; never infer an empty history.
        return PackageHistory(completeness="UNKNOWN", history=history)


def create_readonly_preview(
    settings,
    *,
    enabled=False,
    connector=None,
    clock=_clock,
    cpi_access=CpiQueryAccess(),
    pce_access=CpiQueryAccess(),
    ui_directory=None,
):
    require(enabled is True, "readonly_host_opt_in_required")
    runtime = ReadonlyRuntime(
        settings,
        connector=connector,
        clock=clock,
        cpi_access=cpi_access,
        pce_access=pce_access,
    )
    app = create_app(
        runtime.scopes["cpi"], package_host=runtime.packages, query_host=runtime.query
    )
    add_read_routes(app, runtime.scopes["pce"], clock, write_eligible=False)
    app.state.readonly_runtime = runtime

    def prune(application):
        router = getattr(application, "router", None)
        if router is None:
            return
        router.routes[:] = [
            route
            for route in router.routes
            if getattr(route, "methods", None) is None
            or route.methods <= {"GET", "HEAD"}
            or (
                settings.package_config_save
                and route.methods == {"PUT"}
                and route.path == "/api/r5/packages/{package_id}"
            )
        ]
        for route in router.routes:
            prune(getattr(route, "app", None))

    prune(app)

    @app.middleware("http")
    async def boundary(request, call_next):
        error = None
        config_put = settings.package_config_save and admitted_package_save(request)
        if request.method not in {"GET", "HEAD"} and not config_put:
            error = (405, "method_not_allowed")
        elif not config_put and not admitted(request, origin=CANDIDATE_ORIGIN):
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
            if capability is not None:
                access = runtime.accesses[capability]
                if access.metadata_permission != "allowed":
                    error = (403, "metadata_permission_" + access.metadata_permission)
                elif (
                    request.url.path.endswith(("/read", "/query"))
                    and access.read_permission != "allowed"
                ):
                    error = (403, "read_permission_" + access.read_permission)
        if error:
            if request.url.path == "/api/r5/cpi/query":
                from market_data_foundation.web.cpi_query import (
                    QueryRejected,
                    failure_response,
                )

                response = failure_response(
                    QueryRejected(error[1], error[0], "REJECTED")
                )
            elif request.url.path.startswith("/api/r5/packages"):
                from market_data_foundation.web.packages import failure_response

                response = failure_response(
                    PackageError(error[1], error[0], "REJECTED")
                )
            else:
                response = JSONResponse(
                    {"status": "NOT_AVAILABLE", "classification": error[1]},
                    status_code=error[0],
                )
            if error[0] == 405:
                response.headers["Allow"] = "GET, HEAD"
        else:
            try:
                response = await call_next(request)
            except Exception:
                response = JSONResponse(
                    {
                        "status": "UNKNOWN",
                        "classification": "readonly_host_unavailable",
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
