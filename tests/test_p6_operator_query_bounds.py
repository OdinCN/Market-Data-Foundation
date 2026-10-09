"""Offline application-budget boundaries; no backend resource guarantee."""

from dataclasses import replace
from datetime import timedelta

import pytest

from market_data_foundation.canonical import operator_asof as operator
from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    ReadFailure,
)
from market_data_foundation.canonical.model import (
    Assessment,
    Decision,
    Grade,
    Key,
    Kind,
)
from market_data_foundation.domain_contracts.dense_market import RAW_DAILY_BARS
from tests.test_p6_consumer_read import BASE, _seed_daily
from tests.test_p6_operator_asof import AT, _OperatorDouble, _store


def _inspect(ledger):
    return operator.inspect_as_of(ledger, RAW_DAILY_BARS.key, as_of=AT)


def _budget_failure(ledger):
    before = ledger.snapshot()
    with pytest.raises(ReadContractError) as caught:
        _inspect(ledger)
    error = caught.value
    assert type(error) is operator.OperatorQueryBudgetError
    assert error.classification is operator.OperatorFailure.QUERY_BUDGET_EXCEEDED
    assert error.args == ("operator_query_budget_exceeded",)
    assert vars(error) == {"classification": error.classification}
    assert ledger.snapshot() == before


@pytest.fixture
def observed_budgets(monkeypatch):
    observed = []
    original = operator._WorkBudget

    class Observed(original):
        def __init__(self):
            super().__init__()
            observed.append(self)

    monkeypatch.setattr(operator, "_WorkBudget", Observed)
    return observed


def _unpublished():
    ledger, _, plan, publication = _seed_daily()
    ledger._records.pop((Kind.PUBLICATION.value, publication.key.identity, ""))
    return ledger, plan


def _history(ledger, plan, initial, size):
    previous = initial
    for number in range(1, size):
        at = BASE + timedelta(seconds=3 + number)
        publication = replace(
            initial,
            request=replace(
                initial.request,
                identity=f"history:{number:04d}",
                occurrence=f"history:{number:04d}",
                predecessor=previous.key,
                decision=Decision.WITHDRAW if number % 2 else Decision.RESTORE,
            ),
            decision_at=at,
            usable_at=at,
        )
        _store(ledger, publication)
        previous = publication


@pytest.mark.parametrize("size", [128, 129])
def test_default_candidate_boundary_with_producer_admission(size):
    ledger, plan = _unpublished()
    for number in range(1, size):
        _store(ledger, replace(plan.version, identity=f"version:{number:04d}"))
    # The legacy API is unchanged; the operator uses separate producer admission.
    assert len(ledger.versions_for_definition(RAW_DAILY_BARS.key)) == size
    if size == 129:
        _budget_failure(ledger)
    else:
        view = _inspect(ledger)
        assert len(view.candidates) == 128
        assert view.selection_outcome == "as_of_no_version"


@pytest.mark.parametrize("size", [512, 513])
def test_default_publication_boundary_preserves_all_clocks_and_history(size):
    ledger, _, plan, initial = _seed_daily()
    _history(ledger, plan, initial, size)
    assert len(ledger.publications_for_version(plan.version.key)) == size
    if size == 513:
        _budget_failure(ledger)
    else:
        view = _inspect(ledger)
        assert len(view.candidates[0].publications) == 512
        assert view.candidates[0].effective_publication is not None
        assert view.candidates[0].publications[-1].pending_clocks == (
            "decision_at",
            "usable_at",
        )


def test_history_budget_is_shared_and_late_overflow_returns_no_first_candidate():
    ledger, _, plan, publication = _seed_daily("a")
    _history(ledger, plan, publication, 512)
    other = replace(plan.version, identity="z-version")
    _store(ledger, other)
    _store(
        ledger,
        replace(
            publication,
            request=replace(
                publication.request,
                identity="z-publication",
                occurrence="z-occurrence",
                version=other.key,
            ),
        ),
    )
    _budget_failure(ledger)


def _assessment_chain(ledger, plan, count, *, cycle=False):
    material = ledger.get(plan.version.materials[0])
    keys = tuple(Key(Kind.MATERIAL, f"assessment:{i:04d}") for i in range(count))
    for number, key in enumerate(keys):
        basis = (
            keys[number + 1]
            if number + 1 < count
            else keys[0]
            if cycle
            else material.key
        )
        _store(
            ledger,
            replace(
                material,
                identity=key.identity,
                grade=Grade.ASSESSMENT,
                assessment=Assessment(
                    target=plan.version.key,
                    rule_revision="synthetic-r1",
                    evaluated_at=BASE,
                    result="synthetic",
                    basis=(basis,),
                ),
            ),
        )
    _store(ledger, replace(plan.version, quality_evidence=(keys[0],)))
    return keys


@pytest.mark.parametrize("overflow", [False, True])
def test_default_transitive_lineage_boundary_including_target_backrefs(
    overflow, observed_budgets
):
    ledger, plan = _unpublished()
    _inspect(ledger)
    base = observed_budgets[-1].lineage_records
    _assessment_chain(ledger, plan, 2048 - base + int(overflow))
    if overflow:
        _budget_failure(ledger)
    else:
        assert _inspect(ledger).candidates[0].selection_state == "no_publication"
    assert observed_budgets[-1].lineage_records == 2048


def test_small_semantically_valid_reference_cycle_terminates_and_corruption_rejects():
    ledger, plan = _unpublished()
    keys = _assessment_chain(ledger, plan, 3, cycle=True)
    assert _inspect(ledger).selection_outcome == "as_of_no_version"
    _store(ledger, replace(ledger.get(keys[-1]), digest="private-invalid"))
    with pytest.raises(ReadContractError) as caught:
        _inspect(ledger)
    assert caught.value.classification is ReadFailure.LINEAGE_MISSING_OR_MISMATCHED
    assert caught.value.args == ("lineage_missing_or_mismatched",)


@pytest.mark.parametrize(
    "counter,constant",
    [
        ("resolutions", "_MAX_RESOLUTIONS"),
        ("metadata_nodes", "_MAX_METADATA_NODES"),
        ("text_characters", "_MAX_TEXT_CHARACTERS"),
        ("lineage_records", "_MAX_LINEAGE_RECORDS"),
        ("publications", "_MAX_PUBLICATIONS"),
    ],
)
def test_exact_measured_work_limit_passes_and_one_unit_less_rejects(
    counter, constant, monkeypatch, observed_budgets
):
    ledger, _, _, _ = _seed_daily()
    before = _inspect(ledger)
    used = getattr(observed_budgets[-1], counter)
    assert used > 0
    monkeypatch.setattr(operator, constant, used)
    assert _inspect(ledger) == before
    monkeypatch.setattr(operator, constant, used - 1)
    _budget_failure(ledger)
    # No counter increments past its limit, including metadata's final projection.
    assert getattr(observed_budgets[-1], counter) <= used - 1


def test_lineage_budget_is_shared_between_candidate_closures(monkeypatch):
    ledger, plan = _unpublished()
    _store(ledger, replace(plan.version, identity="z-version"))
    monkeypatch.setattr(operator, "_MAX_LINEAGE_RECORDS", 4)
    _budget_failure(ledger)


@pytest.mark.parametrize("size", [4096, 4097])
def test_default_finite_member_projection_boundary(size):
    ledger, plan = _unpublished()
    coverage = replace(
        plan.version.finite_scope, members=tuple(f"member:{n}" for n in range(size))
    )
    _store(ledger, replace(plan.version, finite_scope=coverage))
    if size == 4097:
        _budget_failure(ledger)
    else:
        assert _inspect(ledger).candidates[0].finite_scope == coverage


@pytest.mark.parametrize("size", [16384, 16385])
def test_default_text_projection_boundary(size):
    ledger, plan = _unpublished()
    _store(ledger, replace(plan.version, completeness="x" * size))
    if size == 16385:
        _budget_failure(ledger)
    else:
        assert _inspect(ledger).candidates[0].completeness == "x" * size


@pytest.mark.parametrize("kind", ["versions", "history"])
def test_oversized_malicious_enumeration_is_rejected_before_item_access(kind):
    ledger, _, _, _ = _seed_daily()

    class Explosive:
        def __getattribute__(self, name):
            raise AssertionError("private item must never be visited")

    class Malicious(_OperatorDouble):
        def versions_for_definition(self, definition):
            if kind == "versions":
                return (Explosive(),) * 129
            return super().versions_for_definition(definition)

        def publications_for_version(self, version):
            return (Explosive(),) * 513

    ledger.__class__ = Malicious
    _budget_failure(ledger)


@pytest.mark.parametrize("kind", ["versions", "history"])
def test_unaccountable_tuple_subclass_is_rejected_without_iteration(kind):
    ledger, _, _, _ = _seed_daily()

    class MaliciousTuple(tuple):
        def __iter__(self):
            raise AssertionError("untrusted iteration must never execute")

    class Malicious(_OperatorDouble):
        def versions_for_definition(self, definition):
            values = super().versions_for_definition(definition)
            return MaliciousTuple(values) if kind == "versions" else values

        def publications_for_version(self, version):
            return MaliciousTuple(super().publications_for_version(version))

    ledger.__class__ = Malicious
    with pytest.raises(ReadContractError) as caught:
        _inspect(ledger)
    assert caught.value.classification is (
        ReadFailure.STORAGE_UNAVAILABLE
        if kind == "versions"
        else ReadFailure.PUBLICATION_STATE_INVALID
    )


@pytest.mark.parametrize("shape", ["deep", "cyclic", "mutable"])
def test_malicious_metadata_shape_never_reaches_recursive_canonical_validation(shape):
    ledger, plan = _unpublished()
    run = ledger.get(plan.version.run)
    malformed = replace(run, trigger="private")
    if shape == "deep":
        value = "private"
        for _ in range(33):
            value = (value,)
        malformed = replace(run, trigger=value)
    elif shape == "cyclic":
        object.__setattr__(malformed, "trigger", malformed)
    else:
        malformed = replace(run, trigger=["private"])

    class Corrupt(_OperatorDouble):
        def get(self, key):
            return malformed if key == run.key else super().get(key)

    ledger.__class__ = Corrupt
    if shape != "mutable":
        _budget_failure(ledger)
    else:
        with pytest.raises(ReadContractError) as caught:
            _inspect(ledger)
        assert caught.value.classification is ReadFailure.LINEAGE_MISSING_OR_MISMATCHED


def test_budget_resets_for_each_call_and_does_not_change_public_contract():
    ledger, _, _, _ = _seed_daily()
    first = _inspect(ledger)
    assert _inspect(ledger) == first
    assert "operator_query_budget_exceeded" not in {item.value for item in ReadFailure}
    assert first.binding_activation == first.source_health == "unobservable"


def test_default_resolution_limit_counts_repeated_pinned_references(observed_budgets):
    ledger, plan = _unpublished()
    run = ledger.get(plan.version.run)
    _store(ledger, replace(run, inputs=(plan.version.materials[0],) * 4096))
    for number in range(1, 8):
        _store(ledger, replace(plan.version, identity=f"version:{number:04d}"))
    _budget_failure(ledger)
    budget = observed_budgets[-1]
    assert budget.resolutions == 32768
    assert budget.lineage_records < 2048


@pytest.mark.parametrize("dimension", ["nodes", "text"])
def test_default_aggregate_metadata_limits_across_candidates(
    dimension, observed_budgets
):
    ledger, plan = _unpublished()
    version = (
        replace(
            plan.version,
            finite_scope=replace(
                plan.version.finite_scope,
                members=tuple(chr(0x1000 + number) for number in range(4096)),
            ),
        )
        if dimension == "nodes"
        else replace(plan.version, completeness="x" * 16384)
    )
    _store(ledger, version)
    for number in range(1, 128):
        _store(ledger, replace(version, identity=f"version:{number:04d}"))
    _budget_failure(ledger)
    budget = observed_budgets[-1]
    if dimension == "nodes":
        assert budget.metadata_nodes == 1048576
        assert budget.text_characters < 4194304
    else:
        assert 4194304 - 16384 < budget.text_characters <= 4194304
        assert budget.metadata_nodes < 1048576


class _UnderreportedText(str):
    def __len__(self):
        return 1

    def __str__(self):
        raise AssertionError("metadata must not invoke custom string conversion")


class _ExplosiveText(_UnderreportedText):
    def __len__(self):
        raise AssertionError("metadata must not invoke custom length")


@pytest.mark.parametrize("text_type", [_UnderreportedText, _ExplosiveText])
@pytest.mark.parametrize("size", [16384, 16385])
@pytest.mark.parametrize("field", ["run_trigger", "version_completeness"])
def test_string_subclass_readback_uses_real_character_boundary(text_type, size, field):
    ledger, plan = _unpublished()
    run = ledger.get(plan.version.run)
    text = text_type("x" * size)
    assert str.__len__(text) == size
    version = (
        replace(plan.version, completeness=text)
        if field == "version_completeness"
        else plan.version
    )
    readback_run = replace(run, trigger=text) if field == "run_trigger" else run

    class CustomReadback(_OperatorDouble):
        def get(self, key):
            if key == version.key:
                return version
            if key == run.key:
                return readback_run
            return super().get(key)

        def versions_for_definition(self, definition):
            return (version,)

    ledger.__class__ = CustomReadback
    if size == 16385:
        _budget_failure(ledger)
    else:
        view = _inspect(ledger)
        assert view.selection_outcome == "as_of_no_version"
        assert view.candidates[0].completeness == version.completeness


@pytest.mark.parametrize("text_type", [_UnderreportedText, _ExplosiveText])
@pytest.mark.parametrize("size", [16384, 16385])
def test_final_projection_string_subclass_uses_real_character_boundary(
    text_type, size, monkeypatch
):
    ledger, _ = _unpublished()
    original = operator.VersionDiagnostic
    text = text_type("x" * size)

    def project(**values):
        return replace(original(**values), completeness=text)

    # Readbacks remain ordinary canonical metadata; only the final diagnostic
    # receives this synthetic string, isolating the final accounting pass.
    monkeypatch.setattr(operator, "VersionDiagnostic", project)
    if size == 16385:
        _budget_failure(ledger)
    else:
        assert _inspect(ledger).candidates[0].completeness is text


@pytest.mark.parametrize("text_type", [str, _UnderreportedText, _ExplosiveText])
def test_aggregate_text_budget_counts_real_repeated_subclass_characters(text_type):
    budget = operator._WorkBudget()
    text = text_type("x" * 16384)
    budget.metadata((text,) * 256)
    assert budget.text_characters == 4194304
    with pytest.raises(operator.OperatorQueryBudgetError) as caught:
        budget.metadata(text)
    assert caught.value.args == ("operator_query_budget_exceeded",)
    assert budget.text_characters == 4194304


@pytest.mark.parametrize("enum_type", [Kind, Decision, Grade])
def test_canonical_strenum_metadata_counts_builtin_character_length(enum_type):
    values = tuple(enum_type)
    budget = operator._WorkBudget()
    budget.metadata(values)
    assert budget.text_characters == sum(str.__len__(value) for value in values)
