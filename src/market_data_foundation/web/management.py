"""R3 A1 catalog projection and explicit trusted management read boundary.

No discovery, environment lookup, provider or database connection is performed.
Instance absence is a fact about this read scope, never evidence of downtime.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field, field_validator

from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from market_data_foundation.web.dto import DTO
from market_data_foundation.web.observability import parse_time
from market_data_foundation.web.source_catalog import (
    DirectoryEntryDTO,
    DirectoryQuery,
    directory,
)

PublicIdentity = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:@-]{0,511}$")]
Ownership = Literal["MDF_MANAGED", "OWNER_MANAGED", "OBSERVE_ONLY"]
FAMILIES = (
    ("CPI", "美国 CPI", "cpi_history"),
    ("PPI", "美国 PPI", "bls_ppi_final_demand"),
    ("EMPLOYMENT", "美国就业", "bls_employment"),
    ("PCE", "美国 PCE", "bea_pce"),
    ("CONSUMPTION", "美国消费与收入", "bea_consumption"),
    ("GDP", "美国 GDP 与增长", "gdp_contract"),
    ("RETAIL", "美国零售", "census_retail"),
    ("CLAIMS", "美国初请与续请失业金", "claims_contract"),
    ("JOLTS", "美国职位空缺与劳动力流动", "jolts_binding"),
    ("M3", "美国耐用品与核心资本品", "m3_contract"),
    ("FINANCIAL_DAILY", "金融条件日频（十项）", "financial_daily_catalog"),
    ("FRED_ALFRED", "FRED / ALFRED vintage", "fred_alfred_vintage"),
    ("FOMC", "FOMC 声明", "fomc_statement"),
    ("ECB", "ECB SDMX", "ECB"),
    ("EUROSTAT", "Eurostat SDMX", "ESTAT"),
)


class InstanceRecord(DTO):
    """Only public symbolic references; no URL, DSN, credential or raw message."""

    identity: PublicIdentity
    catalog_identity: PublicIdentity
    ownership: Ownership
    endpoint_ref: PublicIdentity | None = None
    observed_at: str | None = Field(default=None, max_length=40)
    expires_at: str | None = Field(default=None, max_length=40)
    state: Literal["UNKNOWN", "AVAILABLE", "UNAVAILABLE"] = "UNKNOWN"

    @field_validator("observed_at", "expires_at")
    @classmethod
    def aware(cls, value):
        if value is not None:
            parse_time(value)
        return value


class InstanceDTO(DTO):
    record: InstanceRecord
    freshness: Literal["UNKNOWN", "FRESH", "EXPIRED"]
    effective_state: Literal["UNKNOWN", "AVAILABLE", "UNAVAILABLE"]
    classification: Literal["not_observed", "clock_invalid", "expired", "observed"]
    valid_for_ms: int = Field(default=0, ge=0, le=60000)
    commands: list[str] = Field(default_factory=list, max_length=0)


class FamilyDTO(DTO):
    identity: str
    title: str
    catalog_identities: list[str] = Field(min_length=1, max_length=8)
    qualification: Literal["UNKNOWN"] = "UNKNOWN"


class ManagementDTO(DTO):
    status: Literal["OK"] = "OK"
    evidence_mode: Literal[
        "CODE_CONTRACTS_ONLY", "OFFLINE_FIXTURE", "AUTHORIZED_MANAGEMENT_READ"
    ]
    instance_scope: Literal["UNCONFIGURED", "TRUSTED_READ"]
    persistence: Literal["DEV_PG_GATE_PENDING"] = "DEV_PG_GATE_PENDING"
    entries: list[DirectoryEntryDTO] = Field(max_length=32)
    families: list[FamilyDTO] = Field(
        min_length=len(FAMILIES), max_length=len(FAMILIES)
    )
    instances: list[InstanceDTO] = Field(max_length=64)
    deferred: list[Literal["QMT"]] = Field(default_factory=lambda: ["QMT"])


class ManagementErrorDTO(DTO):
    status: Literal["UNKNOWN", "NOT_AVAILABLE"]
    classification: Literal["invalid_request", "management_read_unavailable"]


@dataclass(frozen=True)
class ManagementHost:
    """Host admission is out of band. HTTP never selects a scope or identity."""

    read: Callable[[], tuple[InstanceRecord, ...]]
    evidence_mode: Literal["OFFLINE_FIXTURE", "AUTHORIZED_MANAGEMENT_READ"]
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


def observe(record: InstanceRecord, now: datetime) -> InstanceDTO:
    freshness = "UNKNOWN"
    classification = "not_observed"
    valid_for_ms = 0
    if record.observed_at and record.expires_at:
        start, end = parse_time(record.observed_at), parse_time(record.expires_at)
        if (
            now.tzinfo is None
            or start > now
            or end <= start
            or (end - start).total_seconds() > 60
        ):
            classification = "clock_invalid"
        elif end <= now:
            freshness, classification = "EXPIRED", "expired"
        else:
            freshness, classification = "FRESH", "observed"
            valid_for_ms = int((end - now).total_seconds() * 1000)
    return InstanceDTO(
        record=record,
        freshness=freshness,
        effective_state=record.state if freshness == "FRESH" else "UNKNOWN",
        classification=classification,
        valid_for_ms=valid_for_ms,
    )


def project_management(host: ManagementHost | None = None) -> ManagementDTO:
    entries = directory(DirectoryQuery()).entries
    families = []
    for identity, title, selector in FAMILIES:
        matched = [
            e.identity
            for e in entries
            if e.domain == "Macro"
            and (
                e.source_name == selector
                if selector in {"ECB", "ESTAT"}
                else e.component_identity.endswith("." + selector)
                or (
                    identity == "CPI"
                    and e.capability_identity == BLS_CPI_HEADLINES.identity
                )
            )
        ]
        families.append(
            FamilyDTO(identity=identity, title=title, catalog_identities=matched)
        )
    records = () if host is None else host.read()
    known = {e.identity: e for e in entries}
    if (
        len(records) > 64
        or len({r.identity for r in records}) != len(records)
        or any(r.catalog_identity not in known for r in records)
        or any(
            known[r.catalog_identity].domain == "KD" and r.ownership == "MDF_MANAGED"
            for r in records
        )
    ):
        raise ValueError("management_identity_invalid")
    now = datetime.now(UTC) if host is None else host.now()
    return ManagementDTO(
        evidence_mode="CODE_CONTRACTS_ONLY" if host is None else host.evidence_mode,
        instance_scope="UNCONFIGURED" if host is None else "TRUSTED_READ",
        entries=entries,
        families=families,
        instances=[observe(r, now) for r in sorted(records, key=lambda r: r.identity)],
    )


def create_management_app(host: ManagementHost | None = None) -> FastAPI:
    app = FastAPI(
        title="MDF R3 A1 read-only management",
        version="r3-a1-r1",
        docs_url=None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def no_store(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, _error):
        return JSONResponse(
            status_code=422,
            content={"status": "NOT_AVAILABLE", "classification": "invalid_request"},
        )

    @app.get(
        "/management",
        response_model=ManagementDTO,
        responses={
            422: {"model": ManagementErrorDTO},
            503: {"model": ManagementErrorDTO},
        },
        operation_id="management_directory",
    )
    def management(request: Request):
        if request.query_params:
            return JSONResponse(
                status_code=422,
                content={
                    "status": "NOT_AVAILABLE",
                    "classification": "invalid_request",
                },
            )
        try:
            return project_management(host)
        except Exception:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "UNKNOWN",
                    "classification": "management_read_unavailable",
                },
            )

    return app
