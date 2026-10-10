"""Offline report behavior and hostile metadata; no real services or content IO."""

import hashlib
import re
import socket
import sys
from dataclasses import replace
from datetime import timedelta, timezone
from html import escape
from html.parser import HTMLParser
from itertools import product
from time import process_time

import pytest

from market_data_foundation.canonical import operator_report as report
from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    ReadFailure,
)
from market_data_foundation.canonical.consumer_wire import handle_request
from market_data_foundation.canonical.model import Decision, Key, Kind
from market_data_foundation.canonical.operator_status import inspect_operator_status
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.domain_contracts.dense_market import RAW_DAILY_BARS
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from tests.test_p6_consumer_read import BASE, _seed_bound_cpi, _seed_daily
from tests.test_p6_consumer_wire import _as_of_request
from tests.test_p6_operator_asof import AT, _store
from tests.test_p6_operator_snapshot import SnapshotDouble
from tests.test_p6_operator_snapshot import _source as snapshot_source


def _status(ledger, definition=BLS_CPI_HEADLINES, at=AT):
    return inspect_operator_status(ledger, definition.key, as_of=at)


def _error(value, classification="operator_report_invalid_status"):
    with pytest.raises(report.OperatorReportError) as caught:
        report.render_operator_report(value)
    assert caught.value.args == (classification,)
    assert caught.value.__cause__ is caught.value.__context__ is None
    return caught.value


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.attributes = []
        self.data = []
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.append(dict(attrs))

    def handle_data(self, data):
        self.data.append(data)


@pytest.mark.parametrize("definition", [BLS_CPI_HEADLINES, RAW_DAILY_BARS])
def test_empty_and_stored_only_are_browsable_distinct_states(definition):
    ledger = InMemoryLedger()
    ledger.put(definition)
    empty = report.render_operator_report(_status(ledger, definition))
    assert "as_of_no_version" in empty
    assert "No Version records for this Definition." in empty
    assert "Stored-only (0)" in empty
    _, plan, _ = _seed_bound_cpi()
    ledger.put(plan.source)
    ledger.put(
        replace(
            plan.binding_candidate,
            mappings=(
                replace(plan.binding_candidate.mappings[0], definition=definition.key),
            ),
        )
    )
    html = report.render_operator_report(_status(ledger, definition))
    assert "Stored-only (1)" in html and "Record state: stored" in html
    assert "Referenced, no eligible Publication (0)" in html
    assert "Publication eligible (0)" in html
    assert "No Version records for this Definition." in html


def _mixed():
    ledger, plan, publication = _seed_bound_cpi()
    unused = replace(plan.binding_candidate, revision="synthetic-stored-only")
    ledger.put(unused)
    referenced = replace(plan.binding_candidate, revision="synthetic-unpublished")
    ledger.put(referenced)
    run = replace(plan.run, identity="unpublished-run", binding=referenced.key)
    ledger.put(run)
    version = replace(
        plan.version,
        identity="a-unpublished",
        binding=referenced.key,
        run=run.key,
        acquisitions=(),
        materials=(),
    )
    # Start with valid canonical lineage; each evidence graph gets an exact new
    # acquisition/material association for the new binding revision.
    acquisition = replace(
        plan.acquisition,
        identity="unpublished-acquisition",
        run=run.key,
        binding=referenced.key,
    )
    ledger.put(acquisition)
    materials = tuple(
        replace(m, identity=f"unpublished-{n}", acquired_from=acquisition.key)
        for n, m in enumerate((plan.raw_material, plan.semantic_material))
    )
    for material in materials:
        ledger.put(material)
    version = replace(
        version,
        acquisitions=(acquisition.key,),
        materials=tuple(m.key for m in materials),
    )
    _store(ledger, version)
    # An additional eligible Version retains the first exact binding. Ambiguity
    # must survive grouping, rather than selecting a latest/first candidate.
    other = replace(plan.version, identity="z-eligible")
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
    return ledger, plan, unused, referenced


def test_groups_exact_revisions_ambiguity_navigation_and_determinism():
    ledger, plan, unused, referenced = _mixed()
    before = ledger.snapshot()
    view = _status(ledger)
    html = report.render_operator_report(view)
    assert "Stored-only (1)" in html
    assert "Referenced, no eligible Publication (1)" in html
    assert "Publication eligible (1)" in html
    for key in (unused.key, referenced.key, plan.binding_candidate.key):
        assert f"revision: {key.revision}" in html
    assert "as_of_ambiguous" in html and "no Version selected" in html
    page = Page(html)
    ids = {attrs["id"] for attrs in page.attributes if "id" in attrs}
    links = [attrs["href"] for attrs in page.attributes if "href" in attrs]
    assert len(ids) == 7  # Four sections plus three exact Version anchors.
    assert all(link.startswith("#") and link[1:] in ids for link in links)
    assert len([link for link in links if link.startswith("#version-")]) == 5
    assert ledger.snapshot() == before
    restored = InMemoryLedger.from_snapshot(before)
    restored._records = dict(reversed(tuple(restored._records.items())))
    assert report.render_operator_report(_status(restored)) == html
    assert report.render_operator_report(view) == html
    # Stable bytes for the empty admitted fixture catch unexpected generated
    # timestamps, assets or formatting changes in the standalone artifact.
    empty = InMemoryLedger()
    empty.put(BLS_CPI_HEADLINES)
    assert hashlib.sha256(
        report.render_operator_report(_status(empty)).encode()
    ).hexdigest() == (
        "bb932515d833767c118cda55f096a350821b3bc698f97fe73575eab2af80aacc"
    )


@pytest.mark.parametrize(
    "seconds,pending",
    [
        (3, "decision_at, usable_at, effective_at"),
        (4, "usable_at, effective_at"),
        (5, "effective_at"),
        (6, "none"),
    ],
)
def test_three_clock_boundaries_are_presented_as_of(seconds, pending):
    ledger, _, publication = _seed_bound_cpi()
    _store(
        ledger,
        replace(
            publication,
            decision_at=BASE + timedelta(seconds=4),
            usable_at=BASE + timedelta(seconds=5),
            request=replace(
                publication.request, effective_at=BASE + timedelta(seconds=6)
            ),
        ),
    )
    view = _status(ledger, at=BASE + timedelta(seconds=seconds))
    html = report.render_operator_report(view)
    assert f"Pending at requested instant</dt><dd>{pending}" in html
    assert (
        "Publication eligible (1)"
        if seconds == 6
        else "Referenced, no eligible Publication (1)"
    ) in html
    assert (
        "publication_eligible" if seconds == 6 else "no_effective_publication"
    ) in html
    for clock in ("decision_at", "usable_at", "effective_at"):
        assert f"{clock} (UTC)" in html
    same_instant = replace(
        view,
        as_of=replace(
            view.as_of, as_of=view.as_of.as_of.astimezone(timezone(timedelta(hours=8)))
        ),
    )
    assert report.render_operator_report(same_instant) == html


@pytest.mark.parametrize(
    "decision", [Decision.WITHDRAW, Decision.RESTORE, Decision.SUPERSEDE]
)
def test_terminal_and_restored_states_are_visible(decision):
    ledger, plan, publication = _seed_bound_cpi()
    steps = (
        (Decision.WITHDRAW, decision) if decision == Decision.RESTORE else (decision,)
    )
    for n, step in enumerate(steps):
        at = BASE + timedelta(seconds=7 + n)
        publication = replace(
            publication,
            request=replace(
                plan.publication_request,
                identity=f"transition:{n}",
                occurrence=f"transition:{n}",
                decision=step,
                predecessor=publication.key,
                supersedes_version=plan.version.key
                if step == Decision.SUPERSEDE
                else None,
            ),
            decision_at=at,
            usable_at=at,
        )
        _store(ledger, publication)
    html = report.render_operator_report(_status(ledger))
    state = {
        Decision.WITHDRAW: "version_withdrawn",
        Decision.RESTORE: "publication_eligible",
        Decision.SUPERSEDE: "version_superseded",
    }[decision]
    assert f"Publication selection state: {state}" in html
    assert f"Decision</dt><dd>{decision.value}" in html


def test_unbound_unknown_and_renderer_has_no_io_or_wire_effect(monkeypatch):
    ledger, _, plan, _ = _seed_daily()
    request = _as_of_request(RAW_DAILY_BARS.key, AT.isoformat())
    wire = handle_request(ledger, request)
    _store(ledger, replace(plan.version, completeness="unknown"))
    view = _status(ledger, RAW_DAILY_BARS)
    before = ledger.snapshot()

    def forbidden(*_args, **_kwargs):
        pytest.fail("renderer attempted IO")

    for name in (
        "get",
        "get_content",
        "put",
        "put_content",
        "publish",
        "operator_reads",
    ):
        monkeypatch.setattr(ledger, name, forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr("builtins.open", forbidden)
    html = report.render_operator_report(view)
    assert "Versions without binding record" in html and "none recorded" in html
    assert "Completeness claim: unknown (recorded)" in html
    for name in report._UNKNOWN_FIELDS:
        assert f"<dt>{name}</dt><dd>unobservable</dd>" in html
    assert ledger.snapshot() == before
    restored = InMemoryLedger.from_snapshot(_seed_daily()[0].snapshot())
    assert handle_request(restored, request) == wire
    invalid = request.replace(b'"mode":"as_of"', b'"mode":"operator_report"')
    assert b'"classification":"invalid_request"' in handle_request(restored, invalid)


@pytest.mark.parametrize(
    "payload",
    [
        "<script>alert('x')</script>",
        '<img src=x onerror="alert(1)">',
        '</code>{"private":"<&\\""}</code>',
        "&lt;script&gt;",
        "中文😀 & ' \"",
    ],
)
def test_key_text_html_json_injection_is_text_only(payload):
    ledger, _, _ = _seed_bound_cpi()
    view = _status(ledger)
    row = view.stored_bindings[0]
    # Unreferenced stored revision permits malicious opaque identifier text to
    # pass a genuine full inspection; no forged projection is needed here.
    ledger.put(replace(_seed_bound_cpi()[1].binding_candidate, identity=payload))
    html = report.render_operator_report(_status(ledger))
    page = Page(html)
    assert payload in "".join(page.data)
    assert escape(payload, quote=True) in html
    assert not {
        "script",
        "img",
        "iframe",
        "object",
        "embed",
        "form",
        "link",
        "base",
    }.intersection(page.tags)
    for attrs in page.attributes:
        assert not any(
            name.startswith("on") or name in {"src", "srcset"} for name in attrs
        )
        if "href" in attrs:
            assert attrs["href"].startswith("#")
    assert row.candidate.binding.identity in html
    assert "default-src 'none'" in html and "form-action 'none'" in html


def test_provider_free_text_and_private_claims_are_never_serialized():
    ledger, plan, _ = _seed_bound_cpi()
    marker = "s3://synthetic-private/receipt?token=synthetic-token <script>bad</script>"
    source = replace(
        plan.source,
        owner=marker,
        product=marker,
        operation=marker,
        temporal_contract=marker,
        freshness_contract=marker,
    )
    _store(ledger, source)
    _store(
        ledger,
        replace(
            plan.binding_candidate, limitations=(marker,), compatibility_policy=marker
        ),
    )
    _store(ledger, replace(plan.version, completeness=marker))
    view = _status(ledger)
    assert marker in repr(view)
    html = report.render_operator_report(view)
    for private in (
        marker,
        escape(marker),
        "synthetic-private",
        "synthetic-token",
        "controlled-receipt",
        "mdf-content://",
        "controlled_ref",
        "raw_bytes",
    ):
        assert private not in html
    assert "recorded value withheld" in html


@pytest.mark.parametrize(
    "payload",
    [
        "s3://synthetic-private/receipt",
        "https://private.invalid/x",
        "file:///tmp/synthetic-private",
        "/tmp/synthetic-private",
        "C:\\private\\x",
        "x\x00y",
        "x\u202ey",
        "x\ud800y",
    ],
)
def test_locator_or_invisible_key_text_fails_closed(payload):
    ledger, plan, _ = _seed_bound_cpi()
    ledger.put(replace(plan.binding_candidate, identity=payload))
    _error(_status(ledger))


def test_encoded_budget_inclusive_and_overflow_returns_no_partial(monkeypatch):
    ledger, _, _ = _seed_bound_cpi()
    # Unicode and HTML entities count by encoded bytes, not source characters.
    ledger.put(replace(_seed_bound_cpi()[1].binding_candidate, identity="中文<&😀"))
    view = _status(ledger)
    expected = report.render_operator_report(view)
    size = len(expected.encode("utf-8"))
    assert size > len(expected)
    monkeypatch.setattr(report, "MAX_REPORT_BYTES", size)
    assert report.render_operator_report(view) == expected
    monkeypatch.setattr(report, "MAX_REPORT_BYTES", size - 1)
    _error(view, "operator_report_budget_exceeded")


def test_real_fixed_aggregate_output_limit_and_string_limit():
    ledger, plan, _ = _seed_bound_cpi()
    for number in range(127):
        ledger.put(
            replace(plan.binding_candidate, identity=f"{number:03d}-" + "😀" * 600)
        )
    view = _status(ledger)
    assert len(view.stored_bindings) == 128
    _error(view, "operator_report_budget_exceeded")
    row = view.stored_bindings[0]
    huge = replace(
        row,
        candidate=replace(row.candidate, binding=Key(Kind.BINDING, "x" * 16385, "r1")),
    )
    _error(replace(view, stored_bindings=(huge,)), "operator_report_budget_exceeded")


@pytest.mark.parametrize(
    "fault",
    [
        "availability",
        "association",
        "eligible",
        "outcome",
        "clock",
        "missing_binding",
        "duplicate",
        "unbound",
        "oversize",
    ],
)
def test_forged_presentation_states_do_not_promote_or_hide_evidence(fault):
    ledger, _, _ = _seed_bound_cpi()
    view = _status(ledger)
    row = view.stored_bindings[0]
    version = view.as_of.candidates[0]
    if fault == "availability":
        view = replace(view, current_availability="available")
    elif fault in {"association", "eligible"}:
        row = replace(
            row,
            **{
                "referencing_versions"
                if fault == "association"
                else "publication_candidates": ()
            },
        )
        view = replace(view, stored_bindings=(row,))
    elif fault == "outcome":
        view = replace(
            view, as_of=replace(view.as_of, selection_outcome="as_of_no_version")
        )
    elif fault == "clock":
        publication = replace(
            version.publications[0], pending_clocks=("private-clock",)
        )
        view = replace(
            view,
            as_of=replace(
                view.as_of, candidates=(replace(version, publications=(publication,)),)
            ),
        )
    elif fault == "missing_binding":
        view = replace(view, stored_bindings=())
    elif fault == "duplicate":
        view = replace(view, stored_bindings=(row, row))
    elif fault == "unbound":
        view = replace(view, versions_without_binding_record=(version.version,))
    else:
        view = replace(view, stored_bindings=(row,) * 129)
    _error(
        view,
        "operator_report_budget_exceeded"
        if fault == "oversize"
        else "operator_report_invalid_status",
    )


def test_bad_type_or_private_exception_is_classified_without_message():
    _error(None)

    class PrivateFailure:
        @property
        def key(self):
            raise RuntimeError("synthetic-private-exception")

    ledger, _, _ = _seed_bound_cpi()
    _error(replace(_status(ledger), definition=PrivateFailure()))


@pytest.mark.parametrize("failure_kind", ["base", "subclass", "str", "args"])
@pytest.mark.parametrize(
    "message", ["synthetic-private-exception", "operator_report_budget_exceeded"]
)
def test_input_report_errors_and_hostile_properties_never_escape(failure_kind, message):
    calls = []

    class HostileStringError(report.OperatorReportError):
        def __str__(self):
            calls.append("str")
            raise RuntimeError("synthetic-private-str-exception")

    class HostileArgsError(report.OperatorReportError):
        @property
        def args(self):
            calls.append("args")
            raise RuntimeError("synthetic-private-args-exception")

    class PrivateError(report.OperatorReportError):
        pass

    failure = (
        report.OperatorReportError(message)
        if failure_kind == "base"
        else PrivateError(message)
        if failure_kind == "subclass"
        else HostileStringError(message)
        if failure_kind == "str"
        else HostileArgsError(message)
    )

    class PrivateFailure:
        @property
        def key(self):
            raise failure from RuntimeError("synthetic-private-cause")

    ledger, _, _ = _seed_bound_cpi()
    error = _error(replace(_status(ledger), definition=PrivateFailure()))
    assert type(error) is report.OperatorReportError
    assert str(error) == "operator_report_invalid_status"
    assert calls == []


@pytest.mark.parametrize("site", ["definition", "source"])
@pytest.mark.parametrize(
    "origin",
    [
        "exact",
        "str",
        "args",
        "helper_bytes",
        "helper_text",
        "helper_rows",
        "replay",
        "nested",
    ],
)
def test_getter_budget_signal_requires_this_render_provenance(site, origin):
    ledger, _, _ = _seed_bound_cpi()
    view = _status(ledger)
    calls = []

    class HostileSignal(report._BudgetExceeded):
        def __str__(self):
            calls.append("str")
            raise RuntimeError("synthetic-private-string")

    class HostileArgs(report._BudgetExceeded):
        @property
        def args(self):
            calls.append("args")
            raise RuntimeError("synthetic-private-args")

    failure = (
        HostileSignal("synthetic-private-budget")
        if origin == "str"
        else HostileArgs("synthetic-private-budget")
        if origin == "args"
        else report._BudgetExceeded("synthetic-private-budget")
    )
    if origin == "replay":
        # An actual signal from earlier independent work is still input here.
        try:
            report._HTML().text("x" * 16385)
        except report._BudgetExceeded as prior:
            failure = prior

    class Getter:
        @property
        def key(self):
            if origin == "helper_bytes":
                report._HTML().add("x" * 262145)
            elif origin == "helper_text":
                report._HTML().text("x" * 16385)
            elif origin == "helper_rows":
                report._items((None,) * 129, 128, report._BudgetExceeded())
            elif origin == "nested":
                report.render_operator_report(
                    replace(view, stored_bindings=view.stored_bindings * 129)
                )
            raise failure from RuntimeError("synthetic-private-cause")

    if site == "definition":
        forged = replace(view, definition=Getter())
    else:
        row = view.stored_bindings[0]
        forged = replace(
            view,
            stored_bindings=(
                replace(row, candidate=replace(row.candidate, source=Getter())),
            ),
        )
    error = _error(forged)
    assert type(error) is report.OperatorReportError
    assert str(error) == "operator_report_invalid_status"
    assert calls == []
    # Rejecting supplied signals must not disable genuine per-render overflow.
    _error(
        replace(view, stored_bindings=view.stored_bindings * 129),
        "operator_report_budget_exceeded",
    )
    assert report.render_operator_report(view)


@pytest.mark.parametrize("caller_kind", ["runtime", "report", "hostile", "group"])
@pytest.mark.parametrize("active_scope", ["except", "finally"])
@pytest.mark.parametrize(
    "fault",
    ["same", "different", "spoof", "invalid_type", "bytes", "text", "rows"],
)
def test_active_private_caller_context_is_removed(
    caller_kind, active_scope, fault, monkeypatch
):
    ledger, _, _ = _seed_bound_cpi()
    view = _status(ledger)
    calls = []
    private_object = object()

    class HostileCaller(report.OperatorReportError):
        def __str__(self):
            calls.append("str")
            raise RuntimeError("synthetic-private-string")

        @property
        def args(self):
            calls.append("args")
            raise RuntimeError("synthetic-private-args")

    caller = (
        RuntimeError(private_object)
        if caller_kind == "runtime"
        else report.OperatorReportError(private_object)
        if caller_kind == "report"
        else HostileCaller(private_object)
        if caller_kind == "hostile"
        else ExceptionGroup("synthetic-private-group", [RuntimeError(private_object)])
    )
    caller.__cause__ = RuntimeError("synthetic-private-caller-cause")
    supplied = (
        caller
        if fault == "same"
        else report._BudgetExceeded(private_object)
        if fault == "spoof"
        else report.OperatorReportError(private_object)
    )

    class Getter:
        @property
        def key(self):
            raise supplied from RuntimeError("synthetic-private-getter-cause")

    if fault in {"same", "different", "spoof"}:
        view = replace(view, definition=Getter())
    elif fault == "invalid_type":
        view = None
    elif fault == "bytes":
        monkeypatch.setattr(report, "MAX_REPORT_BYTES", 1)
    elif fault == "text":
        row = view.stored_bindings[0]
        view = replace(
            view,
            stored_bindings=(
                replace(
                    row,
                    candidate=replace(
                        row.candidate, binding=Key(Kind.BINDING, "x" * 16385, "r1")
                    ),
                ),
            ),
        )
    else:
        view = replace(view, stored_bindings=view.stored_bindings * 129)
    expected = (
        "operator_report_budget_exceeded"
        if fault in {"bytes", "text", "rows"}
        else "operator_report_invalid_status"
    )

    def check():
        assert sys.exception() is caller
        error = _error(view, expected)
        assert type(error) is report.OperatorReportError
        assert str(error) == expected
        assert error is not supplied and error is not caller
        assert error.__cause__ is error.__context__ is None
        assert sys.exception() is caller
        assert calls == []

    if active_scope == "except":
        try:
            raise caller
        except Exception:
            check()
    else:
        try:
            try:
                raise caller
            finally:
                check()
        except Exception as resumed:
            assert resumed is caller


def test_nested_caller_handlers_preserve_clean_errors_and_success():
    ledger, _, _ = _seed_bound_cpi()
    view = _status(ledger)
    expected = report.render_operator_report(view)
    outer = report.OperatorReportError(object())
    inner = RuntimeError(object())
    try:
        raise outer
    except report.OperatorReportError:
        try:
            raise inner
        except RuntimeError:
            assert inner.__context__ is outer
            _error(None)
            _error(
                replace(view, stored_bindings=view.stored_bindings * 129),
                "operator_report_budget_exceeded",
            )
            assert report.render_operator_report(view) == expected
            assert sys.exception() is inner
        assert sys.exception() is outer


def _transition_status(steps, at=AT, pending_clock=None):
    ledger, plan, publication = _seed_bound_cpi()
    for number, decision in enumerate(steps):
        clock = BASE + timedelta(seconds=7 + number)
        publication = replace(
            publication,
            request=replace(
                plan.publication_request,
                identity=f"report-transition:{number}",
                occurrence=f"report-transition:{number}",
                decision=decision,
                predecessor=publication.key,
                supersedes_version=plan.version.key
                if decision == Decision.SUPERSEDE
                else None,
                effective_at=None,
            ),
            decision_at=clock,
            usable_at=clock,
        )
        if pending_clock is not None and number == len(steps) - 1:
            future = at + timedelta(seconds=1)
            if pending_clock == "effective_at":
                publication = replace(
                    publication,
                    request=replace(publication.request, effective_at=future),
                )
            elif pending_clock == "decision_at":
                publication = replace(publication, decision_at=future, usable_at=future)
            else:
                publication = replace(publication, usable_at=future)
        _store(ledger, publication)
    return _status(ledger, at=at)


def _replace_report_version(view, version):
    # Forge the entire claimed summary coherently, as in the independent
    # counterexample. The history must still prevent false promotion/demotion.
    eligible = version.selection_state == "publication_eligible"
    return replace(
        view,
        as_of=replace(
            view.as_of,
            candidates=(version,),
            selection_outcome="one_publication_candidate"
            if eligible
            else "as_of_no_version",
        ),
        stored_bindings=(
            replace(
                view.stored_bindings[0],
                publication_candidates=(version.version,) if eligible else (),
            ),
        ),
    )


@pytest.mark.parametrize("decision", [Decision.WITHDRAW, Decision.SUPERSEDE])
@pytest.mark.parametrize("effective", ["terminal", "earlier_publish"])
def test_terminal_history_blocks_coherent_eligible_promotion(decision, effective):
    view = _transition_status((decision,))
    version = view.as_of.candidates[0]
    assert version.selection_state == (
        "version_withdrawn" if decision == Decision.WITHDRAW else "version_superseded"
    )
    assert report.render_operator_report(view)
    forged = replace(
        version,
        selection_state="publication_eligible",
        effective_publication=version.publications[0].publication
        if effective == "earlier_publish"
        else version.effective_publication,
    )
    _error(_replace_report_version(view, forged))
    # Changing the effective Key alone must also fail, even with a terminal state.
    _error(
        _replace_report_version(
            view,
            replace(version, effective_publication=version.publications[0].publication),
        )
    )


@pytest.mark.parametrize(
    "fault",
    [
        "empty_history",
        "missing_effective",
        "unknown_effective",
        "future_all",
        "future_decision",
        "future_usable",
        "future_effective",
        "duplicate_key",
        "duplicate_clock",
        "reverse_history",
        "invalid_transition",
        "naive_clock",
        "usable_before_decision",
        "wrong_terminal",
        "hide_eligible",
    ],
)
def test_publication_history_counterexamples_fail_closed(fault):
    view = _transition_status((Decision.WITHDRAW, Decision.RESTORE))
    version = view.as_of.candidates[0]
    assert version.selection_state == "publication_eligible"
    assert report.render_operator_report(view)
    history = version.publications
    if fault == "empty_history":
        version = replace(version, publications=())
    elif fault == "missing_effective":
        version = replace(version, effective_publication=None)
    elif fault == "unknown_effective":
        version = replace(
            version, effective_publication=Key(Kind.PUBLICATION, "unknown")
        )
    elif fault.startswith("future_"):
        last = history[-1]
        future = view.as_of.as_of + timedelta(seconds=1)
        changed = (
            {"decision_at": future, "usable_at": future, "effective_at": future}
            if fault == "future_all"
            else {fault.removeprefix("future_") + "_at": future}
        )
        if fault == "future_decision":
            changed["usable_at"] = future
        last = replace(last, **changed)
        last = replace(
            last,
            pending_clocks=tuple(
                name
                for name in ("decision_at", "usable_at", "effective_at")
                if getattr(last, name) is not None
                and getattr(last, name) > view.as_of.as_of
            ),
        )
        version = replace(version, publications=(*history[:-1], last))
    elif fault == "duplicate_key":
        version = replace(version, publications=(*history, history[-1]))
    elif fault == "duplicate_clock":
        version = replace(
            version,
            publications=(
                *history[:-1],
                replace(history[-1], decision_at=history[-2].decision_at),
            ),
        )
    elif fault == "reverse_history":
        version = replace(version, publications=tuple(reversed(history)))
    elif fault == "invalid_transition":
        version = replace(
            version,
            publications=(replace(history[0], decision=Decision.RESTORE), *history[1:]),
        )
    elif fault == "naive_clock":
        version = replace(
            version,
            publications=(
                replace(
                    history[0], usable_at=history[0].usable_at.replace(tzinfo=None)
                ),
                *history[1:],
            ),
        )
    elif fault == "usable_before_decision":
        version = replace(
            version, publications=(replace(history[0], usable_at=BASE), *history[1:])
        )
    elif fault == "wrong_terminal":
        version = replace(version, selection_state="version_withdrawn")
    else:
        version = replace(
            version,
            selection_state="no_effective_publication",
            effective_publication=None,
        )
    _error(_replace_report_version(view, version))


@pytest.mark.parametrize(
    "steps,seconds,state",
    [
        ((), 3, "no_effective_publication"),
        ((), 4, "publication_eligible"),
        ((Decision.WITHDRAW,), 6, "publication_eligible"),
        ((Decision.WITHDRAW,), 7, "version_withdrawn"),
        ((Decision.WITHDRAW, Decision.RESTORE), 7, "version_withdrawn"),
        ((Decision.WITHDRAW, Decision.RESTORE), 8, "publication_eligible"),
        ((Decision.SUPERSEDE,), 6, "publication_eligible"),
        ((Decision.SUPERSEDE,), 7, "version_superseded"),
        (
            (Decision.WITHDRAW, Decision.RESTORE, Decision.SUPERSEDE),
            9,
            "version_superseded",
        ),
    ],
)
def test_exact_effective_history_survives_pending_later_records(steps, seconds, state):
    view = _transition_status(steps, at=BASE + timedelta(seconds=seconds))
    version = view.as_of.candidates[0]
    assert version.selection_state == state
    effective = tuple(p for p in version.publications if not p.pending_clocks)
    assert version.effective_publication == (
        effective[-1].publication if effective else None
    )
    html = report.render_operator_report(view)
    assert f"Publication selection state: {state}" in html
    assert (
        "Publication eligible (1)"
        if state == "publication_eligible"
        else "Referenced, no eligible Publication (1)"
    ) in html


def test_duplicate_publication_key_across_versions_is_rejected():
    ledger, _, _, _ = _mixed()
    view = _status(ledger)
    candidates = list(view.as_of.candidates)
    first, last = candidates[1], candidates[2]
    duplicate = replace(
        last.publications[0], publication=first.publications[0].publication
    )
    candidates[2] = replace(
        last, publications=(duplicate,), effective_publication=duplicate.publication
    )
    _error(replace(view, as_of=replace(view.as_of, candidates=tuple(candidates))))


@pytest.mark.parametrize("clock", ["decision_at", "usable_at", "effective_at"])
@pytest.mark.parametrize(
    "steps,state",
    [
        ((Decision.WITHDRAW,), "publication_eligible"),
        ((Decision.SUPERSEDE,), "publication_eligible"),
        ((Decision.WITHDRAW, Decision.RESTORE), "version_withdrawn"),
    ],
)
def test_pending_terminal_or_restore_keeps_previous_effective_key(clock, steps, state):
    view = _transition_status(steps, pending_clock=clock)
    version = view.as_of.candidates[0]
    assert version.selection_state == state
    assert version.effective_publication == version.publications[-2].publication
    assert clock in version.publications[-1].pending_clocks
    assert report.render_operator_report(view)


def test_actual_fixed_byte_budget_accepts_exact_limit_and_rejects_next_byte():
    ledger, plan, _ = _seed_bound_cpi()
    for number in range(15):
        ledger.put(
            replace(plan.binding_candidate, identity=f"{number:02d}-" + "x" * 16381)
        )
    baseline = ledger.snapshot()
    tail = replace(plan.binding_candidate, identity="tail")
    ledger.put(tail)
    before = report.render_operator_report(_status(ledger))
    extra = 262144 - len(before.encode("utf-8"))
    assert 0 < extra < 16380
    ledger = InMemoryLedger.from_snapshot(baseline)
    ledger.put(replace(tail, identity="tail" + "x" * extra))
    exact = report.render_operator_report(_status(ledger))
    assert report.MAX_REPORT_BYTES == len(exact.encode("utf-8")) == 262144
    previous = replace(tail, identity="tail" + "x" * extra)
    ledger = InMemoryLedger.from_snapshot(baseline)
    ledger.put(replace(previous, identity=previous.identity + "x"))
    _error(_status(ledger), "operator_report_budget_exceeded")


def test_locator_scan_preserves_original_rejection_semantics():
    original = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://|(?:^|\s)[/\\]|[a-zA-Z]:[/\\]")
    alphabet = ("a", "Z", "0", "+", ".", "-", ":", "/", "\\", " ", "\u2003", "é")
    for characters in product(alphabet, repeat=4):
        value = "".join(characters)
        assert report._locator(value) == (original.search(value) is not None)
    for value in (
        "opaque/a",
        "0+.-://opaque",
        "é://opaque",
        "prefix=a0+.-://private",
        "prefix=0a+.-://private",
        "opaque\u2003/private",
        "prefix=C:\\private",
    ):
        assert report._locator(value) == (original.search(value) is not None)


@pytest.mark.parametrize("payload", ["x" * 16384, "0+.-" * 4096, "x" * 16379 + "é://x"])
def test_valid_maximum_ascii_keys_have_no_quadratic_locator_work(payload):
    ledger, plan, _ = _seed_bound_cpi()
    ledger.put(replace(plan.binding_candidate, identity=payload))
    view = _status(ledger)
    assert escape(payload) in report.render_operator_report(view)
    # Coarse CPU ceiling, far above a single-pass scan; the original regex takes
    # about 12 seconds for 32 maximum-length letter Keys on the review host.
    start = process_time()
    for _ in range(32):
        probe = report._HTML()
        probe.key(Key(Kind.BINDING, payload, "r1"), Kind.BINDING)
    assert process_time() - start < 2.0


@pytest.mark.parametrize(
    "suffix", ["a://x", "a:\\x", " /x", "\u2003/x", "\x00", "\u2066"]
)
def test_long_keys_still_reject_locator_and_invisible_suffixes(suffix):
    ledger, plan, _ = _seed_bound_cpi()
    payload = "x" * (16384 - len(suffix)) + suffix
    ledger.put(replace(plan.binding_candidate, identity=payload))
    _error(_status(ledger))


def test_report_usage_waits_for_successful_snapshot_exit():
    ledger, _, _ = _seed_bound_cpi()
    connection = SnapshotDouble(ledger)
    source = snapshot_source(connection)
    with source.operator_snapshot() as scope:
        candidate = inspect_operator_status(scope, BLS_CPI_HEADLINES.key, as_of=AT)
    assert connection.events[-2:] == ["rollback", ("autocommit", False)]
    assert report.render_operator_report(candidate)
    connection.fault = "rollback"
    outputs = []
    with pytest.raises(ReadContractError) as caught:
        with source.operator_snapshot() as scope:
            candidate = inspect_operator_status(scope, BLS_CPI_HEADLINES.key, as_of=AT)
        outputs.append(report.render_operator_report(candidate))
    assert outputs == []
    assert caught.value.classification == ReadFailure.STORAGE_UNAVAILABLE
