"""Exact stored-binding/Publication associations in a caller's consistent scope."""

from dataclasses import dataclass, field
from datetime import datetime

from .consumer_read import ReadFailure, _classified, _fail
from .model import DatasetDefinition, Key, Record, instant
from .operator_asof import (
    AsOfDiagnostic,
    _BudgetedReads,
    _WorkBudget,
)
from .operator_asof import (
    _inspect as _inspect_asof,
)
from .operator_bindings import BindingCandidate
from .operator_bindings import _inspect as _inspect_bindings
from .operator_reads import OperatorReadSource
from .postgres import PostgresLedger


@dataclass(frozen=True)
class StoredBindingStatus:
    candidate: BindingCandidate
    referencing_versions: tuple[Key, ...]
    publication_candidates: tuple[Key, ...]
    record_state: str = "stored"


@dataclass(frozen=True)
class OperatorStatus:
    definition: DatasetDefinition
    as_of: AsOfDiagnostic
    stored_bindings: tuple[StoredBindingStatus, ...]
    versions_without_binding_record: tuple[Key, ...]
    binding_activation: str = "unobservable"
    executable_binding: str = "unobservable"
    source_health: str = "unobservable"
    current_freshness: str = "unobservable"
    external_admission: str = "unobservable"
    finite_coverage: str = "unobservable"
    quality: str = "unobservable"
    content_integrity: str = "unobservable"
    durability: str = "unobservable"
    replayability: str = "unobservable"
    current_availability: str = "unobservable"


@dataclass
class _StatusReads(_BudgetedReads):
    observed: dict[Key, Record | None] = field(default_factory=dict)

    def get(self, key: Key) -> Record | None:
        # Do not substitute cached records or bypass producer/application work
        # accounting. Compare complete readbacks across both inspections. This
        # detects conflicts, but cannot establish a consistent storage snapshot.
        record = super().get(key)
        if key in self.observed and self.observed[key] != record:
            _fail(ReadFailure.LINEAGE_MISSING_OR_MISMATCHED)
        self.observed[key] = record
        return record


def _inspect(reads: _StatusReads, definition: Key, as_of: datetime) -> OperatorStatus:
    diagnostic = _inspect_asof(reads, definition, as_of)
    inventory = _inspect_bindings(reads, definition)
    references: dict[Key, list[Key]] = {
        candidate.binding: [] for candidate in inventory.candidates
    }
    eligible: dict[Key, list[Key]] = {key: [] for key in references}
    unbound = []
    for version in diagnostic.candidates:
        binding = version.binding_record
        if binding is None:
            unbound.append(version.version)
        else:
            # A valid exact lineage record must also occur in the complete
            # stored inventory with a mapping to this exact Definition. Never
            # join by identity alone, choose a revision, or invent a candidate.
            if binding not in references:
                _fail(ReadFailure.LINEAGE_MISSING_OR_MISMATCHED)
            references[binding].append(version.version)
            if version.selection_state == "publication_eligible":
                eligible[binding].append(version.version)
    result = OperatorStatus(
        definition=inventory.definition,
        as_of=diagnostic,
        stored_bindings=tuple(
            StoredBindingStatus(
                candidate=candidate,
                referencing_versions=tuple(references[candidate.binding]),
                publication_candidates=tuple(eligible[candidate.binding]),
            )
            for candidate in inventory.candidates
        ),
        versions_without_binding_record=tuple(unbound),
    )
    reads.budget.metadata(result)
    return result


def inspect_operator_status(
    scope: OperatorReadSource, definition: Key, *, as_of: datetime
) -> OperatorStatus:
    """Project metadata atomically or fail classified, without content access.

    An already authorized caller MUST supply a consistent read-only scope. For
    PostgreSQL, pass the source yielded by explicit operator_snapshot() and use
    the result only after successful context exit. This function creates no
    transaction and does not certify consistency, authorization or availability.
    One producer session and application budget cover both inspections, repeated
    readbacks and the final association projection; failure yields no partial view.
    """
    _classified(lambda: instant(as_of), ReadFailure.INVALID_AS_OF)

    def inspect() -> OperatorStatus:
        # The concrete ledger's ordinary operator_reads() entry has no snapshot
        # ownership. Require its explicit yielded scope, never adopt ambient work.
        # Custom sources still own their consistency contract; this is no security
        # boundary against a caller disguising an unsafe source behind a proxy.
        if isinstance(scope, PostgresLedger):
            _fail(ReadFailure.STORAGE_UNAVAILABLE)
        return _inspect(
            _StatusReads(scope.operator_reads(), _WorkBudget()), definition, as_of
        )

    return _classified(inspect, ReadFailure.STORAGE_UNAVAILABLE)
