"""Manual CPI acceptance and redacted views over the existing local journal.

No backend, identity, credential, source, worker or storage client is inferred.
The trusted host authenticates each request and checks write/CSRF admission.
The separate worker uses CpiLocalJob, never an HTTP background task.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from market_data_foundation.canonical import codec
from market_data_foundation.canonical.consumer_read import ReadFailure
from market_data_foundation.canonical.model import ContractError, digest, require
from market_data_foundation.historical.cpi_job_journal import (
    FileJobJournal,
    valid_identity,
)
from market_data_foundation.historical.cpi_local_job import (
    CpiJobSpec,
    CpiLocalJob,
    DirectCpiAcquirer,
)
from market_data_foundation.web.dto import DTO

Identity = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
PublicIdentity = Annotated[str, Field(pattern=r"^[a-zA-Z0-9:._-]{1,256}$")]
ReleaseSlot = Annotated[str, Field(pattern=r"^[a-zA-Z0-9/:._-]{1,128}$")]
Stage = Literal[
    "registered",
    "preflight",
    "acquiring",
    "validating_candidate",
    "planned",
    "materializing",
    "raw_write",
    "raw_verified",
    "ledger_commit",
    "publication_possible",
    "independent_readback",
    "local_read_verified",
    "stopped",
    "cancelled_before_effects",
    "cancelled_before_storage",
    "restart_detected",
    "reconciled",
    "reconciliation_unresolved",
]
State = Literal[
    "pending", "running", "succeeded", "failed", "cancelled", "outcome_unknown"
]
JobClassification = Literal[
    "preflight_rejected",
    "source_or_plan_rejected",
    "candidate_rejected",
    "plan_rejected",
    "material_protection_unconfirmed",
    "raw_write_unconfirmed",
    "ledger_handoff_unconfirmed",
    "ledger_commit_outcome_unknown",
    "publication_outcome_unknown",
    "independent_readback_rejected",
    "job_action_rejected",
    "interrupted_attempt",
    "no_durable_plan_no_reexecution",
    "bls_live_credential_invalid",
    "bls_live_transport_failure",
    "bls_live_response_too_large",
    "bls_live_redirect_rejected",
    "bls_live_http_failure",
    "bls_live_content_encoding_rejected",
    "bls_live_reference_not_available",
    "bls_live_response_contract_failed",
    "bls_live_credential_persistence_forbidden",
    "bls_live_artifact_write_failed",
]
Classification = JobClassification | ReadFailure


class Intent(DTO):
    job_identity: Identity
    asset_identity: PublicIdentity
    asset_revision: PublicIdentity
    reference: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    release_slot: ReleaseSlot
    mode: Literal["manual_once"]


class JobQuery(DTO):
    job_identity: Identity | None = None


class Empty(DTO):
    pass


class JobError(DTO):
    status: Literal["UNKNOWN", "NOT_AVAILABLE"]
    classification: Literal[
        "operator_not_authorized",
        "job_capability_not_configured",
        "job_storage_unavailable",
        "job_not_found",
        "job_identity_reused",
        "job_scope_already_registered",
        "job_target_unsupported",
        "job_query_budget_exceeded",
        "job_record_invalid",
    ]


class EventDTO(DTO):
    state: State
    stage: Stage
    attempt_identity: PublicIdentity | None
    worker_identity: Identity | None
    updated_at: str
    classification: Classification | None
    cancel_requested: bool


class JobDTO(EventDTO):
    job_identity: Identity
    asset_identity: PublicIdentity
    asset_revision: PublicIdentity
    reference: str
    release_slot: ReleaseSlot
    attempts: Literal[0, 1]
    created_at: str
    started_at: str | None
    finished_at: str | None
    failure_stage: Stage | None
    version_identity: PublicIdentity | None
    publication_occurrence: PublicIdentity | None
    reconciliation: (
        Literal["confirmed_published", "confirmed_not_published", "unresolved"] | None
    )
    history: list[EventDTO] = Field(max_length=256)


class JobsDTO(DTO):
    status: Literal["OK", "UNKNOWN"]
    classification: (
        Literal[
            "job_capability_not_configured",
            "job_storage_unavailable",
            "job_query_budget_exceeded",
            "job_record_invalid",
        ]
        | None
    )
    evidence_mode: Literal["UNCONFIGURED", "OFFLINE_FIXTURE", "AUTHORIZED_HOST"]
    operator_identity: Identity | None
    write_eligible: bool
    asset_identity: str | None
    asset_revision: str | None
    reference: str | None
    release_slot: str | None
    intent_identity: Identity | None
    storage_target: Literal["INHERITED_03_04"] = "INHERITED_03_04"
    jobs: list[JobDTO] = Field(max_length=100)


class AcceptedDTO(DTO):
    status: Literal["OK"] = "OK"
    job_identity: Identity
    # Acceptance is durable; execution status must be read from the journal.
    acceptance: Literal["DURABLE"] = "DURABLE"


@dataclass(frozen=True)
class Operator:
    """Server identity only. Host must authenticate and authorize per request.

    authorize MUST include CSRF/origin protection for cookie-based sessions.
    A browser header/body role or unverified ASGI scope is not authentication.
    """

    identity: str
    write_eligible: bool


@dataclass(frozen=True)
class CpiJobsHost:
    journal: FileJobJournal
    spec: CpiJobSpec
    clock: Callable[[], datetime]
    authorize: Callable[[Request], Operator | None]
    evidence_mode: Literal["OFFLINE_FIXTURE", "AUTHORIZED_HOST"]

    def __post_init__(self):
        require(type(self.journal) is FileJobJournal, "job_journal_required")
        require(not self.journal.read_only, "job_writable_journal_required")
        require(type(self.spec) is CpiJobSpec, "cpi_job_spec_invalid")
        require(self.spec.mode == "acquire", "job_target_unsupported")
        require(
            self.evidence_mode in {"OFFLINE_FIXTURE", "AUTHORIZED_HOST"}
            and (self.spec.effect_domain == "synthetic")
            == (self.evidence_mode == "OFFLINE_FIXTURE"),
            "job_evidence_mode_mismatch",
        )

    def reader(self) -> FileJobJournal:
        return FileJobJournal(self.journal.directory, read_only=True)


@dataclass(frozen=True)
class CpiManualWorker:
    """Explicit single action seam for a separately invoked trusted host worker.

    No daemon/CLI/scheduling is installed. Only pending jobs execute; interrupted
    and ambiguous jobs go through observation-only reconciliation, never retry.
    Host owns worker identity, source/target qualification and preflight opt-in.
    """

    identity: str
    engine: CpiLocalJob
    spec: CpiJobSpec
    preflight: Callable[[CpiJobSpec], None]
    acquirer: DirectCpiAcquirer

    def process(self, job_identity: str, *, enabled: bool = False) -> dict:
        require(enabled is True, "cpi_job_explicit_opt_in_required")
        require(type(self.engine) is CpiLocalJob, "cpi_job_engine_required")
        require(self.spec.mode == "acquire", "job_target_unsupported")
        # Identity validation does not create a file or touch an external client.
        valid_identity(self.identity)
        document = self.engine.journal.get(job_identity)
        require(document["spec"] == self.spec.document(), "job_target_unsupported")
        if document["state"] in {"running", "outcome_unknown"}:
            return self.engine.reconcile(job_identity, enabled=True)
        return self.engine.run(
            job_identity,
            enabled=True,
            preflight=self.preflight,
            acquirer=self.acquirer,
            worker_identity=self.identity,
        )


def _operator(host: CpiJobsHost | None, request: Request) -> Operator | None:
    if host is None:
        return None
    try:
        value = host.authorize(request)
        if type(value) is not Operator or type(value.write_eligible) is not bool:
            return None
        valid_identity(value.identity)
        return value
    except Exception:
        return None


def _error(error: Exception) -> JobError:
    # Only known constant ContractError codes cross this boundary; never str(exc).
    code = error.args[0] if type(error) is ContractError and error.args else None
    allowed = {
        "job_not_found",
        "job_identity_reused",
        "job_scope_already_registered",
        "job_target_unsupported",
        "job_query_budget_exceeded",
    }
    code = code if type(code) is str and code in allowed else "job_storage_unavailable"
    return JobError(
        status="NOT_AVAILABLE" if code in allowed else "UNKNOWN", classification=code
    )


def _event(event: dict) -> EventDTO:
    return EventDTO(
        state=event["state"],
        stage=event["stage"],
        attempt_identity=event["attempt_id"],
        worker_identity=event.get("worker_identity"),
        updated_at=_time(event["updated_at"]),
        classification=(
            ReadFailure(event["classification"])
            if event["classification"] in ReadFailure
            else event["classification"]
        ),
        cancel_requested=event["cancel_requested"],
    )


def _time(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    require(
        parsed.tzinfo is not None and parsed.utcoffset() is not None, "clock_invalid"
    )
    return parsed.isoformat()


def _job(document: dict, history: tuple, spec: CpiJobSpec) -> JobDTO:
    require(document["spec"] == spec.document(), "job_target_unsupported")
    plan = document["plan"]
    return JobDTO(
        **_event(document).model_dump(),
        job_identity=document["job_id"],
        asset_identity=spec.definition,
        asset_revision=spec.definition_revision,
        reference=spec.reference,
        release_slot=spec.release_slot,
        attempts=document["attempts"],
        created_at=_time(document["created_at"]),
        started_at=None
        if document["started_at"] is None
        else _time(document["started_at"]),
        finished_at=None
        if document["finished_at"] is None
        else _time(document["finished_at"]),
        failure_stage=document["failure_stage"],
        version_identity=None
        if plan is None
        else codec.loads(plan["version"]).identity,
        publication_occurrence=(
            None
            if document["last_success"] is None
            else document["last_success"]["evidence"]["publication_occurrence"]
        ),
        reconciliation=(
            None
            if document["reconciliation"] is None
            else document["reconciliation"]["outcome"]
        ),
        history=[_event(e) for e in history],
    )


def install_jobs(
    app: FastAPI, host: CpiJobsHost | None, validation_error: type[DTO]
) -> None:
    responses = {code: {"model": JobError} for code in (403, 409, 503)}
    responses[422] = {"model": validation_error}

    @app.get(
        "/api/r5/cpi/jobs",
        response_model=JobsDTO,
        responses={
            403: {"model": JobError},
            404: {"model": JobError},
            422: {"model": validation_error},
        },
        operation_id="cpi_jobs",
    )
    def jobs(request: Request, query: Annotated[JobQuery, Query()]):
        operator = _operator(host, request)
        if host is not None and operator is None:
            return JSONResponse(
                status_code=403,
                content=JobError(
                    status="NOT_AVAILABLE", classification="operator_not_authorized"
                ).model_dump(),
            )
        data = dict(
            evidence_mode="UNCONFIGURED" if host is None else host.evidence_mode,
            operator_identity=None if operator is None else operator.identity,
            write_eligible=operator is not None and operator.write_eligible,
            asset_identity=None if host is None else host.spec.definition,
            asset_revision=None if host is None else host.spec.definition_revision,
            reference=None if host is None else host.spec.reference,
            release_slot=None if host is None else host.spec.release_slot,
            intent_identity=(
                None
                if host is None
                else "cpi-init-"
                + digest(json.dumps(host.spec.document(), sort_keys=True).encode())[:40]
            ),
        )
        if host is None:
            return JobsDTO(
                status="UNKNOWN",
                classification="job_capability_not_configured",
                jobs=[],
                **data,
            )
        try:
            snapshot = host.reader().snapshot(query.job_identity)
            projected = []
            for document, history in snapshot:
                if document["spec"] != host.spec.document():
                    require(query.job_identity is None, "job_target_unsupported")
                    continue  # The same journal may also contain admitted PPI jobs.
                projected.append(_job(document, history, host.spec))
            return JobsDTO(status="OK", classification=None, jobs=projected, **data)
        except Exception as error:
            failure = _error(error)
            if failure.classification in {"job_not_found", "job_target_unsupported"}:
                return JSONResponse(status_code=404, content=failure.model_dump())
            code = failure.classification
            if (
                isinstance(error, (ValueError, KeyError, TypeError))
                and code != "job_query_budget_exceeded"
            ):
                code = "job_record_invalid"
            return JobsDTO(status="UNKNOWN", classification=code, jobs=[], **data)

    def writable(request: Request):
        operator = _operator(host, request)
        if host is None or operator is None or not operator.write_eligible:
            return JSONResponse(
                status_code=403,
                content=JobError(
                    status="NOT_AVAILABLE", classification="operator_not_authorized"
                ).model_dump(),
            )
        return None

    @app.post(
        "/api/r5/cpi/jobs",
        response_model=AcceptedDTO,
        status_code=202,
        responses=responses,
        operation_id="cpi_job_accept",
    )
    def accept(request: Request, intent: Intent):
        denied = writable(request)
        if denied is not None:
            return denied
        try:
            spec = host.spec
            require(
                (
                    intent.asset_identity,
                    intent.asset_revision,
                    intent.reference,
                    intent.release_slot,
                )
                == (
                    spec.definition,
                    spec.definition_revision,
                    spec.reference,
                    spec.release_slot,
                ),
                "job_target_unsupported",
            )
            host.journal.submit(
                intent.job_identity, spec.document(), spec.scope, host.clock()
            )
            return AcceptedDTO(job_identity=intent.job_identity)
        except Exception as error:
            failure = _error(error)
            return JSONResponse(
                status_code=503 if failure.status == "UNKNOWN" else 409,
                content=failure.model_dump(),
            )

    @app.post(
        "/api/r5/cpi/jobs/{job_identity}/cancel",
        response_model=AcceptedDTO,
        status_code=202,
        responses=responses,
        operation_id="cpi_job_cancel",
    )
    def cancel(request: Request, job_identity: Identity, body: Empty):
        denied = writable(request)
        if denied is not None:
            return denied
        try:
            require(
                host.reader().get(job_identity)["spec"] == host.spec.document(),
                "job_target_unsupported",
            )
            host.journal.cancel(job_identity, host.clock())
            return AcceptedDTO(job_identity=job_identity)
        except Exception as error:
            failure = _error(error)
            return JSONResponse(
                status_code=503 if failure.status == "UNKNOWN" else 409,
                content=failure.model_dump(),
            )
