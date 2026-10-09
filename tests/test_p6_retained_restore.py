"""Synthetic historical restore; no live PostgreSQL, storage, or provider I/O."""

import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from psycopg.pq import TransactionStatus

from market_data_foundation.canonical.codec import dumps, loads
from market_data_foundation.canonical.consumer_wire import handle_request
from market_data_foundation.canonical.model import (
    Assessment,
    ContractError,
    DatasetVersion,
    EvidenceMaterial,
    Grade,
    digest,
)
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.canonical.retained_restore import (
    RestoreBinding,
    RetainedExactGraph,
    restore_in_memory,
    validate_exact_graph,
)
from tests.test_p6_consumer_read import _seed_bound_cpi
from tests.test_p6_tdx_recent_daily import request, seed


def bound(graph):
    publication = graph.publication
    state = {"publications": {"count": 1, "sha256": "synthetic-table-digest"}}
    readback = json.dumps(
        {
            "reconnect_exact_records_content_publication": "PASS",
            "storage_unchanged": True,
            "before": state,
            "after": state,
            "publication": {
                "identity": publication.request.identity,
                "occurrence": publication.request.occurrence,
                "decision_at": publication.decision_at.isoformat(),
                "usable_at": publication.usable_at.isoformat(),
            },
        }
    ).encode()
    return (
        RestoreBinding(
            "synthetic-window",
            graph.sha256(),
            digest(dumps(publication).encode()),
            digest(readback),
        ),
        readback,
    )


def graph_from(ledger, version, publication):
    return RetainedExactGraph(
        tuple(
            loads(payload)
            for key, payload in json.loads(ledger.snapshot())["records"]
            if key[0] != "Publication"
        ),
        version.key,
        ledger.get_content(version.key),
        publication,
    )


@pytest.mark.parametrize("family", ["cpi", "tdx"])
def test_restore_original_wire_bytes_times_idempotency_and_read_failures(family):
    if family == "cpi":
        ledger, plan, publication = _seed_bound_cpi()
    else:
        ledger, _kernel, plan, publication = seed()
    graph = graph_from(ledger, plan.version, publication)
    binding, proof = bound(graph)
    target = InMemoryLedger()
    assert restore_in_memory(target, graph, binding, proof) == publication
    snapshot = target.snapshot()
    assert restore_in_memory(target, graph, binding, proof) == publication
    assert target.snapshot() == snapshot == ledger.snapshot()
    for mode in ("exact_version", "as_of"):
        for at in (
            publication.usable_at,
            publication.usable_at - timedelta(microseconds=1),
        ):
            payload = request(plan, mode, at.isoformat())
            assert handle_request(target, payload) == handle_request(ledger, payload)
    assert target.snapshot() == snapshot


@pytest.mark.parametrize(
    "failure",
    [
        "content",
        "missing",
        "extra",
        "duplicate",
        "lineage",
        "time",
        "usable",
        "proof_time",
        "proof_missing",
        "window_mix",
        "first_decision",
    ],
)
def test_fail_closed_before_mutation_even_with_newly_bound_bad_input(failure):
    ledger, _kernel, plan, publication = seed()
    graph = graph_from(ledger, plan.version, publication)
    if failure == "content":
        graph = replace(graph, content=b"corrupt")
    if failure == "missing":
        graph = replace(graph, records=graph.records[1:])
    if failure == "extra":
        graph = replace(
            graph, records=(*graph.records, replace(plan.run, identity="extra"))
        )
    if failure == "duplicate":
        graph = replace(graph, records=(*graph.records, graph.records[0]))
    if failure in {"lineage", "time"}:
        bad_version = replace(
            plan.version,
            mapping="wrong" if failure == "lineage" else plan.version.mapping,
            frozen_at=(
                publication.decision_at + timedelta(seconds=1)
                if failure == "time"
                else plan.version.frozen_at
            ),
        )
        graph = replace(
            graph,
            records=tuple(
                bad_version if isinstance(r, DatasetVersion) else r
                for r in graph.records
            ),
        )
    if failure == "usable":
        graph = replace(
            graph,
            publication=replace(
                publication, usable_at=publication.decision_at - timedelta(seconds=1)
            ),
        )
    if failure == "first_decision":
        from market_data_foundation.canonical.model import Decision

        graph = replace(
            graph,
            publication=replace(
                publication,
                request=replace(publication.request, decision=Decision.RESTORE),
            ),
        )
    binding, proof = bound(graph)
    if failure in {"proof_time", "proof_missing"}:
        data = json.loads(proof)
        if failure == "proof_missing":
            data.pop("publication")
        else:
            data["publication"]["decision_at"] = (
                publication.decision_at + timedelta(seconds=1)
            ).isoformat()
        proof = json.dumps(data).encode()
        binding = replace(binding, readback_sha256=digest(proof))
    if failure == "window_mix":
        binding = replace(binding, graph_sha256="0" * 64)
    target = InMemoryLedger()
    before = target.snapshot()
    with pytest.raises(ContractError):
        restore_in_memory(target, graph, binding, proof)
    assert target.snapshot() == before


@pytest.mark.parametrize("conflict", ["identity", "content", "occurrence", "history"])
def test_target_conflicts_do_not_modify_existing_graph(conflict):
    ledger, _kernel, plan, publication = seed()
    graph = graph_from(ledger, plan.version, publication)
    binding, proof = bound(graph)
    target = InMemoryLedger()
    if conflict == "identity":
        target.put(replace(plan.version, completeness="conflict"))
    if conflict == "content":
        target.put_content(
            replace(plan.version, content_digest=digest(b"other")), b"other"
        )
    if conflict in {"occurrence", "history"}:
        target.put(
            replace(
                publication,
                request=replace(
                    publication.request,
                    identity="other-publication",
                    occurrence=(
                        publication.request.occurrence
                        if conflict == "occurrence"
                        else "other"
                    ),
                ),
            )
        )
    before = target.snapshot()
    with pytest.raises(ContractError):
        restore_in_memory(target, graph, binding, proof)
    assert target.snapshot() == before


class FakeConnection:
    def __init__(self, ledger):
        self.ledger = ledger
        self.info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)
        self.statements = []
        self.commit_error = False
        self.insert_error = False
        self.index_corrupt = False

    @contextmanager
    def transaction(self):
        before = self.ledger.memory.snapshot()
        try:
            yield
        except Exception:
            self.ledger.memory = InMemoryLedger.from_snapshot(before)
            raise
        if self.commit_error:
            raise OSError("synthetic_commit_outcome_unknown")

    def execute(self, query, params=None):
        statement = query.as_string()
        self.statements.append(statement)
        if "SELECT identity, occurrence" in statement:
            rows = []
            for payload in self.ledger.memory._records.values():
                value = loads(payload)
                if not hasattr(value, "request"):
                    continue
                row = (
                    value.request.identity,
                    "corrupt-occurrence"
                    if self.index_corrupt
                    else value.request.occurrence,
                    value.request.version.identity,
                    value.decision_at,
                    payload,
                )
                if any(row[i] == params[i] for i in range(3)):
                    rows.append(row)
            return SimpleNamespace(fetchall=lambda: rows)
        if "INSERT INTO" in statement:
            if self.insert_error:
                raise OSError("synthetic_insert_failure")
            publication = loads(params[-1])
            assert params[:4] == (
                publication.request.identity,
                publication.request.occurrence,
                publication.request.version.identity,
                publication.decision_at,
            )
            self.ledger.memory.put(publication)


class FakePostgresLedger(PostgresLedger):
    def __init__(self):
        self.memory = InMemoryLedger()
        super().__init__(
            FakeConnection(self), "mdf_p3_canonical_kernel_r1_restore_test"
        )

    def __getattribute__(self, name):
        if name in {
            "get",
            "put",
            "put_content",
            "get_content",
            "publication_by_occurrence",
            "publications_for_version",
        }:
            return getattr(object.__getattribute__(self, "memory"), name)
        return super().__getattribute__(name)


def test_postgres_candidate_commit_atomicity_original_time_and_reconciliation():
    ledger, _kernel, plan, publication = seed()
    graph = graph_from(ledger, plan.version, publication)
    binding, proof = bound(graph)
    target = FakePostgresLedger()
    target.connection.insert_error = True
    before = target.memory.snapshot()
    with pytest.raises(OSError, match="synthetic_insert_failure"):
        target.restore_exact_graph(graph, binding, proof)
    assert target.memory.snapshot() == before
    target.connection.insert_error = False
    target.connection.commit_error = True
    with pytest.raises(OSError, match="synthetic_commit_outcome_unknown"):
        target.restore_exact_graph(graph, binding, proof)
    assert target.memory.get(publication.key) == publication
    target.connection.commit_error = False
    assert target.restore_exact_graph(graph, binding, proof) == publication
    assert target.memory.snapshot() == ledger.snapshot()
    assert "LOCK TABLE" in target.connection.statements[0]
    assert all("clock_timestamp" not in sql for sql in target.connection.statements)
    target.connection.info.transaction_status = TransactionStatus.INTRANS
    with pytest.raises(ContractError, match="restore_connection_not_idle"):
        target.restore_exact_graph(graph, binding, proof)


def test_invalid_proof_never_reaches_postgres_connection():
    ledger, _kernel, plan, publication = seed()
    graph = graph_from(ledger, plan.version, publication)
    binding, proof = bound(graph)
    target = FakePostgresLedger()
    with pytest.raises(ContractError, match="restore_readback_mismatch"):
        target.restore_exact_graph(graph, binding, proof + b"corrupt")
    assert target.connection.statements == []
    assert validate_exact_graph(graph, binding, proof).snapshot() == ledger.snapshot()


def test_postgres_conflict_rolls_back_and_preserves_unrelated_records():
    ledger, _kernel, plan, publication = seed()
    graph = graph_from(ledger, plan.version, publication)
    binding, proof = bound(graph)
    target = FakePostgresLedger()
    unrelated = replace(plan.run, identity="unrelated")
    target.memory.put(unrelated)
    assert target.restore_exact_graph(graph, binding, proof) == publication
    assert target.memory.get(unrelated.key) == unrelated
    conflicting = replace(
        graph,
        publication=replace(
            publication,
            usable_at=publication.usable_at + timedelta(seconds=1),
            request=replace(
                publication.request,
                usable_at=publication.usable_at + timedelta(seconds=1),
            ),
        ),
    )
    other_binding, other_proof = bound(conflicting)
    before = target.memory.snapshot()
    with pytest.raises(ContractError, match="restore_publication_index_conflict"):
        target.restore_exact_graph(conflicting, other_binding, other_proof)
    assert target.memory.snapshot() == before
    target.connection.index_corrupt = True
    with pytest.raises(ContractError, match="restore_publication_index_conflict"):
        target.restore_exact_graph(graph, binding, proof)
    assert target.memory.snapshot() == before


def assert_off_target_rejection(graph, unrelated, classification):
    # Intentionally approve the mutated synthetic bytes: digest mismatch must
    # not conceal a missing type or causality gate. Never use this as provenance.
    binding, proof = bound(graph)
    target = InMemoryLedger()
    target.put(unrelated)
    before = target.snapshot()
    with pytest.raises(ContractError, match=f"^{classification}$"):
        restore_in_memory(target, graph, binding, proof)
    assert target.snapshot() == before
    postgres = FakePostgresLedger()
    postgres.memory.put(unrelated)
    before = postgres.memory.snapshot()
    with pytest.raises(ContractError, match=f"^{classification}$"):
        postgres.restore_exact_graph(graph, binding, proof)
    assert postgres.memory.snapshot() == before
    assert postgres.connection.statements == []


@pytest.mark.parametrize(
    "failure",
    [
        "grade_string",
        "durable_string",
        "replayable_string",
        "durable_int",
        "run_kind_string",
        "run_inputs_frozenset",
        "run_budget_bool",
        "run_time_string",
        "acquisition_attempts_bool",
        "acquisition_parameter_int",
        "definition_qualified_int",
        "binding_fallback_int",
        "requirement_durable_string",
        "finite_member_int",
        "material_identity_int",
        "publication_decision_string",
        "publication_replayable_string",
        "publication_grades_string",
    ],
)
def test_canonical_types_rejected_with_recomputed_synthetic_binding(failure):
    ledger, _kernel, plan, publication = seed()
    graph = graph_from(ledger, plan.version, publication)
    changes = {}
    if failure == "grade_string":
        changes[plan.material.key] = replace(
            plan.material, grade=plan.material.grade.value
        )
    if failure == "durable_string":
        changes[plan.material.key] = replace(plan.material, durable="false")
    if failure == "replayable_string":
        changes[plan.material.key] = replace(plan.material, replayable="false")
    if failure == "durable_int":
        changes[plan.material.key] = replace(plan.material, durable=0)
    if failure.startswith("run_"):
        fields = {
            "run_kind_string": {"kind": plan.run.kind.value},
            "run_inputs_frozenset": {"inputs": frozenset()},
            "run_budget_bool": {"budget": True},
            "run_time_string": {"started_at": plan.run.started_at.isoformat()},
        }
        changes[plan.run.key] = replace(plan.run, **fields[failure])
    if failure == "acquisition_attempts_bool":
        changes[plan.acquisition.key] = replace(
            plan.acquisition, provider_data_attempts=True
        )
    if failure == "acquisition_parameter_int":
        changes[plan.acquisition.key] = replace(
            plan.acquisition, parameters=(("count", 30),)
        )
    if failure == "definition_qualified_int":
        definition = ledger.get(plan.version.definition)
        changes[definition.key] = replace(definition, source_qualified=1)
    if failure in {"binding_fallback_int", "requirement_durable_string"}:
        binding = ledger.get(plan.run.binding)
        changes[binding.key] = (
            replace(binding, silent_fallback=0)
            if failure == "binding_fallback_int"
            else replace(
                binding,
                mappings=(
                    replace(
                        binding.mappings[0],
                        evidence=replace(binding.mappings[0].evidence, durable="false"),
                    ),
                ),
            )
        )
    if failure == "finite_member_int":
        changes[plan.version.key] = replace(
            plan.version,
            finite_scope=replace(plan.version.finite_scope, members=(1,)),
        )
    if failure == "material_identity_int":
        changes[plan.material.key] = replace(plan.material, identity=1)
    if failure.startswith("publication_"):
        fields = {
            "publication_decision_string": {
                "decision": publication.request.decision.value
            },
            "publication_replayable_string": {"claims_replayable": "false"},
            "publication_grades_string": {
                "claimed_grades": frozenset(
                    g.value for g in publication.request.claimed_grades
                )
            },
        }
        graph = replace(
            graph,
            publication=replace(
                publication, request=replace(publication.request, **fields[failure])
            ),
        )
    graph = replace(graph, records=tuple(changes.get(r.key, r) for r in graph.records))
    assert_off_target_rejection(
        graph, replace(plan.run, identity="unrelated"), "restore_canonical_type_invalid"
    )


@pytest.mark.parametrize(
    "failure", ["own_material", "own_acquired_material", "two_runs", "own_version"]
)
def test_input_producer_cycles_rejected_with_recomputed_synthetic_binding(failure):
    ledger, _kernel, plan, publication = seed()
    graph = graph_from(ledger, plan.version, publication)
    material = plan.material
    additions = ()
    input_key = material.key
    if failure == "own_acquired_material":
        material = replace(material, produced_by=None)
    if failure == "two_runs":
        other_run = replace(plan.run, identity="other-run", inputs=(material.key,))
        other_material = replace(
            material,
            identity="other-material",
            acquired_from=None,
            produced_by=other_run.key,
        )
        additions = (other_run, other_material)
        input_key = other_material.key
    if failure == "own_version":
        input_key = plan.version.key
    changes = {
        plan.run.key: replace(plan.run, inputs=(input_key,)),
        plan.material.key: material,
    }
    graph = replace(
        graph,
        records=tuple(changes.get(r.key, r) for r in graph.records) + additions,
    )
    assert_off_target_rejection(
        graph, replace(plan.run, identity="unrelated"), "restore_causal_cycle"
    )


def test_legal_assessment_backref_and_upstream_material_input_are_preserved():
    ledger, _kernel, plan, publication = seed()
    graph = graph_from(ledger, plan.version, publication)
    upstream_run = replace(
        plan.run,
        identity="upstream-run",
        started_at=plan.run.started_at - timedelta(seconds=2),
        finished_at=plan.run.started_at - timedelta(seconds=1),
    )
    upstream_material = replace(
        plan.material,
        identity="upstream-material",
        acquired_from=None,
        produced_by=upstream_run.key,
    )
    assessment = EvidenceMaterial(
        "assessment",
        Grade.ASSESSMENT,
        None,
        plan.version.scope,
        produced_by=plan.run.key,
        assessment=Assessment(
            plan.version.key,
            "synthetic-rule-r1",
            publication.decision_at,
            "pass",
            (plan.material.key,),
        ),
    )
    changes = {
        plan.run.key: replace(plan.run, inputs=(upstream_material.key,)),
        plan.version.key: replace(plan.version, quality_evidence=(assessment.key,)),
    }
    graph = replace(
        graph,
        records=tuple(changes.get(r.key, r) for r in graph.records)
        + (upstream_run, upstream_material, assessment),
        publication=replace(
            publication,
            request=replace(
                publication.request,
                evidence=(*publication.request.evidence, assessment.key),
            ),
        ),
    )
    binding, proof = bound(graph)
    target = InMemoryLedger()
    postgres = FakePostgresLedger()
    for _ in range(2):
        assert restore_in_memory(target, graph, binding, proof) == graph.publication
        assert postgres.restore_exact_graph(graph, binding, proof) == graph.publication
    assert target.snapshot() == postgres.memory.snapshot()
    assert target.get(assessment.key) == assessment
    assert target.get(plan.run.key).inputs == (upstream_material.key,)
