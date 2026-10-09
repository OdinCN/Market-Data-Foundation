"""Internal single-version restore; trusted control bindings are not self-attestation.

The caller independently approves the digests from one retained proof window.
No provider, locator, clock, or file I/O occurs here. This imports a committed
decision; it neither issues a decision nor establishes current durability.
"""

import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from types import UnionType
from typing import get_args, get_origin

from .codec import dumps
from .model import (
    AcquisitionOccurrence,
    ContractError,
    DataRun,
    DatasetVersion,
    EvidenceMaterial,
    Key,
    Publication,
    Record,
    digest,
    instant,
    require,
)
from .repository import InMemoryLedger
from .rules import build_publication, validate_record


@dataclass(frozen=True)
class RetainedExactGraph:
    records: tuple[Record, ...]
    version: Key
    content: bytes
    publication: Publication

    def sha256(self) -> str:
        """Bind all canonical fields, content bytes, and original decision times."""
        return digest(
            dumps(
                (
                    tuple(sorted(self.records, key=lambda item: dumps(item.key))),
                    self.version,
                    self.content,
                    self.publication,
                )
            ).encode()
        )


@dataclass(frozen=True)
class RestoreBinding:
    """Trusted caller allowlist, independently derived before import.

    Hash equality cannot authenticate an arbitrary caller-created history.
    Control must verify the source indexes/provenance and approve all three
    digests together within this window before calling the internal API.
    """

    proof_window: str
    graph_sha256: str
    publication_sha256: str
    readback_sha256: str


def _canonical_type_matches(value: object, expected: object) -> bool:
    """Check declared canonical types before hashing, references or semantics.

    Exact leaf types distinguish StrEnum from str and bool from int. Recurse
    through the model's frozen containers and embedded contracts as well.
    """
    origin, arguments = get_origin(expected), get_args(expected)
    if origin is UnionType:
        return any(_canonical_type_matches(value, item) for item in arguments)
    if origin in {tuple, frozenset}:
        if type(value) is not origin:
            return False
        if origin is frozenset or arguments[-1] is Ellipsis:
            return all(_canonical_type_matches(item, arguments[0]) for item in value)
        return len(value) == len(arguments) and all(
            _canonical_type_matches(item, item_type)
            for item, item_type in zip(value, arguments, strict=True)
        )
    if type(value) is not expected:
        return False
    return not is_dataclass(value) or all(
        _canonical_type_matches(getattr(value, field.name), field.type)
        for field in fields(value)
    )


def _validate_causality(records: dict[Key, Record]) -> None:
    """Only input/producer dependencies must be acyclic, not all references.

    Assessment targets/bases and Version evidence links are canonical
    backrefs, not prerequisites for a Run producing its output.
    """
    dependencies = {}
    for key, record in records.items():
        parents = ()
        if isinstance(record, DataRun):
            parents = record.inputs
        elif isinstance(record, EvidenceMaterial):
            parents = (record.produced_by, record.acquired_from)
        elif isinstance(record, AcquisitionOccurrence):
            parents = (record.run,)
        elif isinstance(record, DatasetVersion):
            parents = (record.run, *record.inputs)
        dependencies[key] = tuple(parent for parent in parents if parent is not None)
    visiting, visited = set(), set()

    def visit(key: Key) -> None:
        require(key not in visiting, "restore_causal_cycle")
        if key in visited:
            return
        visiting.add(key)
        for parent in dependencies[key]:
            visit(parent)
        visiting.remove(key)
        visited.add(key)

    for key in records:
        visit(key)


def _references(value: object) -> set[Key]:
    if isinstance(value, Key):
        return {value}
    if is_dataclass(value):
        return set().union(
            *(_references(getattr(value, field.name)) for field in fields(value))
        )
    if isinstance(value, (tuple, frozenset)):
        return set().union(*(_references(item) for item in value))
    return set()


def _committed_readback(
    publication: Publication, readback: bytes, binding: RestoreBinding
) -> None:
    require(type(readback) is bytes, "restore_readback_invalid")
    require(digest(readback) == binding.readback_sha256, "restore_readback_mismatch")
    try:
        data = json.loads(readback)
        require(
            data.get("reconnect_exact_records_content_publication") == "PASS"
            or data.get("independent_reconnect_exact_records_content_publication")
            == "PASS",
            "restore_commit_unproven",
        )
        require(
            data["storage_unchanged"] is True
            and data["before"] == data["after"]
            and data["before"]["publications"]["count"] > 0,
            "restore_commit_unproven",
        )
        candidates = (
            [data["publication"]]
            if "publication" in data
            else list(data["publications"].values())
        )
        matches = [
            item
            for item in candidates
            if item["identity"] == publication.request.identity
        ]
        require(len(matches) == 1, "restore_publication_unproven")
        item = matches[0]
        require(
            item["occurrence"] == publication.request.occurrence
            and datetime.fromisoformat(item["decision_at"]) == publication.decision_at
            and datetime.fromisoformat(item["usable_at"]) == publication.usable_at,
            "restore_publication_proof_conflict",
        )
        for value in (
            data.get("observed_at_utc"),
            data.get("cutoff", {}).get("as_of"),
        ):
            if value is not None:
                observed = datetime.fromisoformat(value)
                instant(observed)
                require(
                    observed >= publication.usable_at, "restore_temporal_contradiction"
                )
    except ContractError:
        raise
    except ValueError, TypeError, KeyError, AttributeError:
        raise ContractError("restore_readback_invalid") from None


def validate_exact_graph(
    graph: RetainedExactGraph, binding: RestoreBinding, readback: bytes
) -> InMemoryLedger:
    """Validate entirely off-target; reject incomplete or mixed proof windows."""
    require(
        _canonical_type_matches(graph, RetainedExactGraph)
        and _canonical_type_matches(binding, RestoreBinding),
        "restore_canonical_type_invalid",
    )
    require(bool(binding.proof_window), "restore_window_missing")
    require(type(graph.content) is bytes, "mutable_content")
    require(type(graph.records) is tuple, "restore_records_not_frozen")
    require(
        1 <= len(graph.records) <= 16 and len(graph.content) <= 1_048_576,
        "restore_graph_bound_exceeded",
    )
    require(graph.sha256() == binding.graph_sha256, "restore_graph_mismatch")
    require(
        digest(dumps(graph.publication).encode()) == binding.publication_sha256,
        "restore_publication_mismatch",
    )
    _committed_readback(graph.publication, readback, binding)
    records = {record.key: record for record in graph.records}
    require(len(records) == len(graph.records), "restore_duplicate_identity")
    require(
        not any(isinstance(record, Publication) for record in graph.records),
        "restore_publication_in_records",
    )
    versions = [r for r in graph.records if isinstance(r, DatasetVersion)]
    require(
        len(versions) == 1 and versions[0].key == graph.version,
        "restore_single_version_required",
    )
    version = versions[0]
    require(
        not version.inputs and version.supersedes is None,
        "restore_single_version_required",
    )
    require(
        graph.publication.request.version == version.key, "restore_version_conflict"
    )
    require(digest(graph.content) == version.content_digest, "content_digest_mismatch")
    reachable = {version.key}
    pending = _references(graph.publication) | {version.key}
    while pending:
        key = pending.pop()
        require(key in records, "restore_lineage_missing")
        reachable.add(key)
        pending |= _references(records[key]) - reachable
    require(reachable == records.keys(), "restore_unrelated_record")
    _validate_causality(records)
    for record in graph.records:
        validate_record(record, records.get)
    publication = graph.publication
    validate_record(publication, records.get)
    require(
        build_publication(
            publication.request,
            resolve=records.get,
            history=(),
            existing_occurrence=None,
            decision_at=publication.decision_at,
        )
        == publication,
        "restore_publication_state_conflict",
    )
    require(
        version.frozen_at <= publication.decision_at, "restore_temporal_contradiction"
    )
    for record in graph.records:
        if isinstance(record, DataRun):
            require(
                record.finished_at is not None
                and record.started_at <= record.finished_at <= version.frozen_at,
                "restore_temporal_contradiction",
            )
        if isinstance(record, AcquisitionOccurrence):
            run = records[record.run]
            require(
                record.obtained_at is not None
                and run.started_at
                <= record.requested_at
                <= record.obtained_at
                <= run.finished_at,
                "restore_temporal_contradiction",
            )
            require(
                record.source_observed_at is None
                or record.source_observed_at <= publication.decision_at,
                "restore_temporal_contradiction",
            )
        if isinstance(record, EvidenceMaterial) and record.assessment is not None:
            require(
                record.assessment.evaluated_at <= publication.decision_at,
                "restore_temporal_contradiction",
            )
    candidate = InMemoryLedger()
    for record in graph.records:
        candidate.put(record)
    candidate.put_content(version, graph.content)
    candidate.put(publication)
    return candidate


def check_restore_target(target, graph: RetainedExactGraph) -> None:
    """Reject identity/content/occurrence/history conflicts before any mutation."""
    for record in (*graph.records, graph.publication):
        existing = target.get(record.key)
        require(existing is None or existing == record, "canonical_identity_conflict")
    content = target.get_content(graph.version)
    require(content is None or content == graph.content, "version_content_conflict")
    occurrence = target.publication_by_occurrence(graph.publication.request.occurrence)
    require(
        occurrence is None or occurrence == graph.publication,
        "occurrence_identity_conflict",
    )
    history = target.publications_for_version(graph.version)
    require(
        history in ((), (graph.publication,)), "restore_publication_history_conflict"
    )


def restore_in_memory(
    target: InMemoryLedger,
    graph: RetainedExactGraph,
    binding: RestoreBinding,
    readback: bytes,
) -> Publication:
    """Deterministic atomic reference candidate, independent of publish()."""
    candidate = validate_exact_graph(graph, binding, readback)
    check_restore_target(target, graph)
    staged = InMemoryLedger.from_snapshot(target.snapshot())
    for record in graph.records:
        staged.put(record)
    staged.put_content(candidate.get(graph.version), graph.content)
    staged.put(graph.publication)
    target._records = staged._records
    target._contents = staged._contents
    target._publication_occurrences = staged._publication_occurrences
    return graph.publication
