"""R5 CPI reads and explicitly host-bound manual journal acceptance.

A caller supplies a consistent read-only scope. No default production backend is
inferred. No worker/client is created. Existing reader semantics are untouched.
"""

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, field_validator, model_validator

from market_data_foundation.canonical.consumer_read import ReadContractError
from market_data_foundation.canonical.model import (
    DatasetVersion,
    Key,
    Publication,
    Record,
)
from market_data_foundation.canonical.operator_reads import OperatorReads
from market_data_foundation.canonical.operator_status import inspect_operator_status
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from market_data_foundation.historical.cpi_local_read import (
    CpiReadRejected,
    read_local_cpi,
)
from market_data_foundation.web.cpi_jobs import CpiJobsHost, install_jobs
from market_data_foundation.web.cpi_schedules import CpiSchedulesHost, install_schedules
from market_data_foundation.web.dto import DTO
from market_data_foundation.web.observability import (
    ObservabilityDTO,
    ObservabilityHost,
    ObservabilityQuery,
    project,
)
from market_data_foundation.web.source_catalog import (
    DirectoryDTO,
    DirectoryQuery,
    directory,
)

ASSET_ID = BLS_CPI_HEADLINES.identity
SERVICE_ID = ASSET_ID + ":local-read"
Evidence = Literal["UNCONFIGURED", "OFFLINE_FIXTURE", "AUTHORIZED_READ_SCOPE"]


class Failure(StrEnum):
    CAPABILITY_NOT_CONFIGURED = "capability_not_configured"
    INVALID_REQUEST = "invalid_request"
    CATALOG_CONTRACT_INVALID = "catalog_contract_invalid"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    DEFINITION_NOT_ADMITTED = "definition_not_admitted"
    DEFINITION_NOT_FOUND = "definition_not_found"
    VERSION_NOT_FOUND = "version_not_found"
    VERSION_DEFINITION_MISMATCH = "version_definition_mismatch"
    CONTENT_MISSING = "content_missing"
    CONTENT_DIGEST_MISMATCH = "content_digest_mismatch"
    UNPUBLISHED_OR_UNAVAILABLE_AS_OF = "unpublished_or_unavailable_as_of"
    VERSION_WITHDRAWN = "version_withdrawn"
    VERSION_SUPERSEDED = "version_superseded"
    AS_OF_NO_VERSION = "as_of_no_version"
    AS_OF_AMBIGUOUS = "as_of_ambiguous"
    PUBLICATION_STATE_INVALID = "publication_state_invalid"
    LINEAGE_MISSING_OR_MISMATCHED = "lineage_missing_or_mismatched"
    INVALID_AS_OF = "invalid_as_of"
    OPERATOR_QUERY_BUDGET_EXCEEDED = "operator_query_budget_exceeded"
    REFERENCE_NOT_ADMITTED = "reference_not_admitted"
    REFERENCE_STALE_OR_MISMATCHED = "reference_stale_or_mismatched"
    COVERAGE_PARTIAL_OR_MISMATCHED = "coverage_partial_or_mismatched"
    QUALITY_NOT_ADMITTED = "quality_not_admitted"
    CPI_PAYLOAD_INVALID_OR_MISMATCHED = "cpi_payload_invalid_or_mismatched"


class CatalogQuery(DTO):
    reference: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", max_length=7)
    as_of: str = Field(min_length=20, max_length=40)

    @field_validator("as_of")
    @classmethod
    def aware_instant(cls, value: str) -> str:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("invalid_as_of")
        return value


class ReadQuery(CatalogQuery):
    mode: Literal["as_of", "exact_version"]
    version_identity: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def exact_identity(self):
        if (self.mode == "exact_version") != (self.version_identity is not None):
            raise ValueError("invalid_request")
        return self


class ErrorDTO(DTO):
    status: Literal["UNKNOWN", "NOT_AVAILABLE"]
    classification: Failure


class DefinitionDTO(DTO):
    identity: str
    revision: str
    title: str
    registration: Literal["CANONICAL_CONTRACT"] = "CANONICAL_CONTRACT"
    source_qualified_contract: bool
    source_health: Literal["UNKNOWN"] = "UNKNOWN"
    binding_activation: Literal["UNKNOWN"] = "UNKNOWN"


class PublicationDTO(DTO):
    identity: str
    decision: str
    decision_at: str
    usable_at: str
    effective_at: str | None
    pending_clocks: list[str]


class VersionDTO(DTO):
    identity: str
    frozen_at: str
    completeness: str
    selection_state: str
    binding_identity: str | None
    binding_revision: str | None
    publications: list[PublicationDTO]


class AssetDTO(DTO):
    identity: str
    definition_revision: str
    local_holding: Literal["UNKNOWN", "VERIFIED_FOR_REQUEST"]
    qualification: Failure | None
    versions: list[VersionDTO]


class ServiceDTO(DTO):
    identity: str
    asset_identity: str
    version_identity: str
    publication_identity: str
    qualification: Literal["VERIFIED_FOR_REQUEST"] = "VERIFIED_FOR_REQUEST"


class CatalogDTO(DTO):
    status: Literal["OK", "UNKNOWN", "NOT_AVAILABLE"]
    classification: Failure | None
    evidence_mode: Evidence
    reference: str
    as_of: str
    definition: DefinitionDTO
    asset: AssetDTO | None
    service: ServiceDTO | None


class MeasurementDTO(DTO):
    measure_identity: str
    name: str
    series_id: str
    reference_period: str
    comparison_period: str
    seasonal_adjustment: str
    classification: Literal["provider_returned_calculation", "mdf_index_derived_change"]
    unit: Literal["percent"]
    value: str | None
    availability: str
    unavailable_reason: str | None
    preliminary: bool
    finality: str


class ReadDTO(DTO):
    status: Literal["OK"] = "OK"
    evidence_mode: Evidence
    asset_identity: str
    definition_revision: str
    version_identity: str
    publication_identity: str
    publication_decision_at: str
    publication_usable_at: str
    reference: str
    as_of: str
    mode: Literal["as_of", "exact_version"]
    content_mode: Literal["whole_version"] = "whole_version"
    content_sha256: str
    obtained_at: str | None = None
    provider_vintage: Literal["UNKNOWN"] = "UNKNOWN"
    measurements: list[MeasurementDTO] = Field(min_length=4, max_length=4)


class ReadScope(Protocol):
    """Only methods used by existing readers; mutation is absent from this port."""

    def get(self, key: Key) -> Record | None: ...
    def get_content(self, version: Key) -> bytes | None: ...
    def publications_for_version(self, version: Key) -> tuple[Publication, ...]: ...
    def versions_for_definition(
        self, definition: Key
    ) -> tuple[DatasetVersion, ...]: ...
    def operator_reads(self) -> OperatorReads: ...


ScopeFactory = Callable[[], AbstractContextManager[ReadScope]]


def _classification(error: Exception) -> Failure:
    if isinstance(error, (ReadContractError, CpiReadRejected)):
        try:
            return Failure(error.classification)
        except ValueError:
            pass
    return Failure.STORAGE_UNAVAILABLE


def _state(failure: Failure) -> Literal["UNKNOWN", "NOT_AVAILABLE"]:
    if failure in {
        Failure.CAPABILITY_NOT_CONFIGURED,
        Failure.STORAGE_UNAVAILABLE,
        Failure.OPERATOR_QUERY_BUDGET_EXCEEDED,
        Failure.CATALOG_CONTRACT_INVALID,
    }:
        return "UNKNOWN"
    return "NOT_AVAILABLE"


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _read(scope: ReadScope, query: CatalogQuery, version: str | None = None):
    return read_local_cpi(
        scope,
        reference=query.reference,
        as_of=datetime.fromisoformat(query.as_of),
        version_identity=version,
    )


def create_app(
    scope_factory: ScopeFactory | None = None,
    *,
    ui_directory: Path | None = None,
    observability_host: ObservabilityHost | None = None,
    jobs_host: CpiJobsHost | None = None,
    schedules_host: CpiSchedulesHost | None = None,
    evidence_mode: Literal["OFFLINE_FIXTURE", "AUTHORIZED_READ_SCOPE"] = (
        "AUTHORIZED_READ_SCOPE"
    ),
) -> FastAPI:
    """Return an inert ASGI app; the host owns scope admission and consistency.

    No PG adapter is installed by this slice. A future host binding needs its own
    authorization/qualification. Never pass a live mutable ledger as a snapshot.
    observability_host is a separate, explicitly admitted local read capability;
    it is never inferred from the CPI scope, environment, or the serving process.
    jobs_host is a separate trusted identity/journal binding; absent it, manual
    writes fail closed. HTTP acceptance never invokes a source or worker.
    """
    app = FastAPI(
        title="MDF R5 bounded CPI", version="r5-i5", docs_url=None, redoc_url=None
    )
    evidence: Evidence = "UNCONFIGURED" if scope_factory is None else evidence_mode

    @app.middleware("http")
    async def reject_duplicate_query(request: Request, call_next):
        if request.url.path in {
            "/api/r5/cpi",
            "/api/r5/cpi/read",
            "/api/r5/sources",
            "/api/r5/observability",
            "/api/r5/cpi/jobs",
        }:
            keys = [key for key, _ in request.query_params.multi_items()]
            if len(keys) != len(set(keys)):
                return JSONResponse(
                    status_code=422,
                    headers={"Cache-Control": "no-store"},
                    content=ErrorDTO(
                        status="NOT_AVAILABLE", classification=Failure.INVALID_REQUEST
                    ).model_dump(mode="json"),
                )
        response = await call_next(request)
        if request.url.path.startswith("/api/r5/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError):
        # FastAPI's default validation body echoes inputs and validator details.
        return JSONResponse(
            status_code=422,
            content=ErrorDTO(
                status="NOT_AVAILABLE", classification=Failure.INVALID_REQUEST
            ).model_dump(mode="json"),
        )

    @app.get(
        "/api/r5/observability",
        response_model=ObservabilityDTO,
        responses={422: {"model": ErrorDTO}},
        operation_id="runtime_observability",
    )
    def observability(
        query: Annotated[ObservabilityQuery, Query()],
    ) -> ObservabilityDTO:
        return project(observability_host)

    @app.get(
        "/api/r5/sources",
        response_model=DirectoryDTO,
        responses={422: {"model": ErrorDTO}, 503: {"model": ErrorDTO}},
        operation_id="source_directory",
    )
    def sources(query: Annotated[DirectoryQuery, Query()]):
        try:
            return directory(query)
        except Exception:
            # A changed/invalid code contract cannot yield a partial inventory.
            return JSONResponse(
                status_code=503,
                content=ErrorDTO(
                    status="UNKNOWN", classification=Failure.CATALOG_CONTRACT_INVALID
                ).model_dump(mode="json"),
            )

    @app.get(
        "/api/r5/cpi",
        response_model=CatalogDTO,
        responses={422: {"model": ErrorDTO}},
        operation_id="cpi_catalog",
    )
    def catalog(query: Annotated[CatalogQuery, Query()]) -> CatalogDTO:
        definition = DefinitionDTO(
            identity=ASSET_ID,
            revision=BLS_CPI_HEADLINES.revision,
            title="美国 CPI · 四项核心指标",
            source_qualified_contract=BLS_CPI_HEADLINES.source_qualified,
        )
        asset = service = None
        failure = Failure.CAPABILITY_NOT_CONFIGURED
        if scope_factory is not None:
            try:
                with scope_factory() as scope:
                    metadata = inspect_operator_status(
                        scope,
                        BLS_CPI_HEADLINES.key,
                        as_of=datetime.fromisoformat(query.as_of),
                    )
                    versions = [
                        VersionDTO(
                            identity=item.version.identity,
                            frozen_at=_iso(item.frozen_at),
                            completeness=item.completeness,
                            selection_state=item.selection_state,
                            binding_identity=(
                                None
                                if item.binding_record is None
                                else item.binding_record.identity
                            ),
                            binding_revision=(
                                None
                                if item.binding_record is None
                                else item.binding_record.revision
                            ),
                            publications=[
                                PublicationDTO(
                                    identity=pub.publication.identity,
                                    decision=pub.decision.value,
                                    decision_at=_iso(pub.decision_at),
                                    usable_at=_iso(pub.usable_at),
                                    effective_at=_iso(pub.effective_at),
                                    pending_clocks=list(pub.pending_clocks),
                                )
                                for pub in item.publications
                            ],
                        )
                        for item in metadata.as_of.candidates
                    ]
                    try:
                        view = _read(scope, query)
                        service = ServiceDTO(
                            identity=SERVICE_ID,
                            asset_identity=ASSET_ID,
                            version_identity=view.version.identity,
                            publication_identity=view.publication.key.identity,
                        )
                        failure = None
                    except (ReadContractError, CpiReadRejected) as error:
                        failure = _classification(error)
                        # Only domain denials retain the inspected metadata.
                        # Operational failures invalidate the entire catalog.
                        if _state(failure) == "UNKNOWN":
                            raise
                    asset = AssetDTO(
                        identity=ASSET_ID,
                        definition_revision=metadata.definition.revision,
                        local_holding=(
                            "VERIFIED_FOR_REQUEST" if service else "UNKNOWN"
                        ),
                        qualification=failure,
                        versions=versions,
                    )
                # Results become visible only after successful scope exit.
            except Exception as error:
                asset = service = None
                failure = _classification(error)
        return CatalogDTO(
            status="OK" if failure is None else _state(failure),
            classification=failure,
            evidence_mode=evidence,
            reference=query.reference,
            as_of=query.as_of,
            definition=definition,
            asset=asset,
            service=service,
        )

    @app.get(
        "/api/r5/cpi/read",
        response_model=ReadDTO,
        responses={
            409: {"model": ErrorDTO},
            503: {"model": ErrorDTO},
            422: {"model": ErrorDTO},
        },
        operation_id="cpi_read",
    )
    def read(query: Annotated[ReadQuery, Query()]):
        failure = Failure.CAPABILITY_NOT_CONFIGURED
        if scope_factory is not None:
            try:
                with scope_factory() as scope:
                    view = _read(scope, query, query.version_identity)
                    content = json.loads(view.content)
                    data = content["data"]
                    result = ReadDTO(
                        evidence_mode=evidence,
                        asset_identity=ASSET_ID,
                        definition_revision=view.definition.revision,
                        version_identity=view.version.identity,
                        publication_identity=view.publication.key.identity,
                        publication_decision_at=_iso(view.publication.decision_at),
                        publication_usable_at=_iso(view.publication.usable_at),
                        reference=query.reference,
                        as_of=query.as_of,
                        mode=query.mode,
                        content_sha256=view.version.content_digest,
                        obtained_at=content.get("obtained_at"),
                        measurements=[
                            MeasurementDTO(
                                **{
                                    key: item[key]
                                    for key in MeasurementDTO.model_fields
                                }
                            )
                            for item in data["measurements"]
                        ],
                    )
                return result
            except Exception as error:
                failure = _classification(error)
        return JSONResponse(
            status_code=503 if _state(failure) == "UNKNOWN" else 409,
            content=ErrorDTO(status=_state(failure), classification=failure).model_dump(
                mode="json"
            ),
        )

    install_jobs(app, jobs_host, ErrorDTO)
    install_schedules(app, schedules_host, ErrorDTO)

    if ui_directory is not None:
        app.mount("/", StaticFiles(directory=ui_directory, html=True), name="webui")
    return app
