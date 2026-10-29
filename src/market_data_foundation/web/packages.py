"""D-01 runtime package projection and HTTP adapter; no source or job execution.

The Host supplies identity, permissions and evidence. Configuration, evidence and
permission are independent inputs. Replacement secrets are write-only; loader
references never enter public responses. No runtime eligibility is inferred
from contracts. Credential writes require explicit trusted Host assembly.
"""

import json
from collections.abc import Callable

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import Field

from market_data_foundation.web.dto import DTO
from market_data_foundation.web.package_catalog import (
    AssetRevision,
    AssetView,
    CredentialState,
    Eligibility,
    PackageError,
    PackageFailure,
    PackageHistory,
    PackagesView,
    PackageView,
    Permission,
    SaveCommand,
    key,
)
from market_data_foundation.web.package_store import (
    KEY_SOURCES,
    ConfigRecord,
    LocalPackageStore,
)


class PackageAccess(DTO):
    metadata_view_permission: Permission = "unknown"
    write_eligible: bool | None = None
    write_reasons: list[str] = Field(
        default_factory=lambda: ["package_authorization_not_configured"], min_length=1
    )


# Every callback is a trusted Host binding, not a browser-selectable provider.
Authorize = Callable[[Request, str], PackageAccess]
Credentials = Callable[[str, str, ConfigRecord | None], list[CredentialState]]
History = Callable[[str, str, ConfigRecord | None], PackageHistory]


class PackageHost:
    def __init__(
        self,
        store: LocalPackageStore,
        environment_id: str,
        *,
        authorize: Authorize | None = None,
        credentials: Credentials | None = None,
        history: History | None = None,
        credential_replace_enabled: bool = False,
        credential_loader: Callable[[str, str, str], str | None] | None = None,
    ):
        # Validate trusted identity once; environment/path never come from HTTP.
        ConfigRecord(
            environment_id=environment_id,
            package_id="macro",
            revision="r1",
            enabled_capability_ids=[],
        )
        self.store = store
        self.catalog = store.catalog
        self.environment_id = environment_id
        self.authorize = authorize
        self.credentials = credentials
        self.history = history
        self.credential_replace_enabled = credential_replace_enabled
        self.credential_loader = credential_loader

    def credential_states(self, package_id, record, *, source_ids=None):
        sources = {
            s
            for cap in self.catalog.package(package_id).capabilities
            for s in cap.required_sources
        }
        wanted = sources if source_ids is None else set(source_ids)
        if not wanted <= sources:
            raise PackageError("illegal_source_change", 422, "REJECTED")
        references = record.credential_refs if record else {}
        inherited = wanted - set(references)
        states = (
            self.credentials(self.environment_id, package_id, record)
            if inherited and self.credentials
            else []
        )
        if not isinstance(states, (list, tuple)) or not all(
            isinstance(s, CredentialState) for s in states
        ):
            raise PackageError("package_evidence_unavailable")
        state_map = {s.source_id: s for s in states}
        if len(state_map) != len(states) or not set(state_map) <= sources:
            raise PackageError("package_evidence_unavailable")
        result = []
        for source in sorted(wanted):
            state = state_map.get(
                source, CredentialState(source_id=source, credential_state="unknown")
            )
            if source in references:
                try:
                    self.store.resolve_credential(record, source)
                    value = "configured"
                except PackageError:
                    value = "unknown"
                state = CredentialState(source_id=source, credential_state=value)
            result.append(
                state.model_copy(
                    update={
                        "replace_supported": self.credential_replace_enabled
                        and source in KEY_SOURCES
                    }
                )
            )
        return result

    def load_credential(self, package_id, source_id, *, record=None):
        """Host-only loading shared with a Worker; never exposed by HTTP."""
        sources = {
            s
            for c in self.catalog.package(package_id).capabilities
            for s in c.required_sources
        }
        if source_id not in sources:
            raise PackageError("illegal_source_change", 422, "REJECTED")
        current = (
            record
            if record is not None
            else self.store.read(self.environment_id, package_id)
        )
        if current is not None:
            if (
                current.environment_id != self.environment_id
                or current.package_id != package_id
            ):
                raise PackageError("credential_context_mismatch", 403, "REJECTED")
            if source_id in current.credential_refs:
                return self.store.resolve_credential(current, source_id)
        if self.credential_loader is None:
            return None
        try:
            value = self.credential_loader(self.environment_id, package_id, source_id)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError()
            return value
        except Exception:
            raise PackageError("credential_storage_unavailable") from None

    def access(self, request, package_id):
        self.catalog.package(package_id)
        try:
            access = (
                self.authorize(request, package_id)
                if self.authorize
                else PackageAccess()
            )
            if not isinstance(access, PackageAccess):
                raise TypeError()
        except Exception:
            raise PackageError("package_authorization_unavailable") from None
        if access.metadata_view_permission != "allowed":
            raise PackageError(
                "package_metadata_permission_" + access.metadata_view_permission,
                403,
                "REJECTED",
            )
        return access

    def project(self, package_id, record, access):
        package = self.catalog.package(package_id)
        try:
            states = self.credential_states(package_id, record)
            evidence = (
                self.history(self.environment_id, package_id, record)
                if self.history
                else PackageHistory()
            )
            if not isinstance(evidence, PackageHistory) or not all(
                isinstance(s, CredentialState) for s in states
            ):
                raise TypeError()
            state_map = {s.source_id: s for s in states}
            history = {key(h.definition_ref): h for h in evidence.history}
            known_refs = {key(c.definition_ref) for c in package.capabilities}
            sources = {s for c in package.capabilities for s in c.required_sources}
            if (
                len(state_map) != len(states)
                or not set(state_map) <= sources
                or len(history) != len(evidence.history)
                or not set(history) <= known_refs
            ):
                raise ValueError()
        except Exception:
            raise PackageError("package_evidence_unavailable") from None
        enabled = record.enabled_capability_ids if record else []
        required = sorted(
            {
                s
                for c in package.capabilities
                if c.capability_id in enabled
                for s in c.required_sources
            }
        )
        credentials = [
            state_map.get(s, CredentialState(source_id=s, credential_state="unknown"))
            for s in sorted(sources)
        ]
        grouped = {}
        for cap in package.capabilities:
            definition = cap.definition_ref
            rows = self.catalog.entries[package_id, cap.capability_id]
            on = cap.capability_id in enabled
            raw_history = history.get(key(definition))
            permission = (
                raw_history.metadata_view_permission if raw_history else "unknown"
            )
            h = raw_history if permission == "allowed" else None
            retained = h is not None and h.holding == "present"
            if not on and not retained:
                continue
            local = Eligibility(
                allowed=None, reasons=["history_or_read_permission_unknown"]
            )
            holding = "UNKNOWN"
            if h:
                holding = h.holding.upper()
                if (
                    h.holding == "present"
                    and h.read_permission == "allowed"
                    and h.publication_readable is True
                ):
                    local = Eligibility(
                        allowed=True,
                        reasons=["history_read_permission_and_publication_confirmed"],
                    )
                elif h.read_permission == "denied":
                    local = Eligibility(
                        allowed=False, reasons=["read_permission_denied"]
                    )
                elif h.holding == "absent":
                    local = Eligibility(allowed=False, reasons=["no_history"])
                elif h.publication_readable is False:
                    local = Eligibility(
                        allowed=False, reasons=["publication_not_readable"]
                    )
            upstream = Eligibility(
                allowed=None,
                reasons=["source_qualification_and_current_binding_unknown"],
            )
            if not on:
                upstream = Eligibility(allowed=False, reasons=["capability_disabled"])
            elif any(
                state_map.get(
                    s, CredentialState(source_id=s, credential_state="unknown")
                ).credential_state
                == "missing"
                for s in cap.required_sources
            ):
                upstream = Eligibility(allowed=False, reasons=["credential_missing"])
            elif not any(r.binding_contract is not None for r in rows):
                upstream = Eligibility(
                    allowed=False, reasons=["candidate_has_no_runtime_binding"]
                )
            grouped.setdefault(definition.identity, []).append(
                AssetRevision(
                    definition_ref=definition,
                    capability_enabled=on,
                    visibility_basis=(["ENABLED_MAPPING"] if on else [])
                    + (["RETAINED_HISTORY"] if retained else []),
                    source_entries=rows,
                    metadata_view_permission=permission,
                    local_holding=holding,
                    local_read=local,
                    upstream_read=upstream,
                    collect=Eligibility(
                        allowed=False if not on else None,
                        reasons=[
                            "capability_disabled"
                            if not on
                            else "collection_eligibility_not_assessed"
                        ],
                    ),
                    plan=Eligibility(
                        allowed=False if not on else None,
                        reasons=[
                            "capability_disabled" if not on else "plan_not_assessed"
                        ],
                    ),
                )
            )
        complete = evidence.completeness == "COMPLETE" and all(
            key(c.definition_ref) in history
            and history[key(c.definition_ref)].metadata_view_permission == "allowed"
            and history[key(c.definition_ref)].holding != "unknown"
            for c in package.capabilities
        )
        return PackageView(
            environment_id=self.environment_id,
            package_id=package_id,
            name=package.name,
            domain=package.domain,
            description=package.description,
            capabilities=list(package.capabilities),
            configuration_state="PRESENT" if record else "ABSENT",
            config_revision=record.revision if record else None,
            enabled_capability_ids=list(enabled),
            required_sources=required,
            credential_states=credentials,
            history_completeness="COMPLETE" if complete else "UNKNOWN",
            assets=[
                AssetView(identity=i, revisions=v) for i, v in sorted(grouped.items())
            ],
            write_eligible=access.write_eligible,
            write_reasons=access.write_reasons,
        )

    def directory(self, request):
        # Check every package before reading a single storage snapshot. Failure
        # is explicit, never a success-shaped partial/empty directory.
        accesses = {p: self.access(request, p) for p in self.catalog.packages}
        snapshot = self.store.snapshot()
        return PackagesView(
            environment_id=self.environment_id,
            packages=[
                self.project(p, snapshot.record(self.environment_id, p), accesses[p])
                for p in self.catalog.packages
            ],
        )

    def view(self, request, package_id):
        access = self.access(request, package_id)
        return self.project(
            package_id, self.store.read(self.environment_id, package_id), access
        )

    def save(self, request, package_id, command):
        access = self.access(request, package_id)
        if access.write_eligible is not True:
            raise PackageError("package_write_not_authorized", 403, "REJECTED")
        record = self.store.save(
            self.environment_id,
            package_id,
            command,
            allow_replace=self.credential_replace_enabled,
        )
        try:
            return self.project(package_id, record, access)
        except Exception:
            # Commit completed, but the response projection could not be built.
            # The client must re-read and must not blindly replay the old CAS.
            raise PackageError(
                "config_commit_outcome_unknown", status="UNKNOWN", effect="UNKNOWN"
            ) from None


def failure_response(error):
    return JSONResponse(
        status_code=error.http_status,
        content=error.failure.model_dump(),
        headers={"Cache-Control": "no-store"},
    )


def _unique_members(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError()
        result[k] = v
    return result


class PackageRoute(APIRoute):
    """Sanitize validation failures without changing other R5 error contracts."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            try:
                if request.url.query:
                    raise PackageError("invalid_request", 422, "REJECTED")
                if request.method == "PUT":
                    try:
                        json.loads(
                            await request.body(), object_pairs_hook=_unique_members
                        )
                    except ValueError, UnicodeError:
                        raise PackageError("invalid_request", 422, "REJECTED") from None
                return await original(request)
            except RequestValidationError:
                return failure_response(
                    PackageError("invalid_request", 422, "REJECTED")
                )
            except PackageError as exc:
                return failure_response(exc)
            except Exception:
                return failure_response(
                    PackageError(
                        "config_commit_outcome_unknown"
                        if request.method == "PUT"
                        else "package_host_unavailable",
                        status="UNKNOWN" if request.method == "PUT" else "UNAVAILABLE",
                        effect="UNKNOWN" if request.method == "PUT" else "NONE",
                    )
                )

        return handle


def package_router(host: PackageHost | None) -> APIRouter:
    router = APIRouter(route_class=PackageRoute)
    errors = {code: {"model": PackageFailure} for code in (403, 404, 409, 422, 503)}

    def configured():
        if host is None:
            raise PackageError("package_host_not_configured")
        return host

    @router.get(
        "/api/r5/packages",
        response_model=PackagesView,
        responses=errors,
        operation_id="package_directory",
    )
    def packages(request: Request):
        return configured().directory(request)

    @router.get(
        "/api/r5/packages/{package_id}",
        response_model=PackageView,
        responses=errors,
        operation_id="get_package",
    )
    def get_package(request: Request, package_id: str):
        return configured().view(request, package_id)

    @router.put(
        "/api/r5/packages/{package_id}",
        response_model=PackageView,
        responses=errors,
        operation_id="save_package",
    )
    def save_package(request: Request, package_id: str, command: SaveCommand):
        return configured().save(request, package_id, command)

    return router


def create_package_app(host: PackageHost | None = None):
    """Inert product factory for isolated offline callers, with the real R5 API."""
    from market_data_foundation.web.cpi_api import create_app

    return create_app(package_host=host)
