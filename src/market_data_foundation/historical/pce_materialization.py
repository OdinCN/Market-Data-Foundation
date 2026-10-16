"""Garage raw handoff and canonical publication for the bounded PCE plan."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import (
    DatasetVersion,
    Publication,
    digest,
    require,
)
from market_data_foundation.canonical.repository import Ledger
from market_data_foundation.historical.bea_pce import (
    PcePlan,
    durable_plan,
    persist_before_publication,
    publish,
    validate_plan,
)
from market_data_foundation.historical.bls_cpi_material_store import (
    MaterialStore,
    MaterialWriteOutcomeUnknown,
    WriteDisposition,
    WriterProtection,
    WriterProtectionTicket,
)
from market_data_foundation.historical.pce_material_store import PceMaterialIdentity


class PceUnknownPhase(StrEnum):
    LEDGER_HANDOFF = "ledger_handoff"
    PUBLICATION = "publication"


@dataclass(frozen=True)
class PceMaterialVerification:
    """Exact raw observation without inferring the outcome of an unknown PUT."""

    identity: PceMaterialIdentity
    disposition: WriteDisposition | None
    reconciled_outcome_unknown: bool


@dataclass(frozen=True)
class PceMaterializedResult:
    plan: PcePlan
    verification: PceMaterialVerification
    version: DatasetVersion
    publication: Publication


class PceMaterializationOutcomeUnknown(RuntimeError):
    def __init__(
        self,
        phase: PceUnknownPhase,
        identity: PceMaterialIdentity,
        plan: PcePlan,
        ticket: WriterProtectionTicket,
    ) -> None:
        self.phase = phase
        self.identity = identity
        self.plan = plan
        self._ticket = ticket
        self._resolved = False
        ticket.mark_outcome_unknown(identity)
        super().__init__("pce_authoritative_outcome_unknown")

    def reconcile(self, ledger: Ledger) -> str:
        require(not self._resolved, "pce_reconciliation_already_resolved")
        self._ticket.assert_held(self.identity)
        if self.phase == PceUnknownPhase.LEDGER_HANDOFF:
            observed = ledger.get(self.plan.version.key)
            if observed is None:
                require(
                    ledger.get_content(self.plan.version.key) is None,
                    "pce_ledger_absence_mismatch",
                )
                outcome = "confirmed_not_committed"
            else:
                require(observed == self.plan.version, "pce_version_readback_mismatch")
                content = ledger.get_content(self.plan.version.key)
                require(
                    content == self.plan.content
                    and digest(content) == self.plan.version.content_digest,
                    "pce_content_readback_mismatch",
                )
                outcome = "confirmed_committed"
        else:
            observed = ledger.publication_by_occurrence(
                self.plan.publication_request.occurrence
            )
            if observed is None:
                outcome = "confirmed_not_published"
            else:
                require(
                    observed.request == self.plan.publication_request,
                    "pce_publication_readback_mismatch",
                )
                outcome = "confirmed_published"
        self._ticket.assert_held(self.identity)
        self._ticket.release(self.identity)
        self._resolved = True
        return outcome


def _verify(identity: PceMaterialIdentity, payload: bytes, classification: str) -> None:
    require(
        type(payload) is bytes
        and len(payload) == identity.size
        and digest(payload) == identity.sha256,
        classification,
    )


def materialize_persist_publish(
    kernel: CanonicalKernel,
    plan: PcePlan,
    raw: bytes,
    store: MaterialStore,
    protection: WriterProtection,
    *,
    authoritative_commit: Callable[[], None] = lambda: None,
) -> PceMaterializedResult:
    """Create/verify raw first, commit canonical graph, and publish last."""
    validate_plan(plan, raw)
    identity = PceMaterialIdentity(plan.raw_material.digest or "", len(raw))
    ticket = protection.acquire(identity)
    authoritative_started = False
    try:
        ticket.assert_held(identity)
        _verify(identity, raw, "pce_raw_content_mismatch")
        reconciled_outcome_unknown = False
        try:
            disposition = store.create_if_absent(identity, raw)
        except MaterialWriteOutcomeUnknown:
            disposition = None
            reconciled_outcome_unknown = True
        ticket.assert_held(identity)
        observed_raw = store.read_exact(identity)
        require(observed_raw is not None, "pce_raw_write_outcome_unknown")
        _verify(identity, observed_raw, "pce_raw_readback_mismatch")
        materialized = durable_plan(plan, identity.locator)
        authoritative_started = True
        try:
            version = persist_before_publication(kernel, materialized)
            authoritative_commit()
        except Exception as exc:
            raise PceMaterializationOutcomeUnknown(
                PceUnknownPhase.LEDGER_HANDOFF, identity, materialized, ticket
            ) from exc
        try:
            ticket.assert_held(identity)
            publication = publish(kernel, materialized)
            ticket.assert_held(identity)
        except Exception as exc:
            raise PceMaterializationOutcomeUnknown(
                PceUnknownPhase.PUBLICATION, identity, materialized, ticket
            ) from exc
        ticket.release(identity)
        verification = PceMaterialVerification(
            identity, disposition, reconciled_outcome_unknown
        )
        return PceMaterializedResult(materialized, verification, version, publication)
    except PceMaterializationOutcomeUnknown:
        raise
    except Exception:
        if not authoritative_started:
            ticket.release(identity)
        raise
