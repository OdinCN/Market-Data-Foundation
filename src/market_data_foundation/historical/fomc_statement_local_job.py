"""Single-reference FOMC Statement job with owned storage and readback bindings."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, fields, replace
from datetime import datetime

from psycopg.pq import TransactionStatus

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import digest, instant, require
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.canonical.repository import Ledger
from market_data_foundation.historical.bls_cpi_material_store import (
    GarageMaterialStore,
    MaterialStore,
    PostgresAdvisoryWriterProtection,
    WriterProtection,
)
from market_data_foundation.historical.fomc_statement import (
    COMPLETENESS,
    CONTRACT_REVISION,
    DOCUMENT_ID,
    MAX_RESPONSE_BYTES,
    REFERENCE,
    SOURCE_IDENTITY,
    TIMEOUT_SECONDS,
    StatementPlan,
    build_live_plan,
    candidate_from_content,
    validate_live_plan,
)
from market_data_foundation.historical.fomc_statement_contract import STATEMENT_DOCUMENT
from market_data_foundation.historical.fomc_statement_live import (
    ArtifactStore,
    RequestSource,
    StatementCandidate,
    qualify_statement,
)
from market_data_foundation.historical.fomc_statement_local_read import (
    read_local_statement,
)
from market_data_foundation.historical.fomc_statement_material_store import (
    StatementMaterialIdentity,
)
from market_data_foundation.historical.fomc_statement_materialization import (
    StatementMaterializationOutcomeUnknown,
    StatementProtectionReleaseFailure,
    _release,
    materialize_persist_publish,
)
from market_data_foundation.historical.macro_job_journal import (
    MacroJobJournal,
    valid_identity,
)
from market_data_foundation.historical.macro_lifecycle import (
    decode_plan,
    encode_plan,
)

DEV_SCHEMA = "mdf_macro_fomc_statement_r1"
DEV_POSTGRES_HOST = "192.168.10.4"
QUALITY = ("original", "projected")


class StatementJobJournal(MacroJobJournal):
    """Transaction-specific journal; two-raw repair is atomically gated."""

    def _submit(
        self,
        identity: str,
        spec: dict,
        scope: str,
        at: datetime,
        *,
        repair_of: str | None = None,
        target_scope: str | None = None,
    ) -> dict:
        valid_identity(identity)
        instant(at)
        require(not self.read_only, "journal_read_only")
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT document FROM jobs WHERE identity = ?", (identity,)
            ).fetchone()
            if row is not None:
                document = json.loads(row[0])
                require(
                    document["spec"] == spec and document.get("repair_of") == repair_of,
                    "job_identity_reused",
                )
                return document
            if repair_of is not None:
                row = connection.execute(
                    "SELECT scope, document FROM jobs WHERE identity = ?", (repair_of,)
                ).fetchone()
                require(row is not None, "job_not_found")
                old = json.loads(row[1])
                require(
                    target_scope == old.get("target_scope", row[0])
                    and old["reconciliation"] is not None
                    and old["reconciliation"]["outcome"] == "confirmed_not_published"
                    and old["reconciliation"].get("raw_state") == "verified"
                    and old["plan"] is not None
                    and old["raw"] is not None
                    and spec.get("mode") == "retained"
                    and type(spec.get("max_provider_requests")) is int
                    and spec["max_provider_requests"] == 0
                    and spec.get("expected_raw_sha256")
                    == [item["sha256"] for item in old["raw"]],
                    "macro_job_repair_target_mismatch",
                )
                require(
                    (
                        old["state"] in {"failed", "cancelled"}
                        and not old["effects"].get("canonical_possible")
                    )
                    or (
                        old["state"] == "outcome_unknown"
                        and old["reconciliation"] is not None
                        and old["reconciliation"]["outcome"]
                        == "confirmed_not_published"
                        and old["reconciliation"].get("raw_state") == "verified"
                    ),
                    "macro_job_repair_reconciliation_required",
                )
            require(
                connection.execute(
                    "SELECT 1 FROM jobs WHERE scope = ?", (scope,)
                ).fetchone()
                is None,
                "job_scope_already_registered",
            )
            document = {
                "job_id": identity,
                "spec": spec,
                "state": "pending",
                "stage": "registered",
                "attempt_id": None,
                "attempts": 0,
                "created_at": at.isoformat(),
                "updated_at": at.isoformat(),
                "started_at": None,
                "finished_at": None,
                "cancel_requested": False,
                "classification": None,
                "failure_stage": None,
                "plan": None,
                "raw": None,
                "effects": {},
                "reconciliation": None,
                "last_success": None,
            }
            if repair_of is not None:
                document.update(repair_of=repair_of, target_scope=target_scope)
            connection.execute(
                "INSERT INTO jobs VALUES (?, ?, ?)",
                (
                    identity,
                    scope,
                    json.dumps(
                        document, sort_keys=True, separators=(",", ":"), allow_nan=False
                    ),
                ),
            )
            self._event(connection, document)
            return document


@dataclass(frozen=True)
class DirectStatementAcquirer:
    """Exact live edge with journal callbacks bracketing one logical request."""

    request_source: RequestSource
    artifact_store: ArtifactStore
    clock: Callable[[], datetime]
    sleep: Callable[[float], None] = lambda _: None

    def acquire(self, job_id, before_request, after_request) -> StatementCandidate:
        previous = None
        attempts = 0

        def source(request):
            nonlocal previous, attempts
            if request != previous:
                # A bookkeeping rejection is terminal under the Stage-1 policy.
                import http.client

                try:
                    before_request()
                except Exception:
                    raise http.client.InvalidURL(
                        "statement_metering_rejected"
                    ) from None
                previous, attempts = request, 0
            attempts += 1
            import http.client

            def observed(received):
                try:
                    after_request(received, attempts)
                except Exception:
                    raise http.client.InvalidURL(
                        "statement_metering_rejected"
                    ) from None

            try:
                response = self.request_source(request)
            except Exception:
                observed(False)
                raise
            if response.status_code != 200:
                observed(False)
            return response

        return qualify_statement(
            request_source=source,
            artifact_store=self.artifact_store,
            live_opt_in=True,
            clock=self.clock,
            sleep=self.sleep,
            after_request=after_request,
        )


@dataclass(frozen=True)
class StatementJobSpec:
    definition: str = STATEMENT_DOCUMENT.identity
    definition_revision: str = STATEMENT_DOCUMENT.revision
    event: str = DOCUMENT_ID
    document_kind: str = "FOMC_STATEMENT"
    reference: str = REFERENCE
    source_identity: str = SOURCE_IDENTITY
    source_revision: str = CONTRACT_REVISION
    completeness: str = COMPLETENESS
    quality: tuple[str, ...] = QUALITY
    mode: str = "acquire"
    effect_domain: str = "synthetic"
    max_provider_requests: int = 2
    max_response_bytes: int = MAX_RESPONSE_BYTES
    timeout_seconds: float = TIMEOUT_SECONDS
    max_raw_creates: int = 2
    max_ledger_handoffs: int = 1
    max_publications: int = 1
    expected_raw_sha256: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        require(
            self.definition == STATEMENT_DOCUMENT.identity
            and self.definition_revision == STATEMENT_DOCUMENT.revision
            and self.event == DOCUMENT_ID
            and self.document_kind == "FOMC_STATEMENT"
            and self.reference == REFERENCE
            and self.source_identity == SOURCE_IDENTITY
            and self.source_revision == CONTRACT_REVISION
            and self.completeness == COMPLETENESS
            and self.quality == QUALITY,
            "statement_job_target_not_admitted",
        )
        require(self.mode in {"acquire", "retained"}, "statement_job_mode_not_admitted")
        require(
            self.mode != "retained" or self.expected_raw_sha256 is not None,
            "statement_job_retained_digest_required",
        )
        if self.expected_raw_sha256 is not None:
            require(
                type(self.expected_raw_sha256) is tuple
                and len(self.expected_raw_sha256) == 2,
                "statement_job_two_digests_required",
            )
            for sha in self.expected_raw_sha256:
                StatementMaterialIdentity(sha, 1)
        require(
            self.effect_domain in {"synthetic", "owner_nonproduction"},
            "statement_job_effect_domain_not_admitted",
        )
        require(
            (
                self.max_provider_requests,
                self.max_raw_creates,
                self.max_ledger_handoffs,
                self.max_publications,
            )
            == (2 if self.mode == "acquire" else 0, 2, 1, 1)
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
            and type(self.timeout_seconds) in {int, float}
            and self.max_response_bytes == MAX_RESPONSE_BYTES
            and self.timeout_seconds == TIMEOUT_SECONDS,
            "statement_job_budget_not_admitted",
        )

    def document(self) -> dict:
        result = {field.name: getattr(self, field.name) for field in fields(self)}
        result["quality"] = list(self.quality)
        if self.expected_raw_sha256 is not None:
            result["expected_raw_sha256"] = list(self.expected_raw_sha256)
        return result

    @classmethod
    def from_document(cls, document: dict) -> StatementJobSpec:
        value = dict(document)
        value["quality"] = tuple(value["quality"])
        if value.get("expected_raw_sha256") is not None:
            value["expected_raw_sha256"] = tuple(value["expected_raw_sha256"])
        return cls(**value)

    @property
    def scope(self) -> str:
        return digest(
            "\0".join(
                (
                    self.definition,
                    self.definition_revision,
                    self.event,
                    self.document_kind,
                    self.reference,
                    self.source_identity,
                    self.source_revision,
                )
            ).encode()
        )


def _encode_plan(plan: StatementPlan) -> dict:
    return encode_plan(plan)


def _decode_plan(value: dict) -> StatementPlan:
    return decode_plan(value, StatementPlan, "statement_job_plan_invalid")


class StatementLocalJob:
    """One attempt; execution owns materialization and fresh local readback."""

    spec_type = StatementJobSpec

    def __init__(
        self,
        journal: StatementJobJournal,
        clock: Callable[[], datetime],
        kernel: CanonicalKernel,
        store: MaterialStore,
        protection: WriterProtection,
        readback: Callable[[], AbstractContextManager[Ledger]],
        commit: Callable[[], None],
    ) -> None:
        require(type(journal) is StatementJobJournal, "statement_job_journal_required")
        self.journal = journal
        self.clock = clock
        self.kernel = kernel
        self.store = store
        self.protection = protection
        self.readback = readback
        self.commit = commit
        self._unknown_handles: dict[str, StatementMaterializationOutcomeUnknown] = {}
        self._readback_release_handles: dict[
            str, StatementProtectionReleaseFailure
        ] = {}
        self._check_pg_target(kernel.ledger)

    @staticmethod
    def _check_pg_target(ledger: Ledger) -> None:
        if isinstance(ledger, PostgresLedger):
            require(
                ledger.schema == DEV_SCHEMA
                and ledger.connection.info.host == DEV_POSTGRES_HOST,
                "statement_job_nonproduction_target_required",
            )

    def _check_bindings(self, spec: StatementJobSpec) -> None:
        ledger = self.kernel.ledger
        self._check_pg_target(ledger)
        if isinstance(ledger, PostgresLedger):
            require(
                spec.effect_domain == "owner_nonproduction",
                "statement_job_owner_effect_domain_required",
            )
        if spec.effect_domain == "owner_nonproduction":
            require(
                isinstance(ledger, PostgresLedger)
                and isinstance(self.store, GarageMaterialStore)
                and isinstance(self.protection, PostgresAdvisoryWriterProtection)
                and self.protection.connection is ledger.connection
                and not ledger.connection.closed,
                "statement_job_owned_nonproduction_bindings_required",
            )
            require(
                ledger.connection.info.transaction_status is TransactionStatus.IDLE,
                "statement_job_idle_pg_writer_required",
            )

    def _writer_session(self) -> dict:
        ledger = self.kernel.ledger
        if isinstance(ledger, PostgresLedger):
            backend_pid = ledger.connection.info.backend_pid
            require(
                type(backend_pid) is int and backend_pid > 0,
                "statement_job_writer_session_invalid",
            )
            return {
                "kind": "postgres",
                "host": DEV_POSTGRES_HOST,
                "backend_pid": backend_pid,
            }
        return {"kind": "synthetic", "lock_release_proven": False}

    def _check_independent(self, ledger: Ledger, spec: StatementJobSpec) -> None:
        require(
            ledger is not self.kernel.ledger, "statement_job_fresh_readback_required"
        )
        self._check_pg_target(ledger)
        if spec.effect_domain == "owner_nonproduction":
            writer = self.kernel.ledger
            require(
                isinstance(writer, PostgresLedger)
                and isinstance(ledger, PostgresLedger)
                and ledger.connection.read_only is True
                and ledger.connection is not writer.connection
                and ledger.connection.info.backend_pid
                != writer.connection.info.backend_pid,
                "statement_job_independent_pg_connection_required",
            )

    def submit(self, identity: str, spec: StatementJobSpec) -> dict:
        require(type(spec) is StatementJobSpec, "statement_job_spec_invalid")
        return self.journal.submit(identity, spec.document(), spec.scope, self.clock())

    def repair(
        self, job_id: str, replacement_id: str, *, enabled: bool = False
    ) -> dict:
        require(enabled is True, "statement_job_explicit_opt_in_required")
        with self.journal.executor(job_id):
            old = self.journal.get(job_id)
            require(
                old["raw"] is not None
                and old["reconciliation"] is not None
                and old["reconciliation"]["outcome"] == "confirmed_not_published"
                and old["reconciliation"].get("raw_state") == "verified",
                "statement_job_repair_reconciliation_required",
            )
            spec = replace(
                StatementJobSpec.from_document(old["spec"]),
                mode="retained",
                max_provider_requests=0,
                expected_raw_sha256=tuple(item["sha256"] for item in old["raw"]),
            )
            return self.journal.submit_repair(
                job_id, replacement_id, spec.document(), spec.scope, self.clock()
            )

    def _candidate(
        self, identity: str, candidate: StatementCandidate, spec: StatementJobSpec
    ) -> tuple[StatementPlan, tuple[bytes, ...]]:
        require(
            type(candidate) is StatementCandidate, "statement_job_candidate_invalid"
        )
        document = self.journal.get(identity)
        if document.get("repair_of") is not None:
            old = self.journal.get(document["repair_of"])
            original = _decode_plan(old["plan"])
            plan = build_live_plan(
                candidate,
                identity=original.run.identity.removesuffix(":run"),
                finished_at=original.run.finished_at,
                frozen_at=original.version.frozen_at,
            )
            require(
                _encode_plan(plan) == old["plan"], "statement_job_repair_plan_mismatch"
            )
        else:
            at = self.clock()
            plan = build_live_plan(
                candidate,
                identity="t07-statement-" + identity + "-a1",
                finished_at=at,
                frozen_at=at,
            )
        raw = tuple(read.raw for read in candidate.reads)
        require(
            spec.expected_raw_sha256 is None
            or tuple(digest(payload) for payload in raw) == spec.expected_raw_sha256,
            "statement_job_retained_digest_mismatch",
        )
        validate_live_plan(plan, raw)
        return plan, raw

    def _readback(
        self,
        spec: StatementJobSpec,
        plan: StatementPlan,
        raw: tuple[bytes, ...],
        identities: tuple[StatementMaterialIdentity, ...],
    ) -> dict:
        self._check_bindings(spec)
        tickets = []
        try:
            for identity, payload in zip(identities, raw, strict=True):
                ticket = self.protection.acquire(identity)
                tickets.append((identity, ticket))
                ticket.assert_held(identity)
                require(
                    self.store.read_exact(identity) == payload,
                    "statement_job_raw_readback_failed",
                )
            with self.readback() as ledger:
                self._check_independent(ledger, spec)
                at = self.clock()
                exact = read_local_statement(
                    ledger,
                    reference=REFERENCE,
                    version_identity=plan.version.identity,
                    as_of=at,
                )
                as_of = read_local_statement(ledger, reference=REFERENCE, as_of=at)
                require(exact == as_of, "statement_job_exact_as_of_mismatch")
            for identity, ticket in tickets:
                ticket.assert_held(identity)
        finally:
            _release(tickets)
        return {
            "version_identity": exact.version.identity,
            "publication_identity": (exact.publication.request.identity),
            "raw_sha256": [item.sha256 for item in identities],
            "known_at": (exact.publication.decision_at.isoformat()),
        }

    def run(
        self,
        identity: str,
        *,
        enabled: bool,
        preflight: Callable[[StatementJobSpec], None],
        acquirer: DirectStatementAcquirer | None = None,
        retained: StatementCandidate | None = None,
    ) -> dict:
        require(enabled is True, "statement_job_explicit_opt_in_required")
        with self.journal.executor(identity):
            document = self.journal.get(identity)
            if document["state"] != "pending":
                require(
                    document["state"] != "running",
                    "statement_job_restart_reconciliation_required",
                )
                return document
            spec = StatementJobSpec.from_document(document["spec"])
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
                    (
                        spec.mode == "acquire"
                        and type(acquirer) is DirectStatementAcquirer
                        and retained is None
                    )
                    or (
                        spec.mode == "retained"
                        and acquirer is None
                        and type(retained) is StatementCandidate
                    ),
                    "statement_job_direct_acquirer_required",
                )
                preflight(spec)
                self._check_bindings(spec)
                if self.journal.get(identity)["cancel_requested"]:
                    return self._cancelled(identity, "after_preflight")

                def before_request() -> None:
                    self._check_bindings(spec)
                    require(
                        not self.journal.get(identity)["cancel_requested"],
                        "statement_job_cancelled",
                    )
                    require(
                        self.journal.get(identity)["effects"].get(
                            "provider_requests_possible", 0
                        )
                        < 2,
                        "statement_job_network_budget_exhausted",
                    )
                    self.journal._update(
                        identity,
                        self.clock(),
                        stage="acquisition_intent",
                        effects={
                            "provider_requests_possible": self.journal.get(identity)[
                                "effects"
                            ].get("provider_requests_possible", 0)
                            + 1
                        },
                    )

                def after_request(received: bool, attempts: int) -> None:
                    self.journal._update(
                        identity,
                        self.clock(),
                        effects={
                            "provider_attempts_observed": attempts,
                            "provider_response_received": received,
                        },
                    )

                candidate = (
                    acquirer.acquire(identity, before_request, after_request)
                    if acquirer is not None
                    else retained
                )
                self._check_bindings(spec)
                plan, raw = self._candidate(identity, candidate, spec)
                raw_identities = tuple(
                    StatementMaterialIdentity(digest(payload), len(payload))
                    for payload in raw
                )
                self.journal._update(
                    identity,
                    self.clock(),
                    stage="acquired",
                    plan=_encode_plan(plan),
                    raw=[
                        {
                            "sha256": item.sha256,
                            "size": item.size,
                            "locator": item.locator,
                            "observed_at": read.artifact.obtained_at.isoformat(),
                        }
                        for item, read in zip(
                            raw_identities, candidate.reads, strict=True
                        )
                    ],
                    effects={"provider_requests_returned": 2}
                    if spec.mode == "acquire"
                    else {"source_candidate_validated": True},
                )
                if self.journal.get(identity)["cancel_requested"]:
                    return self._cancelled(identity, "before_storage")
                self.journal._update(
                    identity,
                    self.clock(),
                    stage="materialization_intent",
                    writer_session=self._writer_session(),
                    effects={
                        "raw_creates_possible": 2,
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
                        current = self.journal.get(identity)
                        if current["state"] == "outcome_unknown":
                            self.journal._update(
                                identity,
                                self.clock(),
                                reconciliation={
                                    "outcome": "unresolved",
                                    "lock_release_proven": True,
                                },
                            )
                        else:
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
                    tuple(v.identity for v in result.verification) == raw_identities,
                    "statement_material_verification_mismatch",
                )
                self.journal._update(
                    identity,
                    self.clock(),
                    stage="independent_readback",
                    effects={
                        "publication_returned": 1,
                        "raw_dispositions": [
                            "unknown_reconciled_present"
                            if v.reconciled_outcome_unknown
                            else v.disposition.value
                            for v in result.verification
                        ],
                    },
                )
                evidence = self._readback(spec, result.plan, raw, raw_identities)
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
            except StatementMaterializationOutcomeUnknown as exc:
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
        require(enabled is True, "statement_job_explicit_opt_in_required")
        with self.journal.executor(identity):
            document = self.journal.get(identity)
            if document["state"] not in {"running", "outcome_unknown"}:
                return document
            spec = StatementJobSpec.from_document(document["spec"])
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
            pending_readback_release = self._readback_release_handles.get(identity)
            if pending_readback_release is not None:
                try:
                    pending_readback_release.retry_release()
                except StatementProtectionReleaseFailure as exc:
                    self._readback_release_handles[identity] = exc
                    return self.journal._update(
                        identity,
                        self.clock(),
                        stage="reconciliation_unresolved",
                        classification="readback_protection_release_incomplete",
                        reconciliation={
                            "outcome": "unresolved",
                            "classification": "readback_protection_release_incomplete",
                        }
                        | release_proof,
                    )
                del self._readback_release_handles[identity]
                document = self.journal._update(
                    identity,
                    self.clock(),
                    reconciliation={
                        "outcome": "unresolved",
                        "readback_lock_release_proven": True,
                    }
                    | release_proof,
                )
            try:
                require(document["plan"] is not None, "statement_job_plan_missing")
                plan = _decode_plan(document["plan"])
                raw_info = document["raw"]
                require(
                    type(raw_info) is list and len(raw_info) == 2,
                    "statement_job_two_raw_required",
                )
                raw_identities = tuple(
                    StatementMaterialIdentity(item["sha256"], item["size"])
                    for item in raw_info
                )
                require(
                    all(
                        item["locator"] == raw_id.locator
                        for item, raw_id in zip(raw_info, raw_identities, strict=True)
                    ),
                    "statement_job_locator_mismatch",
                )
                candidate = candidate_from_content(plan.content)
                raw = tuple(read.raw for read in candidate.reads)
                validate_live_plan(plan, raw)
                require(
                    raw_identities
                    == tuple(StatementMaterialIdentity(digest(b), len(b)) for b in raw),
                    "statement_job_raw_identity_mismatch",
                )
                require(
                    all(
                        set(item) == {"sha256", "size", "locator", "observed_at"}
                        and item["observed_at"] == read.artifact.obtained_at.isoformat()
                        for item, read in zip(raw_info, candidate.reads, strict=True)
                    ),
                    "statement_job_raw_metadata_mismatch",
                )
                handle = self._unknown_handles.get(identity)
                session = document.get("writer_session")
                require(type(session) is dict, "statement_job_writer_session_missing")
                if handle is not None:
                    with self.readback() as ledger:
                        self._check_independent(ledger, spec)
                        handle.reconcile(ledger)
                        # The callback inside reconcile has already durably
                        # recorded release proof. Drop the resolved handle before
                        # any secondary journal observation can fail; a later
                        # reconcile can then resume from durable proof.
                        del self._unknown_handles[identity]
                        release_proof = {"lock_release_proven": True}
                        self.journal._update(
                            identity,
                            self.clock(),
                            reconciliation={"outcome": "unresolved"} | release_proof,
                        )
                elif spec.effect_domain == "owner_nonproduction":
                    current = self._writer_session()
                    require(
                        session.get("kind") == "postgres"
                        and session.get("host") == DEV_POSTGRES_HOST
                        and type(session.get("backend_pid")) is int
                        and current["backend_pid"] != session["backend_pid"],
                        "statement_job_old_writer_session_not_gone",
                    )
                else:
                    require(
                        session == {"kind": "synthetic", "lock_release_proven": True}
                        or (
                            session
                            == {"kind": "synthetic", "lock_release_proven": False}
                            and release_proof.get("lock_release_proven") is True
                        ),
                        "statement_job_synthetic_lock_release_unproven",
                    )

                tickets = []
                try:
                    for raw_id, payload in zip(raw_identities, raw, strict=True):
                        ticket = self.protection.acquire(raw_id)
                        tickets.append((raw_id, ticket))
                        ticket.assert_held(raw_id)
                        require(
                            self.store.read_exact(raw_id) == payload,
                            "statement_job_raw_readback_failed",
                        )
                    with self.readback() as ledger:
                        self._check_independent(ledger, spec)
                        publication = ledger.publication_by_occurrence(
                            plan.publication_request.occurrence
                        )
                        if publication is None:
                            observation = {
                                "outcome": "confirmed_not_published",
                                "raw_state": "verified",
                                "version_present": ledger.get(plan.version.key)
                                is not None,
                            }
                        else:
                            exact = read_local_statement(
                                ledger,
                                as_of=self.clock(),
                                version_identity=plan.version.identity,
                            )
                            latest = read_local_statement(ledger, as_of=self.clock())
                            require(
                                exact == latest, "statement_job_exact_as_of_mismatch"
                            )
                            observation = {
                                "outcome": "confirmed_published",
                                "raw_state": "verified",
                                "evidence": {
                                    "version_identity": exact.version.identity,
                                    "publication_identity": (
                                        exact.publication.request.identity
                                    ),
                                    "raw_sha256": [i.sha256 for i in raw_identities],
                                    "known_at": (
                                        exact.publication.decision_at.isoformat()
                                    ),
                                },
                            }
                finally:
                    try:
                        _release(tickets)
                    except StatementProtectionReleaseFailure as exc:
                        self._readback_release_handles[identity] = exc
                if identity in self._readback_release_handles:
                    return self.journal._update(
                        identity,
                        self.clock(),
                        stage="reconciliation_unresolved",
                        classification="readback_protection_release_incomplete",
                        reconciliation={
                            "outcome": "unresolved",
                            "classification": "readback_protection_release_incomplete",
                        }
                        | release_proof,
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
