"""Four-raw exact handoff; all writer tickets span authoritative outcomes."""

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
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.canonical.repository import Ledger
from market_data_foundation.historical.bls_cpi_material_store import (
    MaterialIdentity,
    MaterialStore,
    MaterialWriteOutcomeUnknown,
    WriteDisposition,
    WriterProtection,
    WriterProtectionTicket,
)
from market_data_foundation.historical.fred_alfred_vintage import (
    VintagePlan,
    durable_plan,
    persist_before_publication,
    publish,
    validate_live_plan,
)


class VintageUnknownPhase(StrEnum):
    LEDGER_HANDOFF = "ledger_handoff"
    PUBLICATION = "publication"


@dataclass(frozen=True)
class VintageMaterialVerification:
    identity: MaterialIdentity
    disposition: WriteDisposition | None
    reconciled_outcome_unknown: bool


@dataclass(frozen=True)
class VintageMaterializedResult:
    plan: VintagePlan
    verification: tuple[VintageMaterialVerification, ...]
    version: DatasetVersion
    publication: Publication


class VintageProtectionReleaseFailure(RuntimeError):
    """Retain each ticket and release progress for an explicit retry."""

    def __init__(self, tickets, released):
        self.tickets = tuple(tickets)
        self.released_ticket_ids = released
        super().__init__("vintage_protection_release_incomplete")

    def retry_release(self):
        _release(self.tickets, self.released_ticket_ids)


def _release(tickets, released=None):
    released = set() if released is None else released
    errors = []
    for identity, ticket in reversed(tickets):
        if id(ticket) in released:
            continue
        try:
            ticket.release(identity)
            released.add(id(ticket))
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise VintageProtectionReleaseFailure(tickets, released) from errors[0]


class VintageMaterializationOutcomeUnknown(RuntimeError):
    def __init__(
        self,
        phase: VintageUnknownPhase,
        plan: VintagePlan,
        tickets: tuple[tuple[MaterialIdentity, WriterProtectionTicket], ...],
        protection_released: Callable[[], None],
        writer_ledger: Ledger,
    ):
        self._writer_ledger = writer_ledger
        self.phase = phase
        self.plan = plan
        self.tickets = tickets
        self._released = set()
        self._resolved = False
        self._protection_released = protection_released
        for identity, ticket in tickets:
            ticket.mark_outcome_unknown(identity)
        super().__init__("vintage_authoritative_outcome_unknown")

    def reconcile(self, ledger: Ledger) -> str:
        require(not self._resolved, "vintage_reconciliation_already_resolved")
        require(
            ledger is not self._writer_ledger, "vintage_independent_ledger_required"
        )
        if isinstance(self._writer_ledger, PostgresLedger):
            require(
                isinstance(ledger, PostgresLedger)
                and ledger.connection is not self._writer_ledger.connection
                and ledger.schema == self._writer_ledger.schema
                and ledger.connection.info.host
                == self._writer_ledger.connection.info.host
                and ledger.connection.info.backend_pid
                != self._writer_ledger.connection.info.backend_pid,
                "vintage_independent_connection_required",
            )
        for identity, ticket in self.tickets:
            if id(ticket) not in self._released:
                ticket.assert_held(identity)
        if self.phase == VintageUnknownPhase.LEDGER_HANDOFF:
            version = ledger.get(self.plan.version.key)
            content = ledger.get_content(self.plan.version.key)
            if version is None:
                require(content is None, "vintage_ledger_absence_mismatch")
                outcome = "confirmed_not_committed"
            else:
                require(
                    version == self.plan.version
                    and content == self.plan.content
                    and digest(content) == version.content_digest,
                    "vintage_ledger_readback_mismatch",
                )
                outcome = "confirmed_committed"
        else:
            publication = ledger.publication_by_occurrence(
                self.plan.publication_request.occurrence
            )
            if publication is None:
                outcome = "confirmed_not_published"
            else:
                require(
                    publication.request == self.plan.publication_request,
                    "vintage_publication_readback_mismatch",
                )
                outcome = "confirmed_published"
        for identity, ticket in reversed(self.tickets):
            if id(ticket) not in self._released:
                ticket.release(identity)
                self._released.add(id(ticket))
        self._protection_released()
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
    plan: VintagePlan,
    raw: tuple[bytes, ...],
    store: MaterialStore,
    protection: WriterProtection,
    *,
    authoritative_commit: Callable[[], None] = lambda: None,
    protection_released: Callable[[], None] = lambda: None,
) -> VintageMaterializedResult:
    tickets, verifications = [], []
    authoritative_started = False
    try:
        validate_live_plan(plan, raw)
        for material, payload in zip(plan.raw_materials, raw, strict=True):
            identity = MaterialIdentity(material.digest or "", len(payload))
            ticket = protection.acquire(identity)
            tickets.append((identity, ticket))
            ticket.assert_held(identity)
            _verify(identity, payload, "vintage_raw_content_mismatch")
            unknown = False
            try:
                disposition = store.create_if_absent(identity, payload)
            except MaterialWriteOutcomeUnknown:
                disposition, unknown = None, True
            ticket.assert_held(identity)
            observed = store.read_exact(identity)
            require(observed is not None, "vintage_raw_write_outcome_unknown")
            _verify(identity, observed, "vintage_raw_readback_mismatch")
            verifications.append(
                VintageMaterialVerification(identity, disposition, unknown)
            )
        materialized = durable_plan(
            plan, tuple(identity.locator for identity, _ in tickets)
        )
        for identity, ticket in tickets:
            ticket.assert_held(identity)
        version = persist_before_publication(kernel, materialized)
        authoritative_started = True
        try:
            authoritative_commit()
        except Exception as exc:
            raise VintageMaterializationOutcomeUnknown(
                VintageUnknownPhase.LEDGER_HANDOFF,
                materialized,
                tuple(tickets),
                protection_released,
                kernel.ledger,
            ) from exc
        try:
            for identity, ticket in tickets:
                ticket.assert_held(identity)
            publication = publish(kernel, materialized)
        except Exception as exc:
            raise VintageMaterializationOutcomeUnknown(
                VintageUnknownPhase.PUBLICATION,
                materialized,
                tuple(tickets),
                protection_released,
                kernel.ledger,
            ) from exc
        _release(tickets)
        protection_released()
        return VintageMaterializedResult(
            materialized, tuple(verifications), version, publication
        )
    except VintageMaterializationOutcomeUnknown:
        raise
    except Exception:
        if not authoritative_started:
            _release(tickets)
            protection_released()
        raise
