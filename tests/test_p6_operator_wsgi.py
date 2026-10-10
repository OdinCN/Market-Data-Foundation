"""Standard-library WSGI hosts and synthetic scopes only; never a listener."""

from io import BytesIO, StringIO
from wsgiref.handlers import SimpleHandler
from wsgiref.util import setup_testing_defaults
from wsgiref.validate import validator

import pytest

from market_data_foundation.canonical import operator_web as web
from market_data_foundation.canonical.operator_wsgi import OperatorWSGIGateway
from tests.test_p6_consumer_read import _seed_bound_cpi, _seed_daily
from tests.test_p6_operator_snapshot import IDLE
from tests.test_p6_operator_web import (
    HostileError,
    SyntheticHost,
    _request,
    no_network,  # noqa: F401 -- reuse the offline autouse guard
)


def _environ(selector="bls-cpi-headlines", **changes):
    request = _request(selector)
    path, query = request.target.decode().split("?", 1)
    env = {}
    setup_testing_defaults(env)
    env.pop("HTTP_HOST")  # Optional with this standard HTTP/1.0 test host.
    env.update(PATH_INFO=path, QUERY_STRING=query)
    env.update(changes)
    return env


def _call(app, env=None, *, check=True, before_start=None):
    calls = []
    env = _environ() if env is None else env

    def start_response(status, headers, exc_info=None):
        if before_start:
            before_start()
        assert type(status) is str
        assert type(headers) is list
        assert exc_info is None
        calls.append((status, headers))
        return lambda data: pytest.fail("gateway must not call WSGI write()")

    def unchanged_environment(environ, start):
        before = environ.copy()  # validator itself wraps input/errors in-place.
        result = app(environ, start)
        assert environ == before
        return result

    iterable = (validator(unchanged_environment) if check else unchanged_environment)(
        env.copy(), start_response
    )
    try:
        chunks = list(iterable)
    finally:
        if hasattr(iterable, "close"):
            iterable.close()
    assert len(calls) == 1
    assert len(chunks) == 1 and type(chunks[0]) is bytes
    status, headers = calls[0]
    if env.get("REQUEST_METHOD") == "HEAD":
        assert chunks == [b""]
        assert not any(name.lower() == "content-length" for name, _ in headers)
    else:
        assert dict(headers)["Content-Length"] == str(len(chunks[0]))
    allows = [value for name, value in headers if name.lower() == "allow"]
    assert allows == (["GET"] if status == "405 Method Not Allowed" else [])
    assert dict(headers)["Cache-Control"] == "no-store"
    assert dict(headers)["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in dict(headers)["Content-Security-Policy"]
    return status, dict(headers), chunks[0]


def _error(result, status, classification, *, head=False):
    actual, headers, body = result
    assert actual == status
    assert headers["Content-Type"] == "text/plain; charset=utf-8"
    assert body == (b"" if head else (classification + "\n").encode())
    assert len(body) <= 64 and b"<html" not in body


def _gateway(host, context_factory=None):
    return OperatorWSGIGateway(
        host.entry,
        context_factory=context_factory or (lambda: host.context),
    )


def _standard_handler(app, env, *, check):
    output, errors = BytesIO(), StringIO()
    handler = SimpleHandler(env["wsgi.input"], output, errors, env.copy())
    handler.os_environ = {}  # Do not import private process environment values.
    handler.run(validator(app) if check else app)
    head, body = output.getvalue().split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    headers = [tuple(line.split(b": ", 1)) for line in lines[1:]]
    assert errors.getvalue() == ""
    return lines[0], headers, body


@pytest.mark.parametrize("check", [False, True])
@pytest.mark.parametrize("selector", ["bls-cpi-headlines", "raw-daily-bars"])
@pytest.mark.parametrize(
    "changes,status",
    [
        ({}, "405 Method Not Allowed"),
        ({"CONTENT_LENGTH": "1", "wsgi.input": BytesIO(b"x")}, "400 Bad Request"),
        ({"QUERY_STRING": "x" * 513}, "400 Bad Request"),
        ({"HTTP_X_A": "x\r\nRole: Operator"}, "400 Bad Request"),
    ],
)
def test_standard_handler_head_has_no_body_or_inferred_length(
    check, selector, changes, status
):
    host = SyntheticHost(_seed_bound_cpi()[0])

    def inject():
        pytest.fail("rejected HEAD must not obtain context")

    actual, headers, body = _standard_handler(
        _gateway(host, inject),
        _environ(selector, REQUEST_METHOD="HEAD", **changes),
        check=check,
    )
    assert actual == f"HTTP/1.0 {status}".encode()
    assert body == b""
    assert not any(name.lower() == b"content-length" for name, _ in headers)
    assert [value for name, value in headers if name.lower() == b"allow"] == (
        [b"GET"] if status == "405 Method Not Allowed" else []
    )
    assert host.calls == host.connection.events == host.connection.calls == []


@pytest.mark.parametrize("check", [False, True])
@pytest.mark.parametrize(
    "method", ["POST", "OPTIONS", "PUT", "DELETE", "PATCH", "TRACE", "CONNECT"]
)
def test_standard_handler_405_has_one_allow_and_preserves_error_body(check, method):
    host = SyntheticHost(_seed_bound_cpi()[0])

    def inject():
        pytest.fail("unsupported method must not obtain context")

    expected = host.handle(_request(method=method.encode()))
    actual, headers, body = _standard_handler(
        _gateway(host, inject), _environ(REQUEST_METHOD=method), check=check
    )
    assert actual == b"HTTP/1.0 405 Method Not Allowed"
    assert [value for name, value in headers if name.lower() == b"allow"] == [b"GET"]
    assert body == expected.body == b"method_not_allowed\n"
    assert (b"Content-Length", str(len(body)).encode()) in headers
    assert host.calls == host.connection.events == host.connection.calls == []


@pytest.mark.parametrize("check", [False, True])
@pytest.mark.parametrize("fault", ["ok", "forbidden", "route", "body", "scope"])
def test_standard_handler_get_preserves_entry_transport(check, fault):
    host = SyntheticHost(_seed_bound_cpi()[0])
    env = _environ()
    request = _request()
    if fault == "forbidden":
        host.entry._authorize = None
    elif fault == "route":
        env["PATH_INFO"] = "/other"
        request = _request(target=b"/other?" + env["QUERY_STRING"].encode())
    elif fault == "body":
        env.update(CONTENT_LENGTH="1", **{"wsgi.input": BytesIO(b"x")})
        request = _request(body=b"x")
    elif fault == "scope":
        host.entry._snapshot = None
    expected = host.handle(request)
    actual, headers, body = _standard_handler(_gateway(host), env, check=check)
    assert actual.split(b" ", 2)[1] == str(expected.status).encode()
    assert [
        (name.decode(), value.decode()) for name, value in headers if name != b"Date"
    ] == list(expected.headers)
    assert body == expected.body
    assert not any(name.lower() == b"allow" for name, _ in headers)


@pytest.mark.parametrize("check", [False, True])
@pytest.mark.parametrize("status", [200, 400, 403, 404, 405, 503])
def test_head_transport_suppresses_every_response_status(check, status, monkeypatch):
    from market_data_foundation.canonical import operator_wsgi as gateway

    # Probe the response seam as well as reachable 400/405 paths: suppression
    # must be independent of status and of the spelling of Content-Length.
    expected = web._error(status, "synthetic_classification")
    expected = web.OperatorWebResponse(
        status,
        tuple((name.lower(), value) for name, value in expected.headers),
        expected.body,
    )
    monkeypatch.setattr(gateway, "_admit", lambda request: expected)
    actual, headers, body = _standard_handler(
        OperatorWSGIGateway(), _environ(REQUEST_METHOD="HEAD"), check=check
    )
    assert actual.split(b" ", 2)[1] == str(status).encode()
    assert body == b""
    assert not any(name.lower() == b"content-length" for name, _ in headers)
    assert [value for name, value in headers if name.lower() == b"allow"] == (
        [b"GET"] if status == 405 else []
    )


@pytest.mark.parametrize("check", [False, True])
@pytest.mark.parametrize("method", ["HEAD", "POST", "OPTIONS"])
def test_405_transport_replaces_conflicting_allow_without_duplicates(
    check, method, monkeypatch
):
    from market_data_foundation.canonical import operator_wsgi as gateway

    expected = web._error(405, "method_not_allowed")
    expected = web.OperatorWebResponse(
        405,
        expected.headers + (("allow", "POST"), ("ALLOW", "GET, HEAD")),
        expected.body,
    )
    monkeypatch.setattr(gateway, "_admit", lambda request: expected)
    actual, headers, body = _standard_handler(
        OperatorWSGIGateway(), _environ(REQUEST_METHOD=method), check=check
    )
    assert actual == b"HTTP/1.0 405 Method Not Allowed"
    assert [value for name, value in headers if name.lower() == b"allow"] == [b"GET"]
    assert body == (b"" if method == "HEAD" else expected.body)


@pytest.mark.parametrize(
    "selector,seed",
    [("bls-cpi-headlines", _seed_bound_cpi), ("raw-daily-bars", _seed_daily)],
)
def test_validator_real_chain_repeated_calls_and_mounted_path(selector, seed):
    ledger = seed()[0]
    before = ledger.snapshot()
    host = SyntheticHost(ledger)
    app = _gateway(host)
    expected = host.handle(_request(selector))
    for _ in range(2):

        def after_cleanup():
            assert host.connection.info.transaction_status is IDLE
            assert host.connection.events[-2:] == ["rollback", ("autocommit", False)]

        result = _call(
            app, _environ(selector, SCRIPT_NAME="/mounted"), before_start=after_cleanup
        )
        assert result[0] == "200 OK"
        assert result[1] == dict(expected.headers)
        assert result[2] == expected.body
    assert host.connection.events.count("rollback") == 3
    assert ledger.snapshot() == before


@pytest.mark.parametrize(
    "injection", ["missing", "none", "spoof", "error", "interrupt"]
)
def test_environ_and_headers_cannot_supply_context(injection):
    host = SyntheticHost(_seed_bound_cpi()[0])
    calls = []

    def inject():
        calls.append("inject")
        if injection == "error":
            raise HostileError("synthetic private")
        if injection == "interrupt":
            raise KeyboardInterrupt("synthetic private")
        return None if injection == "none" else {"role": "Operator"}

    app = OperatorWSGIGateway(
        host.entry, context_factory=None if injection == "missing" else inject
    )
    env = _environ(
        HTTP_AUTHORIZATION="Operator",
        HTTP_COOKIE="role=Operator",
        HTTP_X_ROLE="Operator",
        REMOTE_USER="Operator",
    )
    env["mdf.operator_context"] = host.context
    env["mdf.context_factory"] = lambda: host.context
    env["operator_context"] = "Operator"
    _error(_call(app, env), "403 Forbidden", "operator_forbidden")
    assert "snapshot" not in host.calls
    assert host.connection.events == []
    assert calls == ([] if injection == "missing" else ["inject"])


@pytest.mark.parametrize("fault", ["missing", "false", "truthy", "error"])
def test_independent_authorizer_required(fault):
    host = SyntheticHost(_seed_bound_cpi()[0])

    def fail(*args):
        raise HostileError("synthetic private")

    host.entry._authorize = {
        "missing": None,
        "false": lambda *a: False,
        "truthy": lambda *a: 1,
        "error": fail,
    }[fault]
    _error(_call(_gateway(host)), "403 Forbidden", "operator_forbidden")
    assert host.connection.events == []


@pytest.mark.parametrize(
    "changes,status,classification",
    [
        ({"REQUEST_METHOD": "POST"}, "405 Method Not Allowed", "method_not_allowed"),
        ({"REQUEST_METHOD": "HEAD"}, "405 Method Not Allowed", "method_not_allowed"),
        ({"REQUEST_METHOD": "get"}, "400 Bad Request", "invalid_request"),
        ({"REQUEST_METHOD": "G" * 17}, "400 Bad Request", "invalid_request"),
        ({"PATH_INFO": "/other"}, "404 Not Found", "route_not_found"),
        ({"PATH_INFO": "/operator/%72eport"}, "404 Not Found", "route_not_found"),
        ({"PATH_INFO": "/operator/report?x=y"}, "400 Bad Request", "invalid_request"),
        ({"PATH_INFO": "/operator/report#x"}, "400 Bad Request", "invalid_request"),
        ({"PATH_INFO": "/operator/report\x00"}, "400 Bad Request", "invalid_request"),
        ({"PATH_INFO": "/é"}, "400 Bad Request", "invalid_request"),
        ({"QUERY_STRING": "x" * 513}, "400 Bad Request", "invalid_request"),
        (
            {"QUERY_STRING": "definition=latest&as_of=x"},
            "400 Bad Request",
            "definition_not_admitted",
        ),
        (
            {
                "QUERY_STRING": "definition=bls-cpi-headlines&"
                "as_of=2026-10-06T00%3A00%3A20Z"
            },
            "400 Bad Request",
            "invalid_as_of",
        ),
        (
            {"QUERY_STRING": "definition=bls-cpi-headlines&as_of=2026-02-30T00:00:20Z"},
            "400 Bad Request",
            "invalid_as_of",
        ),
        (
            {"QUERY_STRING": "definition=bls-cpi-headlines&as_of=x&role=Operator"},
            "400 Bad Request",
            "invalid_request",
        ),
        (
            {"CONTENT_LENGTH": "1", "wsgi.input": BytesIO(b"x")},
            "400 Bad Request",
            "invalid_request",
        ),
        ({"CONTENT_LENGTH": "9" * 2000}, "400 Bad Request", "invalid_request"),
        ({"CONTENT_LENGTH": "-0"}, "400 Bad Request", "invalid_request"),
        ({"CONTENT_LENGTH": " 0"}, "400 Bad Request", "invalid_request"),
        ({"HTTP_TRANSFER_ENCODING": "chunked"}, "400 Bad Request", "invalid_request"),
        ({"wsgi.input_terminated": True}, "400 Bad Request", "invalid_request"),
        ({"HTTP_X_A": "x\r\nRole: Operator"}, "400 Bad Request", "invalid_request"),
        ({"HTTP_X_A": "é"}, "400 Bad Request", "invalid_request"),
        ({"HTTP_X_A": "v" * 1025}, "400 Bad Request", "invalid_request"),
        ({"HTTP_" + "X" * 65: "v"}, "400 Bad Request", "invalid_request"),
        ({"HTTP_X_A": "x", "HTTP_X-A": "y"}, "400 Bad Request", "invalid_request"),
        ({"HTTP_": "v"}, "400 Bad Request", "invalid_request"),
    ],
)
def test_validator_rejections_precede_injection(changes, status, classification):
    host = SyntheticHost(_seed_bound_cpi()[0])
    injections = []

    def inject():
        injections.append("inject")
        return host.context

    app = _gateway(host, inject)
    _error(
        _call(app, _environ(**changes)),
        status,
        classification,
        head=changes.get("REQUEST_METHOD") == "HEAD",
    )
    assert injections == []
    assert host.calls == host.connection.events == host.connection.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"PATH_INFO": b"/operator/report"},
        {"PATH_INFO": "界"},
        {"QUERY_STRING": "\ud800"},
        {"CONTENT_LENGTH": 0},
        {"HTTP_X": object()},
        {"REQUEST_METHOD": None},
        {"HTTP_x": "v"},
        {"CONTENT_LENGTH": "0,0"},
        {"HTTP_CONTENT_LENGTH": "0"},
        {"HTTP_CONTENT_TYPE": "text/plain"},
    ],
)
def test_malformed_nonstandard_environ_fails_closed(changes):
    _error(
        _call(OperatorWSGIGateway(), _environ(**changes), check=False),
        "400 Bad Request",
        "invalid_request",
    )


def test_standard_header_mapping_and_zero_body_never_reads_input(monkeypatch):
    host = SyntheticHost(_seed_bound_cpi()[0])
    app = _gateway(host)
    requests = []
    original = web.OperatorWebEntry.handle

    def handle(self, request, **kwargs):
        requests.append(request)
        return original(self, request, **kwargs)

    class NoRead:
        def read(self, *args):
            pytest.fail("must not read beyond declared zero length")

        readline = read
        readlines = read
        __iter__ = read

    monkeypatch.setattr(web.OperatorWebEntry, "handle", handle)
    for length in (None, "", "0", "00"):
        env = _environ(HTTP_X_ROLE="Operator", CONTENT_TYPE="text/plain")
        env["wsgi.input"] = NoRead()
        if length is not None:
            env["CONTENT_LENGTH"] = length
        before = env.copy()
        assert _call(app, env)[0] == "200 OK"
        assert env == before
        request = requests[-1]
        assert request.target == _request().target and request.body == b""
        assert (b"X-ROLE", b"Operator") in request.headers
        assert (b"Content-Type", b"text/plain") in request.headers
        assert (
            (b"Content-Length", length.encode()) in request.headers
            if length
            else b"Content-Length" not in dict(request.headers)
        )


def test_cgi_headers_share_existing_count_component_and_aggregate_budgets():
    host = SyntheticHost(_seed_bound_cpi()[0])
    app = _gateway(host)
    headers = {f"HTTP_X_{i}": "v" * 1021 for i in range(4)}
    assert _call(app, _environ(**headers))[0] == "200 OK"
    _error(
        _call(app, _environ(**headers, HTTP_Z="")), "400 Bad Request", "invalid_request"
    )
    headers = {f"HTTP_H{i}": "" for i in range(16)}
    assert _call(app, _environ(**headers))[0] == "200 OK"
    _error(
        _call(app, _environ(**headers, CONTENT_LENGTH="0")),
        "400 Bad Request",
        "invalid_request",
    )
    assert _call(app, _environ(**{"HTTP_" + "X" * 64: "v" * 1024}))[0] == "200 OK"


@pytest.mark.parametrize(
    "fault",
    [
        "enable",
        "begin_before",
        "verify",
        "read",
        "rollback",
        "rollback_stuck",
        "restore",
        "missing",
    ],
)
def test_scope_failure_response_starts_only_after_failed_cleanup(fault, monkeypatch):
    host = SyntheticHost(_seed_bound_cpi()[0])
    if fault == "missing":
        host.entry._snapshot = None
    else:
        host.connection.fault = fault
    monkeypatch.setattr(
        web, "render_operator_report", lambda *a: pytest.fail("partial HTML")
    )
    _error(
        _call(_gateway(host)), "503 Service Unavailable", "operator_status_unavailable"
    )
    assert "COMMIT" not in host.connection.events


def test_inspection_interrupt_and_failed_discard_prevent_partial_html(monkeypatch):
    host = SyntheticHost(_seed_bound_cpi()[0])
    host.connection.fault = "rollback"

    def fail(*args, **kwargs):
        raise HostileError("synthetic private")

    host.connection.close = fail
    monkeypatch.setattr(web, "inspect_operator_status", fail)
    monkeypatch.setattr(
        web, "render_operator_report", lambda *a: pytest.fail("partial HTML")
    )
    _error(
        _call(_gateway(host)), "503 Service Unavailable", "operator_status_unavailable"
    )
    assert host.connection.events[-1] == "rollback"


@pytest.mark.parametrize("fault", ["error", "interrupt", "overflow", "surrogate"])
def test_render_failure_after_cleanup_returns_fixed_length_error(fault, monkeypatch):
    host = SyntheticHost(_seed_bound_cpi()[0])

    def render(status):
        assert host.connection.info.transaction_status is IDLE
        if fault == "error":
            raise HostileError("synthetic private")
        if fault == "interrupt":
            raise SystemExit("synthetic private")
        return (
            "界" * (web.MAX_REPORT_BYTES // 3 + 1) if fault == "overflow" else "\ud800"
        )

    monkeypatch.setattr(web, "render_operator_report", render)
    _error(
        _call(_gateway(host)), "503 Service Unavailable", "operator_report_unavailable"
    )


def test_start_response_failure_is_not_retried_and_scope_already_closed():
    host = SyntheticHost(_seed_bound_cpi()[0])
    calls = []
    error = HostileError("synthetic host transport failure")

    def start_response(*args):
        assert host.connection.info.transaction_status is IDLE
        calls.append(args)
        raise error

    with pytest.raises(HostileError) as caught:
        _gateway(host)(_environ(), start_response)
    assert caught.value is error and len(calls) == 1
    assert host.connection.events.count("rollback") == 1


def test_default_gateway_is_validator_compatible_and_denies():
    _error(_call(OperatorWSGIGateway()), "403 Forbidden", "operator_forbidden")


def test_standard_handler_uses_memory_streams_without_server_or_process_environ():
    host = SyntheticHost(_seed_bound_cpi()[0])
    output, errors = BytesIO(), StringIO()
    handler = SimpleHandler(BytesIO(), output, errors, _environ())
    handler.os_environ = {}  # Do not import private process environment values.
    handler.run(validator(_gateway(host)))
    head, body = output.getvalue().split(b"\r\n\r\n", 1)
    assert head.startswith(b"HTTP/1.0 200 OK\r\n")
    assert f"Content-Length: {len(body)}".encode() in head.split(b"\r\n")
    assert body == host.handle(_request()).body
    assert errors.getvalue() == ""


def test_injection_is_fresh_and_receives_no_request_arguments():
    host = SyntheticHost(_seed_bound_cpi()[0])
    contexts = iter((host.context, None))
    app = _gateway(host, lambda: next(contexts))
    env = _environ(HTTP_X_ROLE="Operator", REQUEST_URI="/untrusted?role=Operator")
    assert _call(app, env)[0] == "200 OK"
    _error(_call(app, env), "403 Forbidden", "operator_forbidden")
    assert host.connection.events.count("rollback") == 1


@pytest.mark.parametrize(
    "error", [HostileError("synthetic private"), KeyboardInterrupt()]
)
def test_failed_inspection_cleans_scope_before_start_response(error, monkeypatch):
    host = SyntheticHost(_seed_bound_cpi()[0])

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(web, "inspect_operator_status", fail)
    monkeypatch.setattr(
        web, "render_operator_report", lambda *a: pytest.fail("partial HTML")
    )

    def after_cleanup():
        assert host.connection.info.transaction_status is IDLE
        assert host.connection.events[-2:] == ["rollback", ("autocommit", False)]

    _error(
        _call(_gateway(host), before_start=after_cleanup),
        "503 Service Unavailable",
        "operator_status_unavailable",
    )


def test_suppressing_scope_cannot_release_html(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def suppressing():
        try:
            yield object()
        except BaseException:
            pass

    def fail(*args, **kwargs):
        raise HostileError("synthetic private")

    monkeypatch.setattr(web, "inspect_operator_status", fail)
    monkeypatch.setattr(
        web, "render_operator_report", lambda *a: pytest.fail("partial HTML")
    )
    entry = web.OperatorWebEntry(authorize=lambda *a: True, snapshot=suppressing)
    _error(
        _call(OperatorWSGIGateway(entry, context_factory=object)),
        "503 Service Unavailable",
        "operator_status_unavailable",
    )


def test_target_join_budget_counts_question_mark_and_query():
    _error(
        _call(
            OperatorWSGIGateway(), _environ(PATH_INFO="/" + "x" * 510, QUERY_STRING="")
        ),
        "404 Not Found",
        "route_not_found",
    )
    _error(
        _call(
            OperatorWSGIGateway(), _environ(PATH_INFO="/" + "x" * 511, QUERY_STRING="")
        ),
        "400 Bad Request",
        "invalid_request",
    )


def test_hostile_exception_is_never_inspected_or_logged():
    host = SyntheticHost(_seed_bound_cpi()[0])
    observations = []

    class PrivateError(BaseException):
        def __str__(self):
            observations.append("str")
            raise AssertionError

        @property
        def args(self):
            observations.append("args")
            raise AssertionError

        @property
        def classification(self):
            observations.append("classification")
            raise AssertionError

    def fail(*args):
        raise PrivateError("synthetic private")

    env = _environ()
    _error(_call(_gateway(host, fail), env), "403 Forbidden", "operator_forbidden")
    host.entry._authorize = fail
    _error(_call(_gateway(host), env), "403 Forbidden", "operator_forbidden")
    assert observations == [] and env["wsgi.errors"].getvalue() == ""
