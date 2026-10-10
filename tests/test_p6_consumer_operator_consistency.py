"""Same synthetic graph/time across delivered consumer and Operator surfaces."""

import json
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, timedelta, timezone
from itertools import count

import psycopg
import pytest

from market_data_foundation.canonical import operator_web
from market_data_foundation.canonical.codec import dumps
from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    ReadFailure,
    read_as_of,
    read_exact_version,
)
from market_data_foundation.canonical.consumer_wire import handle_request
from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import Decision, Kind
from market_data_foundation.canonical.operator_reads import MemoryOperatorReads
from market_data_foundation.canonical.operator_report import render_operator_report
from market_data_foundation.canonical.operator_wsgi import OperatorWSGIGateway
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.domain_contracts.dense_market import RAW_DAILY_BARS
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from market_data_foundation.historical import bls_cpi_direct as cpi
from market_data_foundation.historical.daily_bars import (
    persist_replay_before_publication,
)
from tests.test_p6_consumer_read_postgres_integration import (
    BASE,
    _cpi_plan,
    _daily_bars_plan,
)
from tests.test_p6_consumer_wire import _as_of_request, _exact_request
from tests.test_p6_operator_wsgi import _call, _environ

SELECTORS = ("bls-cpi-headlines", "raw-daily-bars")
DEFINITIONS = (BLS_CPI_HEADLINES, RAW_DAILY_BARS)
MICROSECOND = timedelta(microseconds=1)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("cross-surface deterministic proof must remain offline")

    monkeypatch.setattr("socket.socket", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(psycopg.Connection, "connect", forbidden)


@contextmanager
def memory_scope(ledger):
    before = ledger.snapshot()
    yield ledger
    assert ledger.snapshot() == before


def build_graph(kernel, write_scope, anchor):
    """Accepted kernel creates both graphs; no injected publication records."""
    plans = (_cpi_plan(), _daily_bars_plan())
    replacements = tuple(
        replace(
            p.version,
            identity=p.version.identity + ":replacement",
            supersedes=p.version.key,
        )
        for p in plans
    )
    with write_scope():
        cpi.persist_live_plan_before_publication(kernel, plans[0])
        persist_replay_before_publication(kernel, plans[1])
        for plan, version in zip(plans, replacements, strict=True):
            kernel.persist_version(version, plan.content)

    histories = [[], []]
    replacement_publications = []
    for decision, usable, effective in (
        (Decision.PUBLISH, 2, 3),
        (Decision.WITHDRAW, 5, 6),
        (Decision.RESTORE, 8, 9),
    ):
        for index, plan in enumerate(plans):
            # Bound CPI evidence/grades/replay claims apply to every decision.
            request = replace(
                plan.publication_request,
                identity=f"cross:{index}:{decision.value}",
                occurrence=f"cross-occurrence:{index}:{decision.value}",
                decision=decision,
                predecessor=histories[index][-1].key if histories[index] else None,
                usable_at=anchor + timedelta(seconds=usable),
                effective_at=anchor + timedelta(seconds=effective),
            )
            histories[index].append(kernel.publish(request))
    for index, plan in enumerate(plans):
        # A new PUBLISH is distinct from the old Version's SUPERSEDE event.
        replacement_publications.append(
            kernel.publish(
                replace(
                    plan.publication_request,
                    identity=f"cross:{index}:replacement",
                    occurrence=f"cross-occurrence:{index}:replacement",
                    version=replacements[index].key,
                    usable_at=anchor + timedelta(seconds=11),
                    effective_at=anchor + timedelta(seconds=12),
                )
            )
        )
        histories[index].append(
            kernel.publish(
                replace(
                    plan.publication_request,
                    identity=f"cross:{index}:supersede",
                    occurrence=f"cross-occurrence:{index}:supersede",
                    decision=Decision.SUPERSEDE,
                    predecessor=histories[index][-1].key,
                    supersedes_version=plan.version.key,
                    usable_at=anchor + timedelta(seconds=14),
                    effective_at=anchor + timedelta(seconds=15),
                )
            )
        )
    return plans, replacements, histories, replacement_publications, anchor


@pytest.fixture
def graph():
    ledger = InMemoryLedger()
    ticks = count(5)
    kernel = CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=next(ticks)))
    graph = build_graph(kernel, nullcontext, BASE + timedelta(minutes=1))
    return ledger, graph


def _outcome(call):
    try:
        return call()
    except ReadContractError as error:
        assert error.args == (error.classification.value,)
        assert error.__cause__ is error.__context__ is None
        return error.classification


def _expect(actual, expected):
    if isinstance(expected, ReadFailure):
        assert actual == expected
    else:
        version, publication, content = expected
        assert not isinstance(actual, ReadFailure)
        assert (actual.version.key, actual.publication.key, actual.content) == (
            version,
            publication,
            content,
        )


def probe(consumer_scope, operator_scope, index, at, rows, exact, asof, monkeypatch):
    """Real inspection/render are observed, never replaced with test results.

    All consumer calls and the WSGI request use exactly the same Definition/time.
    Caller freezes storage between scopes. Rows are independent explicit oracles.
    """
    definition = DEFINITIONS[index].key
    timestamp = (
        at.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    requests = [
        *(_exact_request(definition, key.identity, as_of=timestamp) for key in exact),
        _as_of_request(definition, timestamp),
    ]
    with consumer_scope() as ledger:
        outcomes = [
            *(
                _outcome(
                    lambda key=key: read_exact_version(
                        ledger, definition, key, as_of=at
                    )
                )
                for key in exact
            ),
            _outcome(lambda: read_as_of(ledger, definition, as_of=at)),
        ]
        for actual, expected in zip(outcomes, (*exact.values(), asof), strict=True):
            _expect(actual, expected)
        wire_before = [handle_request(ledger, request) for request in requests]
        for raw, actual in zip(wire_before, outcomes, strict=True):
            wire = json.loads(raw)
            assert wire["wire_revision"] == "mdf.consumer.read.v1"
            if isinstance(actual, ReadFailure):
                assert wire == {
                    "wire_revision": "mdf.consumer.read.v1",
                    "status": "error",
                    "classification": actual.value,
                }
            else:
                assert wire["status"] == "ok"
                assert wire["view"]["version"]["identity"] == actual.version.identity
                assert wire["view"]["publication"]["request"]["identity"] == (
                    actual.publication.request.identity
                )

    statuses, failures, inspections = [], [], []
    real_inspect = operator_web.inspect_operator_status
    capability = object()  # Synthetic host capability, never request authentication.

    def inspect(scope, key, *, as_of):
        inspections.append((key, as_of))
        assert (key, as_of) == (definition, at)
        try:
            status = real_inspect(scope, key, as_of=as_of)
        except ReadContractError as error:
            failures.append(error.classification)
            raise
        statuses.append(status)
        return status

    def no_content(*args, **kwargs):
        pytest.fail("Operator metadata eligibility must not read consumer content")

    with monkeypatch.context() as patch:
        patch.setattr(operator_web, "inspect_operator_status", inspect)
        patch.setattr(InMemoryLedger, "get_content", no_content)
        patch.setattr(PostgresLedger, "get_content", no_content)
        app = OperatorWSGIGateway(
            operator_web.OperatorWebEntry(
                authorize=lambda context, key, instant: (
                    context is capability and (key, instant) == (definition, at)
                ),
                snapshot=operator_scope,
            ),
            context_factory=lambda: capability,
        )
        response = _call(
            app,
            _environ(
                SELECTORS[index],
                QUERY_STRING=f"definition={SELECTORS[index]}&as_of={timestamp}",
            ),
        )
    diagnostic = {
        "http_status": response[0],
        "inspection_calls": len(inspections),
        "failure_classifications": tuple(failure.value for failure in failures),
        "status_count": len(statuses),
    }
    assert inspections == [(definition, at)], diagnostic
    if isinstance(rows, ReadFailure):
        assert failures == [rows] and statuses == []
        assert response[0] == "503 Service Unavailable"
        assert response[2] == b"operator_status_unavailable\n"
    else:
        assert failures == [] and len(statuses) == 1, diagnostic
        status = statuses[0]
        assert (status.definition.key, status.as_of.as_of) == (definition, at)
        assert {
            item.version: (item.selection_state, item.effective_publication)
            for item in status.as_of.candidates
        } == rows
        eligible = [
            key for key, (state, _) in rows.items() if state == "publication_eligible"
        ]
        expected_selection = (
            "as_of_no_version"
            if not eligible
            else "one_publication_candidate"
            if len(eligible) == 1
            else "as_of_ambiguous"
        )
        assert status.as_of.selection_outcome == expected_selection
        assert status.content_integrity == status.current_availability == "unobservable"
        assert status.binding_activation == status.external_admission == "unobservable"
        for view in outcomes:
            if isinstance(view, ReadFailure):
                continue
            candidate = next(
                c for c in status.as_of.candidates if c.version == view.version.key
            )
            assert candidate.binding_record == (
                view.binding.key if view.binding else None
            )
        for candidate in status.as_of.candidates:
            if candidate.selection_state == "publication_eligible":
                expected_read = exact[candidate.version]
                if not isinstance(expected_read, ReadFailure):
                    assert candidate.effective_publication == expected_read[1]
            for publication in candidate.publications:
                assert publication.pending_clocks == tuple(
                    name
                    for name, clock in (
                        ("decision_at", publication.decision_at),
                        ("usable_at", publication.usable_at),
                        ("effective_at", publication.effective_at),
                    )
                    if clock is not None and clock > at
                )
        assert response[0] == "200 OK"
        assert response[2] == render_operator_report(status).encode()
    with consumer_scope() as ledger:
        assert [handle_request(ledger, request) for request in requests] == wire_before
    return statuses[0] if statuses else None


def check_timeline(graph, index, consumer_scope, operator_scope, monkeypatch):
    plans, replacements, histories, replacement_pubs, anchor = graph
    plan, new = plans[index], replacements[index]
    publish, withdraw, restore, supersede = histories[index]
    new_pub = replacement_pubs[index]
    # Equality is inclusive. A pending later decision must preserve the previous
    # effective event; it must not erase or accelerate it.
    cases = [
        (publish.decision_at - MICROSECOND, "no_effective_publication", None),
        (publish.decision_at, "no_effective_publication", None),
        (publish.usable_at - MICROSECOND, "no_effective_publication", None),
        (publish.usable_at, "no_effective_publication", None),
        (anchor + timedelta(seconds=3) - MICROSECOND, "no_effective_publication", None),
        (anchor + timedelta(seconds=3), "publication_eligible", publish.key),
        (withdraw.usable_at - MICROSECOND, "publication_eligible", publish.key),
        (withdraw.usable_at, "publication_eligible", publish.key),
        (
            anchor + timedelta(seconds=6) - MICROSECOND,
            "publication_eligible",
            publish.key,
        ),
        (anchor + timedelta(seconds=6), "version_withdrawn", withdraw.key),
        (restore.usable_at - MICROSECOND, "version_withdrawn", withdraw.key),
        (restore.usable_at, "version_withdrawn", withdraw.key),
        (
            anchor + timedelta(seconds=9) - MICROSECOND,
            "version_withdrawn",
            withdraw.key,
        ),
        (anchor + timedelta(seconds=9), "publication_eligible", restore.key),
        (new_pub.usable_at - MICROSECOND, "publication_eligible", restore.key),
        (new_pub.usable_at, "publication_eligible", restore.key),
        (
            anchor + timedelta(seconds=12) - MICROSECOND,
            "publication_eligible",
            restore.key,
        ),
        (anchor + timedelta(seconds=12), "publication_eligible", restore.key),
        (supersede.usable_at - MICROSECOND, "publication_eligible", restore.key),
        (supersede.usable_at, "publication_eligible", restore.key),
        (
            anchor + timedelta(seconds=15) - MICROSECOND,
            "publication_eligible",
            restore.key,
        ),
        (anchor + timedelta(seconds=15), "version_superseded", supersede.key),
    ]
    for at, state, publication in cases:
        new_active = at >= anchor + timedelta(seconds=12)
        old_read = (
            (plan.version.key, publication, plan.content)
            if state == "publication_eligible"
            else ReadFailure.UNPUBLISHED_OR_UNAVAILABLE_AS_OF
            if state == "no_effective_publication"
            else ReadFailure(state)
        )
        new_read = (
            (new.key, new_pub.key, plan.content)
            if new_active
            else ReadFailure.UNPUBLISHED_OR_UNAVAILABLE_AS_OF
        )
        probe(
            consumer_scope,
            operator_scope,
            index,
            at,
            {
                plan.version.key: (state, publication),
                new.key: ("publication_eligible", new_pub.key)
                if new_active
                else ("no_effective_publication", None),
            },
            {plan.version.key: old_read, new.key: new_read},
            ReadFailure.AS_OF_AMBIGUOUS
            if new_active and state == "publication_eligible"
            else new_read
            if new_active
            else old_read
            if state == "publication_eligible"
            else ReadFailure.AS_OF_NO_VERSION,
            monkeypatch,
        )


@pytest.mark.parametrize("index", [0, 1])
def test_same_graph_exact_time_lifecycle(graph, index, monkeypatch):
    ledger, data = graph
    check_timeline(
        data,
        index,
        lambda: memory_scope(ledger),
        lambda: memory_scope(ledger),
        monkeypatch,
    )


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize(
    "offset", [timedelta(hours=8), -timedelta(hours=5, minutes=30)]
)
def test_non_utc_driver_times_keep_same_instant_and_reach_inspection(
    graph, index, offset, monkeypatch
):
    ledger, data = graph
    plans, replacements, histories, replacement_pubs, anchor = data
    zone = timezone(offset)

    def driver_time(publication):
        return replace(
            publication,
            decision_at=publication.decision_at.astimezone(zone),
            usable_at=publication.usable_at.astimezone(zone),
        )

    at = histories[index][0].decision_at.astimezone(zone)
    old_timestamp = at.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def forbidden_context():
        pytest.fail("noncanonical timestamp must be rejected before host context")

    # Negative control: the original fixture's target fails admission before
    # authorization/snapshot/inspection, even though the datetime is valid.
    response = _call(
        OperatorWSGIGateway(context_factory=forbidden_context),
        _environ(
            SELECTORS[index],
            QUERY_STRING=f"definition={SELECTORS[index]}&as_of={old_timestamp}",
        ),
    )
    assert response[0] == "400 Bad Request"
    assert response[2] == b"invalid_as_of\n"
    offset_graph = (
        plans,
        replacements,
        [
            [driver_time(publication) for publication in history]
            for history in histories
        ],
        [driver_time(publication) for publication in replacement_pubs],
        anchor.astimezone(zone),
    )
    # Change only datetime representations. Keep canonical graph bytes, every
    # Version/Publication key, microsecond boundary and full probe oracle.
    check_timeline(
        offset_graph,
        index,
        lambda: memory_scope(ledger),
        lambda: memory_scope(ledger),
        monkeypatch,
    )


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("state", ["empty", "unpublished"])
def test_no_selection_or_arbitrary_version_fallback(graph, index, state, monkeypatch):
    _, data = graph
    plans, replacements, _, _, anchor = data
    plan = plans[index]
    at = anchor + timedelta(seconds=12)
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=5))
    for definition in DEFINITIONS:
        kernel.add(definition)
    if state == "empty":
        rows, exact = {}, {}
        asof = ReadFailure.AS_OF_NO_VERSION
    elif state == "unpublished":
        cpi.persist_live_plan_before_publication(kernel, plans[0])
        persist_replay_before_publication(kernel, plans[1])
        for p, version in zip(plans, replacements, strict=True):
            kernel.persist_version(version, p.content)
        rows = {
            v.key: ("no_publication", None) for v in (plan.version, replacements[index])
        }
        exact = {key: ReadFailure.UNPUBLISHED_OR_UNAVAILABLE_AS_OF for key in rows}
        asof = ReadFailure.AS_OF_NO_VERSION

    def scope():
        return memory_scope(ledger)

    probe(scope, scope, index, at, rows, exact, asof, monkeypatch)


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize(
    "fault,failure,operator_failure",
    [
        ("content_missing", ReadFailure.CONTENT_MISSING, None),
        ("content_corrupt", ReadFailure.CONTENT_DIGEST_MISMATCH, None),
        ("content_storage", ReadFailure.STORAGE_UNAVAILABLE, None),
        (
            "lineage",
            ReadFailure.LINEAGE_MISSING_OR_MISMATCHED,
            ReadFailure.LINEAGE_MISSING_OR_MISMATCHED,
        ),
        (
            "history",
            ReadFailure.PUBLICATION_STATE_INVALID,
            ReadFailure.PUBLICATION_STATE_INVALID,
        ),
        (
            "metadata_storage",
            ReadFailure.STORAGE_UNAVAILABLE,
            ReadFailure.STORAGE_UNAVAILABLE,
        ),
        (
            "definition_missing",
            ReadFailure.DEFINITION_NOT_FOUND,
            ReadFailure.DEFINITION_NOT_FOUND,
        ),
    ],
)
def test_classified_failures_and_content_non_equivalence(
    graph, index, fault, failure, operator_failure, monkeypatch
):
    ledger, data = graph
    plans, replacements, histories, _, anchor = data
    plan = plans[index]
    if fault == "content_missing":
        ledger._contents.pop(plan.version.identity)
    elif fault == "content_corrupt":
        ledger._contents[plan.version.identity] = (
            plan.version.content_digest,
            b"corrupt",
        )
    elif fault in {"content_storage", "metadata_storage"}:

        def unavailable(*args):
            raise RuntimeError("synthetic-private-error-sentinel")

        monkeypatch.setattr(
            ledger, "get_content" if fault == "content_storage" else "get", unavailable
        )
        if fault == "metadata_storage":
            monkeypatch.setattr(MemoryOperatorReads, "get", unavailable)
    elif fault == "lineage":
        key = plan.version.materials[0]
        ledger._records.pop((key.kind.value, key.identity, key.revision))
    elif fault == "definition_missing":
        key = plan.version.definition
        ledger._records.pop((key.kind.value, key.identity, key.revision))
    else:
        publication = histories[index][1]
        ledger._records[
            (Kind.PUBLICATION.value, publication.key.identity, publication.key.revision)
        ] = dumps(
            replace(publication, request=replace(publication.request, predecessor=None))
        )

    def scope():
        return memory_scope(ledger)

    status = probe(
        scope,
        scope,
        index,
        anchor + timedelta(seconds=9),
        operator_failure
        or {
            plan.version.key: ("publication_eligible", histories[index][2].key),
            replacements[index].key: ("no_effective_publication", None),
        },
        {plan.version.key: failure},
        failure,
        monkeypatch,
    )
    if operator_failure is None:
        assert status.as_of.selection_outcome == "one_publication_candidate"
