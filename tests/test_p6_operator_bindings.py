"""Offline inventory proofs; SQL doubles do not execute PostgreSQL."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from market_data_foundation.canonical import operator_reads as producer
from market_data_foundation.canonical.codec import dumps
from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    ReadFailure,
)
from market_data_foundation.canonical.consumer_wire import handle_request
from market_data_foundation.canonical.model import (
    EvidenceRequirement,
    Grade,
    Key,
    Kind,
    Scope,
)
from market_data_foundation.canonical.operator_asof import OperatorQueryBudgetError
from market_data_foundation.canonical.operator_bindings import (
    inspect_binding_candidates,
)
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.domain_contracts.dense_market import RAW_DAILY_BARS
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from tests.test_p6_consumer_read import _seed_bound_cpi
from tests.test_p6_consumer_wire import _as_of_request
from tests.test_p6_operator_asof import AT, _store
from tests.test_p6_operator_producer_admission import _source


@pytest.fixture(params=["memory", "sql_double"])
def backend(request):
    return request.param


def _seed():
    _, plan, _ = _seed_bound_cpi()
    ledger = InMemoryLedger()
    for record in (
        BLS_CPI_HEADLINES,
        RAW_DAILY_BARS,
        plan.source,
        plan.binding_candidate,
    ):
        ledger.put(record)
    return ledger, plan


def _inventory(source, definition=BLS_CPI_HEADLINES.key):
    return inspect_binding_candidates(source, definition)


def _failure(source, expected, definition=BLS_CPI_HEADLINES.key):
    with pytest.raises(ReadContractError) as caught:
        _inventory(source, definition)
    assert caught.value.classification == expected
    assert caught.value.args == (expected.value,)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


def test_empty_known_and_standalone_candidates_are_readonly(backend):
    ledger, plan = _seed()
    source = _source(ledger, backend)
    snapshot = ledger.snapshot()
    first = _inventory(source)
    candidate = first.candidates[0]
    assert candidate.binding == plan.binding_candidate.key
    assert candidate.source == plan.source
    assert candidate.matching_mappings == plan.binding_candidate.mappings
    assert candidate.limitations == plan.binding_candidate.limitations
    assert candidate.compatibility_policy == plan.binding_candidate.compatibility_policy
    assert candidate.silent_fallback is False
    assert first.definition == BLS_CPI_HEADLINES
    assert _inventory(source, RAW_DAILY_BARS.key).candidates == ()
    assert first == _inventory(source)
    assert ledger.snapshot() == snapshot
    assert not any(k[0] in {Kind.VERSION, Kind.PUBLICATION} for k in ledger._records)
    for name in (
        "binding_activation",
        "executable_binding",
        "source_health",
        "current_freshness",
        "external_admission",
        "finite_coverage",
        "quality",
        "durability",
        "replayability",
    ):
        assert getattr(first, name) == "unobservable"
    empty = InMemoryLedger()
    empty.put(BLS_CPI_HEADLINES)
    assert _inventory(_source(empty, backend)).candidates == ()


def test_unpublished_and_published_version_do_not_change_inventory_or_public_wire(
    backend,
):
    ledger, plan, publication = _seed_bound_cpi()
    request = _as_of_request(BLS_CPI_HEADLINES.key, AT.isoformat())
    before = handle_request(ledger, request)
    snapshot = ledger.snapshot()
    published = _inventory(_source(ledger, backend))
    assert handle_request(ledger, request) == before
    assert ledger.snapshot() == snapshot
    key = publication.key
    ledger._records.pop((key.kind.value, key.identity, key.revision))
    unpublished_wire = handle_request(ledger, request)
    snapshot = ledger.snapshot()
    assert _inventory(_source(ledger, backend)) == published
    assert handle_request(ledger, request) == unpublished_wire
    assert ledger.snapshot() == snapshot
    assert published.candidates[0].binding == plan.binding_candidate.key


def test_multiple_revisions_and_matching_mappings_have_stable_order(backend):
    ledger, plan = _seed()
    unrelated = replace(RAW_DAILY_BARS, identity="synthetic.unadmitted")
    ledger.put(unrelated)
    base = plan.binding_candidate.mappings[0]
    mappings = (
        replace(base, identity="z"),
        replace(base, identity="other", definition=unrelated.key),
        replace(base, identity="a"),
    )
    for identity, revision in (("z", "r2"), ("a", "r2"), ("a", "r1")):
        ledger.put(
            replace(
                plan.binding_candidate,
                identity=identity,
                revision=revision,
                mappings=mappings,
            )
        )
    ledger.put(
        replace(plan.binding_candidate, identity="unrelated", mappings=(mappings[1],))
    )
    source = _source(ledger, backend)
    snapshot = ledger.snapshot()
    first = _inventory(source)
    assert [
        (c.binding.identity, c.binding.revision) for c in first.candidates
    ] == sorted([(c.binding.identity, c.binding.revision) for c in first.candidates])
    assert len(first.candidates) == 4
    assert all(c.binding.identity != "unrelated" for c in first.candidates)
    assert [m.identity for m in first.candidates[0].matching_mappings] == ["a", "z"]
    assert all(
        m.definition == BLS_CPI_HEADLINES.key
        for c in first.candidates
        for m in c.matching_mappings
    )
    ledger._records = dict(reversed(list(ledger._records.items())))
    assert _inventory(_source(ledger, backend)) == first
    assert ledger.snapshot() == snapshot
    _failure(source, ReadFailure.DEFINITION_NOT_ADMITTED, unrelated.key)


@pytest.mark.parametrize(
    "case",
    [
        "source_missing",
        "source_identity",
        "source_revision",
        "source_write",
        "source_capability",
        "definition_missing",
        "definition_revision",
        "definition_corrupt",
        "silent_fallback",
        "duplicate_mapping",
        "no_mapping",
        "mapping_scope",
        "mapping_kind",
        "mapping_identity",
        "mapping_revision",
        "mapping_type",
        "evidence_type",
        "evidence_claim",
        "source_type",
        "limitation_type",
    ],
)
def test_invalid_source_mapping_and_claims_reject_all_candidates(backend, case):
    ledger, plan = _seed()
    binding, source = plan.binding_candidate, plan.source
    mapping = binding.mappings[0]
    if case == "source_missing":
        ledger._records.pop(
            (Kind.SOURCE.value, source.identity, source.contract_revision)
        )
    elif case.startswith("source_"):
        invalid = {
            "source_identity": replace(source, identity="private-wrong"),
            "source_revision": replace(source, contract_revision="private-wrong"),
            "source_write": replace(source, access_effect="write-data"),
            "source_capability": replace(source, evidence_capability=frozenset()),
            "source_type": replace(source, freshness_contract=123),
        }[case]
        ledger._records[
            (Kind.SOURCE.value, source.identity, source.contract_revision)
        ] = dumps(invalid)
    elif case == "definition_missing":
        ledger._records.pop(
            (
                Kind.DEFINITION.value,
                BLS_CPI_HEADLINES.identity,
                BLS_CPI_HEADLINES.revision,
            )
        )
    elif case == "definition_corrupt":
        _store(ledger, replace(BLS_CPI_HEADLINES, temporal_semantics="private altered"))
    else:
        invalid_mapping = {
            "definition_revision": replace(
                mapping,
                definition=Key(
                    Kind.DEFINITION, BLS_CPI_HEADLINES.identity, "private-wrong"
                ),
            ),
            "mapping_scope": replace(mapping, scope=Scope()),
            "mapping_kind": replace(mapping, definition=source.key),
            "mapping_identity": replace(mapping, identity=""),
            "mapping_revision": replace(mapping, semantic_revision=""),
            "mapping_type": "private-invalid",
            "evidence_type": replace(mapping, evidence="private-invalid"),
            "evidence_claim": replace(
                mapping, evidence=replace(mapping.evidence, durable="private-invalid")
            ),
        }.get(case, mapping)
        if case == "mapping_scope":
            _store(
                ledger, replace(source, scope=Scope(frozenset({"operator:synthetic"})))
            )
        invalid_binding = replace(binding, mappings=(invalid_mapping,))
        if case == "silent_fallback":
            invalid_binding = replace(binding, silent_fallback=True)
        elif case == "duplicate_mapping":
            invalid_binding = replace(binding, mappings=(mapping, mapping))
        elif case == "no_mapping":
            invalid_binding = replace(binding, mappings=())
        elif case == "limitation_type":
            invalid_binding = replace(binding, limitations=(123,))
        _store(ledger, invalid_binding)
    snapshot = ledger.snapshot()
    expected = (
        ReadFailure.DEFINITION_NOT_FOUND
        if case == "definition_missing"
        else ReadFailure.LINEAGE_MISSING_OR_MISMATCHED
    )
    _failure(_source(ledger, backend), expected)
    assert ledger.snapshot() == snapshot


def test_evidence_requirements_and_contract_claims_are_preserved_without_observation(
    backend,
):
    ledger, plan = _seed()
    mapping = replace(
        plan.binding_candidate.mappings[0],
        evidence=EvidenceRequirement(
            frozenset({Grade.PROJECTED}),
            durable=True,
            replayable=True,
        ),
    )
    _store(
        ledger,
        replace(
            plan.binding_candidate,
            mappings=(mapping,),
            limitations=("finite synthetic scope only",),
        ),
    )
    view = _inventory(_source(ledger, backend))
    assert view.candidates[0].matching_mappings == (mapping,)
    assert (
        view.durability == view.replayability == view.finite_coverage == "unobservable"
    )


@pytest.mark.parametrize(
    "case",
    ["malformed", "wrong_type", "wrong_identity", "wrong_revision", "missing_link"],
)
def test_unrelated_late_bad_candidate_prevents_partial_success(backend, case):
    ledger, plan = _seed()
    unrelated = replace(
        plan.binding_candidate,
        identity="z-unrelated",
        mappings=(
            replace(
                plan.binding_candidate.mappings[0],
                definition=RAW_DAILY_BARS.key,
            ),
        ),
    )
    ledger.put(unrelated)
    invalid = {
        "malformed": "private malformed payload",
        "wrong_type": dumps(plan.source),
        "wrong_identity": dumps(replace(unrelated, identity="private wrong")),
        "wrong_revision": dumps(replace(unrelated, revision="private wrong")),
        "missing_link": dumps(
            replace(unrelated, source=Key(Kind.SOURCE, "private missing", "r1"))
        ),
    }[case]
    ledger._records[(Kind.BINDING.value, unrelated.identity, unrelated.revision)] = (
        invalid
    )
    snapshot = ledger.snapshot()
    with pytest.raises(ReadContractError) as caught:
        _inventory(_source(ledger, backend))
    assert caught.value.args == (caught.value.classification.value,)
    assert "private" not in str(caught.value)
    assert ledger.snapshot() == snapshot


@pytest.mark.parametrize("count", [128, 129])
def test_global_binding_count_is_admitted_before_any_decode(
    backend, monkeypatch, count
):
    ledger, plan = _seed()
    for number in range(1, count):
        ledger.put(
            replace(
                plan.binding_candidate,
                identity=f"other:{number:04d}",
                mappings=(
                    replace(
                        plan.binding_candidate.mappings[0],
                        definition=RAW_DAILY_BARS.key,
                    ),
                ),
            )
        )
    seen = []
    original = producer.loads

    def observe(payload):
        seen.append(payload)
        return original(payload)

    monkeypatch.setattr(producer, "loads", observe)
    session = _source(ledger, backend).operator_reads()
    if count == 128:
        assert len(session.binding_candidates(128)) == 128
        assert len(seen) == 128
        assert len(_inventory(_source(ledger, backend)).candidates) == 1
    else:
        with pytest.raises(producer.OperatorReadAdmissionError):
            session.binding_candidates(128)
        assert seen == []
        with pytest.raises(OperatorQueryBudgetError):
            _inventory(_source(ledger, backend))


@pytest.mark.parametrize(
    "backend,case",
    [(b, c) for b in ("memory", "sql_double") for c in ("bytes", "total", "depth")]
    + [("memory", "slots")],
)
def test_whole_batch_limits_precede_decode(backend, monkeypatch, case):
    ledger, plan = _seed()
    second = replace(plan.binding_candidate, identity="z-second")
    ledger.put(second)
    if case == "bytes":
        ledger._records[(Kind.BINDING.value, second.identity, second.revision)] = (
            "x" * (producer.MAX_PAYLOAD_BYTES + 1)
        )
    elif case == "depth":
        ledger._records[(Kind.BINDING.value, second.identity, second.revision)] = (
            "[" * 65 + "0" + "]" * 65
        )
    elif case == "total":
        total = sum(
            len(p.encode()) for k, p in ledger._records.items() if k[0] == Kind.BINDING
        )
        monkeypatch.setattr(producer, "MAX_TOTAL_BYTES", total - 1)
    else:
        monkeypatch.setattr(producer, "MAX_MEMORY_SLOTS", len(ledger._records) - 1)
    seen = []
    monkeypatch.setattr(producer, "loads", lambda p: seen.append(p))
    with pytest.raises(producer.OperatorReadAdmissionError):
        _source(ledger, backend).operator_reads().binding_candidates(128)
    assert seen == []


@pytest.mark.parametrize(
    "case", ["duplicate", "conflict", "oversize", "wrong_batch", "missing_method"]
)
def test_untrusted_capability_results_fail_closed(case):
    ledger, plan = _seed()
    bounded = ledger.operator_reads()
    candidates = {
        "duplicate": (plan.binding_candidate, plan.binding_candidate),
        "conflict": (
            replace(plan.binding_candidate, limitations=("private conflict",)),
        ),
        "oversize": (plan.binding_candidate,) * 129,
        "wrong_batch": [plan.binding_candidate],
        "missing_method": (),
    }[case]
    session = SimpleNamespace(get=bounded.get)
    if case != "missing_method":
        session.binding_candidates = lambda maximum: candidates
    source = SimpleNamespace(operator_reads=lambda: session)
    expected = (
        "operator_query_budget_exceeded"
        if case == "oversize"
        else ReadFailure.STORAGE_UNAVAILABLE
        if case == "missing_method"
        else ReadFailure.LINEAGE_MISSING_OR_MISMATCHED
    )
    with pytest.raises(ReadContractError) as caught:
        _inventory(source)
    assert caught.value.classification == expected


def test_no_legacy_reads_content_versions_publications_or_writes(backend):
    ledger, _ = _seed()
    source = _source(ledger, backend)
    bounded = source.operator_reads()

    def forbidden(*_args):
        pytest.fail("unexpected legacy, content, publication or mutation path")

    bounded.versions_for_definition = bounded.publications_for_version = forbidden
    for name in (
        "get",
        "put",
        "publish",
        "put_content",
        "get_content",
        "versions_for_definition",
        "publications_for_version",
    ):
        setattr(source, name, forbidden)
    source.operator_reads = lambda: bounded
    assert len(_inventory(source).candidates) == 1
    _failure(SimpleNamespace(get=forbidden), ReadFailure.STORAGE_UNAVAILABLE)


def test_alias_row_cannot_hide_a_duplicate_exact_candidate(backend):
    ledger, plan = _seed()
    ledger._records[(Kind.BINDING.value, "private alias", "r1")] = dumps(
        plan.binding_candidate
    )
    with pytest.raises(ReadContractError) as caught:
        _inventory(_source(ledger, backend))
    assert caught.value.args == (caught.value.classification.value,)


@pytest.mark.parametrize("case", ["text", "collection"])
def test_decoded_metadata_admission_precedes_validation_and_sorting(case):
    ledger, plan = _seed()
    bounded = ledger.operator_reads()
    invalid = (
        replace(plan.binding_candidate, identity="x" * 16385)
        if case == "text"
        else replace(
            plan.binding_candidate,
            mappings=plan.binding_candidate.mappings * 4097,
        )
    )
    source = SimpleNamespace(
        operator_reads=lambda: SimpleNamespace(
            get=bounded.get,
            binding_candidates=lambda maximum: (invalid,),
        )
    )
    with pytest.raises(OperatorQueryBudgetError) as caught:
        _inventory(source)
    assert caught.value.args == ("operator_query_budget_exceeded",)
