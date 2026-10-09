"""Operator-only canonical metadata inspection; no consumer selection or content IO.

Callers must already be authorized to inspect the ledger. This in-process helper
does not establish an authentication boundary or expose a public wire mode.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime

from market_data_foundation.domain_contracts.dense_market import RAW_DAILY_BARS
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES

from .consumer_read import (
    ReadFailure,
    _classified,
    _definition,
    _effective_publication,
    _fail,
    _get,
    _history,
    _lineage,
)
from .model import (
    ContractError,
    DatasetDefinition,
    DatasetVersion,
    Decision,
    FiniteScope,
    Key,
    Publication,
    Record,
    Scope,
    SourceBindingRevision,
    instant,
)
from .repository import Ledger
from .rules import validate_record


@dataclass(frozen=True)
class PublicationDiagnostic:
    publication: Key
    decision: Decision
    scope: Scope
    decision_at: datetime
    usable_at: datetime
    effective_at: datetime | None
    pending_clocks: tuple[str, ...]


@dataclass(frozen=True)
class VersionDiagnostic:
    version: Key
    frozen_at: datetime
    finite_scope: FiniteScope
    scope: Scope
    completeness: str
    binding_record: Key | None
    binding_limitations: tuple[str, ...]
    publications: tuple[PublicationDiagnostic, ...]
    effective_publication: Key | None
    selection_state: str


@dataclass(frozen=True)
class AsOfDiagnostic:
    definition: Key
    as_of: datetime
    selection_outcome: str
    candidates: tuple[VersionDiagnostic, ...]
    temporal_semantics: str
    completeness_semantics: str
    binding_activation: str = "unobservable"
    source_health: str = "unobservable"
    current_freshness: str = "unobservable"
    external_admission: str = "unobservable"
    content_integrity: str = "unobservable"
    durability: str = "unobservable"
    replayability: str = "unobservable"


def _publication(value: Publication, as_of: datetime) -> PublicationDiagnostic:
    clocks = (
        ("decision_at", value.decision_at),
        ("usable_at", value.usable_at),
        ("effective_at", value.request.effective_at),
    )
    return PublicationDiagnostic(
        publication=value.key,
        decision=value.request.decision,
        scope=value.request.scope,
        decision_at=value.decision_at,
        usable_at=value.usable_at,
        effective_at=value.request.effective_at,
        pending_clocks=tuple(
            name for name, at in clocks if at is not None and at > as_of
        ),
    )


def _validated_binding(
    ledger: Ledger,
    definition: DatasetDefinition,
    version: DatasetVersion,
    history: tuple[Publication, ...],
) -> SourceBindingRevision | None:
    def validate() -> SourceBindingRevision | None:
        records: dict[Key, Record] = {}
        pending: deque[Record] = deque()

        def resolve(key: Key) -> Record:
            if key not in records:
                record = ledger.get(key)
                if record is None or record.key != key:
                    raise ContractError("resolver_record_key_mismatch")
                records[key] = record
                pending.append(record)
            return records[key]

        for record in (definition, version, *history):
            if resolve(record.key) != record:
                raise ContractError("resolver_record_mismatch")
        # Canonical validation resolves related records but does not recursively
        # validate them. Drain every referenced record once, including DataRuns.
        # Pinned exact-key records also support legitimate assessment backrefs.
        while pending:
            validate_record(pending.popleft(), resolve)
        binding, *_ = _lineage(ledger, definition, version, None, resolver=resolve)
        for publication in history:
            _lineage(ledger, definition, version, publication, resolver=resolve)
        return binding

    return _classified(validate, ReadFailure.LINEAGE_MISSING_OR_MISMATCHED)


def _inspect(ledger: Ledger, definition_key: Key, as_of: datetime) -> AsOfDiagnostic:
    definition = _definition(ledger, definition_key)
    if definition.key != definition_key or definition not in (
        BLS_CPI_HEADLINES,
        RAW_DAILY_BARS,
    ):
        _fail(ReadFailure.LINEAGE_MISSING_OR_MISMATCHED)
    _classified(
        lambda: validate_record(definition, ledger.get),
        ReadFailure.LINEAGE_MISSING_OR_MISMATCHED,
    )
    versions = _classified(
        lambda: ledger.versions_for_definition(definition_key),
        ReadFailure.STORAGE_UNAVAILABLE,
    )
    if (
        not isinstance(versions, tuple)
        or any(
            not isinstance(version, DatasetVersion)
            or version.definition != definition_key
            for version in versions
        )
        or tuple(sorted(versions, key=lambda item: item.identity)) != versions
        or len({version.key for version in versions}) != len(versions)
    ):
        _fail(ReadFailure.STORAGE_UNAVAILABLE)

    candidates = []
    eligible_count = 0
    for version in versions:
        if _get(ledger, version.key) != version:
            _fail(ReadFailure.LINEAGE_MISSING_OR_MISMATCHED)

        def validate_coverage() -> None:
            if type(version.finite_scope) is not FiniteScope:
                _fail(ReadFailure.LINEAGE_MISSING_OR_MISMATCHED)
            FiniteScope(
                version.finite_scope.members,
                version.finite_scope.start,
                version.finite_scope.end,
            )

        _classified(validate_coverage, ReadFailure.LINEAGE_MISSING_OR_MISMATCHED)
        history = _history(ledger, version)
        # Validate even unpublished/future/terminal candidates. A corrupt candidate
        # rejects the whole inspection; no partial state is returned.
        for publication in history:
            if _get(ledger, publication.key) != publication:
                _fail(ReadFailure.PUBLICATION_STATE_INVALID)
        binding = _validated_binding(ledger, definition, version, history)
        effective = _effective_publication(history, as_of)
        if not history:
            state = "no_publication"
        elif effective is None:
            state = "no_effective_publication"
        elif effective.request.decision == Decision.WITHDRAW:
            state = ReadFailure.VERSION_WITHDRAWN.value
        elif effective.request.decision == Decision.SUPERSEDE:
            state = ReadFailure.VERSION_SUPERSEDED.value
        else:
            state = "publication_eligible"
            eligible_count += 1
        candidates.append(
            VersionDiagnostic(
                version=version.key,
                frozen_at=version.frozen_at,
                finite_scope=version.finite_scope,
                scope=version.scope,
                completeness=version.completeness,
                binding_record=None if binding is None else binding.key,
                binding_limitations=() if binding is None else binding.limitations,
                publications=tuple(_publication(item, as_of) for item in history),
                effective_publication=None if effective is None else effective.key,
                selection_state=state,
            )
        )
    outcome = (
        ReadFailure.AS_OF_NO_VERSION.value
        if eligible_count == 0
        else ReadFailure.AS_OF_AMBIGUOUS.value
        if eligible_count > 1
        else "one_publication_candidate"
    )
    return AsOfDiagnostic(
        definition=definition_key,
        as_of=as_of,
        selection_outcome=outcome,
        candidates=tuple(candidates),
        temporal_semantics=definition.temporal_semantics,
        completeness_semantics=definition.completeness_semantics,
    )


def inspect_as_of(
    ledger: Ledger, definition: Key, *, as_of: datetime
) -> AsOfDiagnostic:
    """Inspect all exact-definition metadata or fail closed with a classification.

    Eligibility is Publication-only, matching consumer as-of cardinality. Even one
    candidate does not prove a successful exact read, content integrity, service
    availability, or source activation. No version is selected or content fetched.
    """
    _classified(lambda: instant(as_of), ReadFailure.INVALID_AS_OF)
    return _classified(
        lambda: _inspect(ledger, definition, as_of), ReadFailure.STORAGE_UNAVAILABLE
    )
