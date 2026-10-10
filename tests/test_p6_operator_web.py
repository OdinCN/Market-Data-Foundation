"""In-process synthetic host and SQL/transaction doubles; no real auth or PG."""

import socket
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from market_data_foundation.canonical import operator_web as web
from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    ReadFailure,
)
from market_data_foundation.canonical.consumer_wire import handle_request
from market_data_foundation.canonical.model import (
    Decision,
    Kind,
    PublicationRequest,
    Scope,
)
from market_data_foundation.canonical.operator_asof import OperatorQueryBudgetError
from market_data_foundation.canonical.operator_report import render_operator_report
from market_data_foundation.canonical.operator_status import inspect_operator_status
from market_data_foundation.domain_contracts.dense_market import RAW_DAILY_BARS
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from tests.test_p6_consumer_read import BASE, _seed_bound_cpi, _seed_daily
from tests.test_p6_consumer_wire import _as_of_request
from tests.test_p6_operator_asof import AT, _store
from tests.test_p6_operator_report import Page
from tests.test_p6_operator_snapshot import IDLE, INTRANS, SnapshotDouble
from tests.test_p6_operator_snapshot import _source as snapshot_source


def _request(selector="bls-cpi-headlines", at=AT, **changes):
    timestamp = at.isoformat().replace("+00:00", "Z")
    return replace(
        web.OperatorWebRequest(
            b"GET",
            f"/operator/report?definition={selector}&as_of={timestamp}".encode(),
        ),
        **changes,
    )


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []

    def denied(*args, **kwargs):
        attempts.append("network")
        pytest.fail("offline request must not open/connect/bind a service")

    monkeypatch.setattr(socket, "socket", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(psycopg, "connect", denied)
    monkeypatch.setattr(psycopg.Connection, "connect", denied)
    yield
    assert attempts == []


class SyntheticHost:
    """Identity capability test fixture only; never a product authorization policy."""

    def __init__(self, ledger):
        self.context = object()
        self.calls = []
        self.connection = SnapshotDouble(ledger)
        self.entry = web.OperatorWebEntry(
            authorize=self.authorize, snapshot=self.snapshot
        )

    def authorize(self, context, definition, as_of):
        self.calls.append(("authorize", context, definition, as_of))
        return context is self.context

    def snapshot(self):
        self.calls.append("snapshot")
        return snapshot_source(self.connection).operator_snapshot()

    def handle(self, request):
        return self.entry.handle(request, operator_context=self.context)


def _error(response, status, classification):
    assert response.status == status
    assert response.body == (classification + "\n").encode()
    assert len(response.body) <= 64
    headers = dict(response.headers)
    assert headers["Content-Type"] == "text/plain; charset=utf-8"
    assert headers["Content-Length"] == str(len(response.body))
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


@pytest.mark.parametrize(
    "definition,selector,seed",
    [
        (BLS_CPI_HEADLINES, "bls-cpi-headlines", _seed_bound_cpi),
        (RAW_DAILY_BARS, "raw-daily-bars", _seed_daily),
    ],
)
def test_real_chain_and_second_request_fresh_scope(
    definition, selector, seed, monkeypatch
):
    ledger = seed()[0]
    before = ledger.snapshot()
    expected = render_operator_report(
        inspect_operator_status(ledger, definition.key, as_of=AT)
    ).encode()
    host = SyntheticHost(ledger)
    observations = []
    original_inspect = web.inspect_operator_status
    original_render = web.render_operator_report

    def inspect(scope, key, *, as_of):
        assert host.calls[-1] == "snapshot"
        assert host.connection.info.transaction_status is INTRANS
        assert host.connection.settings == ("repeatable read", "on")
        observations.append("inspect")
        result = original_inspect(scope, key, as_of=as_of)
        assert result.definition.key == definition.key
        assert result.as_of.as_of == AT
        return result

    def render(status):
        assert host.connection.info.transaction_status is IDLE
        assert host.connection.autocommit is False
        assert host.connection.events[-2:] == ["rollback", ("autocommit", False)]
        observations.append("render_after_exit")
        return original_render(status)

    monkeypatch.setattr(web, "inspect_operator_status", inspect)
    monkeypatch.setattr(web, "render_operator_report", render)
    for _ in range(2):
        response = host.handle(_request(selector))
        assert response.status == 200
        assert response.body == expected
        headers = dict(response.headers)
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        assert headers["Content-Length"] == str(len(expected))
        assert headers["Cache-Control"] == "no-store"
        assert len(response.body) <= web.MAX_REPORT_BYTES
        assert "unobservable" in response.body.decode()
    assert observations == ["inspect", "render_after_exit"] * 2
    assert [call[0] if type(call) is tuple else call for call in host.calls] == [
        "authorize",
        "snapshot",
        "authorize",
        "snapshot",
    ]
    assert set(host.connection.read_views) == {1, 2}
    assert host.connection.events.count("rollback") == 2
    assert not host.connection.closed
    assert ledger.snapshot() == before


@pytest.mark.parametrize("context", [None, {"role": "Operator"}, "Operator", True])
def test_client_headers_and_untrusted_context_cannot_authorize(context):
    host = SyntheticHost(_seed_bound_cpi()[0])
    request = _request(headers=((b"X-Role", b"Operator"), (b"X-Identity", b"owner")))
    _error(
        host.entry.handle(request, operator_context=context), 403, "operator_forbidden"
    )
    assert "snapshot" not in host.calls
    assert host.connection.events == host.connection.calls == []


def test_missing_trusted_authority_denies_even_with_scope_and_claims():
    host = SyntheticHost(_seed_bound_cpi()[0])
    entry = web.OperatorWebEntry(snapshot=host.snapshot)
    _error(
        entry.handle(
            _request(headers=((b"Authorization", b"Operator"),)),
            operator_context=host.context,
        ),
        403,
        "operator_forbidden",
    )
    assert host.calls == []
    _error(web.OperatorWebEntry().handle(_request()), 403, "operator_forbidden")


@pytest.mark.parametrize("decision", [False, None, 1, "Operator", object()])
def test_only_literal_true_authorization_allows_scope(decision):
    calls = []
    entry = web.OperatorWebEntry(
        authorize=lambda *args: decision, snapshot=lambda: calls.append("scope")
    )
    _error(
        entry.handle(_request(), operator_context=object()), 403, "operator_forbidden"
    )
    assert calls == []


def test_authority_receives_exact_selection_and_can_restrict_it():
    host = SyntheticHost(_seed_bound_cpi()[0])
    host.entry._authorize = lambda context, definition, at: (
        context is host.context and definition == BLS_CPI_HEADLINES.key and at == AT
    )
    _error(host.handle(_request("raw-daily-bars")), 403, "operator_forbidden")
    _error(
        host.handle(_request(at=AT + timedelta(microseconds=1))),
        403,
        "operator_forbidden",
    )
    assert host.calls == []
    assert host.handle(_request()).status == 200


class HostileError(RuntimeError):
    def __str__(self):
        pytest.fail("must not stringify internal exceptions")

    @property
    def classification(self):
        pytest.fail("must not inspect internal exception properties")


@pytest.mark.parametrize(
    "error", [HostileError("private locator"), KeyboardInterrupt("private")]
)
def test_authorization_failure_never_opens_scope(error):
    host = SyntheticHost(_seed_bound_cpi()[0])

    def fail(*args):
        raise error

    host.entry._authorize = fail
    _error(host.handle(_request()), 403, "operator_forbidden")
    assert host.calls == []
    assert host.connection.events == []


@pytest.mark.parametrize(
    "changes,status,classification",
    [
        ({"method": b"POST"}, 405, "method_not_allowed"),
        ({"method": b"HEAD"}, 405, "method_not_allowed"),
        ({"method": b"get"}, 400, "invalid_request"),
        ({"method": b"GET\r\n"}, 400, "invalid_request"),
        ({"method": b"G" * 17}, 400, "invalid_request"),
        ({"method": "GET"}, 400, "invalid_request"),
        ({"target": b"/consumer/read"}, 404, "route_not_found"),
        ({"target": b"http://host/operator/report"}, 404, "route_not_found"),
        ({"target": b"/operator/%72eport"}, 404, "route_not_found"),
        ({"target": b"/operator/report"}, 400, "invalid_request"),
        (
            {"target": b"/operator/report?definition=raw-daily-bars"},
            400,
            "invalid_request",
        ),
        (
            {"target": b"/operator/report?definition=a&definition=b"},
            400,
            "invalid_request",
        ),
        (
            {"target": b"/operator/report?definition=a&as_of=x&role=Operator"},
            400,
            "invalid_request",
        ),
        (
            {"target": b"/operator/report?definition=a&as_of=x=private"},
            400,
            "invalid_request",
        ),
        (
            {"target": b"/operator/report?definition=latest&as_of=x"},
            400,
            "definition_not_admitted",
        ),
        ({"target": b"x" * 513}, 400, "invalid_request"),
        ({"target": b"\xff"}, 400, "invalid_request"),
        ({"target": b"/operator/report\x00"}, 400, "invalid_request"),
        ({"target": bytearray(b"/operator/report")}, 400, "invalid_request"),
        ({"body": b"role=Operator"}, 400, "invalid_request"),
        ({"body": ""}, 400, "invalid_request"),
        ({"headers": []}, 400, "invalid_request"),
        ({"headers": ((b"X", b"y"),) * 17}, 400, "invalid_request"),
        ({"headers": ((b"X", b"y"), (b"x", b"z"))}, 400, "invalid_request"),
        ({"headers": ((b"bad name", b"y"),)}, 400, "invalid_request"),
        ({"headers": ((b"X" * 65, b"y"),)}, 400, "invalid_request"),
        ({"headers": ((b"X", b"y" * 1025),)}, 400, "invalid_request"),
        ({"headers": ((b"X", b"y\r\nX-Role: Operator"),)}, 400, "invalid_request"),
        ({"headers": ((b"X", b"\xff"),)}, 400, "invalid_request"),
        ({"headers": (("X", b"y"),)}, 400, "invalid_request"),
        ({"headers": ((b"X", "y"),)}, 400, "invalid_request"),
        ({"headers": ((b"X",),)}, 400, "invalid_request"),
        ({"headers": ([b"X", b"y"],)}, 400, "invalid_request"),
    ],
)
def test_invalid_request_has_no_authority_storage_or_render_effects(
    changes, status, classification
):
    host = SyntheticHost(_seed_bound_cpi()[0])
    _error(host.handle(_request(**changes)), status, classification)
    assert host.calls == []
    assert host.connection.events == host.connection.calls == []


@pytest.mark.parametrize("foreign_request", [None, {}, b"GET", object()])
def test_foreign_request_objects_are_rejected(foreign_request):
    _error(web.OperatorWebEntry().handle(foreign_request), 400, "invalid_request")


@pytest.mark.parametrize(
    "timestamp",
    [
        "",
        "latest",
        "2026-10-06",
        "2026-10-06T00:00:20",
        "2026-10-06T00:00:20+00:00",
        "2026-10-06T00:00:20z",
        "2026-10-06T00:00:20.1Z",
        "2026-10-06T00:00:20%5AZ",
        "2026-10-06T00:00:20Z#private",
        "0000-01-01T00:00:00Z",
        "2026-02-30T00:00:00Z",
        "2026-10-06T24:00:00Z",
        "2026-10-06T00:00:60Z",
        "9" * 400,
    ],
)
def test_bounded_canonical_utc_timestamp_is_required(timestamp):
    host = SyntheticHost(_seed_bound_cpi()[0])
    request = _request(
        target=f"/operator/report?definition=bls-cpi-headlines&as_of={timestamp}".encode()
    )
    _error(host.handle(request), 400, "invalid_as_of")
    assert host.calls == []


def test_admission_header_aggregate_and_component_boundaries():
    host = SyntheticHost(_seed_bound_cpi()[0])
    headers = tuple((f"X-{i}".encode(), b"v" * 1021) for i in range(4))
    assert sum(len(n) + len(v) for n, v in headers) == web.MAX_HEADER_BYTES
    assert host.handle(_request(headers=headers)).status == 200
    _error(
        host.handle(_request(headers=(*headers, (b"X", b"")))), 400, "invalid_request"
    )
    assert host.handle(_request(headers=((b"X" * 64, b"v" * 1024),))).status == 200
    assert (
        host.handle(
            _request(headers=tuple((f"H{i}".encode(), b"") for i in range(16)))
        ).status
        == 200
    )


@pytest.mark.parametrize(
    "at", [datetime.min.replace(tzinfo=UTC), datetime.max.replace(tzinfo=UTC), AT]
)
def test_supported_calendar_and_reordered_parameters(at):
    host = SyntheticHost(_seed_bound_cpi()[0])
    stamp = at.isoformat(timespec="microseconds").replace("+00:00", "Z")
    request = _request(
        target=f"/operator/report?as_of={stamp}&definition=bls-cpi-headlines".encode()
    )
    assert host.handle(request).status == 200
    assert host.calls[0][3] == at


@pytest.mark.parametrize(
    "fault",
    [
        "enable",
        "begin_before",
        "begin_after",
        "verify",
        "read",
        "rollback",
        "rollback_stuck",
        "restore",
    ],
)
def test_real_snapshot_setup_read_and_cleanup_failures_never_render(fault, monkeypatch):
    host = SyntheticHost(_seed_bound_cpi()[0])
    host.connection.fault = fault

    def forbidden(status):
        pytest.fail("render must follow successful scope exit")

    monkeypatch.setattr(web, "render_operator_report", forbidden)
    _error(host.handle(_request()), 503, "operator_status_unavailable")
    assert host.calls.count("snapshot") == 1
    assert host.connection.events.count("rollback") <= 1
    assert "COMMIT" not in host.connection.events


def test_ambient_transaction_untouched_and_missing_scope_denied():
    host = SyntheticHost(_seed_bound_cpi()[0])
    host.connection.info.transaction_status = INTRANS
    _error(host.handle(_request()), 503, "operator_status_unavailable")
    assert host.connection.events == host.connection.calls == []
    host.entry._snapshot = None
    _error(host.handle(_request()), 503, "operator_status_unavailable")


@pytest.mark.parametrize(
    "error",
    [
        HostileError("private"),
        OperatorQueryBudgetError(),
        ReadContractError(ReadFailure.LINEAGE_MISSING_OR_MISMATCHED),
        KeyboardInterrupt("private"),
    ],
)
def test_failed_inspection_and_interrupt_clean_up_without_partial_response(
    error, monkeypatch
):
    host = SyntheticHost(_seed_bound_cpi()[0])

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(web, "inspect_operator_status", fail)
    monkeypatch.setattr(
        web, "render_operator_report", lambda status: pytest.fail("partial render")
    )
    _error(host.handle(_request()), 503, "operator_status_unavailable")
    assert host.connection.info.transaction_status is IDLE
    assert host.connection.events[-2:] == ["rollback", ("autocommit", False)]


def test_custom_scope_suppression_cannot_convert_failed_inspection_to_success(
    monkeypatch,
):
    @contextmanager
    def suppressing():
        try:
            yield object()
        except BaseException:
            pass

    def fail(*args, **kwargs):
        raise HostileError("private")

    monkeypatch.setattr(web, "inspect_operator_status", fail)
    entry = web.OperatorWebEntry(authorize=lambda *args: True, snapshot=suppressing)
    _error(
        entry.handle(_request(), operator_context=object()),
        503,
        "operator_status_unavailable",
    )


def test_actual_corrupt_lineage_and_real_report_budget_fail_closed():
    ledger, plan, _ = _seed_bound_cpi()
    ledger._records.pop(
        (Kind.SOURCE.value, plan.source.key.identity, plan.source.key.revision)
    )
    host = SyntheticHost(ledger)
    _error(host.handle(_request()), 503, "operator_status_unavailable")
    assert host.connection.info.transaction_status is IDLE

    ledger, plan, _ = _seed_bound_cpi()
    _store(ledger, replace(plan.version, identity="x" * 16385))
    host = SyntheticHost(ledger)
    _error(host.handle(_request()), 503, "operator_status_unavailable")

    ledger, plan, _ = _seed_bound_cpi()
    for i in range(45):
        ledger.put(replace(plan.binding_candidate, revision=f"{i:02d}" + "x" * 6000))
    host = SyntheticHost(ledger)
    _error(host.handle(_request()), 503, "operator_report_unavailable")
    assert host.connection.info.transaction_status is IDLE


@pytest.mark.parametrize(
    "fault", ["exception", "interrupt", "bytes", "chars", "utf8", "surrogate"]
)
def test_report_failure_after_exit_has_only_fixed_error(fault, monkeypatch):
    host = SyntheticHost(_seed_bound_cpi()[0])

    def render(status):
        assert host.connection.info.transaction_status is IDLE
        if fault == "exception":
            raise HostileError("private")
        if fault == "interrupt":
            raise SystemExit("private")
        return {
            "bytes": b"private",
            "chars": "x" * (web.MAX_REPORT_BYTES + 1),
            "utf8": "界" * (web.MAX_REPORT_BYTES // 3 + 1),
            "surrogate": "\ud800",
        }[fault]

    monkeypatch.setattr(web, "render_operator_report", render)
    _error(host.handle(_request()), 503, "operator_report_unavailable")
    assert host.connection.events.count("rollback") == 1


def test_parameterized_asof_changes_real_status_and_no_content_access(monkeypatch):
    ledger, kernel, plan, publication = _seed_daily()
    withdraw_at = BASE + timedelta(seconds=10)
    kernel.clock = lambda: withdraw_at
    kernel.publish(
        PublicationRequest(
            identity="web-withdraw",
            occurrence="web-withdraw-occurrence",
            version=plan.version.key,
            rule_revision="synthetic-web",
            decision=Decision.WITHDRAW,
            scope=Scope(),
            predecessor=publication.key,
            evidence=(),
            claimed_grades=frozenset(),
            reason="synthetic withdrawal",
        )
    )
    host = SyntheticHost(ledger)
    before = ledger.snapshot()
    monkeypatch.setattr(
        ledger, "get_content", lambda *args: pytest.fail("content read")
    )
    earlier = host.handle(
        _request("raw-daily-bars", withdraw_at - timedelta(microseconds=1))
    )
    later = host.handle(_request("raw-daily-bars", withdraw_at))
    assert earlier.status == later.status == 200
    assert b"publication_eligible" in earlier.body
    assert b"withdrawn" in later.body
    assert earlier.body != later.body
    assert ledger.snapshot() == before


def test_html_whitelist_injection_and_public_consumer_unchanged():
    ledger, plan, _ = _seed_bound_cpi()
    public_request = _as_of_request(BLS_CPI_HEADLINES.key, AT.isoformat())
    original = handle_request(ledger, public_request)
    ledger.put(replace(plan.binding_candidate, revision='<script>"&owned'))
    _store(
        ledger,
        replace(
            plan.source, owner="PRIVATE_OWNER", freshness_contract="PRIVATE_FRESHNESS"
        ),
    )
    host = SyntheticHost(ledger)
    response = host.handle(_request())
    assert response.status == 200
    html = response.body.decode()
    assert "&lt;script&gt;&quot;&amp;owned" in html
    assert "PRIVATE_OWNER" not in html and "PRIVATE_FRESHNESS" not in html
    page = Page(html)
    assert not {"script", "form", "iframe", "img"}.intersection(page.tags)
    assert all(a.get("href", "#").startswith("#") for a in page.attributes)
    # Source metadata is deliberately changed in this fixture; compare public
    # bytes before/after the adapter itself, and retain its accepted revision.
    changed = handle_request(ledger, public_request)
    assert b"mdf.consumer.read.v1" in original and b"mdf.consumer.read.v1" in changed
    host.handle(_request())
    assert handle_request(ledger, public_request) == changed


def test_request_inside_active_private_handler_returns_no_exception_object():
    try:
        raise HostileError("private")
    except HostileError:
        response = web.OperatorWebEntry().handle(_request())
    _error(response, 403, "operator_forbidden")
    assert type(response.body) is bytes


def test_scope_factory_failure_and_failed_discard_hide_private_errors():
    host = SyntheticHost(_seed_bound_cpi()[0])

    def fail_factory():
        raise HostileError("private factory error")

    host.entry._snapshot = fail_factory
    _error(host.handle(_request()), 503, "operator_status_unavailable")
    assert host.connection.events == []
    host.entry._snapshot = host.snapshot
    host.connection.fault = "rollback"

    def fail_close():
        host.connection.events.append("close")
        raise HostileError("private discard error")

    host.connection.close = fail_close
    _error(host.handle(_request()), 503, "operator_status_unavailable")
    assert host.connection.events[-2:] == ["rollback", "close"]


def test_real_renderer_rejects_unsafe_key_after_successful_exit():
    ledger, plan, _ = _seed_bound_cpi()
    ledger.put(replace(plan.binding_candidate, revision="https://private.invalid/path"))
    host = SyntheticHost(ledger)
    _error(host.handle(_request()), 503, "operator_report_unavailable")
    assert host.connection.info.transaction_status is IDLE
