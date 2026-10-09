"""Operator-only canonical metadata inspection; no consumer selection or content IO.

Callers must already be authorized to inspect the ledger. This in-process helper
does not establish an authentication boundary or expose a public wire mode.
"""

from collections import deque
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from enum import StrEnum

from market_data_foundation.domain_contracts.dense_market import RAW_DAILY_BARS
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES

from .consumer_read import (
    ReadContractError,
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

# Fixed operator-only limits. Ledger calls materialize before these checks;
# these are application work units, not backend IO/memory/CPU/time guarantees.
_MAX_VERSIONS = 128
_MAX_PUBLICATIONS = 512
_MAX_LINEAGE_RECORDS = 2048
_MAX_RESOLUTIONS = 32768
_MAX_METADATA_NODES = 1048576
_MAX_TEXT_CHARACTERS = 4194304
_MAX_STRING_CHARACTERS = 16384
_MAX_COLLECTION_ITEMS = 4096
_MAX_METADATA_DEPTH = 32


class OperatorFailure(StrEnum):
    QUERY_BUDGET_EXCEEDED = "operator_query_budget_exceeded"


class OperatorQueryBudgetError(ReadContractError):
    """Operator-only classification; never added to the public wire contract."""

    def __init__(self) -> None:
        self.classification = OperatorFailure.QUERY_BUDGET_EXCEEDED
        Exception.__init__(self, self.classification.value)


def _bounded(value: int, limit: int) -> None:
    if value > limit:
        raise OperatorQueryBudgetError() from None


@dataclass
class _WorkBudget:
    publications: int = 0
    lineage_records: int = 0
    resolutions: int = 0
    metadata_nodes: int = 0
    text_characters: int = 0

    def take(self, name: str, count: int, limit: int) -> None:
        value = getattr(self, name) + count
        _bounded(value, limit)
        setattr(self, name, value)

    def metadata(self, value: object, depth: int = 0) -> None:
        # Check shape before canonical validators, equality, sorting, digest
        # projection or recursive immutable_shape can traverse large metadata.
        # Count repeated occurrences too; no identity dedup hides repeated work.
        _bounded(depth, _MAX_METADATA_DEPTH)
        self.take("metadata_nodes", 1, _MAX_METADATA_NODES)
        if isinstance(value, str):
            # Use the built-in payload length, bypassing subclass __len__.
            # This also preserves canonical StrEnum metadata without conversion.
            characters = str.__len__(value)
            _bounded(characters, _MAX_STRING_CHARACTERS)
            self.take("text_characters", characters, _MAX_TEXT_CHARACTERS)
        elif value is None or isinstance(value, (bool, datetime)):
            return
        elif type(value) is int:
            _bounded(value.bit_length(), 64)
        elif type(value) in (tuple, frozenset):
            _bounded(len(value), _MAX_COLLECTION_ITEMS)
            for item in value:
                self.metadata(item, depth + 1)
        elif is_dataclass(value) and not isinstance(value, type):
            for field in fields(value):
                self.metadata(getattr(value, field.name), depth + 1)
        else:
            raise ContractError("invalid_metadata_shape")


@dataclass
class _BudgetedReads:
    ledger: Ledger
    budget: _WorkBudget

    def get(self, key: Key) -> Record | None:
        self.budget.take("resolutions", 1, _MAX_RESOLUTIONS)
        self.budget.metadata(key)
        record = self.ledger.get(key)
        self.budget.metadata(record)
        return record

    def versions_for_definition(self, definition: Key) -> tuple[DatasetVersion, ...]:
        versions = self.ledger.versions_for_definition(definition)
        if type(versions) is not tuple:
            _fail(ReadFailure.STORAGE_UNAVAILABLE)
        if versions:
            _bounded(len(versions), _MAX_VERSIONS)
            self.budget.metadata(versions)
        return versions

    def publications_for_version(self, version: Key) -> tuple[Publication, ...]:
        history = self.ledger.publications_for_version(version)
        if type(history) is not tuple:
            _fail(ReadFailure.PUBLICATION_STATE_INVALID)
        if history:
            self.budget.take("publications", len(history), _MAX_PUBLICATIONS)
            self.budget.metadata(history)
        return history


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
    budget: _WorkBudget,
) -> SourceBindingRevision | None:
    def validate() -> SourceBindingRevision | None:
        records: dict[Key, Record] = {}
        pending: deque[Record] = deque()

        def resolve(key: Key) -> Record:
            budget.take("resolutions", 1, _MAX_RESOLUTIONS)
            if key not in records:
                budget.take("lineage_records", 1, _MAX_LINEAGE_RECORDS)
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


def _inspect(
    ledger: _BudgetedReads, definition_key: Key, as_of: datetime
) -> AsOfDiagnostic:
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
        binding = _validated_binding(
            ledger, definition, version, history, ledger.budget
        )
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
    diagnostic = AsOfDiagnostic(
        definition=definition_key,
        as_of=as_of,
        selection_outcome=outcome,
        candidates=tuple(candidates),
        temporal_semantics=definition.temporal_semantics,
        completeness_semantics=definition.completeness_semantics,
    )
    ledger.budget.metadata(diagnostic)
    return diagnostic


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
        lambda: _inspect(_BudgetedReads(ledger, _WorkBudget()), definition, as_of),
        ReadFailure.STORAGE_UNAVAILABLE,
    )
