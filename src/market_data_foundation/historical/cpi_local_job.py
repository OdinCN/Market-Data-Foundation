"""Manual, opt-in, single CPI vertical; no default network/storage clients.

The journal is a restricted single-host file journal. The canonical Version and
Publication still belong exclusively to the existing kernel/ledger. Recovery
only observes exact identities; it never retries acquisition or publication.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, fields, replace
from datetime import datetime
from pathlib import Path
from weakref import ReferenceType, WeakKeyDictionary, ref

from psycopg.pq import TransactionStatus

from market_data_foundation.canonical import codec
from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    read_as_of,
    read_exact_version,
)
from market_data_foundation.canonical.consumer_wire import WIRE_REVISION, handle_request
from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import Kind, digest, instant, require
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.canonical.repository import Ledger
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from market_data_foundation.historical.bls_cpi_direct import (
    MAX_RESPONSE_BYTES,
    SEPTEMBER_REFERENCE_CONTRACT,
    SERIES_IDS,
    DirectCpiLivePlan,
    ReleaseCausation,
    build_future_live_plan,
)
from market_data_foundation.historical.bls_cpi_direct_live import (
    TIMEOUT_SECONDS,
    LiveExecutionCandidate,
    LiveExecutionError,
    Transport,
    execute_live_candidate,
)
from market_data_foundation.historical.bls_cpi_material_store import (
    MaterialIdentity,
    MaterializationOutcomeUnknown,
    MaterialStore,
    PostgresAdvisoryWriterProtection,
    WriterProtection,
    WriterProtectionTicket,
    materialize_persist_publish,
)
from market_data_foundation.historical.bls_cpi_release_runner import (
    REFERENCE,
    RELEASE,
    SLOT_IDENTITY,
)
from market_data_foundation.historical.cpi_job_journal import (
    FileJobJournal,
    valid_identity,
)
from market_data_foundation.historical.macro_lifecycle import (
    MeteredTransport,
    RetainedRepair,
    decode_plan,
    encode_plan,
    observe_exact_material,
)

DEV_SCHEMA = "mdf_local_first_cpi_operable_r1"
CONTRACT = SEPTEMBER_REFERENCE_CONTRACT
QUALITY = ("original", "projected")
# A live connection keeps an abandoned/ambiguous claim even if its engine is
# collected. Weak keys/owner references do not keep connections or engines alive.
_PG_SESSION_OWNERS: WeakKeyDictionary[
    object, tuple[ReferenceType[_PgJobProtection], bool]
] = WeakKeyDictionary()


@dataclass(frozen=True)
class CpiJobSpec:
    """One exact supported target; no arbitrary revision/calendar assertions.

    provider_revision is explicitly not_declared by the current BLS adapter.
    source_revision is the admitted operation contract, not a provider vintage.
    Only the existing September release slot is executable in this first slice.
    """

    definition: str
    definition_revision: str
    series: tuple[str, ...]
    reference: str
    source_identity: str
    source_revision: str
    provider_revision: str
    completeness: str
    quality: tuple[str, ...]
    release_slot: str
    mode: str
    max_provider_requests: int
    max_response_bytes: int
    timeout_seconds: float
    effect_domain: str
    expected_raw_sha256: str | None = None
    max_raw_creates: int = 1
    max_ledger_handoffs: int = 1
    max_publications: int = 1

    def __post_init__(self):
        require(
            self.definition == BLS_CPI_HEADLINES.identity
            and self.definition_revision == BLS_CPI_HEADLINES.revision
            and self.series == SERIES_IDS
            and self.reference == REFERENCE
            and self.source_identity == CONTRACT.source_identity
            and self.source_revision == CONTRACT.contract_revision
            and self.provider_revision == "not_declared"
            and self.completeness == CONTRACT.completeness
            and self.quality == QUALITY
            and self.release_slot == SLOT_IDENTITY,
            "cpi_job_target_not_admitted",
        )
        require(
            self.mode in {"acquire", "retained"}
            and type(self.max_provider_requests) is int
            and self.max_provider_requests == (1 if self.mode == "acquire" else 0)
            and type(self.max_response_bytes) is int
            and self.max_response_bytes == MAX_RESPONSE_BYTES
            and type(self.timeout_seconds) in {float, int}
            and self.timeout_seconds == TIMEOUT_SECONDS,
            "cpi_job_budget_not_admitted",
        )
        require(
            self.effect_domain in {"synthetic", "owner_nonproduction"}
            and all(
                type(value) is int
                for value in (
                    self.max_raw_creates,
                    self.max_ledger_handoffs,
                    self.max_publications,
                )
            )
            and (self.max_raw_creates, self.max_ledger_handoffs, self.max_publications)
            == (1, 1, 1),
            "cpi_job_effect_budget_not_admitted",
        )
        if self.expected_raw_sha256 is not None:
            MaterialIdentity(self.expected_raw_sha256, 1)
        require(
            self.mode != "retained" or self.expected_raw_sha256 is not None,
            "cpi_job_retained_digest_required",
        )

    def document(self) -> dict:
        return json.loads(
            json.dumps(
                {field.name: getattr(self, field.name) for field in fields(self)}
            )
        )

    @classmethod
    def from_document(cls, document: dict) -> CpiJobSpec:
        value = dict(document)
        value["series"] = tuple(value["series"])
        value["quality"] = tuple(value["quality"])
        return cls(**value)

    @property
    def scope(self) -> str:
        # Budget/mode/job ID never bypass the same-range reservation.
        return digest(
            codec.dumps(
                (
                    self.definition,
                    self.definition_revision,
                    self.series,
                    self.reference,
                    self.source_identity,
                    self.source_revision,
                    self.provider_revision,
                )
            ).encode()
        )


def _causation(job_id: str) -> ReleaseCausation:
    return ReleaseCausation(SLOT_IDENTITY, job_id, 1, REFERENCE, RELEASE)


@dataclass(frozen=True)
class DirectCpiAcquirer:
    """Owner seam: all clients/paths explicit; reuse one logical BLS request.

    A fake transport and synthetic key use this exact same path in deterministic
    tests. The host may explicitly inject ExactOneHttpsTransport after opt-in.
    """

    transport: Transport
    secret_path: Path
    artifact_root: Path
    clock: Callable[[], datetime]

    def acquire(
        self,
        job_id: str,
        before_request: Callable[[], None],
        after_request: Callable[[bool, int], None] = lambda _received, _attempts: None,
    ):
        return execute_live_candidate(
            secret_path=self.secret_path,
            artifact_root=self.artifact_root,
            transport=MeteredTransport(self.transport, before_request, after_request),
            clock=self.clock,
            identity_factory=lambda: job_id + "-a1",
            reference_contract=CONTRACT,
            release_causation=_causation(job_id),
        )


def _validate_candidate(
    spec: CpiJobSpec, candidate: LiveExecutionCandidate, job_id: str
) -> tuple[DirectCpiLivePlan, bytes]:
    require(type(candidate) is LiveExecutionCandidate, "cpi_job_candidate_invalid")
    evidence, plan = candidate.evidence, candidate.plan
    require(evidence.reference_contract == CONTRACT, "cpi_job_reference_mismatch")
    # Reconstruct from the actual typed evidence, never trust a caller's quality,
    # durability, coverage, publication, or pre-promoted material flags.
    if spec.mode == "acquire":
        identity = "p4-bls-cpi-direct-live-" + job_id + "-a1"
        causation = _causation(job_id)
    else:
        identity = plan.run.identity.removesuffix(":run")
        params = dict(plan.acquisition.parameters)
        causation = ReleaseCausation(
            params.get("release_slot_identity", ""),
            params.get("release_execution_identity", ""),
            int(params.get("release_attempt", "0")),
            params.get("release_reference", ""),
            datetime.fromisoformat(params.get("release_instant", "")),
        )
        require(
            causation.slot_identity == SLOT_IDENTITY and causation.release == RELEASE,
            "cpi_job_retained_release_mismatch",
        )
    expected = build_future_live_plan(
        evidence,
        identity=identity,
        finished_at=plan.run.finished_at,
        frozen_at=plan.version.frozen_at,
        release_causation=causation,
    )
    require(plan == expected, "cpi_job_plan_mismatch")
    raw = evidence.raw_bytes
    require(
        spec.expected_raw_sha256 is None or digest(raw) == spec.expected_raw_sha256,
        "cpi_job_retained_digest_mismatch",
    )
    require(
        evidence.requested_at >= RELEASE and plan.version.frozen_at >= RELEASE,
        "cpi_job_before_release",
    )
    return plan, raw


def _encode_plan(plan: DirectCpiLivePlan) -> dict:
    return encode_plan(plan)


def _decode_plan(value: dict) -> DirectCpiLivePlan:
    return decode_plan(value, DirectCpiLivePlan, "cpi_job_plan_invalid")


def _durable_plan(plan: DirectCpiLivePlan, identity: MaterialIdentity):
    return replace(
        plan,
        raw_material=replace(
            plan.raw_material,
            controlled_ref=identity.locator,
            durable=True,
            replayable=True,
        ),
    )


def _failure(exc: Exception, stage: str) -> str:
    if isinstance(exc, ReadContractError):
        return exc.classification.value
    if isinstance(exc, LiveExecutionError) and exc.classification in {
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
    }:
        return exc.classification
    return {
        "preflight": "preflight_rejected",
        "acquiring": "source_or_plan_rejected",
        "validating_retained": "retained_candidate_rejected",
        "validating_candidate": "candidate_rejected",
        "planned": "plan_rejected",
        "materializing": "material_protection_unconfirmed",
        "raw_write": "raw_write_unconfirmed",
        "raw_verified": "ledger_handoff_unconfirmed",
        "ledger_commit": "ledger_commit_outcome_unknown",
        "publication_possible": "publication_outcome_unknown",
        "independent_readback": "independent_readback_rejected",
    }.get(stage, "job_action_rejected")


def _verify_local(ledger: Ledger, plan: DirectCpiLivePlan, at: datetime) -> dict:
    # Full expected graph equality, not just presence of raw/version/publication.
    for record in (
        BLS_CPI_HEADLINES,
        plan.source,
        plan.binding_candidate,
        plan.run,
        plan.acquisition,
        plan.raw_material,
        plan.semantic_material,
        plan.version,
    ):
        require(ledger.get(record.key) == record, "cpi_job_lineage_readback_mismatch")
    require(
        ledger.get_content(plan.version.key) == plan.content,
        "cpi_job_content_readback_mismatch",
    )
    publication = ledger.publication_by_occurrence(plan.publication_request.occurrence)
    require(
        publication is not None and publication.request == plan.publication_request,
        "cpi_job_publication_readback_mismatch",
    )
    exact = read_exact_version(
        ledger, BLS_CPI_HEADLINES.key, plan.version.key, as_of=at
    )
    asof = read_as_of(ledger, BLS_CPI_HEADLINES.key, as_of=at)
    require(exact == asof and exact.publication == publication, "cpi_job_asof_mismatch")
    for mode in ("exact_version", "as_of"):
        request = {
            "wire_revision": WIRE_REVISION,
            "mode": mode,
            "definition": {
                "kind": Kind.DEFINITION.value,
                "identity": BLS_CPI_HEADLINES.identity,
                "revision": BLS_CPI_HEADLINES.revision,
            },
            "as_of": at.isoformat(),
        }
        if mode == "exact_version":
            request["version_identity"] = plan.version.identity
        result = json.loads(handle_request(ledger, json.dumps(request).encode()))
        require(result["status"] == "ok", "cpi_job_wire_read_rejected")
    return {
        "version_identity": plan.version.identity,
        "publication_occurrence": publication.request.occurrence,
        "decision_at": publication.decision_at.isoformat(),
        "usable_at": publication.usable_at.isoformat(),
        "known_at": plan.acquisition.obtained_at.isoformat(),
        "raw_sha256": plan.raw_material.digest,
        "coverage": list(plan.version.finite_scope.members),
        "quality": list(QUALITY),
    }


class _PgJobProtection:
    """Job-local ownership of one PG session and its material ticket.

    Every ticket operation checks session/job identity without SQL first. Only
    an attached core ticket may use the job's own INTRANS/INERROR cleanup; all
    detached operations require IDLE. An ownership gap permanently revokes use.
    The host must keep this connection private and serialize all its calls.
    """

    def __init__(self, owner: CpiLocalJob, job_id: str, spec: CpiJobSpec):
        owner._check_pg_writer(spec)
        self.owner, self.job_id, self.spec = owner, job_id, spec
        self.ledger, self.protection = owner.kernel.ledger, owner.protection
        self.connection = self.ledger.connection
        self.session = self.connection.pgconn
        self.backend_pid = self.connection.info.backend_pid
        self.valid, self.materializing = True, False
        self.ticket: _PgJobTicket | None = None
        self.unknown: MaterializationOutcomeUnknown | None = None
        previous = _PG_SESSION_OWNERS.get(self.connection)
        require(
            previous is None or not previous[1],
            "cpi_job_pg_session_already_owned",
        )
        if previous is not None and (previous_owner := previous[0]()) is not None:
            previous_owner.valid = False
        _PG_SESSION_OWNERS[self.connection] = (ref(self), False)

    def check(self, *, ticket: _PgJobTicket | None = None):
        try:
            require(self.valid, "cpi_job_pg_protection_owner_lost")
            claim = _PG_SESSION_OWNERS.get(self.connection)
            require(
                self.owner.kernel.ledger is self.ledger
                and self.owner.protection is self.protection
                and self.owner._pg_owners.get(self.job_id) is self
                and claim is not None
                and claim[0]() is self
                and self.ledger.connection is self.connection
                and self.connection.pgconn is self.session
                and self.session is not None
                and type(self.backend_pid) is int
                and self.backend_pid > 0
                and self.connection.info.backend_pid == self.backend_pid,
                "cpi_job_pg_protection_owner_lost",
            )
            require(
                (self.ticket is None and not claim[1])
                or (
                    isinstance(self.ticket, _PgJobTicket)
                    and self.ticket.owner is self
                    and self.ticket.job_id == self.job_id
                    and self.ticket.held == claim[1]
                ),
                "cpi_job_pg_protection_owner_lost",
            )
            if ticket is not None:
                require(
                    ticket is self.ticket
                    and ticket.owner is self
                    and ticket.job_id == self.job_id
                    and ticket.held
                    and claim[1],
                    "cpi_job_pg_ticket_owner_lost",
                )
            self.owner._check_pg_writer(
                self.spec,
                allow_job_transaction=self.materializing and ticket is not None,
            )
        except Exception:
            self.valid = False
            raise

    def acquire(self, identity: MaterialIdentity) -> _PgJobTicket:
        self.check()
        require(
            not _PG_SESSION_OWNERS[self.connection][1],
            "cpi_job_pg_ticket_already_acquired",
        )
        # Acquisition itself can lose its acknowledgement. Until a known ticket
        # release, no engine may reenter this connection's session lock.
        _PG_SESSION_OWNERS[self.connection] = (ref(self), True)
        self.ticket = _PgJobTicket(self, self.protection.acquire(identity), identity)
        return self.ticket


class _PgJobTicket:
    def __init__(
        self,
        owner: _PgJobProtection,
        ticket: WriterProtectionTicket,
        identity: MaterialIdentity,
    ):
        self.owner, self.ticket, self.held = owner, ticket, True
        self.job_id, self.identity = owner.job_id, identity

    def _check(self, identity: MaterialIdentity):
        try:
            require(self.held, "cpi_job_pg_ticket_released")
            require(identity == self.identity, "cpi_job_pg_ticket_identity_mismatch")
            self.owner.check(ticket=self)
        except Exception:
            self.owner.valid = False
            raise

    def assert_held(self, identity: MaterialIdentity):
        self._check(identity)
        try:
            self.ticket.assert_held(identity)
        except Exception:
            self.owner.valid = False
            raise

    def mark_outcome_unknown(self, identity: MaterialIdentity):
        self._check(identity)
        try:
            self.ticket.mark_outcome_unknown(identity)
        except Exception:
            self.owner.valid = False
            raise

    def release(self, identity: MaterialIdentity):
        self._check(identity)
        try:
            self.ticket.release(identity)
        except Exception:
            self.owner.valid = False
            raise
        self.held = False
        _PG_SESSION_OWNERS[self.owner.connection] = (ref(self.owner), False)


class CpiLocalJob(RetainedRepair):
    """Explicit job execution and observation-only restart reconciliation.

    Host bindings must supply the dedicated DEV_SCHEMA ledger, Garage data role,
    protected writer and a fresh read-only ledger context on another connection.
    No secret/config reads or client construction occur in this class.
    """

    spec_type = CpiJobSpec

    def __init__(
        self,
        journal: FileJobJournal,
        clock: Callable[[], datetime],
        kernel: CanonicalKernel,
        store: MaterialStore,
        protection: WriterProtection,
        readback: Callable[[], AbstractContextManager[Ledger]],
        commit: Callable[[], None],
    ):
        self.journal, self.clock, self.kernel = journal, clock, kernel
        self.store, self.protection, self.readback, self.commit = (
            store,
            protection,
            readback,
            commit,
        )
        self._unknowns: dict[str, MaterializationOutcomeUnknown] = {}
        self._pg_owners: dict[str, _PgJobProtection] = {}
        self._check_pg_target(kernel.ledger)

    @staticmethod
    def _check_pg_target(ledger: Ledger) -> None:
        if isinstance(ledger, PostgresLedger):
            require(
                ledger.schema == DEV_SCHEMA
                and ledger.connection.info.host == "192.168.10.4",
                "cpi_job_nonproduction_target_required",
            )

    def _independent(self, ledger: Ledger) -> None:
        require(
            ledger is not self.kernel.ledger, "cpi_job_independent_readback_required"
        )
        self._check_pg_target(ledger)
        if isinstance(self.kernel.ledger, PostgresLedger):
            require(
                isinstance(ledger, PostgresLedger)
                and ledger.connection is not self.kernel.ledger.connection,
                "cpi_job_independent_pg_connection_required",
            )

    def _check_pg_writer(
        self, spec: CpiJobSpec, *, allow_job_transaction: bool = False
    ) -> None:
        ledger = self.kernel.ledger
        if isinstance(ledger, PostgresLedger):
            self._check_pg_target(ledger)
            require(
                spec.effect_domain == "owner_nonproduction"
                and isinstance(self.protection, PostgresAdvisoryWriterProtection)
                and self.protection.connection is ledger.connection,
                "cpi_job_owned_pg_writer_required",
            )
            # Inspect only. Only a session/job-checked attached core ticket may
            # clean its own transaction; entry/recovery/readback require IDLE.
            statuses = (TransactionStatus.IDLE,)
            if allow_job_transaction:
                statuses += (TransactionStatus.INTRANS, TransactionStatus.INERROR)
            require(
                not ledger.connection.closed
                and any(
                    ledger.connection.info.transaction_status is status
                    for status in statuses
                ),
                "cpi_job_idle_pg_writer_required",
            )
            claim = _PG_SESSION_OWNERS.get(ledger.connection)
            require(
                claim is None
                or not claim[1]
                or ((owner := claim[0]()) is not None and owner.owner is self),
                "cpi_job_pg_session_already_owned",
            )

    def submit(self, job_id: str, spec: CpiJobSpec) -> dict:
        require(type(spec) is CpiJobSpec, "cpi_job_spec_invalid")
        return self.journal.submit(job_id, spec.document(), spec.scope, self.clock())

    def _job_protection(self, job_id: str, spec: CpiJobSpec) -> WriterProtection:
        owner = self._pg_owners.get(job_id)
        if owner is not None:
            owner.check()
            require(owner.spec == spec, "cpi_job_pg_protection_owner_lost")
            return owner
        self._check_pg_writer(spec)
        if isinstance(self.kernel.ledger, PostgresLedger):
            owner = _PgJobProtection(self, job_id, spec)
            self._pg_owners[job_id] = owner
            owner.check()
            return owner
        return self.protection

    def _check_reconciliation_writer(self, job_id: str, spec: CpiJobSpec):
        unknown = self._unknowns.get(job_id)
        owner = self._pg_owners.get(job_id)
        if owner is not None:
            owner.check()
            require(
                owner.owner is self
                and owner.job_id == job_id
                and owner.spec == spec
                and owner.unknown is unknown,
                "cpi_job_pg_unknown_owner_required",
            )
            if unknown is not None:
                require(
                    unknown._ticket is owner.ticket
                    and owner.ticket is not None
                    and owner.ticket.held,
                    "cpi_job_pg_unknown_owner_required",
                )
            else:
                # An unreleased readback ticket has no core reconciliation
                # handle. Never reacquire/reenter its session's advisory lock.
                require(
                    not _PG_SESSION_OWNERS[owner.connection][1],
                    "cpi_job_pg_protection_owner_lost",
                )
        else:
            self._check_pg_writer(spec)
            require(
                unknown is None or not isinstance(self.kernel.ledger, PostgresLedger),
                "cpi_job_pg_unknown_owner_required",
            )

    def _update(self, job_id: str, **changes):
        return self.journal._update(job_id, self.clock(), **changes)

    def _cancelled(self, job_id: str):
        return self.journal.get(job_id)["cancel_requested"]

    def run(
        self,
        job_id: str,
        *,
        enabled: bool = False,
        preflight: Callable[[CpiJobSpec], None],
        acquirer: DirectCpiAcquirer | None = None,
        retained: LiveExecutionCandidate | None = None,
        worker_identity: str | None = None,
    ) -> dict:
        """One attempt only; pending registration and opt-in precede all effects.

        Preflight is an explicit host-owned, read-only check of nonproduction
        target, role, budget and release authorization. It must leave the writer
        idle, explicitly cleaning up only its own isolated probe transaction.
        A duplicate completed call returns its recorded result without effects.
        """
        if worker_identity is not None:
            valid_identity(worker_identity)
        with self.journal.executor(job_id):
            document = self.journal.get(job_id)
            if document["state"] != "pending":
                require(
                    document["state"] != "running",
                    "job_restart_reconciliation_required",
                )
                return document
            spec = CpiJobSpec.from_document(document["spec"])
            if self._cancelled(job_id):
                return self._update(
                    job_id,
                    state="cancelled",
                    stage="cancelled_before_effects",
                    finished_at=self.clock().isoformat(),
                )
            require(enabled is True, "cpi_job_explicit_opt_in_required")
            now = self.clock()
            instant(now)
            require(now >= RELEASE, "cpi_job_release_not_due")
            self._update(
                job_id,
                state="running",
                stage="preflight",
                attempts=1,
                attempt_id=job_id + "-a1",
                started_at=now.isoformat(),
                worker_identity=worker_identity,
            )
            try:
                self._check_pg_writer(spec)
                preflight(spec)
                self._check_pg_writer(spec)
                if spec.mode == "acquire":
                    require(
                        type(acquirer) is DirectCpiAcquirer and retained is None,
                        "cpi_job_acquirer_required",
                    )
                else:
                    require(
                        acquirer is None and retained is not None,
                        "cpi_job_retained_required",
                    )
                if self._cancelled(job_id):
                    return self._update(
                        job_id,
                        state="cancelled",
                        stage="cancelled_before_effects",
                        finished_at=self.clock().isoformat(),
                    )

                def before_request():
                    self._check_pg_writer(spec)
                    require(not self._cancelled(job_id), "cpi_job_cancelled")
                    require(
                        not self.journal.get(job_id)["effects"].get(
                            "provider_request_possible"
                        ),
                        "cpi_job_network_budget_exhausted",
                    )
                    self._update(
                        job_id,
                        stage="acquiring",
                        effects={"provider_request_possible": 1},
                    )

                def after_request(received: bool, attempts: int):
                    self._update(
                        job_id,
                        effects={
                            "provider_attempts_observed": attempts,
                            "provider_response_received": received,
                        },
                    )

                self._update(
                    job_id,
                    stage="acquiring"
                    if spec.mode == "acquire"
                    else "validating_retained",
                )
                self._check_pg_writer(spec)
                candidate = (
                    acquirer.acquire(job_id, before_request, after_request)
                    if acquirer
                    else retained
                )
                self._update(job_id, stage="validating_candidate")
                plan, raw = _validate_candidate(spec, candidate, job_id)
                self._check_repair_candidate(job_id, plan)
                identity = MaterialIdentity(digest(raw), len(raw))
                self._update(
                    job_id,
                    stage="planned",
                    plan=_encode_plan(plan),
                    raw={
                        "sha256": identity.sha256,
                        "size": identity.size,
                        "locator": identity.locator,
                    },
                    effects={"source_candidate_validated": True},
                )
                if self._cancelled(job_id):
                    return self._update(
                        job_id,
                        state="cancelled",
                        stage="cancelled_before_storage",
                        finished_at=self.clock().isoformat(),
                    )
                owner = self

                class ObservedStore:
                    def create_if_absent(self, target, payload):
                        require(not owner._cancelled(job_id), "cpi_job_cancelled")
                        owner._update(
                            job_id,
                            stage="raw_write",
                            effects={
                                "raw_create_calls_possible": 1,
                                "raw_state": "outcome_unknown",
                            },
                        )
                        result = owner.store.create_if_absent(target, payload)
                        owner._update(
                            job_id,
                            effects={
                                "raw_create_returned": True,
                                "raw_disposition": result.value,
                            },
                        )
                        return result

                    def read_exact(self, target):
                        observed = owner.store.read_exact(target)
                        if (
                            observed is not None
                            and len(observed) == target.size
                            and digest(observed) == target.sha256
                        ):
                            owner._update(
                                job_id,
                                stage="raw_verified",
                                effects={"raw_state": "verified"},
                            )
                        require(not owner._cancelled(job_id), "cpi_job_cancelled")
                        return observed

                def commit():
                    require(not self._cancelled(job_id), "cpi_job_cancelled")
                    self._update(
                        job_id,
                        stage="ledger_commit",
                        effects={"ledger_commit_possible": True},
                    )
                    self.commit()
                    self._update(
                        job_id,
                        stage="publication_possible",
                        effects={"ledger_commit_confirmed": True},
                    )
                    require(not self._cancelled(job_id), "cpi_job_cancelled")

                self._check_pg_writer(spec)
                # Persist all possible downstream outcomes before entering core.
                # Counts are conservative upper bounds, not fictitious success.
                self._update(
                    job_id, stage="materializing", effects={"canonical_possible": True}
                )
                protection = self._job_protection(job_id, spec)
                if isinstance(protection, _PgJobProtection):
                    protection.materializing = True
                try:
                    materialized = materialize_persist_publish(
                        self.kernel,
                        plan,
                        raw,
                        ObservedStore(),
                        protection,
                        authoritative_commit=commit,
                    )
                except MaterializationOutcomeUnknown as exc:
                    if isinstance(protection, _PgJobProtection):
                        protection.unknown = exc
                    raise
                finally:
                    if isinstance(protection, _PgJobProtection):
                        protection.materializing = False
                if isinstance(protection, _PgJobProtection):
                    protection.check()
                self._update(
                    job_id,
                    stage="independent_readback",
                    effects={
                        "publication_returned": True,
                        "raw_disposition": materialized.verification.disposition.value,
                        "raw_write_reconciled_outcome_unknown": (
                            materialized.verification.reconciled_outcome_unknown
                        ),
                    },
                )
                self._check_pg_writer(spec)
                ticket = protection.acquire(identity)
                try:
                    ticket.assert_held(identity)
                    observed_raw = self.store.read_exact(identity)
                    require(observed_raw == raw, "cpi_job_raw_readback_failed")
                    with self.readback() as ledger:
                        self._independent(ledger)
                        evidence = _verify_local(
                            ledger, _durable_plan(plan, identity), self.clock()
                        )
                    ticket.assert_held(identity)
                finally:
                    ticket.release(identity)
                return self._update(
                    job_id,
                    state="succeeded",
                    stage="local_read_verified",
                    finished_at=self.clock().isoformat(),
                    reconciliation={
                        "outcome": "confirmed_published",
                        "evidence": evidence,
                    },
                )
            except Exception as exc:
                if isinstance(exc, MaterializationOutcomeUnknown):
                    self._unknowns[job_id] = exc
                document = self.journal.get(job_id)
                possible = document["effects"].get("canonical_possible", False)
                # Never persist str(exc), args, response bodies or caller codes.
                return self._update(
                    job_id,
                    state="outcome_unknown"
                    if possible
                    else ("cancelled" if document["cancel_requested"] else "failed"),
                    stage="stopped",
                    classification=_failure(exc, document["stage"]),
                    failure_stage=document["stage"],
                    finished_at=self.clock().isoformat(),
                )

    def reconcile(self, job_id: str, *, enabled: bool = False) -> dict:
        """Read-only external effects. Never re-execute any write or source.

        outcome_unknown remains monotone; confirmed reconciliation is a separate
        append-only observation, not a rewritten successful attempt.
        """
        require(enabled is True, "cpi_job_explicit_opt_in_required")
        with self.journal.executor(job_id):
            document = self.journal.get(job_id)
            if document["state"] not in {"running", "outcome_unknown"}:
                return document
            spec = CpiJobSpec.from_document(document["spec"])
            # Reject caller transactions/abandoned handles before any journal
            # transition, protection SQL or core reconciliation can run.
            self._check_reconciliation_writer(job_id, spec)
            if document["state"] == "running":
                document = self._update(
                    job_id,
                    state="outcome_unknown",
                    stage="restart_detected",
                    classification="interrupted_attempt",
                    finished_at=self.clock().isoformat(),
                )
            try:
                if document["plan"] is None:
                    return self._update(
                        job_id,
                        stage="reconciled",
                        reconciliation={
                            "outcome": "unresolved",
                            "classification": "no_durable_plan_no_reexecution",
                        },
                    )
                plan = _decode_plan(document["plan"])
                raw_info = document["raw"]
                identity = MaterialIdentity(raw_info["sha256"], raw_info["size"])
                require(
                    identity.locator == raw_info["locator"], "cpi_job_locator_mismatch"
                )
                unknown = self._unknowns.get(job_id)
                protection = self._pg_owners.get(job_id)
                if unknown is not None:
                    with self.readback() as ledger:
                        self._independent(ledger)
                        self._check_reconciliation_writer(job_id, spec)
                        unknown.reconcile(ledger)
                    del self._unknowns[job_id]
                    if protection is not None:
                        protection.unknown = None
                if protection is None:
                    protection = self._job_protection(job_id, spec)
                observation = observe_exact_material(
                    store=self.store,
                    protection=protection,
                    identity=identity,
                    readback=self.readback,
                    check_ledger=self._independent,
                    plan=plan,
                    verify_local=lambda ledger: _verify_local(
                        ledger, _durable_plan(plan, identity), self.clock()
                    ),
                )
                return self._update(
                    job_id, stage="reconciled", reconciliation=observation
                )
            except Exception:
                return self._update(
                    job_id,
                    stage="reconciliation_unresolved",
                    reconciliation={
                        "outcome": "unresolved",
                        "classification": "independent_readback_rejected",
                    },
                )
