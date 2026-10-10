"""Offline association proofs; no real PostgreSQL, provider or content reads."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from market_data_foundation.canonical import operator_asof as application
from market_data_foundation.canonical import operator_reads as producer
from market_data_foundation.canonical import operator_status as status
from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    ReadFailure,
    read_exact_version,
)
from market_data_foundation.canonical.consumer_wire import handle_request
from market_data_foundation.canonical.model import Decision, Key, Kind
from market_data_foundation.canonical.operator_asof import (
    OperatorQueryBudgetError,
    inspect_as_of,
)
from market_data_foundation.canonical.operator_bindings import (
    inspect_binding_candidates,
)
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.domain_contracts.dense_market import RAW_DAILY_BARS
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from tests.test_p6_consumer_read import BASE, _seed_bound_cpi, _seed_daily
from tests.test_p6_consumer_wire import _as_of_request
from tests.test_p6_operator_asof import AT, _store
from tests.test_p6_operator_producer_admission import _source as _producer_source
from tests.test_p6_operator_snapshot import SnapshotDouble
from tests.test_p6_operator_snapshot import _source as _snapshot_source


@pytest.fixture(params=["memory", "sql_double"])
def backend(request):
    return request.param


def _source(ledger, backend):
    source = _producer_source(ledger, backend)
    if backend == "memory":
        return source
    # Explicit static synthetic read scope, using the admitted SQL producer.
    # This double has no concurrent writer/real transaction. Concrete snapshot
    # entry/exit behavior is separately exercised below with SnapshotDouble.
    return SimpleNamespace(operator_reads=source.operator_reads)


def _view(scope, definition=BLS_CPI_HEADLINES.key, as_of=AT):
    return status.inspect_operator_status(scope, definition, as_of=as_of)


def _failure(call, expected):
    with pytest.raises(ReadContractError) as caught:
        call()
    assert caught.value.classification == expected
    assert caught.value.args == (expected.value,)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    return caught.value


@pytest.mark.parametrize("definition", [BLS_CPI_HEADLINES, RAW_DAILY_BARS])
def test_empty_definition_and_stored_only_binding_are_distinct(backend, definition):
    ledger = InMemoryLedger()
    ledger.put(definition)
    empty = _view(_source(ledger, backend), definition.key)
    assert empty.definition == definition
    assert empty.as_of.selection_outcome == "as_of_no_version"
    assert empty.as_of.candidates == empty.stored_bindings == ()
    assert empty.versions_without_binding_record == ()

    _, plan, _ = _seed_bound_cpi()
    ledger.put(plan.source)
    binding = replace(
        plan.binding_candidate,
        mappings=(
            replace(plan.binding_candidate.mappings[0], definition=definition.key),
        ),
    )
    ledger.put(binding)
    before = ledger.snapshot()
    view = _view(_source(ledger, backend), definition.key)
    assert view.as_of == empty.as_of
    stored = view.stored_bindings[0]
    assert stored.candidate.binding == binding.key
    assert stored.record_state == "stored"
    assert stored.referencing_versions == stored.publication_candidates == ()
    assert ledger.snapshot() == before


def test_exact_revision_join_reverse_references_and_stable_order(backend):
    ledger, plan, publication = _seed_bound_cpi()
    other_binding = replace(plan.binding_candidate, revision="synthetic-r2")
    unused_binding = replace(plan.binding_candidate, identity="a-unused")
    ledger.put(other_binding)
    ledger.put(unused_binding)
    unpublished = replace(plan.version, identity="a-unpublished")
    other_run = replace(plan.run, identity="z-run", binding=other_binding.key)
    other_acquisition = replace(
        plan.acquisition,
        identity="z-acquisition",
        run=other_run.key,
        binding=other_binding.key,
    )
    other_materials = tuple(
        replace(material, identity=f"z-{name}", acquired_from=other_acquisition.key)
        for name, material in (
            ("raw", plan.raw_material),
            ("semantic", plan.semantic_material),
        )
    )
    other_version = replace(
        plan.version,
        identity="z-other-revision",
        binding=other_binding.key,
        run=other_run.key,
        acquisitions=(other_acquisition.key,),
        materials=tuple(material.key for material in other_materials),
    )
    for record in (other_run, other_acquisition, *other_materials):
        ledger.put(record)
    _store(ledger, unpublished)
    _store(ledger, other_version)
    _store(
        ledger,
        replace(
            publication,
            request=replace(
                publication.request,
                identity="z-publication",
                occurrence="z-occurrence",
                version=other_version.key,
                evidence=other_version.materials,
            ),
        ),
    )
    before = ledger.snapshot()
    view = _view(_source(ledger, backend))
    assert view.as_of.selection_outcome == "as_of_ambiguous"
    assert tuple(s.candidate.binding for s in view.stored_bindings) == (
        unused_binding.key,
        plan.binding_candidate.key,
        other_binding.key,
    )
    assert view.stored_bindings[0].referencing_versions == ()
    assert view.stored_bindings[1].referencing_versions == (
        unpublished.key,
        plan.version.key,
    )
    assert view.stored_bindings[1].publication_candidates == (plan.version.key,)
    assert view.stored_bindings[2].referencing_versions == (other_version.key,)
    assert view.stored_bindings[2].publication_candidates == (other_version.key,)
    assert view.versions_without_binding_record == ()
    assert view.as_of == inspect_as_of(ledger, BLS_CPI_HEADLINES.key, as_of=AT)
    assert tuple(s.candidate for s in view.stored_bindings) == (
        inspect_binding_candidates(ledger, BLS_CPI_HEADLINES.key).candidates
    )
    restored = InMemoryLedger.from_snapshot(before)
    restored._records = dict(reversed(tuple(restored._records.items())))
    assert _view(_source(restored, backend)) == view
    assert ledger.snapshot() == before
    with pytest.raises(FrozenInstanceError):
        view.current_availability = "available"


@pytest.mark.parametrize(
    "seconds,pending",
    [
        (4, ("usable_at", "effective_at")),
        (5, ("effective_at",)),
        (6, ()),
    ],
)
def test_bound_candidate_clock_boundaries_preserve_full_diagnostic(
    backend, seconds, pending
):
    ledger, plan, publication = _seed_bound_cpi()
    _store(
        ledger,
        replace(
            publication,
            usable_at=BASE + timedelta(seconds=5),
            request=replace(
                publication.request, effective_at=BASE + timedelta(seconds=6)
            ),
        ),
    )
    at = BASE + timedelta(seconds=seconds)
    view = _view(_source(ledger, backend), as_of=at)
    assert view.as_of == inspect_as_of(ledger, BLS_CPI_HEADLINES.key, as_of=at)
    assert view.as_of.candidates[0].publications[0].pending_clocks == pending
    assert view.stored_bindings[0].referencing_versions == (plan.version.key,)
    assert view.stored_bindings[0].publication_candidates == (
        () if pending else (plan.version.key,)
    )


@pytest.mark.parametrize(
    "decision", [Decision.WITHDRAW, Decision.RESTORE, Decision.SUPERSEDE]
)
def test_terminal_and_restored_publications_only_change_candidate_association(
    backend, decision
):
    ledger, plan, publication = _seed_bound_cpi()
    previous = publication
    # RESTORE must follow a withdrawal; preserve the canonical history gates.
    decisions = (
        (Decision.WITHDRAW, decision) if decision == Decision.RESTORE else (decision,)
    )
    for number, step in enumerate(decisions):
        at = BASE + timedelta(seconds=7 + number)
        previous = replace(
            previous,
            request=replace(
                plan.publication_request,
                identity=f"transition:{number}",
                occurrence=f"transition:{number}",
                decision=step,
                predecessor=previous.key,
                supersedes_version=plan.version.key
                if step == Decision.SUPERSEDE
                else None,
            ),
            decision_at=at,
            usable_at=at,
        )
        _store(ledger, previous)
    view = _view(_source(ledger, backend))
    assert view.stored_bindings[0].referencing_versions == (plan.version.key,)
    assert view.stored_bindings[0].publication_candidates == (
        (plan.version.key,) if decision == Decision.RESTORE else ()
    )
    assert view.as_of.candidates[0].effective_publication == previous.key


@pytest.mark.parametrize("bound", [False, True])
def test_unknown_runtime_states_redaction_and_no_content_or_public_wire_change(
    backend, bound
):
    ledger, *rest = _seed_bound_cpi() if bound else _seed_daily()
    plan = rest[0] if bound else rest[1]
    definition = BLS_CPI_HEADLINES if bound else RAW_DAILY_BARS
    request = _as_of_request(definition.key, AT.isoformat())
    before_wire = handle_request(ledger, request)
    ledger._contents.clear()
    before = ledger.snapshot()
    source = _source(ledger, backend)

    def forbidden(*_args):
        pytest.fail("unexpected content, mutation or legacy read")

    for name in (
        "get",
        "get_content",
        "put_content",
        "put",
        "publish",
        "versions_for_definition",
        "publications_for_version",
    ):
        setattr(source, name, forbidden)
    view = _view(source, definition.key)
    assert view.as_of.selection_outcome == "one_publication_candidate"
    assert view.versions_without_binding_record == (
        () if bound else (plan.version.key,)
    )
    for name in (
        "binding_activation",
        "executable_binding",
        "source_health",
        "current_freshness",
        "external_admission",
        "finite_coverage",
        "quality",
        "content_integrity",
        "durability",
        "replayability",
        "current_availability",
    ):
        assert getattr(view, name) == "unobservable"
    for hidden in (
        "controlled-receipt",
        "mdf-content://",
        "controlled_ref",
        "raw_bytes",
    ):
        assert hidden not in repr(view)
    assert not hasattr(view, "selected_version")
    assert ledger.snapshot() == before
    # Reconstruct a plain ledger after installing forbidden legacy methods.
    restored = InMemoryLedger.from_snapshot(before)
    _failure(
        lambda: read_exact_version(
            restored, definition.key, plan.version.key, as_of=AT
        ),
        ReadFailure.CONTENT_MISSING,
    )
    restored = InMemoryLedger.from_snapshot(
        (_seed_bound_cpi() if bound else _seed_daily())[0].snapshot()
    )
    assert handle_request(restored, request) == before_wire
    invalid_mode = request.replace(b'"mode":"as_of"', b'"mode":"operator_status"')
    assert b'"classification":"invalid_request"' in handle_request(
        restored, invalid_mode
    )


@pytest.mark.parametrize("at", [None, datetime(2026, 10, 10), "private-invalid"])
def test_invalid_asof_precedes_any_producer_access(at):
    def forbidden():
        pytest.fail("invalid instant must be classified before reads")

    _failure(
        lambda: _view(SimpleNamespace(operator_reads=forbidden), as_of=at),
        ReadFailure.INVALID_AS_OF,
    )


@pytest.mark.parametrize("case", ["unadmitted", "revision", "missing", "altered"])
def test_definition_admission_is_unchanged(backend, case):
    ledger, _, _ = _seed_bound_cpi()
    key = BLS_CPI_HEADLINES.key
    expected = ReadFailure.DEFINITION_NOT_ADMITTED
    if case == "unadmitted":
        key = Key(Kind.DEFINITION, "synthetic.unadmitted", "p3-r1")
    elif case == "revision":
        key = replace(key, revision="p3-r2")
    elif case == "missing":
        ledger._records.pop((key.kind.value, key.identity, key.revision))
        expected = ReadFailure.DEFINITION_NOT_FOUND
    else:
        _store(ledger, replace(BLS_CPI_HEADLINES, temporal_semantics="private altered"))
        expected = ReadFailure.LINEAGE_MISSING_OR_MISMATCHED
    _failure(lambda: _view(_source(ledger, backend), key), expected)


@pytest.mark.parametrize(
    "case", ["omitted", "wrong_revision", "binding_changed", "source_changed"]
)
def test_join_rejects_incomplete_inventory_and_cross_inspection_readback_conflicts(
    case,
):
    ledger, plan, _ = _seed_bound_cpi()
    reads = ledger.operator_reads()
    original = reads.binding_candidates

    def candidates(maximum):
        if case == "omitted":
            return ()
        if case == "wrong_revision":
            other = replace(plan.binding_candidate, revision="synthetic-r2")
            _store(ledger, other)
            return (other,)
        if case == "binding_changed":
            _store(
                ledger,
                replace(plan.binding_candidate, limitations=("private changed",)),
            )
        else:
            _store(ledger, replace(plan.source, freshness_contract="private changed"))
        return original(maximum)

    reads.binding_candidates = candidates
    scope = SimpleNamespace(operator_reads=lambda: reads)
    _failure(lambda: _view(scope), ReadFailure.LINEAGE_MISSING_OR_MISMATCHED)
    # Each separate inspection succeeds under the incomplete-enumeration fault;
    # the association gate adds a separately falsifiable invariant.
    if case in {"omitted", "wrong_revision"}:
        assert inspect_as_of(ledger, BLS_CPI_HEADLINES.key, as_of=AT).candidates
        assert len(
            inspect_binding_candidates(scope, BLS_CPI_HEADLINES.key).candidates
        ) == (0 if case == "omitted" else 1)


@pytest.mark.parametrize("case", ["unrelated_corruption", "publication", "lineage"])
def test_late_or_ineligible_corruption_never_returns_partial_status(backend, case):
    ledger, plan, publication = _seed_bound_cpi()
    if case == "unrelated_corruption":
        ledger.put(RAW_DAILY_BARS)
        unrelated = replace(
            plan.binding_candidate,
            identity="z-unrelated",
            mappings=(
                replace(
                    plan.binding_candidate.mappings[0], definition=RAW_DAILY_BARS.key
                ),
            ),
            silent_fallback=True,
        )
        _store(ledger, unrelated)
    elif case == "publication":
        _store(ledger, replace(publication, usable_at=BASE))
    else:
        _store(ledger, replace(plan.raw_material, digest="private invalid"))
    before = ledger.snapshot()
    _failure(
        lambda: _view(_source(ledger, backend), as_of=BASE),
        ReadFailure.PUBLICATION_STATE_INVALID
        if case == "publication"
        else ReadFailure.LINEAGE_MISSING_OR_MISMATCHED,
    )
    assert ledger.snapshot() == before


def test_missing_capability_and_storage_failure_do_not_use_legacy_fallback():
    def broken(*_args):
        raise RuntimeError("private transport detail")

    _failure(
        lambda: _view(SimpleNamespace(get=broken)), ReadFailure.STORAGE_UNAVAILABLE
    )
    ledger, _, _ = _seed_bound_cpi()
    reads = ledger.operator_reads()
    reads.binding_candidates = broken
    _failure(
        lambda: _view(SimpleNamespace(operator_reads=lambda: reads)),
        ReadFailure.STORAGE_UNAVAILABLE,
    )


@pytest.mark.parametrize("backend", ["memory", "sql_double"])
def test_one_producer_session_shares_cumulative_bytes_and_resets_per_status(
    backend, monkeypatch
):
    ledger, _, _ = _seed_bound_cpi()
    source = _source(ledger, backend)
    create = source.operator_reads
    sessions = []

    def observe():
        session = create()
        sessions.append(session)
        return session

    source.operator_reads = observe
    expected = _view(source)
    assert len(sessions) == 1
    used = sessions[-1].admission.bytes_read
    monkeypatch.setattr(producer, "MAX_TOTAL_BYTES", used)
    assert _view(source) == expected
    assert len(sessions) == 2 and sessions[0] is not sessions[1]
    monkeypatch.setattr(producer, "MAX_TOTAL_BYTES", used - 1)
    # Standalone calls still fit; a combined call has a single inclusive budget.
    assert inspect_as_of(source, BLS_CPI_HEADLINES.key, as_of=AT)
    assert inspect_binding_candidates(source, BLS_CPI_HEADLINES.key)
    with pytest.raises(OperatorQueryBudgetError) as caught:
        _view(source)
    assert caught.value.args == ("operator_query_budget_exceeded",)
    assert sessions[-1].admission.bytes_read <= used - 1


@pytest.mark.parametrize(
    "counter,constant",
    [
        ("resolutions", "_MAX_RESOLUTIONS"),
        ("metadata_nodes", "_MAX_METADATA_NODES"),
        ("text_characters", "_MAX_TEXT_CHARACTERS"),
    ],
)
def test_shared_application_and_final_projection_budget_boundaries(
    counter, constant, monkeypatch
):
    ledger, _, _ = _seed_bound_cpi()
    observed = []
    original = status._WorkBudget

    class Budget(original):
        def __init__(self):
            super().__init__()
            observed.append(self)

    monkeypatch.setattr(status, "_WorkBudget", Budget)
    expected = _view(ledger)
    assert len(observed) == 1
    used = getattr(observed[-1], counter)
    monkeypatch.setattr(application, constant, used)
    assert _view(ledger) == expected
    monkeypatch.setattr(application, constant, used - 1)
    assert inspect_as_of(ledger, BLS_CPI_HEADLINES.key, as_of=AT)
    assert inspect_binding_candidates(ledger, BLS_CPI_HEADLINES.key)
    with pytest.raises(OperatorQueryBudgetError) as caught:
        _view(ledger)
    assert caught.value.args == ("operator_query_budget_exceeded",)
    assert getattr(observed[-1], counter) <= used - 1


@pytest.mark.parametrize("kind", [Kind.VERSION, Kind.BINDING])
@pytest.mark.parametrize("size", [128, 129])
def test_existing_global_row_boundaries_apply_to_combined_call(backend, kind, size):
    ledger, plan, publication = _seed_bound_cpi()
    ledger._records.pop((Kind.PUBLICATION.value, publication.key.identity, ""))
    record = plan.version if kind == Kind.VERSION else plan.binding_candidate
    for number in range(1, size):
        ledger.put(replace(record, identity=f"synthetic:{number:04d}"))
    before = ledger.snapshot()
    if size == 129:
        with pytest.raises(OperatorQueryBudgetError):
            _view(_source(ledger, backend))
    else:
        view = _view(_source(ledger, backend))
        assert (
            len(view.as_of.candidates if kind == Kind.VERSION else view.stored_bindings)
            == 128
        )
    assert ledger.snapshot() == before


def test_explicit_snapshot_models_writer_and_rejects_escaped_source_and_failed_exit():
    ledger, plan, _ = _seed_bound_cpi()
    expected = _view(ledger)
    connection = SnapshotDouble(ledger)
    source = _snapshot_source(connection)
    connection.after_read = lambda: _store(
        ledger, replace(plan.binding_candidate, limitations=("synthetic new view",))
    )
    with source.operator_snapshot() as scope:
        candidate = _view(scope)
    assert candidate == expected
    assert set(connection.read_views) == {1}
    assert connection.events[-2:] == ["rollback", ("autocommit", False)]
    _failure(lambda: _view(scope), ReadFailure.STORAGE_UNAVAILABLE)
    with source.operator_snapshot() as scope:
        later = _view(scope)
    assert later != expected
    assert later.stored_bindings[0].candidate.limitations == ("synthetic new view",)

    connection.fault = "rollback"

    def fails_exit():
        with source.operator_snapshot() as scope:
            _view(scope)

    _failure(fails_exit, ReadFailure.STORAGE_UNAVAILABLE)
    assert connection.closed is True


def test_bare_postgres_ledger_is_rejected_without_sql_or_transaction_adoption():
    ledger, _, _ = _seed_bound_cpi()
    connection = SnapshotDouble(ledger)
    source = _snapshot_source(connection)
    _failure(lambda: _view(source), ReadFailure.STORAGE_UNAVAILABLE)
    assert connection.calls == connection.events == []
    with source.operator_snapshot() as scope:
        before = list(connection.events)
        _failure(lambda: _view(source), ReadFailure.STORAGE_UNAVAILABLE)
        assert connection.events == before and connection.calls == []
        assert _view(scope).stored_bindings


def test_scope_type_inspection_fault_is_classified_without_raw_exception():
    class BrokenScope:
        @property
        def __class__(self):
            raise RuntimeError("private type inspection failure")

    _failure(lambda: _view(BrokenScope()), ReadFailure.STORAGE_UNAVAILABLE)
