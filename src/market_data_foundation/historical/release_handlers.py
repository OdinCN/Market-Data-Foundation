"""Two concrete retained-only T09 handlers. No Source clients or service startup.

Bindings are explicitly injected offline capabilities. Each slot has one existing
source-specific MacroJobJournal, including its original repair/reconcile rules.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from market_data_foundation.canonical.model import digest, require
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical import fomc_statement as statement
from market_data_foundation.historical import fred_alfred_vintage as vintage
from market_data_foundation.historical.bls_cpi_material_store import (
    GarageMaterialStore,
    PostgresAdvisoryWriterProtection,
)
from market_data_foundation.historical.fomc_statement_local_job import (
    StatementJobJournal,
    StatementJobSpec,
    StatementLocalJob,
)
from market_data_foundation.historical.fomc_statement_local_read import (
    read_local_statement,
)
from market_data_foundation.historical.macro_job_journal import valid_identity
from market_data_foundation.historical.macro_lifecycle import decode_plan
from market_data_foundation.historical.vintage_local_job import (
    VintageJobJournal,
    VintageJobSpec,
    VintageLocalJob,
)
from market_data_foundation.historical.vintage_local_read import read_local_vintage

FOMC = "fomc-statement"
VINTAGE = "fred-alfred-vintage"


def _descriptor(plan, *, family, reference, release_at, semantics):
    import json

    content = json.loads(plan.content)
    # Artifact locations are not acquisition identities. Moving retained files
    # must not allow a second publication of the same acquisition.
    for request in content["requests"]:
        request.pop("raw_path")
        request.pop("request_path")
    fingerprint = digest(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    )
    return {
        "family": family,
        "reference": reference,
        "evidence_sha256": fingerprint,
        "raw_sha256": [m.digest for m in plan.raw_materials],
        "requested_at": min(a.requested_at for a in plan.acquisitions).isoformat(),
        "acquired_at": max(a.obtained_at for a in plan.acquisitions).isoformat(),
        "source_release_at": None if release_at is None else release_at.isoformat(),
        "time_semantics": semantics,
    }


def _observation(view, descriptor):
    return descriptor | {
        "version_identity": view.version.identity,
        "publication_identity": view.publication.request.identity,
        "known_at": view.publication.decision_at.isoformat(),
        "usable_at": view.publication.usable_at.isoformat(),
        "coverage": list(view.version.finite_scope.members),
    }


@dataclass
class OfflineBindings:
    """Trusted injected fixture IO; real PG/Garage bindings are rejected.

    Custom store/protection/readback/commit hooks must be offline. This is a
    capability boundary, not a sandbox for arbitrary caller-supplied Python.
    """

    root: Path
    clock: object
    kernel: object
    store: object
    protection: object
    readback: object
    commit: object
    _engines: dict = field(default_factory=dict, init=False, repr=False)

    def check(self):
        require(
            isinstance(self.kernel.ledger, InMemoryLedger)
            and not isinstance(self.store, GarageMaterialStore)
            and not isinstance(self.protection, PostgresAdvisoryWriterProtection),
            "scheduler_offline_bindings_required",
        )

    def _engine(self, slot, journal_type, engine_type, *, create):
        valid_identity(slot)
        self.check()
        path = self.root / slot
        require(
            create or (path / journal_type.filename).is_file(), "handler_job_missing"
        )
        if slot not in self._engines:
            self._engines[slot] = engine_type(
                journal_type(path),
                self.clock,
                self.kernel,
                self.store,
                self.protection,
                self.fresh_readback,
                self.commit,
            )
        return self._engines[slot]

    @contextmanager
    def fresh_readback(self):
        """Offline authority is the synchronous writer, never a reader's claim.

        Check serialized state using the base implementation, then expose a new
        base ledger, so custom reader methods cannot invent absence. Check both
        sides of context exit as injected hooks may change the writer there.
        No PG transaction/replica freshness is implied by this offline protocol.
        """
        self.check()
        writer = self.kernel.ledger
        with self.readback() as reader:
            require(
                isinstance(reader, InMemoryLedger) and reader is not writer,
                "scheduler_offline_independent_read_required",
            )
            authoritative = InMemoryLedger.snapshot(writer)
            require(
                InMemoryLedger.snapshot(reader) == authoritative,
                "scheduler_stale_readback",
            )
            yield InMemoryLedger.from_snapshot(authoritative)
            require(
                InMemoryLedger.snapshot(writer) == authoritative,
                "scheduler_writer_changed_during_readback",
            )
        require(
            InMemoryLedger.snapshot(writer) == authoritative,
            "scheduler_writer_changed_during_readback",
        )

    def recovery_evidence(self, slot, document):
        """Bind a resolution to the exact attempt, validated plan and raw bytes."""
        import json

        reconciliation = document["reconciliation"] or {}
        outcome = reconciliation.get("outcome")
        require(
            outcome in {"confirmed_published", "confirmed_not_published"}
            and reconciliation.get("readback_protection_pending") is False,
            "scheduler_recovery_protection_unproven",
        )
        require(
            document["job_id"] == slot["attempt_jobs"][-1]
            and document["attempt_id"] == document["job_id"] + "-a1"
            and document["attempts"] == 1,
            "scheduler_recovery_attempt_mismatch",
        )
        module = statement if self.family == FOMC else vintage
        plan_type = (
            statement.StatementPlan if self.family == FOMC else vintage.VintagePlan
        )
        plan = decode_plan(
            document["plan"], plan_type, "scheduler_recovery_plan_invalid"
        )
        candidate = module.candidate_from_content(plan.content)
        raw = tuple(r.raw for r in candidate.reads)
        module.validate_live_plan(plan, raw)
        require(
            self.describe(candidate, self.clock()) == slot["descriptor"]
            and [digest(b) for b in raw] == slot["descriptor"]["raw_sha256"]
            and document["spec"]["expected_raw_sha256"]
            == slot["descriptor"]["raw_sha256"]
            and [r["sha256"] for r in document["raw"]]
            == slot["descriptor"]["raw_sha256"]
            and [r["size"] for r in document["raw"]] == [len(b) for b in raw],
            "scheduler_recovery_raw_plan_mismatch",
        )
        with self.fresh_readback() as ledger:
            publication = ledger.publication_by_occurrence(
                plan.publication_request.occurrence
            )
            if outcome == "confirmed_not_published":
                require(
                    publication is None
                    and not ledger.publications_for_version(plan.version.key),
                    "scheduler_recovery_absence_unproven",
                )
                require(
                    reconciliation.get("raw_state") == "verified",
                    "scheduler_recovery_raw_unverified",
                )
            else:
                require(
                    publication is not None
                    and publication.request == plan.publication_request,
                    "scheduler_recovery_publication_mismatch",
                )
            snapshot_sha256 = digest(InMemoryLedger.snapshot(ledger).encode())
        binding = {
            key: document[key] for key in ("job_id", "attempt_id", "plan", "raw")
        }
        return {
            "authority": "offline_synchronous_inmemory_writer",
            "binding_sha256": digest(
                json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
            ),
            "snapshot_sha256": snapshot_sha256,
        }


class StatementReleaseHandler(OfflineBindings):
    family = FOMC
    reference = statement.REFERENCE
    semantics = "official_document_release"
    raw_budget = 2

    def engine(self, slot, *, create=False):
        return self._engine(slot, StatementJobJournal, StatementLocalJob, create=create)

    def describe(self, candidate, at: datetime):
        self.check()
        plan = statement.build_live_plan(
            candidate, identity="t09-inspect-statement", finished_at=at, frozen_at=at
        )
        statement.validate_live_plan(plan, tuple(r.raw for r in candidate.reads))
        return _descriptor(
            plan,
            family=self.family,
            reference=self.reference,
            release_at=candidate.document.official_published_at,
            semantics=self.semantics,
        )

    def spec(self, descriptor):
        return StatementJobSpec(
            mode="retained",
            max_provider_requests=0,
            expected_raw_sha256=tuple(descriptor["raw_sha256"]),
        )

    def observe(self, document, descriptor, at):
        self.check()
        evidence = document["last_success"]["evidence"]
        with self.fresh_readback() as ledger:
            require(
                isinstance(ledger, InMemoryLedger), "scheduler_offline_read_required"
            )
            view = read_local_statement(
                ledger, as_of=at, version_identity=evidence["version_identity"]
            )
            candidate = statement.candidate_from_content(view.content)
            require(
                self.describe(candidate, at) == descriptor, "slot_readback_mismatch"
            )
            return _observation(view, descriptor)


class VintageReleaseHandler(OfflineBindings):
    family = VINTAGE
    reference = vintage.REFERENCE
    semantics = "aggregator_acquisition_window"
    raw_budget = 4

    def engine(self, slot, *, create=False):
        return self._engine(slot, VintageJobJournal, VintageLocalJob, create=create)

    def describe(self, candidate, at: datetime):
        self.check()
        plan = vintage.build_live_plan(
            candidate, identity="t09-inspect-vintage", finished_at=at, frozen_at=at
        )
        vintage.validate_live_plan(plan, tuple(r.raw for r in candidate.reads))
        return _descriptor(
            plan,
            family=self.family,
            reference=self.reference,
            release_at=None,
            semantics=self.semantics,
        )

    def spec(self, descriptor):
        return VintageJobSpec(
            mode="retained",
            max_provider_requests=0,
            expected_raw_sha256=tuple(descriptor["raw_sha256"]),
        )

    def observe(self, document, descriptor, at):
        import json

        self.check()
        evidence = document["last_success"]["evidence"]
        with self.fresh_readback() as ledger:
            require(
                isinstance(ledger, InMemoryLedger), "scheduler_offline_read_required"
            )
            view = read_local_vintage(
                ledger, as_of=at, version_identity=evidence["version_identity"]
            )
            candidate = vintage.candidate_from_content(view.content)
            require(
                self.describe(candidate, at) == descriptor, "slot_readback_mismatch"
            )
            content = json.loads(view.content)
            return _observation(view, descriptor) | {
                "selected_vintages": content["selected_vintages"],
                "unavailable_members": [
                    f"{r['series_id']}|{r['vintage']}|{r['date']}"
                    for r in content["observations"]
                    if r["unavailable"]
                ],
            }


HANDLER_TYPES = {FOMC: StatementReleaseHandler, VINTAGE: VintageReleaseHandler}
UNSUPPORTED = {
    "bls-cpi": "scheduler_adapter_not_implemented",
    "bls-ppi": "scheduler_adapter_not_implemented",
    "bls-employment": "scheduler_adapter_not_implemented",
    "bea-pce": "scheduler_adapter_not_implemented",
    "census-retail": "scheduler_adapter_not_implemented",
    "ecb-sdmx": "source_and_handoff_claims_need_dedicated_scheduler_adapter",
    "eurostat-sdmx": "source_and_handoff_claims_need_dedicated_scheduler_adapter",
}
