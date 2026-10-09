"""Offline operator metadata diagnostics, independent of public consumer reads."""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from market_data_foundation.canonical.codec import dumps
from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    ReadFailure,
    read_exact_version,
)
from market_data_foundation.canonical.consumer_wire import handle_request
from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import (
    Assessment,
    Decision,
    Grade,
    Key,
    Kind,
)
from market_data_foundation.canonical.operator_asof import inspect_as_of
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.domain_contracts.dense_market import RAW_DAILY_BARS
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from market_data_foundation.historical.daily_bars import (
    persist_replay_before_publication,
    publish_replay,
)
from tests.test_p6_consumer_read import (
    BASE,
    _classification,
    _daily_plan,
    _seed_bound_cpi,
    _seed_daily,
)
from tests.test_p6_consumer_wire import _as_of_request

AT = BASE + timedelta(seconds=20)


def _store(ledger, record):
    key = record.key
    ledger._records[(key.kind.value, key.identity, key.revision)] = dumps(record)


def _failure(ledger, expected, definition=RAW_DAILY_BARS.key, as_of=AT):
    with pytest.raises(ReadContractError) as exc:
        inspect_as_of(ledger, definition, as_of=as_of)
    assert _classification(exc) == expected
    assert "private" not in str(exc.value)


def test_empty_and_unpublished_are_distinct_without_latest_or_public_details():
    ledger = InMemoryLedger()
    ledger.put(RAW_DAILY_BARS)
    empty = inspect_as_of(ledger, RAW_DAILY_BARS.key, as_of=AT)
    assert empty.selection_outcome == "as_of_no_version"
    assert empty.candidates == ()

    plan = _daily_plan()
    kernel = CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=3))
    persist_replay_before_publication(kernel, plan)
    request = _as_of_request(RAW_DAILY_BARS.key, AT.isoformat())
    public_before = handle_request(ledger, request)
    snapshot = ledger.snapshot()
    view = inspect_as_of(ledger, RAW_DAILY_BARS.key, as_of=AT)
    assert view.selection_outcome == "as_of_no_version"
    assert view.candidates[0].selection_state == "no_publication"
    assert view.candidates[0].publications == ()
    assert view.candidates[0].effective_publication is None
    assert (
        handle_request(ledger, request)
        == public_before
        == (
            b'{"classification":"as_of_no_version","status":"error",'
            b'"wire_revision":"mdf.consumer.read.v1"}'
        )
    )
    assert ledger.snapshot() == snapshot


@pytest.mark.parametrize(
    "seconds,pending,outcome",
    [
        (2, ("decision_at", "usable_at", "effective_at"), "as_of_no_version"),
        (3, ("usable_at", "effective_at"), "as_of_no_version"),
        (4, ("effective_at",), "as_of_no_version"),
        (5, (), "one_publication_candidate"),
    ],
)
def test_all_three_clocks_have_exact_inclusive_boundaries(seconds, pending, outcome):
    plan = _daily_plan()
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=3))
    persist_replay_before_publication(kernel, plan)
    publication = kernel.publish(
        replace(
            plan.publication_request,
            usable_at=BASE + timedelta(seconds=4),
            effective_at=BASE + timedelta(seconds=5),
        )
    )
    view = inspect_as_of(
        ledger, RAW_DAILY_BARS.key, as_of=BASE + timedelta(seconds=seconds)
    )
    assert view.selection_outcome == outcome
    candidate = view.candidates[0]
    assert candidate.publications[0].pending_clocks == pending
    assert candidate.publications[0].decision_at == publication.decision_at
    assert candidate.effective_publication == (None if pending else publication.key)


def test_ambiguous_candidates_are_sorted_and_never_resolved_by_latest_or_coverage():
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=3))
    plans = [_daily_plan("z"), _daily_plan("a")]
    for plan in plans:
        persist_replay_before_publication(kernel, plan)
        publish_replay(kernel, plan)
    snapshot = ledger.snapshot()
    request = _as_of_request(RAW_DAILY_BARS.key, AT.isoformat())
    public_before = handle_request(ledger, request)
    view = inspect_as_of(ledger, RAW_DAILY_BARS.key, as_of=AT)
    assert view.selection_outcome == "as_of_ambiguous"
    assert tuple(item.version.identity for item in view.candidates) == (
        "a:version",
        "z:version",
    )
    assert all(
        item.selection_state == "publication_eligible" for item in view.candidates
    )
    assert view.candidates[0].finite_scope == plans[1].version.finite_scope
    assert view.candidates[0].completeness == plans[1].version.completeness
    assert view.temporal_semantics == RAW_DAILY_BARS.temporal_semantics
    assert view.completeness_semantics == RAW_DAILY_BARS.completeness_semantics
    assert not hasattr(view, "selected_version")
    assert not hasattr(view, "content")
    assert (
        inspect_as_of(
            InMemoryLedger.from_snapshot(snapshot), RAW_DAILY_BARS.key, as_of=AT
        )
        == view
    )
    assert (
        handle_request(ledger, request)
        == public_before
        == (
            b'{"classification":"as_of_ambiguous","status":"error",'
            b'"wire_revision":"mdf.consumer.read.v1"}'
        )
    )
    assert ledger.snapshot() == snapshot


def test_withdraw_restore_supersede_keep_history_and_delayed_effects():
    ledger, kernel, plan, previous = _seed_daily()
    at = BASE + timedelta(seconds=4)
    for decision, state in (
        (Decision.WITHDRAW, "version_withdrawn"),
        (Decision.RESTORE, "publication_eligible"),
        (Decision.SUPERSEDE, "version_superseded"),
    ):
        kernel.clock = lambda: at
        request = replace(
            plan.publication_request,
            identity=f"transition:{decision}",
            occurrence=f"transition:{decision}",
            decision=decision,
            predecessor=previous.key,
            effective_at=at + timedelta(seconds=1),
            supersedes_version=(
                plan.version.key if decision == Decision.SUPERSEDE else None
            ),
        )
        publication = kernel.publish(request)
        before = inspect_as_of(ledger, RAW_DAILY_BARS.key, as_of=at)
        assert before.candidates[0].effective_publication == previous.key
        after = inspect_as_of(
            ledger, RAW_DAILY_BARS.key, as_of=at + timedelta(seconds=1)
        )
        assert after.candidates[0].selection_state == state
        assert after.candidates[0].effective_publication == publication.key
        assert after.candidates[0].publications[-1].decision == decision
        assert after.selection_outcome == (
            "one_publication_candidate"
            if decision == Decision.RESTORE
            else "as_of_no_version"
        )
        previous = publication
        at += timedelta(seconds=2)


def test_binding_record_does_not_prove_activation_health_freshness_or_admission():
    ledger, plan, _publication = _seed_bound_cpi()

    class MetadataOnlyLedger(InMemoryLedger):
        def get_content(self, *_args):
            raise AssertionError("operator must not fetch content")

        def put(self, *_args):
            raise AssertionError("operator must not write")

        def put_content(self, *_args):
            raise AssertionError("operator must not write")

        def publish(self, *_args):
            raise AssertionError("operator must not publish")

    ledger.__class__ = MetadataOnlyLedger
    before = ledger.snapshot()
    view = inspect_as_of(ledger, BLS_CPI_HEADLINES.key, as_of=AT)
    assert view.candidates[0].binding_record == plan.binding_candidate.key
    assert view.candidates[0].binding_limitations == plan.binding_candidate.limitations
    for field in (
        "binding_activation",
        "source_health",
        "current_freshness",
        "external_admission",
        "content_integrity",
        "durability",
        "replayability",
    ):
        assert getattr(view, field) == "unobservable"
    assert "controlled-receipt" not in repr(view)
    assert "mdf-content://" not in repr(view)
    assert not hasattr(view.candidates[0], "source_event_at")
    assert ledger.snapshot() == before


@pytest.mark.parametrize("corrupt", [False, True])
def test_publication_candidate_never_bypasses_exact_content_failures(corrupt):
    ledger, _kernel, plan, _publication = _seed_daily()
    if corrupt:
        ledger._contents[plan.version.identity] = (
            plan.version.content_digest,
            b"corrupt",
        )
    else:
        ledger._contents.clear()
    view = inspect_as_of(ledger, RAW_DAILY_BARS.key, as_of=AT)
    assert view.selection_outcome == "one_publication_candidate"
    assert view.content_integrity == "unobservable"
    with pytest.raises(ReadContractError) as exc:
        read_exact_version(ledger, RAW_DAILY_BARS.key, plan.version.key, as_of=AT)
    assert _classification(exc) == (
        ReadFailure.CONTENT_DIGEST_MISMATCH if corrupt else ReadFailure.CONTENT_MISSING
    )


@pytest.mark.parametrize("at", [datetime(2026, 10, 9), None, "private-invalid-time"])
def test_invalid_instant_is_classified(at):
    _failure(InMemoryLedger(), ReadFailure.INVALID_AS_OF, as_of=at)


@pytest.mark.parametrize(
    "case", ["not_admitted", "wrong_revision", "missing", "altered"]
)
def test_exact_two_definitions_only(case):
    ledger, _kernel, _plan, _publication = _seed_daily()
    definition = RAW_DAILY_BARS.key
    expected = ReadFailure.DEFINITION_NOT_ADMITTED
    if case == "not_admitted":
        definition = Key(Kind.DEFINITION, "mdf.security.universe.a-share", "p3-r1")
    elif case == "wrong_revision":
        definition = Key(Kind.DEFINITION, definition.identity, "p3-r2")
    elif case == "missing":
        ledger._records.pop(
            (definition.kind.value, definition.identity, definition.revision)
        )
        expected = ReadFailure.DEFINITION_NOT_FOUND
    else:
        _store(
            ledger, replace(RAW_DAILY_BARS, completeness_semantics="private-expanded")
        )
        expected = ReadFailure.LINEAGE_MISSING_OR_MISMATCHED
    _failure(ledger, expected, definition=definition)


@pytest.mark.parametrize(
    "case", ["list", "wrong_definition", "duplicate", "unsorted", "unresolved"]
)
def test_invalid_candidate_enumeration_returns_no_partial_projection(case):
    ledger, _kernel, plan, _publication = _seed_daily()
    other = replace(plan.version, identity="a-earlier")
    values = {
        "list": [plan.version],
        "wrong_definition": (replace(plan.version, definition=BLS_CPI_HEADLINES.key),),
        "duplicate": (plan.version, plan.version),
        "unsorted": (plan.version, other),
        "unresolved": (other,),
    }[case]

    class BadEnumeration(InMemoryLedger):
        def versions_for_definition(self, _definition):
            return values

    ledger.__class__ = BadEnumeration
    _failure(
        ledger,
        ReadFailure.LINEAGE_MISSING_OR_MISMATCHED
        if case == "unresolved"
        else ReadFailure.STORAGE_UNAVAILABLE,
    )


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize(
    "case", ["version", "binding", "source", "acquisition", "material"]
)
def test_corrupt_lineage_in_unpublished_and_future_candidates_fails_closed(
    case, published
):
    ledger, plan, publication = _seed_bound_cpi()
    if not published:
        ledger._records.pop((Kind.PUBLICATION.value, publication.key.identity, ""))
    record = {
        "version": replace(plan.version, content_digest="private-invalid-digest"),
        "binding": replace(plan.binding_candidate, silent_fallback=True),
        "source": replace(plan.source, access_effect="write_provider"),
        "acquisition": replace(
            plan.acquisition, source=Key(Kind.SOURCE, "absent", "r1")
        ),
        "material": replace(plan.raw_material, produced_by=Key(Kind.RUN, "absent")),
    }[case]
    _store(ledger, record)
    # Future publications and unpublished records cannot bypass lineage validation.
    _failure(
        ledger,
        ReadFailure.LINEAGE_MISSING_OR_MISMATCHED,
        definition=BLS_CPI_HEADLINES.key,
        as_of=BASE,
    )


@pytest.mark.parametrize("case", ["predecessor", "naive", "usable_before_decision"])
def test_invalid_publication_history_is_not_projected(case):
    ledger, _kernel, _plan, publication = _seed_daily()
    if case == "predecessor":
        invalid = replace(
            publication,
            request=replace(
                publication.request, predecessor=Key(Kind.PUBLICATION, "absent")
            ),
        )
    elif case == "naive":
        invalid = replace(
            publication, decision_at=publication.decision_at.replace(tzinfo=None)
        )
    else:
        invalid = replace(publication, usable_at=BASE)
    _store(ledger, invalid)
    _failure(ledger, ReadFailure.PUBLICATION_STATE_INVALID)


@pytest.mark.parametrize("stage", ["definition", "versions", "history", "lineage"])
def test_storage_and_lineage_exceptions_expose_only_classification(stage):
    ledger, plan, _publication = _seed_bound_cpi()

    class Broken(InMemoryLedger):
        def get(self, key):
            if (stage == "definition" and key == BLS_CPI_HEADLINES.key) or (
                stage == "lineage" and key == plan.source.key
            ):
                raise RuntimeError("private storage details")
            return super().get(key)

        def versions_for_definition(self, key):
            if stage == "versions":
                raise RuntimeError("private storage details")
            return super().versions_for_definition(key)

        def publications_for_version(self, key):
            if stage == "history":
                raise RuntimeError("private storage details")
            return super().publications_for_version(key)

    ledger.__class__ = Broken
    _failure(
        ledger,
        ReadFailure.LINEAGE_MISSING_OR_MISMATCHED
        if stage == "lineage"
        else ReadFailure.STORAGE_UNAVAILABLE,
        definition=BLS_CPI_HEADLINES.key,
    )


def test_operator_inspection_is_not_a_public_wire_mode():
    ledger, _kernel, _plan, _publication = _seed_daily()
    request = _as_of_request(RAW_DAILY_BARS.key, AT.isoformat()).replace(
        b'"mode":"as_of"', b'"mode":"inspect_as_of"'
    )
    assert handle_request(ledger, request) == (
        b'{"classification":"invalid_request","status":"error",'
        b'"wire_revision":"mdf.consumer.read.v1"}'
    )


def test_invalid_coverage_cannot_leak_through_a_valid_publication():
    ledger, _kernel, plan, _publication = _seed_daily()
    _store(ledger, replace(plan.version, finite_scope="private-invalid-coverage"))
    _failure(ledger, ReadFailure.LINEAGE_MISSING_OR_MISMATCHED)


@pytest.mark.parametrize("case", ["duplicate", "reversed", "unresolved"])
def test_invalid_history_enumeration_returns_no_partial_projection(case):
    ledger, kernel, plan, publication = _seed_daily()
    kernel.clock = lambda: BASE + timedelta(seconds=4)
    terminal = kernel.publish(
        replace(
            plan.publication_request,
            identity="withdraw",
            occurrence="withdraw",
            decision=Decision.WITHDRAW,
            predecessor=publication.key,
        )
    )
    values = {
        "duplicate": (publication, publication),
        "reversed": (terminal, publication),
        "unresolved": (replace(publication, decision_at=BASE), terminal),
    }[case]

    class BadHistory(InMemoryLedger):
        def publications_for_version(self, _version):
            return values

    ledger.__class__ = BadHistory
    _failure(ledger, ReadFailure.PUBLICATION_STATE_INVALID)


def test_corrupt_second_candidate_rejects_entire_inspection():
    ledger, kernel, _plan, _publication = _seed_daily("a-valid")
    invalid = _daily_plan("z-invalid")
    persist_replay_before_publication(kernel, invalid)
    _store(ledger, replace(invalid.version, content_digest="private-invalid"))
    _failure(ledger, ReadFailure.LINEAGE_MISSING_OR_MISMATCHED)


def test_resolver_cannot_substitute_the_other_accepted_definition():
    class WrongDefinition(InMemoryLedger):
        def get(self, _key):
            return BLS_CPI_HEADLINES

    _failure(WrongDefinition(), ReadFailure.LINEAGE_MISSING_OR_MISMATCHED)


_CORRECTION_STATES = ("unpublished", "future", "eligible", "withdrawn", "superseded")


def _correction_seed(state):
    ledger, plan, publication = _seed_bound_cpi()
    at = AT
    if state == "unpublished":
        ledger._records.pop((Kind.PUBLICATION.value, publication.key.identity, ""))
    elif state == "future":
        at = BASE
    elif state in {"withdrawn", "superseded"}:
        decision = Decision.WITHDRAW if state == "withdrawn" else Decision.SUPERSEDE
        CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=5)).publish(
            replace(
                publication.request,
                identity=f"correction:{state}",
                occurrence=f"correction:{state}",
                decision=decision,
                predecessor=publication.key,
                supersedes_version=(
                    plan.version.key if decision == Decision.SUPERSEDE else None
                ),
            )
        )
    view = inspect_as_of(ledger, BLS_CPI_HEADLINES.key, as_of=at)
    assert (
        view.candidates[0].selection_state
        == {
            "unpublished": "no_publication",
            "future": "no_effective_publication",
            "eligible": "publication_eligible",
            "withdrawn": "version_withdrawn",
            "superseded": "version_superseded",
        }[state]
    )
    return ledger, plan, at


@pytest.mark.parametrize("state", _CORRECTION_STATES)
@pytest.mark.parametrize(
    "role,mutation",
    [
        (role, "identity")
        for role in ("binding", "source", "acquisition", "material", "run")
    ]
    + [("binding", "revision"), ("source", "revision")],
)
def test_correction_exact_resolver_keys_fail_closed(state, role, mutation):
    ledger, plan, at = _correction_seed(state)
    original = {
        "binding": plan.binding_candidate,
        "source": plan.source,
        "acquisition": plan.acquisition,
        "material": plan.raw_material,
        "run": ledger.get(plan.version.run),
    }[role]
    # Revision is part of Source/Binding identity.
    if mutation == "revision":
        substitute = replace(
            original,
            **{"revision" if role == "binding" else "contract_revision": "private-r2"},
        )
    else:
        substitute = replace(original, identity=f"private-substitute:{role}:{mutation}")
    requested = original.key
    ledger._records[(requested.kind.value, requested.identity, requested.revision)] = (
        dumps(substitute)
    )
    before = ledger.snapshot()
    _failure(
        ledger,
        ReadFailure.LINEAGE_MISSING_OR_MISMATCHED,
        definition=BLS_CPI_HEADLINES.key,
        as_of=at,
    )
    assert ledger.snapshot() == before


@pytest.mark.parametrize("state", _CORRECTION_STATES)
@pytest.mark.parametrize("clock", ["reversed", "naive_start", "naive_finish"])
def test_correction_invalid_datarun_clocks_fail_closed(state, clock):
    ledger, plan, at = _correction_seed(state)
    run = ledger.get(plan.version.run)
    invalid = {
        "reversed": replace(run, finished_at=run.started_at - timedelta(seconds=1)),
        "naive_start": replace(run, started_at=run.started_at.replace(tzinfo=None)),
        "naive_finish": replace(run, finished_at=run.finished_at.replace(tzinfo=None)),
    }[clock]
    _store(ledger, invalid)
    before = ledger.snapshot()
    _failure(
        ledger,
        ReadFailure.LINEAGE_MISSING_OR_MISMATCHED,
        definition=BLS_CPI_HEADLINES.key,
        as_of=at,
    )
    assert ledger.snapshot() == before


@pytest.mark.parametrize("finished", ["equal", "later", "unfinished"])
def test_correction_valid_datarun_clock_boundaries_are_preserved(finished):
    ledger, plan, at = _correction_seed("eligible")
    before = inspect_as_of(ledger, BLS_CPI_HEADLINES.key, as_of=at)
    run = ledger.get(plan.version.run)
    _store(
        ledger,
        replace(
            run,
            finished_at={
                "equal": run.started_at,
                "later": run.started_at + timedelta(seconds=1),
                "unfinished": None,
            }[finished],
        ),
    )
    assert inspect_as_of(ledger, BLS_CPI_HEADLINES.key, as_of=at) == before


@pytest.mark.parametrize("case", ["identity", "clock"])
def test_correction_corrupt_second_candidate_never_returns_first_candidate(case):
    ledger, kernel, _plan, _publication = _seed_daily("a-valid")
    other = _daily_plan("z-invalid")
    persist_replay_before_publication(kernel, other)
    publish_replay(kernel, other)
    run = ledger.get(other.version.run)
    invalid = (
        replace(run, identity="private-substitute-run")
        if case == "identity"
        else replace(run, finished_at=run.started_at - timedelta(seconds=1))
    )
    ledger._records[(run.key.kind.value, run.key.identity, run.key.revision)] = dumps(
        invalid
    )
    _failure(ledger, ReadFailure.LINEAGE_MISSING_OR_MISMATCHED)


@pytest.mark.parametrize(
    "case", ["producer_identity", "producer_clock", "basis_identity", "basis_digest"]
)
def test_correction_transitive_assessment_lineage_is_fully_validated(case):
    ledger, plan, publication = _seed_bound_cpi()
    run = replace(plan.run, identity="assessment-run")
    basis = replace(
        plan.raw_material,
        identity="assessment-basis",
        acquired_from=None,
        produced_by=run.key,
    )
    assessment = replace(
        basis,
        identity="assessment-evidence",
        grade=Grade.ASSESSMENT,
        assessment=Assessment(
            target=plan.version.key,
            rule_revision="synthetic-r1",
            evaluated_at=BASE + timedelta(seconds=3),
            result="synthetic",
            basis=(basis.key,),
        ),
    )
    for record in (run, basis, assessment):
        _store(ledger, record)
    _store(
        ledger,
        replace(
            publication,
            request=replace(
                publication.request,
                evidence=(*publication.request.evidence, assessment.key),
            ),
        ),
    )
    # Legitimate assessment -> target Version backrefs must not recurse forever.
    assert inspect_as_of(ledger, BLS_CPI_HEADLINES.key, as_of=AT).selection_outcome == (
        "one_publication_candidate"
    )
    original = run if case.startswith("producer") else basis
    invalid = {
        "producer_identity": replace(run, identity="private-wrong-producer"),
        "producer_clock": replace(
            run, finished_at=run.started_at - timedelta(seconds=1)
        ),
        "basis_identity": replace(basis, identity="private-wrong-basis"),
        "basis_digest": replace(basis, digest="private-invalid-digest"),
    }[case]
    key = original.key
    ledger._records[(key.kind.value, key.identity, key.revision)] = dumps(invalid)
    _failure(
        ledger,
        ReadFailure.LINEAGE_MISSING_OR_MISMATCHED,
        definition=BLS_CPI_HEADLINES.key,
    )
