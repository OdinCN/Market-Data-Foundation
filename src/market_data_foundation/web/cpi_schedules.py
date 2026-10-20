"""Strict HTTP acceptance/control of finite CPI plans; never executes a tick."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from market_data_foundation.canonical.model import ContractError, require
from market_data_foundation.historical.cpi_history import HistoryQualification
from market_data_foundation.historical.cpi_scheduling import CpiPlanStore, PlanSpec
from market_data_foundation.web.cpi_jobs import (
    CpiJobsHost,
    Identity,
    PublicIdentity,
    _operator,
)
from market_data_foundation.web.dto import DTO


class PlanIntent(DTO):
    plan_identity: Identity
    asset_identity: PublicIdentity
    asset_revision: PublicIdentity
    name: Identity
    start: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    end: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    timezone: str = Field(min_length=1, max_length=64)
    window_start: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    window_end: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    first_due: str = Field(min_length=20, max_length=40)
    interval_seconds: int = Field(ge=60, le=31622400)
    cycles: int = Field(ge=1, le=12)


class PlanAction(DTO):
    action: Literal["pause", "resume", "cancel"]


class Backfill(DTO):
    start: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    end: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")


class ChildDTO(DTO):
    job_identity: Identity
    state: Literal[
        "pending", "running", "failed", "cancelled", "succeeded", "outcome_unknown"
    ]
    attempt_identity: str | None
    raw_sha256: str | None
    version_identity: str | None
    publication_occurrence: str | None
    read_as_of: str | None
    classification: str | None
    reconciliation: str | None


class BatchDTO(DTO):
    key: str
    reference: str
    jobs: list[ChildDTO] = Field(max_length=3)
    complete: bool


class PlanSpecDTO(DTO):
    name: str
    start: str
    end: str
    timezone: str
    window_start: str
    window_end: str
    first_due: str
    interval_seconds: int
    cycles: int
    effect_domain: Literal["synthetic", "owner_nonproduction"]


class PlanDTO(DTO):
    identity: Identity
    spec: PlanSpecDTO
    state: Literal["active", "paused", "cancelled", "completed"]
    cycle: int
    cursor: int
    inflight_key: str | None
    retry_queue: list[str]
    next_due: str
    created_at: str
    updated_at: str
    batches: list[BatchDTO] = Field(max_length=4320)
    completed_batches: int
    total_batches: int


class PlansDTO(DTO):
    status: Literal["OK", "UNKNOWN"]
    classification: str | None
    evidence_mode: Literal["UNCONFIGURED", "OFFLINE_FIXTURE", "AUTHORIZED_HOST"]
    write_eligible: bool
    asset_identity: str | None
    asset_revision: str | None
    tier: Literal["unregistered", "registered"] | None
    plans: list[PlanDTO] = Field(max_length=50)


class PlanAccepted(DTO):
    status: Literal["OK"] = "OK"
    acceptance: Literal["DURABLE"] = "DURABLE"
    plan_identity: Identity


class PlanError(DTO):
    status: Literal["UNKNOWN", "NOT_AVAILABLE"]
    classification: str


@dataclass(frozen=True)
class CpiSchedulesHost:
    jobs: CpiJobsHost
    store: CpiPlanStore
    qualification: HistoryQualification

    def __post_init__(self):
        require(
            self.store.journal.path.resolve() == self.jobs.journal.path.resolve(),
            "cpi_plan_journal_mismatch",
        )
        require(
            type(self.qualification) is HistoryQualification,
            "cpi_history_qualification_required",
        )


def install_schedules(app: FastAPI, host: CpiSchedulesHost | None, validation_error):
    def authorization(request, write=False):
        operator = _operator(None if host is None else host.jobs, request)
        if host is None or operator is None or (write and not operator.write_eligible):
            return None
        return operator

    def failure(error):
        code = error.args[0] if type(error) is ContractError and error.args else None
        allowed = {
            "cpi_plan_not_found",
            "cpi_plan_identity_reused",
            "cpi_plan_terminal",
            "cpi_plan_action_invalid",
            "cpi_plan_window_invalid",
            "cpi_plan_timezone_invalid",
            "cpi_plan_due_invalid",
            "cpi_plan_failed_window_required",
            "cpi_plan_backfill_reconciliation_required",
            "cpi_plan_retry_budget_exceeded",
            "cpi_history_range_budget_exceeded",
            "job_executor_busy",
            "job_target_unsupported",
        }
        code = (
            code
            if type(code) is str and code in allowed
            else "cpi_plan_storage_unavailable"
        )
        return JSONResponse(
            status_code=503 if code == "cpi_plan_storage_unavailable" else 409,
            content=PlanError(
                status="UNKNOWN"
                if code == "cpi_plan_storage_unavailable"
                else "NOT_AVAILABLE",
                classification=code,
            ).model_dump(),
        )

    def denied():
        return JSONResponse(
            status_code=403,
            content=PlanError(
                status="NOT_AVAILABLE", classification="operator_not_authorized"
            ).model_dump(),
        )

    def require_plan_domain(identity):
        # Accepted plan specs are immutable. Check with a read-only connection
        # before entering any mutation/lock, and expose no foreign plan state.
        document = host.store.reader().get(identity)
        require(
            document["spec"]["effect_domain"] == host.jobs.spec.effect_domain,
            "cpi_plan_not_found",
        )

    errors = {
        403: {"model": PlanError},
        409: {"model": PlanError},
        503: {"model": PlanError},
        422: {"model": validation_error},
    }

    @app.get(
        "/api/r5/cpi/plans",
        response_model=PlansDTO,
        responses=errors,
        operation_id="cpi_plans",
    )
    def plans(request: Request):
        operator = authorization(request)
        if host is not None and operator is None:
            return denied()
        data = dict(
            evidence_mode="UNCONFIGURED" if host is None else host.jobs.evidence_mode,
            write_eligible=operator is not None and operator.write_eligible,
            asset_identity=None if host is None else host.jobs.spec.definition,
            asset_revision=None if host is None else host.jobs.spec.definition_revision,
            tier=None if host is None else host.qualification.tier,
        )
        if host is None:
            return PlansDTO(
                status="UNKNOWN",
                classification="cpi_plan_capability_not_configured",
                plans=[],
                **data,
            )
        try:
            return PlansDTO(
                status="OK",
                classification=None,
                plans=host.store.reader().snapshot(
                    effect_domain=host.jobs.spec.effect_domain
                ),
                **data,
            )
        except Exception:
            return PlansDTO(
                status="UNKNOWN",
                classification="cpi_plan_storage_unavailable",
                plans=[],
                **data,
            )

    @app.post(
        "/api/r5/cpi/plans",
        response_model=PlanAccepted,
        status_code=202,
        responses=errors,
        operation_id="cpi_plan_accept",
    )
    def accept(request: Request, intent: PlanIntent):
        if authorization(request, True) is None:
            return denied()
        try:
            require(
                (intent.asset_identity, intent.asset_revision)
                == (host.jobs.spec.definition, host.jobs.spec.definition_revision),
                "job_target_unsupported",
            )
            spec = PlanSpec(
                **intent.model_dump(
                    exclude={"plan_identity", "asset_identity", "asset_revision"}
                ),
                effect_domain=host.jobs.spec.effect_domain,
            )
            # Parse the instant here; no browser source/storage/provider selection.
            datetime.fromisoformat(spec.first_due)
            host.store.accept(intent.plan_identity, spec, host.jobs.clock())
            return PlanAccepted(plan_identity=intent.plan_identity)
        except Exception as error:
            return failure(error)

    @app.post(
        "/api/r5/cpi/plans/{plan_identity}/action",
        response_model=PlanAccepted,
        status_code=202,
        responses=errors,
        operation_id="cpi_plan_action",
    )
    def action(request: Request, plan_identity: Identity, body: PlanAction):
        if authorization(request, True) is None:
            return denied()
        try:
            require_plan_domain(plan_identity)
            host.store.action(plan_identity, body.action, host.jobs.clock())
            return PlanAccepted(plan_identity=plan_identity)
        except Exception as error:
            return failure(error)

    @app.post(
        "/api/r5/cpi/plans/{plan_identity}/backfill",
        response_model=PlanAccepted,
        status_code=202,
        responses=errors,
        operation_id="cpi_plan_backfill",
    )
    def backfill(request: Request, plan_identity: Identity, body: Backfill):
        if authorization(request, True) is None:
            return denied()
        try:
            require_plan_domain(plan_identity)
            host.store.backfill(plan_identity, body.start, body.end, host.jobs.clock())
            return PlanAccepted(plan_identity=plan_identity)
        except Exception as error:
            return failure(error)
