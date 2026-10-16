"""Garage raw handoff and canonical publication for the bounded Census Retail plan."""

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
from market_data_foundation.historical.bls_cpi_material_store import (
    MaterialIdentity,
    MaterialStore,
    MaterialWriteOutcomeUnknown,
    WriteDisposition,
    WriterProtection,
    WriterProtectionTicket,
)
from market_data_foundation.historical.census_retail import (
    RetailPlan,
    durable_plan,
    persist_before_publication,
    publish,
    validate_live_plan,
)


class RetailUnknownPhase(StrEnum):
    LEDGER_HANDOFF = "ledger_handoff"
    PUBLICATION = "publication"


@dataclass(frozen=True)
class RetailMaterialVerification:
    """Exact raw observation without inferring the outcome of an unknown PUT."""

    identity: MaterialIdentity
    disposition: WriteDisposition | None
    reconciled_outcome_unknown: bool


@dataclass(frozen=True)
class RetailMaterializedResult:
    plan: RetailPlan
    verification: RetailMaterialVerification
    version: DatasetVersion
    publication: Publication


class RetailMaterializationOutcomeUnknown(RuntimeError):
    def __init__(
        self,
        phase: RetailUnknownPhase,
        identity: MaterialIdentity,
        plan: RetailPlan,
        ticket: WriterProtectionTicket,
    ) -> None:
        self.phase = phase
        self.identity = identity
        self.plan = plan
        self._ticket = ticket
        self._resolved = False
        ticket.mark_outcome_unknown(identity)
        super().__init__("retail_authoritative_outcome_unknown")

    def reconcile(self, ledger: Ledger) -> str:
        require(not self._resolved, "retail_reconciliation_already_resolved")
        self._ticket.assert_held(self.identity)
        if self.phase == RetailUnknownPhase.LEDGER_HANDOFF:
            observed = ledger.get(self.plan.version.key)
            if observed is None:
                require(
                    ledger.get_content(self.plan.version.key) is None,
                    "retail_ledger_absence_mismatch",
                )
                outcome = "confirmed_not_committed"
            else:
                require(
                    observed == self.plan.version, "retail_version_readback_mismatch"
                )
                content = ledger.get_content(self.plan.version.key)
                require(
                    content == self.plan.content
                    and digest(content) == self.plan.version.content_digest,
                    "retail_content_readback_mismatch",
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
                    "retail_publication_readback_mismatch",
                )
                outcome = "confirmed_published"
        self._ticket.assert_held(self.identity)
        self._ticket.release(self.identity)
        self._resolved = True
        return outcome


def _verify(identity: MaterialIdentity, payload: bytes, classification: str) -> None:
    require(
        type(payload) is bytes
        and len(payload) == identity.size
        and digest(payload) == identity.sha256,
        classification,
    )


def materialize_persist_publish(
    kernel: CanonicalKernel,
    plan: RetailPlan,
    raw: bytes,
    store: MaterialStore,
    protection: WriterProtection,
    *,
    authoritative_commit: Callable[[], None] = lambda: None,
    protection_released: Callable[[], None] = lambda: None,
) -> RetailMaterializedResult:
    """Create/verify raw first, commit canonical graph, and publish last."""
    validate_live_plan(plan, raw)
    identity = MaterialIdentity(plan.raw_material.digest or "", len(raw))
    ticket = protection.acquire(identity)
    authoritative_started = False
    try:
        ticket.assert_held(identity)
        _verify(identity, raw, "retail_raw_content_mismatch")
        reconciled_outcome_unknown = False
        try:
            disposition = store.create_if_absent(identity, raw)
        except MaterialWriteOutcomeUnknown:
            disposition = None
            reconciled_outcome_unknown = True
        ticket.assert_held(identity)
        observed_raw = store.read_exact(identity)
        require(observed_raw is not None, "retail_raw_write_outcome_unknown")
        _verify(identity, observed_raw, "retail_raw_readback_mismatch")
        materialized = durable_plan(plan, identity.locator)
        version = persist_before_publication(kernel, materialized)
        authoritative_started = True
        try:
            authoritative_commit()
        except Exception as exc:
            raise RetailMaterializationOutcomeUnknown(
                RetailUnknownPhase.LEDGER_HANDOFF, identity, materialized, ticket
            ) from exc
        ticket.assert_held(identity)
        try:
            publication = publish(kernel, materialized)
        except Exception as exc:
            raise RetailMaterializationOutcomeUnknown(
                RetailUnknownPhase.PUBLICATION, identity, materialized, ticket
            ) from exc
        ticket.assert_held(identity)
        ticket.release(identity)
        protection_released()
        verification = RetailMaterialVerification(
            identity, disposition, reconciled_outcome_unknown
        )
        return RetailMaterializedResult(
            materialized, verification, version, publication
        )
    except RetailMaterializationOutcomeUnknown:
        raise
    except Exception:
        if not authoritative_started:
            ticket.release(identity)
            protection_released()
        raise
