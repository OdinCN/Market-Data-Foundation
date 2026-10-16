"""Retained Employment snapshot job with owned storage and fresh readback."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, fields
from datetime import datetime

from psycopg.pq import TransactionStatus

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import digest, instant, require
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.canonical.repository import Ledger
from market_data_foundation.historical.bls_cpi_material_store import (
    GarageMaterialStore,
    MaterialIdentity,
    MaterialStore,
    PostgresAdvisoryWriterProtection,
    WriterProtection,
)
from market_data_foundation.historical.bls_employment import (
    COMPLETENESS,
    CONTRACT_REVISION,
    MAX_RESPONSE_BYTES,
    REFERENCE,
    RELEASE_SLOT,
    SERIES_IDS,
    SOURCE_IDENTITY,
    EmploymentPlan,
    build_plan,
    validate_plan,
)
from market_data_foundation.historical.employment_candidate import (
    EmploymentRetainedCandidate,
)
from market_data_foundation.historical.employment_contract import BLS_EMPLOYMENT
from market_data_foundation.historical.employment_local_read import (
    read_local_employment,
)
from market_data_foundation.historical.employment_materialization import (
    EmploymentMaterializationOutcomeUnknown,
    materialize_persist_publish,
)
from market_data_foundation.historical.macro_job_journal import MacroJobJournal
from market_data_foundation.historical.macro_lifecycle import (
    RetainedRepair,
    decode_plan,
    encode_plan,
    observe_exact_material,
)

DEV_SCHEMA = "mdf_macro_bls_employment_vertical_r1"
DEV_POSTGRES_HOST = "192.168.10.4"
QUALITY = ("original", "projected")


class EmploymentJobJournal(MacroJobJournal):
    """Transaction-specific journal type; unknown states remain monotone."""


@dataclass(frozen=True)
class EmploymentJobSpec:
    definition: str = BLS_EMPLOYMENT.identity
    definition_revision: str = BLS_EMPLOYMENT.revision
    series: tuple[str, ...] = SERIES_IDS
    reference: str = REFERENCE
    source_identity: str = SOURCE_IDENTITY
    source_revision: str = CONTRACT_REVISION
    release_slot: str = RELEASE_SLOT
    completeness: str = COMPLETENESS
    quality: tuple[str, ...] = QUALITY
    mode: str = "retained"
    effect_domain: str = "synthetic"
    max_provider_requests: int = 0
    max_response_bytes: int = MAX_RESPONSE_BYTES
    max_raw_creates: int = 1
    max_ledger_handoffs: int = 1
    max_publications: int = 1
    expected_raw_sha256: str | None = None

    def __post_init__(self) -> None:
        require(
            self.definition == BLS_EMPLOYMENT.identity
            and self.definition_revision == BLS_EMPLOYMENT.revision
            and self.series == SERIES_IDS
            and self.reference == REFERENCE
            and self.source_identity == SOURCE_IDENTITY
            and self.source_revision == CONTRACT_REVISION
            and self.release_slot == RELEASE_SLOT
            and self.completeness == COMPLETENESS
            and self.quality == QUALITY,
            "employment_job_target_not_admitted",
        )
        require(self.mode == "retained", "employment_job_mode_not_admitted")
        require(
            self.mode != "retained" or self.expected_raw_sha256 is not None,
            "employment_job_retained_digest_required",
        )
        if self.expected_raw_sha256 is not None:
            MaterialIdentity(self.expected_raw_sha256, 1)
        require(
            self.effect_domain in {"synthetic", "owner_nonproduction"},
            "employment_job_effect_domain_not_admitted",
        )
        require(
            (
                self.max_provider_requests,
                self.max_raw_creates,
                self.max_ledger_handoffs,
                self.max_publications,
            )
            == (0, 1, 1, 1)
            and all(
                type(value) is int
                for value in (
                    self.max_provider_requests,
                    self.max_raw_creates,
                    self.max_ledger_handoffs,
                    self.max_publications,
                )
            )
            and type(self.max_response_bytes) is int
            and self.max_response_bytes == MAX_RESPONSE_BYTES,
            "employment_job_budget_not_admitted",
        )

    def document(self) -> dict:
        result = {field.name: getattr(self, field.name) for field in fields(self)}
        result["series"] = list(self.series)
        result["quality"] = list(self.quality)
        return result

    @classmethod
    def from_document(cls, document: dict) -> EmploymentJobSpec:
        value = dict(document)
        value["series"] = tuple(value["series"])
        value["quality"] = tuple(value["quality"])
        return cls(**value)

    @property
    def scope(self) -> str:
        return digest(
            "\0".join(
                (
                    self.definition,
                    self.definition_revision,
                    *self.series,
                    self.reference,
                    self.source_identity,
                    self.source_revision,
                    self.expected_raw_sha256 or "",
                )
            ).encode()
        )


def _encode_plan(plan: EmploymentPlan) -> dict:
    return encode_plan(plan)


def _decode_plan(value: dict) -> EmploymentPlan:
    return decode_plan(value, EmploymentPlan, "employment_job_plan_invalid")


class EmploymentLocalJob(RetainedRepair):
    """One attempt; execution owns materialization and fresh local readback."""

    spec_type = EmploymentJobSpec

    def __init__(
        self,
        journal: EmploymentJobJournal,
        clock: Callable[[], datetime],
        kernel: CanonicalKernel,
        store: MaterialStore,
        protection: WriterProtection,
        readback: Callable[[], AbstractContextManager[Ledger]],
        commit: Callable[[], None],
    ) -> None:
        require(
            type(journal) is EmploymentJobJournal, "employment_job_journal_required"
        )
        self.journal = journal
        self.clock = clock
        self.kernel = kernel
        self.store = store
        self.protection = protection
        self.readback = readback
        self.commit = commit
        self._unknown_handles: dict[str, EmploymentMaterializationOutcomeUnknown] = {}
        self._check_pg_target(kernel.ledger)

    @staticmethod
    def _check_pg_target(ledger: Ledger) -> None:
        if isinstance(ledger, PostgresLedger):
            require(
                ledger.schema == DEV_SCHEMA
                and ledger.connection.info.host == DEV_POSTGRES_HOST,
                "employment_job_nonproduction_target_required",
            )

    def _check_bindings(self, spec: EmploymentJobSpec) -> None:
        ledger = self.kernel.ledger
        self._check_pg_target(ledger)
        if isinstance(ledger, PostgresLedger):
            require(
                spec.effect_domain == "owner_nonproduction",
                "employment_job_owner_effect_domain_required",
            )
        if spec.effect_domain == "owner_nonproduction":
            require(
                isinstance(ledger, PostgresLedger)
                and isinstance(self.store, GarageMaterialStore)
                and isinstance(self.protection, PostgresAdvisoryWriterProtection)
                and self.protection.connection is ledger.connection
                and not ledger.connection.closed,
                "employment_job_owned_nonproduction_bindings_required",
            )
            require(
                ledger.connection.info.transaction_status is TransactionStatus.IDLE,
                "employment_job_idle_pg_writer_required",
            )

    def _writer_session(self) -> dict:
        ledger = self.kernel.ledger
        if isinstance(ledger, PostgresLedger):
            backend_pid = ledger.connection.info.backend_pid
            require(
                type(backend_pid) is int and backend_pid > 0,
                "employment_job_writer_session_invalid",
            )
            return {
                "kind": "postgres",
                "host": DEV_POSTGRES_HOST,
                "backend_pid": backend_pid,
            }
        return {"kind": "synthetic", "lock_release_proven": False}

    def _check_independent(self, ledger: Ledger, spec: EmploymentJobSpec) -> None:
        require(
            ledger is not self.kernel.ledger, "employment_job_fresh_readback_required"
        )
        self._check_pg_target(ledger)
        if spec.effect_domain == "owner_nonproduction":
            writer = self.kernel.ledger
            require(
                isinstance(writer, PostgresLedger)
                and isinstance(ledger, PostgresLedger)
                and ledger.connection is not writer.connection,
                "employment_job_independent_pg_connection_required",
            )

    def submit(self, identity: str, spec: EmploymentJobSpec) -> dict:
        require(type(spec) is EmploymentJobSpec, "employment_job_spec_invalid")
        return self.journal.submit(identity, spec.document(), spec.scope, self.clock())

    def _candidate(
        self,
        identity: str,
        candidate: EmploymentRetainedCandidate,
        spec: EmploymentJobSpec,
    ) -> tuple[EmploymentPlan, bytes]:
        require(
            type(candidate) is EmploymentRetainedCandidate,
            "employment_job_candidate_invalid",
        )
        expected = candidate.plan.run.identity.removesuffix(":run")
        require(
            spec.expected_raw_sha256 is None
            or digest(candidate.evidence.raw_bytes) == spec.expected_raw_sha256,
            "employment_job_retained_digest_mismatch",
        )
        rebuilt = build_plan(
            candidate.evidence,
            identity=expected,
            finished_at=candidate.plan.run.finished_at,
            frozen_at=candidate.plan.version.frozen_at,
        )
        require(candidate.plan == rebuilt, "employment_job_candidate_plan_mismatch")
        validate_plan(rebuilt, candidate.evidence.raw_bytes)
        self._check_repair_candidate(identity, rebuilt)
        return rebuilt, candidate.evidence.raw_bytes

    def _readback(
        self,
        spec: EmploymentJobSpec,
        plan: EmploymentPlan,
        raw: bytes,
        identity: MaterialIdentity,
    ) -> dict:
        self._check_bindings(spec)
        ticket = self.protection.acquire(identity)
        try:
            ticket.assert_held(identity)
            observed = self.store.read_exact(identity)
            require(observed == raw, "employment_job_raw_readback_failed")
            with self.readback() as ledger:
                self._check_independent(ledger, spec)
                at = self.clock()
                exact = read_local_employment(
                    ledger,
                    reference=REFERENCE,
                    version_identity=plan.version.identity,
                    as_of=at,
                )
                as_of = read_local_employment(ledger, reference=REFERENCE, as_of=at)
                require(
                    exact.version == plan.version
                    and exact.content == plan.content
                    and exact.publication.request == plan.publication_request,
                    "employment_job_exact_readback_mismatch",
                )
                require(
                    as_of.acquisitions[0].obtained_at
                    >= exact.acquisitions[0].obtained_at
                    and (
                        as_of.acquisitions[0].obtained_at
                        != exact.acquisitions[0].obtained_at
                        or as_of == exact
                    ),
                    "employment_job_as_of_selection_mismatch",
                )
            ticket.assert_held(identity)
        finally:
            ticket.release(identity)
        return {
            "version_identity": exact.version.identity,
            "publication_identity": exact.publication.request.identity,
            "raw_sha256": identity.sha256,
            "known_at": exact.publication.decision_at.isoformat(),
        }

    def run(
        self,
        identity: str,
        *,
        enabled: bool,
        preflight: Callable[[EmploymentJobSpec], None],
        retained: EmploymentRetainedCandidate | None = None,
    ) -> dict:
        require(enabled is True, "employment_job_explicit_opt_in_required")
        with self.journal.executor(identity):
            document = self.journal.get(identity)
            if document["state"] != "pending":
                require(
                    document["state"] != "running",
                    "employment_job_restart_reconciliation_required",
                )
                return document
            spec = EmploymentJobSpec.from_document(document["spec"])
            if document["cancel_requested"]:
                return self._cancelled(identity, "before_preflight")
            self.journal._update(
                identity,
                self.clock(),
                state="running",
                stage="preflight",
                attempt_id=identity + "-a1",
                attempts=1,
                started_at=self.clock().isoformat(),
            )
            try:
                self._check_bindings(spec)
                require(
                    type(retained) is EmploymentRetainedCandidate,
                    "employment_job_retained_candidate_required",
                )
                preflight(spec)
                self._check_bindings(spec)
                if self.journal.get(identity)["cancel_requested"]:
                    return self._cancelled(identity, "after_preflight")

                candidate = retained
                self._check_bindings(spec)
                plan, raw = self._candidate(identity, candidate, spec)
                raw_identity = MaterialIdentity(digest(raw), len(raw))
                self.journal._update(
                    identity,
                    self.clock(),
                    stage="acquired",
                    plan=_encode_plan(plan),
                    raw={
                        "sha256": raw_identity.sha256,
                        "size": raw_identity.size,
                        "locator": raw_identity.locator,
                        "observed_at": candidate.evidence.obtained_at.isoformat(),
                    },
                    effects={"source_candidate_validated": True},
                )
                if self.journal.get(identity)["cancel_requested"]:
                    return self._cancelled(identity, "before_storage")
                self.journal._update(
                    identity,
                    self.clock(),
                    stage="materialization_intent",
                    writer_session=self._writer_session(),
                    effects={
                        "raw_creates_possible": 1,
                        "ledger_handoffs_possible": 1,
                        "publications_possible": 1,
                        "canonical_possible": True,
                    },
                )

                def commit() -> None:
                    self.journal._update(
                        identity,
                        self.clock(),
                        stage="ledger_commit",
                        effects={"ledger_commit_possible": True},
                    )
                    self.commit()
                    self._check_bindings(spec)
                    self.journal._update(
                        identity,
                        self.clock(),
                        stage="publication_possible",
                        effects={"ledger_commit_confirmed": True},
                    )

                def protection_released() -> None:
                    if spec.effect_domain == "synthetic":
                        self.journal._update(
                            identity,
                            self.clock(),
                            writer_session={
                                "kind": "synthetic",
                                "lock_release_proven": True,
                            },
                        )

                self._check_bindings(spec)
                result = materialize_persist_publish(
                    self.kernel,
                    plan,
                    raw,
                    self.store,
                    self.protection,
                    authoritative_commit=commit,
                    protection_released=protection_released,
                )
                require(
                    result.verification.identity == raw_identity,
                    "employment_material_verification_mismatch",
                )
                verification = result.verification
                if verification.reconciled_outcome_unknown:
                    require(
                        verification.disposition is None,
                        "employment_unknown_raw_write_overclaimed",
                    )
                    raw_disposition = "unknown_reconciled_present"
                else:
                    require(
                        verification.disposition is not None,
                        "employment_raw_disposition_missing",
                    )
                    raw_disposition = verification.disposition.value
                self.journal._update(
                    identity,
                    self.clock(),
                    stage="independent_readback",
                    effects={
                        "publication_returned": 1,
                        "raw_disposition": raw_disposition,
                        "raw_write_reconciled_outcome_unknown": (
                            verification.reconciled_outcome_unknown
                        ),
                    },
                )
                evidence = self._readback(spec, result.plan, raw, raw_identity)
                observed_at = self.clock()
                instant(observed_at)
                return self.journal._update(
                    identity,
                    observed_at,
                    state="succeeded",
                    stage="verified",
                    finished_at=observed_at.isoformat(),
                    classification="canonical_verified",
                    reconciliation={
                        "outcome": "confirmed_published",
                        "evidence": evidence,
                    },
                )
            except EmploymentMaterializationOutcomeUnknown as exc:
                self._unknown_handles[identity] = exc
                return self._unknown(
                    identity, exc.phase.value, "authoritative_outcome_unknown"
                )
            except Exception:
                possible = self.journal.get(identity)["effects"].get(
                    "canonical_possible", False
                )
                if possible:
                    return self._unknown(
                        identity,
                        self.journal.get(identity)["stage"],
                        "job_action_outcome_unknown",
                    )
                if self.journal.get(identity)["cancel_requested"]:
                    return self._cancelled(
                        identity, self.journal.get(identity)["stage"]
                    )
                return self._failed(
                    identity, self.journal.get(identity)["stage"], "job_action_rejected"
                )

    def reconcile(self, identity: str, *, enabled: bool) -> dict:
        """Observe exact journal identities; never rewrite an unknown terminal."""
        require(enabled is True, "employment_job_explicit_opt_in_required")
        with self.journal.executor(identity):
            document = self.journal.get(identity)
            if document["state"] not in {"running", "outcome_unknown"}:
                return document
            spec = EmploymentJobSpec.from_document(document["spec"])
            self._check_bindings(spec)
            if document["state"] == "running":
                document = self.journal._update(
                    identity,
                    self.clock(),
                    state="outcome_unknown",
                    stage="restart_detected",
                    classification="interrupted_attempt",
                    finished_at=self.clock().isoformat(),
                )
            release_proof = (
                {"lock_release_proven": True}
                if (document.get("reconciliation") or {}).get("lock_release_proven")
                is True
                else {}
            )
            try:
                require(document["plan"] is not None, "employment_job_plan_missing")
                plan = _decode_plan(document["plan"])
                raw_info = document["raw"]
                raw_identity = MaterialIdentity(raw_info["sha256"], raw_info["size"])
                require(
                    raw_info["locator"] == raw_identity.locator,
                    "employment_job_locator_mismatch",
                )
                handle = self._unknown_handles.get(identity)
                session = document.get("writer_session")
                require(type(session) is dict, "employment_job_writer_session_missing")
                if handle is not None:
                    with self.readback() as ledger:
                        self._check_independent(ledger, spec)
                        handle.reconcile(ledger)
                        # Release has returned. Commit its evidence before even
                        # leaving readback: context exit or later observation
                        # can fail after the original ticket is gone.
                        release_proof = {"lock_release_proven": True}
                        self.journal._update(
                            identity,
                            self.clock(),
                            reconciliation={"outcome": "unresolved"} | release_proof,
                        )
                        del self._unknown_handles[identity]
                elif spec.effect_domain == "owner_nonproduction":
                    current = self._writer_session()
                    require(
                        session.get("kind") == "postgres"
                        and session.get("host") == DEV_POSTGRES_HOST
                        and type(session.get("backend_pid")) is int
                        and current["backend_pid"] != session["backend_pid"],
                        "employment_job_old_writer_session_not_gone",
                    )
                else:
                    require(
                        session == {"kind": "synthetic", "lock_release_proven": True}
                        or (
                            session
                            == {"kind": "synthetic", "lock_release_proven": False}
                            and release_proof.get("lock_release_proven") is True
                        ),
                        "employment_job_synthetic_lock_release_unproven",
                    )

                def verify_local(ledger):
                    exact = read_local_employment(
                        ledger,
                        reference=REFERENCE,
                        version_identity=plan.version.identity,
                        as_of=self.clock(),
                    )
                    return {
                        "version_identity": exact.version.identity,
                        "publication_identity": exact.publication.request.identity,
                        "raw_sha256": raw_identity.sha256,
                        "known_at": exact.publication.decision_at.isoformat(),
                    }

                observation = observe_exact_material(
                    store=self.store,
                    protection=self.protection,
                    identity=raw_identity,
                    readback=self.readback,
                    check_ledger=lambda ledger: self._check_independent(ledger, spec),
                    plan=plan,
                    verify_local=verify_local,
                )
                classification = (
                    "canonical_verified"
                    if observation["outcome"] == "confirmed_published"
                    else "confirmed_not_published_no_retry"
                )
                return self.journal._update(
                    identity,
                    self.clock(),
                    stage="reconciled",
                    classification=classification,
                    reconciliation=observation | release_proof,
                )
            except Exception:
                return self.journal._update(
                    identity,
                    self.clock(),
                    stage="reconciliation_unresolved",
                    classification="reconciliation_unresolved",
                    reconciliation={
                        "outcome": "unresolved",
                        "classification": "independent_readback_rejected",
                    }
                    | release_proof,
                )

    def _unknown(self, identity: str, stage: str, classification: str) -> dict:
        at = self.clock()
        return self.journal._update(
            identity,
            at,
            state="outcome_unknown",
            stage=stage,
            finished_at=at.isoformat(),
            classification=classification,
            failure_stage=stage,
        )

    def _failed(self, identity: str, stage: str, classification: str) -> dict:
        at = self.clock()
        return self.journal._update(
            identity,
            at,
            state="failed",
            stage=stage,
            finished_at=at.isoformat(),
            classification=classification,
            failure_stage=stage,
        )

    def _cancelled(self, identity: str, stage: str) -> dict:
        at = self.clock()
        return self.journal._update(
            identity,
            at,
            state="cancelled",
            stage=stage,
            finished_at=at.isoformat(),
            classification="cancelled_before_storage_effect",
        )
