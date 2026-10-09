"""Stored candidates; caller owns authorization and snapshot consistency."""

from dataclasses import dataclass, fields

from market_data_foundation.domain_contracts.dense_market import RAW_DAILY_BARS
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES

from .consumer_read import ReadFailure, _classified, _definition
from .model import (
    DatasetDefinition,
    EvidenceRequirement,
    Grade,
    Key,
    OutputMapping,
    Record,
    Scope,
    SourceBindingRevision,
    SourceOperationRef,
    require,
)
from .operator_asof import _bounded, _BudgetedReads, _WorkBudget
from .operator_reads import MAX_BINDINGS, OperatorReadSource
from .rules import validate_record


@dataclass(frozen=True)
class BindingCandidate:
    binding: Key
    source: SourceOperationRef
    matching_mappings: tuple[OutputMapping, ...]
    compatibility_policy: str
    limitations: tuple[str, ...]
    silent_fallback: bool


@dataclass(frozen=True)
class BindingInventory:
    definition: DatasetDefinition
    candidates: tuple[BindingCandidate, ...]
    binding_activation: str = "unobservable"
    executable_binding: str = "unobservable"
    source_health: str = "unobservable"
    current_freshness: str = "unobservable"
    external_admission: str = "unobservable"
    finite_coverage: str = "unobservable"
    quality: str = "unobservable"
    durability: str = "unobservable"
    replayability: str = "unobservable"


def _text(value: object, *, nonempty: bool = False) -> None:
    require(type(value) is str and (not nonempty or bool(value)), "invalid_text")


def _key(value: Key) -> None:
    require(type(value) is Key, "invalid_key")
    _text(value.identity, nonempty=True)
    _text(value.revision)
    Key(value.kind, value.identity, value.revision)


def _scope(value: Scope) -> None:
    require(type(value) is Scope, "invalid_scope")
    Scope(value.principals)


def _shape(record: Record) -> None:
    # Canonical validators enforce relationships; these checks reject malformed
    # decoded field types before those validators or the inventory projection.
    _key(record.key)
    if isinstance(record, SourceOperationRef):
        optional = {"build_revision", "registry_revision", "provider_app_revision"}
        for field in fields(record):
            if field.name not in {"scope", "evidence_capability"}:
                value = getattr(record, field.name)
                if field.name not in optional or value is not None:
                    _text(value)
        _scope(record.scope)
        require(
            type(record.evidence_capability) is frozenset
            and all(type(g) is Grade for g in record.evidence_capability),
            "invalid_evidence_capability",
        )
    elif isinstance(record, DatasetDefinition):
        _scope(record.scope)
        require(type(record.source_qualified) is bool, "invalid_source_qualified")
        for field in fields(record):
            if field.name not in {"scope", "source_qualified"}:
                _text(getattr(record, field.name))
    elif isinstance(record, SourceBindingRevision):
        _key(record.source)
        _text(record.compatibility_policy)
        require(type(record.silent_fallback) is bool, "invalid_silent_fallback")
        require(type(record.limitations) is tuple, "invalid_limitations")
        for limitation in record.limitations:
            _text(limitation)
        require(type(record.mappings) is tuple, "invalid_mappings")
        for mapping in record.mappings:
            require(type(mapping) is OutputMapping, "invalid_mapping")
            _text(mapping.identity, nonempty=True)
            _text(mapping.semantic_revision, nonempty=True)
            _text(mapping.temporal_revision, nonempty=True)
            _key(mapping.definition)
            _scope(mapping.scope)
            require(type(mapping.evidence) is EvidenceRequirement, "invalid_evidence")
            EvidenceRequirement(
                mapping.evidence.allowed,
                mapping.evidence.durable,
                mapping.evidence.replayable,
            )
            require(
                type(mapping.evidence.durable) is bool
                and type(mapping.evidence.replayable) is bool,
                "invalid_evidence_claim",
            )
    else:
        require(False, "invalid_inventory_record")


def _inspect(reads: _BudgetedReads, definition_key: Key) -> BindingInventory:
    definition = _definition(reads, definition_key)
    accepted = {d.key: d for d in (BLS_CPI_HEADLINES, RAW_DAILY_BARS)}
    records: dict[Key, Record] = {}

    def resolve(key: Key) -> Record:
        _key(key)
        if key not in records:
            record = reads.get(key)
            require(record is not None and record.key == key, "record_key_mismatch")
            _shape(record)
            # Merely storing a lookalike/revision does not admit a Definition.
            if key in accepted:
                require(record == accepted[key], "accepted_definition_mismatch")
            records[key] = record
            validate_record(record, resolve)
        return records[key]

    def validate() -> BindingInventory:
        require(resolve(definition_key) == definition, "definition_mismatch")
        bindings = _classified(
            lambda: reads._read(reads.ledger.binding_candidates, MAX_BINDINGS),
            ReadFailure.STORAGE_UNAVAILABLE,
        )
        require(type(bindings) is tuple, "invalid_binding_batch")
        _bounded(len(bindings), MAX_BINDINGS)
        reads.budget.metadata(bindings)
        require(
            all(type(binding) is SourceBindingRevision for binding in bindings),
            "invalid_binding_batch",
        )
        require(len({b.key for b in bindings}) == len(bindings), "duplicate_binding")
        # Validate the COMPLETE global batch, including unrelated mappings. No
        # successful partial inventory can escape if any stored candidate is bad.
        for binding in bindings:
            require(resolve(binding.key) == binding, "binding_readback_mismatch")
        candidates = tuple(
            BindingCandidate(
                binding=binding.key,
                source=resolve(binding.source),
                matching_mappings=tuple(
                    sorted(
                        (m for m in binding.mappings if m.definition == definition_key),
                        key=lambda m: m.identity,
                    )
                ),
                compatibility_policy=binding.compatibility_policy,
                limitations=binding.limitations,
                silent_fallback=binding.silent_fallback,
            )
            for binding in sorted(bindings, key=lambda b: (b.identity, b.revision))
            if any(m.definition == definition_key for m in binding.mappings)
        )
        result = BindingInventory(definition, candidates)
        reads.budget.metadata(result)
        return result

    return _classified(validate, ReadFailure.LINEAGE_MISSING_OR_MISMATCHED)


def inspect_binding_candidates(
    ledger: OperatorReadSource, definition: Key
) -> BindingInventory:
    """Inventory stored revisions, independent of Version/Publication presence.

    Only exact accepted Definition mappings are projected. Contract/evidence
    requirements are preserved claims, never observed runtime capabilities.
    Missing producer capability fails closed; there is no legacy read fallback.
    """
    return _classified(
        lambda: _inspect(
            _BudgetedReads(ledger.operator_reads(), _WorkBudget()), definition
        ),
        ReadFailure.STORAGE_UNAVAILABLE,
    )
