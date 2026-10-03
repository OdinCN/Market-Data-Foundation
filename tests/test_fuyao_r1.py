"""仅 synthetic/offline 检查；不读取凭据，不执行 provider CLI。"""

import hashlib
import importlib.util
import json
import socket
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from market_data_foundation.qualification import Capability, load_fixtures
from provider_lab import fuyao_r1 as r1
from provider_lab import fuyao_single_attempt as single

ROOT = Path(__file__).resolve().parents[1]


def test_frozen_budget_and_offline_contract():
    plan = r1.load_plan()
    fixtures = load_fixtures(r1.FIXTURES)
    assert len(Capability) == 16
    assert plan["limits"] == {
        "total_http_attempts": 24,
        "hard_ceiling": 35,
        "serial": True,
        "process_seconds": 30,
        "live_seconds": 1200,
        "automatic_retry": False,
        "reallocate_unused_budget": False,
        "automatic_pagination": False,
        "automatic_window_split": False,
    }
    assert sum(plan["command_max_calls"].values()) == 24
    assert sum(plan["capability_max_calls"].values()) == 24
    assert set(plan["capability_max_calls"]) == set(Capability)
    assert plan["global_argv"] == ["--source", "remote", "--format", "json"]
    assert all(f.capability in Capability for f in fixtures)
    assert all(row["process_timeout_seconds"] <= 30 for row in plan["requests"])
    modes = Counter()
    for row in plan["requests"]:
        if row["command"] == "market.history":
            argv = row["argv"]
            modes[argv[argv.index("--adjust") + 1]] += 1
            start = int(argv[argv.index("--start-ms") + 1])
            end = int(argv[argv.index("--end-ms") + 1])
            assert 0 < end - start < 10 * 365.25 * 86400000
    assert modes == {"none": 2, "forward": 2, "backward": 2}
    assert plan["capability_max_calls"]["adjustment.factors"] == 0
    assert "market.adjustment-factors" not in plan["command_max_calls"]


def test_ths_independent_and_requested_semantics_unproven():
    ths = [
        f
        for f in load_fixtures(r1.FIXTURES)
        if f.capability.value.startswith("sector.")
    ]
    tdx = [
        f
        for f in load_fixtures(ROOT / "provider_lab/fixtures.toml")
        if f.capability.value.startswith("sector.")
    ]
    assert {f.semantics["taxonomy_source"] for f in ths} == {"THS"}
    assert {f.semantics["taxonomy_source"] for f in tdx} == {"TDX"}
    assert not {f.fixture_id for f in ths} & {f.fixture_id for f in tdx}
    record = r1.blocked_observation()
    assert record["schema_version"] == 2 and record["mode"] == "offline"
    assert not record["success"] and record["pagination"]["pages"] == 0
    assert record["coverage"]["complete"] is None
    assert record["semantics"] == record["units"] == {}
    assert record["exception"]["type"] == "SingleAttemptGateBlocked"
    assert record["endpoint"]["host"] is None


@pytest.mark.parametrize("opt_in", [False, True])
def test_gate_precedes_all_io(monkeypatch, opt_in):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("gate 前不允许 IO")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setenv("FUYAO_SINGLE_ATTEMPT", "1")
    monkeypatch.setenv("FUYAO_LIVE_AUTHORIZED", "1")
    monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "synthetic-not-a-credential")
    if opt_in:
        monkeypatch.setattr(sys, "argv", ["fuyao_r1.py", "--live-provider"])
        assert r1.main() == 3
    else:
        with pytest.raises(r1.SingleAttemptGateBlocked):
            r1.run_live()
    assert calls == []


@pytest.mark.parametrize(
    ("http", "code", "reason"),
    [
        (401, 0, "authentication"),
        (403, 0, "authentication"),
        (429, 0, "rate_limit"),
        (503, 0, "http_failure"),
        (None, 0, "unknown_http_status"),
        (200, None, "unknown_business_code"),
        (200, 2000, "authentication"),
        (200, 2001, "authentication"),
        (200, 2003, "authentication"),
        (200, 2999, "authentication"),
        (200, 4001, "business_failure"),
        (200, 5001, "business_failure"),
        (200, 9999, "business_failure"),
        (200, True, "unknown_business_code"),
        (200, 0, None),
    ],
)
def test_failure_stops_without_retry(http, code, reason):
    assert r1.stop_reason(http, code) == reason


def test_ths_response_selection():
    assert (
        r1.select_ths_group([{"thscode": "880002.TI"}, {"thscode": "880001.TI"}])
        == "880001.TI"
    )
    assert r1.select_ths_group([]) is None
    with pytest.raises(ValueError):
        r1.select_ths_group([{"name": "不能猜标识"}])


def test_plan_and_fixture_tampering_fail_closed(tmp_path, monkeypatch):
    plan = tmp_path / "plan.json"
    plan.write_bytes(r1.PLAN.read_bytes() + b" ")
    monkeypatch.setattr(r1, "PLAN", plan)
    with pytest.raises(ValueError, match="request-plan"):
        r1.load_plan()
    plan.write_bytes((ROOT / "provider_lab/fuyao_r1.request-plan.json").read_bytes())
    fixture = tmp_path / "fixtures.toml"
    fixture.write_bytes(r1.FIXTURES.read_bytes() + b"\n")
    monkeypatch.setattr(r1, "FIXTURES", fixture)
    with pytest.raises(ValueError, match="fixtures"):
        r1.load_plan()
    assert hashlib.sha256(plan.read_bytes()).hexdigest() == r1.PLAN_SHA256


def test_live_module_import_performs_no_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("live module collection 必须离线")

    spec = importlib.util.spec_from_file_location(
        "fuyao_live_collection", ROOT / "tests/test_fuyao_r1_live.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    spec.loader.exec_module(module)


def test_observation_jsonschema_when_dependency_available():
    import jsonschema

    schema = json.loads((ROOT / "provider_lab/observation.schema.json").read_text())
    jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    ).validate(r1.blocked_observation())


# 单次 transport 的证明全用 synthetic 响应，不调用 vendor CLI。

SYNTHETIC_KEY = "synthetic-private-input-only"


class FakeResponse:
    def __init__(self, status=200, body=None, error=None):
        self.status = status
        self.body = (
            body
            if body is not None
            else json.dumps(
                {
                    "code": 0,
                    "message": "ok",
                    "request_id": "synthetic-provider-id",
                    "data": {"item": [{"date": "2024-01-02"}]},
                }
            ).encode()
        )
        self.error = error
        self.read_sizes = []

    def read1(self, amount):
        self.read_sizes.append(amount)
        if self.error:
            raise self.error
        chunk, self.body = self.body[:amount], self.body[amount:]
        return chunk


@pytest.fixture
def fake_https(monkeypatch):
    state = {
        "connections": [],
        "requests": [],
        "response": FakeResponse(),
        "request_error": None,
        "response_error": None,
        "factory_error": None,
    }

    class FakeConnection:
        def __init__(self, host, port, *, timeout, context):
            if state["factory_error"]:
                raise state["factory_error"]
            state["connections"].append(self)
            self.target = (host, port, timeout)
            self.context = context
            self.closed = False

        def request(self, method, path, *, headers):
            state["requests"].append((method, path, headers))
            if state["request_error"]:
                raise state["request_error"]

        def getresponse(self):
            if state["response_error"]:
                raise state["response_error"]
            return state["response"]

        def close(self):
            self.closed = True

    def forbidden(*args, **kwargs):
        raise AssertionError("测试不能触网")

    monkeypatch.setattr(single, "_PROCESS_USED", False)
    monkeypatch.setattr(single, "_HTTPSConnection", FakeConnection)
    monkeypatch.setattr(socket, "socket", forbidden)
    return state


def assert_single_call(state, result):
    assert len(state["connections"]) == len(state["requests"]) == 1
    assert state["connections"][0].target == ("fuyao.aicubes.cn", 443, 30)
    assert state["connections"][0].closed
    assert result.metadata["http_request_invocations"] == 1
    assert result.metadata["request_id"] == "r01"
    assert result.metadata["planned_command"] == "market.calendar"
    assert result.metadata["capability_pass"] is False
    assert result.metadata["connected_endpoint"] is None
    assert SYNTHETIC_KEY not in json.dumps(result.metadata)


def test_single_attempt_success_and_contract_headers(fake_https):
    result = single.execute_one("r01", SYNTHETIC_KEY)
    assert_single_call(fake_https, result)
    assert result.metadata["stop_reason"] is None
    assert result.metadata["transport_success"] is True
    assert result.metadata["business_code"] == 0
    assert result.metadata["provider_request_id"] == "synthetic-provider-id"
    assert result.metadata["byte_count"] > 0
    assert result.data["item"]
    assert fake_https["requests"] == [
        (
            "GET",
            "/api/a-share/calendar/trading-days",
            {
                "X-api-key": SYNTHETIC_KEY,
                "X-THS-Skill-Id": "Hithink-Finance-CLI",
                "Accept": "application/json",
            },
        )
    ]


@pytest.mark.parametrize("stage", ["request_error", "response_error", "read_error"])
@pytest.mark.parametrize(
    "error,reason",
    [
        (OSError(SYNTHETIC_KEY + " raw network message"), "network_error"),
        (TimeoutError(SYNTHETIC_KEY + " raw timeout message"), "timeout"),
        (single.http.client.BadStatusLine(SYNTHETIC_KEY), "network_error"),
    ],
)
def test_single_attempt_exceptions_do_not_retry(fake_https, stage, error, reason):
    if stage == "read_error":
        fake_https["response"].error = error
    else:
        fake_https[stage] = error
    result = single.execute_one("r01", SYNTHETIC_KEY)
    assert_single_call(fake_https, result)
    assert result.metadata["stop_reason"] == reason
    assert result.metadata["transport_success"] is False
    assert "raw" not in json.dumps(result.metadata)


@pytest.mark.parametrize(
    "status", [301, 302, 303, 307, 308, 401, 403, 429, 400, 500, 502, 503, 504]
)
def test_single_attempt_http_stop_no_redirect(fake_https, status):
    # FakeResponse 无 Location 处理接口；重定向处理会直接使测试失败。
    fake_https["response"] = FakeResponse(status=status)
    result = single.execute_one("r01", SYNTHETIC_KEY)
    assert_single_call(fake_https, result)
    assert result.metadata["stop_reason"] == r1.stop_reason(status, 0)
    assert result.data is None


@pytest.mark.parametrize(
    "code", [2000, 2001, 2003, 2999, 4001, 5001, 5002, 5003, -1, 9999, None, "0", True]
)
def test_single_attempt_business_stop_no_retry(fake_https, code):
    fake_https["response"] = FakeResponse(
        body=json.dumps(
            {"code": code, "message": SYNTHETIC_KEY, "data": {"item": []}}
        ).encode()
    )
    result = single.execute_one("r01", SYNTHETIC_KEY)
    assert_single_call(fake_https, result)
    assert result.metadata["stop_reason"] is not None
    assert result.data is None


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff",
        b"[]",
        b"null",
        b"{}",
        b'{"code":0,"data":{"item":[]}}',
        b'{"code":0,"message":"ok","data":{"item":[]},"request_id":1}',
        b'{"code":0,"message":"ok"}',
        b'{"code":0,"message":"ok","data":null}',
        b'{"code":0,"message":"ok","data":{"item":[1]}}',
        b'{"code":0,"message":"ok","data":{"item":[]},"x":NaN}',
        b'{"code":9,"code":0,"message":"ok","data":{"item":[]}}',
    ],
)
def test_single_attempt_invalid_json_envelope_data(fake_https, body):
    fake_https["response"] = FakeResponse(body=body)
    result = single.execute_one("r01", SYNTHETIC_KEY)
    assert_single_call(fake_https, result)
    assert not result.metadata["transport_success"]
    assert result.metadata["stop_reason"] is not None
    assert result.data is None


def test_body_cap_stops_reading_immediately(fake_https):
    response = FakeResponse(body=b"x" * (single.BODY_CAP + 10000))
    fake_https["response"] = response
    result = single.execute_one("r01", SYNTHETIC_KEY)
    assert_single_call(fake_https, result)
    assert result.metadata["stop_reason"] == "body_cap_exceeded"
    assert result.metadata["byte_count"] == single.BODY_CAP + 1
    assert len(response.body) == 9999
    assert response.read_sizes[-1] == 1


def test_body_exact_cap_is_permitted(fake_https):
    body = json.dumps({"code": 0, "message": "ok", "data": {"item": []}}).encode()
    fake_https["response"] = FakeResponse(
        body=body + b" " * (single.BODY_CAP - len(body))
    )
    result = single.execute_one("r01", SYNTHETIC_KEY)
    assert_single_call(fake_https, result)
    assert result.metadata["transport_success"] is True
    assert result.metadata["byte_count"] == single.BODY_CAP


@pytest.mark.parametrize("first_failure", [False, True])
def test_process_latch_survives_success_and_failure(fake_https, first_failure):
    if first_failure:
        fake_https["request_error"] = OSError("synthetic")
    first = single.execute_one("r01", SYNTHETIC_KEY)
    second = single.execute_one("r02", SYNTHETIC_KEY)
    assert_single_call(fake_https, first)
    assert second.metadata["http_request_invocations"] == 0
    assert second.metadata["stop_reason"] == "process_already_used"


@pytest.mark.parametrize(
    "request_id",
    [
        "https://evil.invalid/",
        "//evil.invalid/",
        "/api/evil",
        "POST",
        "r01?host=evil",
        "r25",
    ],
)
def test_arbitrary_target_injection_fails_closed(fake_https, request_id):
    result = single.execute_one(request_id, SYNTHETIC_KEY)
    assert result.metadata["stop_reason"] is not None
    assert result.metadata["http_request_invocations"] == 0
    assert fake_https["requests"] == fake_https["connections"] == []
    assert request_id not in json.dumps(result.metadata)
    for keyword in ("host", "method", "path", "query", "url"):
        with pytest.raises(TypeError):
            single.execute_one("r01", SYNTHETIC_KEY, **{keyword: "evil"})


@pytest.mark.parametrize("key", ["", "bad\r\nInjected: yes", "bad\x00", "非ASCII"])
def test_key_input_header_injection_fails_closed(fake_https, key):
    result = single.execute_one("r01", key)
    assert result.metadata["stop_reason"] == "invalid_key_input"
    assert not fake_https["requests"]


def test_provider_credential_echo_never_reaches_safe_outputs(fake_https):
    fake_https["response"] = FakeResponse(
        body=json.dumps(
            {
                "code": 0,
                "message": "ok",
                "request_id": SYNTHETIC_KEY,
                "data": {"item": [{"echo": SYNTHETIC_KEY}]},
            }
        ).encode()
    )
    result = single.execute_one("r01", SYNTHETIC_KEY)
    assert_single_call(fake_https, result)
    assert result.metadata["provider_request_id"] is None
    assert result.metadata["stop_reason"] == "credential_echo"
    assert result.data is None


def test_cli_private_fd_no_key_argv_or_logs(fake_https, monkeypatch, capsys):
    import os

    read_fd, write_fd = os.pipe()
    seal_read, seal_write = os.pipe()
    response_read, response_write = os.pipe()
    os.write(seal_write, b"{}")
    os.close(seal_write)
    try:
        os.write(write_fd, SYNTHETIC_KEY.encode())
        os.close(write_fd)
        monkeypatch.setattr(single, "validate_seal", lambda _: None)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "executor",
                "--request-id",
                "r02",
                "--key-fd",
                str(read_fd),
                "--review-seal-fd",
                str(seal_read),
                "--response-fd",
                str(response_write),
            ],
        )
        assert SYNTHETIC_KEY not in " ".join(sys.argv)
        assert single.main() == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        meta = json.loads(captured.out)
        assert SYNTHETIC_KEY not in captured.out
        assert meta["http_request_invocations"] == 1
        assert meta["capability_pass"] is False
    finally:
        os.close(read_fd)
        os.close(seal_read)
        os.close(response_read)
        os.close(response_write)


@pytest.mark.parametrize(
    "argv",
    [
        ["--request-id", "r01", "--api-key", SYNTHETIC_KEY],
        ["--request-id", "r01", "--host", "evil.invalid"],
        ["--request-id", "r01", "--method", "POST"],
        ["--request-id", "r01", "--path", "/evil"],
    ],
)
def test_cli_rejects_unsupported_inputs_without_echo(
    fake_https, monkeypatch, capsys, argv
):
    monkeypatch.setattr(sys, "argv", ["executor", *argv])
    assert single.main() == 3
    captured = capsys.readouterr()
    assert SYNTHETIC_KEY not in captured.out + captured.err
    assert not fake_https["requests"]


def test_executor_cli_review_gate_precedes_key_read(fake_https, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("门禁前不读凭据或 catalog")

    monkeypatch.setattr(single, "_read_fd", forbidden)
    monkeypatch.setattr(sys, "argv", ["executor", "--request-id", "r01"])
    assert single.main() == 3
    assert (
        json.loads(capsys.readouterr().out)["stop_reason"] == "external_review_required"
    )
    assert not fake_https["requests"]


def test_all_frozen_rows_exact_endpoint_query_mapping():
    expected = {
        "market.calendar": ("/api/a-share/calendar/trading-days", set()),
        "symbol.list": (
            "/api/meta/tickers/list",
            {"exchange", "asset_type", "limit", "offset"},
        ),
        "symbol.search": ("/api/meta/tickers/search", {"q", "asset_type", "limit"}),
        "market.snapshot": (
            "/api/a-share/prices/snapshot",
            {"thscodes", "limit", "offset"},
        ),
        "market.history": (
            "/api/a-share/prices/historical",
            {"thscode", "start", "end", "adjust", "interval"},
        ),
        "market.corporate-actions": (
            "/api/a-share/corporate-actions/adjustment-factors",
            {"thscode", "from", "to"},
        ),
        "financials.balance-sheet": (
            "/api/a-share/financials/balance-sheets",
            {"thscode", "period", "limit"},
        ),
        "financials.income": (
            "/api/a-share/financials/income-statements",
            {"thscode", "period", "limit"},
        ),
        "financials.cash-flow": (
            "/api/a-share/financials/cash-flow-statements",
            {"thscode", "period", "limit"},
        ),
        "index.catalog": ("/api/a-share-index/catalog/ths-index-list", {"tag"}),
        "index.constituents": (
            "/api/a-share-index/constituents/ths-stock-list",
            {"thscode"},
        ),
    }
    plan = r1.load_plan()
    assert (
        len(plan["requests"])
        == sum(row["max_http_attempts"] for row in plan["requests"])
        == 24
    )
    assert plan["limits"]["hard_ceiling"] == 35
    assert set(single.COMMANDS) == set(expected)
    for row in plan["requests"]:
        prior = (
            synthetic_catalog("r21" if row["id"] == "r22" else "r23")
            if "selection" in row
            else None
        )
        actual_row, target = single.resolve_request(row["id"], prior)
        assert actual_row == row
        path, names = expected[row["command"]]
        assert target == {
            "scheme": "https",
            "host": "fuyao.aicubes.cn",
            "port": 443,
            "method": "GET",
            "path": path,
            "query": target["query"],
        }
        assert set(target["query"]) == names
        if row["command"] == "market.history":
            assert target["query"]["interval"] == "1d"
            assert target["query"]["start"] == "1704124800000"
            assert target["query"]["end"] == "1704902399999"
        if row["command"] == "market.corporate-actions":
            assert target["query"]["from"] == "2024-01-02"
            assert target["query"]["to"] == "2024-01-10"


def synthetic_catalog(request_id="r21", items=None):
    row, target = single.resolve_request(request_id)
    meta = single._metadata()
    meta.update(
        request_id=request_id,
        planned_command=row["command"],
        target=target,
        http_request_invocations=1,
        http_status=200,
        business_code=0,
        transport_success=True,
    )
    return single.Result(
        meta,
        {
            "item": items
            if items is not None
            else [{"thscode": "880002.TI"}, {"thscode": "880001.TI"}]
        },
    )


def test_dynamic_selection_only_from_corresponding_prior_catalog(fake_https):
    prior = synthetic_catalog()
    _, target = single.resolve_request("r22", prior)
    assert target["query"] == {"thscode": "880001.TI"}
    for invalid in (None, synthetic_catalog("r23"), "880001.TI"):
        with pytest.raises(ValueError):
            single.resolve_request("r22", invalid)
    prior.metadata["transport_success"] = False
    with pytest.raises(ValueError):
        single.resolve_request("r22", prior)
    with pytest.raises(ValueError):
        single.resolve_request("r22", synthetic_catalog(items=[{"name": "unknown"}]))
    result = single.execute_one(
        "r22", SYNTHETIC_KEY, catalog=synthetic_catalog(items=[])
    )
    assert result.metadata["stop_reason"] == "empty_catalog_skip"
    assert result.metadata["http_request_invocations"] == 0
    assert not fake_https["connections"]


def test_tdx_fixtures_taxonomy_unchanged_from_baseline():
    # 对照只读 baseline，不能通过重写 TDX fixtures 来满足新 transport 测试。
    for relative in (
        "provider_lab/fixtures.toml",
        "provider_lab/tdx_r1.matrix.json",
        "src/market_data_foundation/qualification/tdx_semantics.py",
    ):
        baseline = subprocess.check_output(
            ["git", "show", f"{r1.load_plan()['baseline']}:{relative}"], cwd=ROOT
        )
        assert (ROOT / relative).read_bytes() == baseline


def test_total_deadline_interrupts_continuous_reads(fake_https, monkeypatch):
    def slow_read(_):
        assert 0 < single.signal.getitimer(single.signal.ITIMER_REAL)[0] <= 30
        single.signal.raise_signal(single.signal.SIGALRM)

    monkeypatch.setattr(fake_https["response"], "read1", slow_read)
    result = single.execute_one("r01", SYNTHETIC_KEY)
    assert_single_call(fake_https, result)
    assert result.metadata["stop_reason"] == "timeout"
    assert single.signal.getitimer(single.signal.ITIMER_REAL)[0] == 0


def test_connection_creation_failure_is_zero_attempts_no_retry(fake_https):
    fake_https["factory_error"] = OSError(SYNTHETIC_KEY)
    result = single.execute_one("r01", SYNTHETIC_KEY)
    assert result.metadata["stop_reason"] == "network_error"
    assert result.metadata["http_request_invocations"] == 0
    assert fake_https["connections"] == fake_https["requests"] == []
    assert SYNTHETIC_KEY not in json.dumps(result.metadata)
    assert (
        single.execute_one("r01", SYNTHETIC_KEY).metadata["stop_reason"]
        == "process_already_used"
    )


def test_private_fd_rejects_files_stdout_and_stderr(tmp_path):
    import os

    for fd in (0, 1, 2):
        with pytest.raises(ValueError):
            single._private_fd(fd)
    file = tmp_path / "synthetic-input"
    file.write_text("synthetic")
    with file.open("rb") as stream:
        with pytest.raises(ValueError):
            single._private_fd(stream.fileno())
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"x" * 65)
        os.close(write_fd)
        with pytest.raises(ValueError):
            single._read_fd(read_fd, 64)
    finally:
        os.close(read_fd)


def test_executor_and_live_import_collection_no_network_or_secret_io(monkeypatch):
    import builtins
    import importlib
    import os

    def forbidden(*args, **kwargs):
        raise AssertionError("import/collection 不允许任何网络或 secret IO")

    for module, name in (
        (Path, "open"),
        (Path, "read_bytes"),
        (builtins, "open"),
        (os, "open"),
        (os, "read"),
        (socket, "socket"),
        (socket, "create_connection"),
        (subprocess, "Popen"),
        (single.http.client.HTTPSConnection, "connect"),
    ):
        monkeypatch.setattr(module, name, forbidden)
    importlib.reload(r1)
    importlib.reload(single)
    from provider_lab import fuyao_r1_live_runner

    importlib.reload(fuyao_r1_live_runner)
    spec = importlib.util.spec_from_file_location(
        "fuyao_live_collection_guarded", ROOT / "tests/test_fuyao_r1_live.py"
    )
    spec.loader.exec_module(importlib.util.module_from_spec(spec))


def test_incomplete_content_length_fails_closed(fake_https):
    fake_https["response"].length = 10
    result = single.execute_one("r01", SYNTHETIC_KEY)
    assert_single_call(fake_https, result)
    assert result.metadata["stop_reason"] == "network_error"
    assert result.data is None


def test_stdlib_wire_headers_only_vendor_contract_and_required_host(monkeypatch):
    sent = []

    def capture_send(self, data):
        sent.append(data)

    def forbidden(*args, **kwargs):
        raise AssertionError("wire 序列化测试不能建立连接")

    monkeypatch.setattr(single.http.client.HTTPConnection, "send", capture_send)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    connection = single._HTTPSConnection(single.HOST, single.PORT)
    try:
        connection.request(
            "GET",
            "/api/a-share/calendar/trading-days",
            headers={
                "X-api-key": SYNTHETIC_KEY,
                "X-THS-Skill-Id": "Hithink-Finance-CLI",
                "Accept": "application/json",
            },
        )
        lines = b"".join(sent).decode().split("\r\n")
        assert lines[0] == "GET /api/a-share/calendar/trading-days HTTP/1.1"
        header_names = {line.split(":", 1)[0] for line in lines[1:] if line}
        assert header_names == {"Host", "X-api-key", "X-THS-Skill-Id", "Accept"}
    finally:
        connection.close()
