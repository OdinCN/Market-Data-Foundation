"""Independent, fabricated BEA edge fixture; never real Source qualification.

Frozen expected parameters and rows do not call the adapter's request/mapping
builders. Spawned doubles prohibit external sockets and real credential loading.
"""

import base64
import builtins
import gzip
import hashlib
import json
import multiprocessing
import random
import socket
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import partial
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit

import psycopg
import pytest

from market_data_foundation.historical import bea_pce as pce
from market_data_foundation.historical import bea_pce_live as edge

REAL_KEY_LOADER = edge._load_key  # Used only with a mocked file in the child.

# Fabricated authentication marker, unrelated to any actual BEA credential.
DUMMY = "offline-BEA-23456789-abcd"
IDENTITY = edge.ExecutionIdentity(
    "offline-owner-local-fixture",
    "a" * 40,
    "b" * 64,
    "offline-attempt-001",
    "offline-observation-001",
)
PARAMS = {
    "method": "GetData",
    "DatasetName": "NIPA",
    "Frequency": "M",
    "Year": "2025,2026",
    "ResultFormat": "JSON",
}


def forbid_external(*_args, **_kwargs):
    raise AssertionError("offline_external_io_forbidden")


def dummy_loader():
    # Spawn does not inherit pytest monkeypatches, so guard inside the child too.
    socket.socket.connect = forbid_external
    socket.socket.connect_ex = forbid_external
    socket.create_connection = forbid_external
    socket.getaddrinfo = forbid_external
    psycopg.connect = forbid_external
    psycopg.Connection.connect = forbid_external
    edge._load_key = forbid_external
    return DUMMY


@pytest.fixture(autouse=True)
def no_external(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", forbid_external)
    monkeypatch.setattr(socket.socket, "connect_ex", forbid_external)
    monkeypatch.setattr(socket, "create_connection", forbid_external)
    monkeypatch.setattr(socket, "getaddrinfo", forbid_external)
    monkeypatch.setattr(psycopg, "connect", forbid_external)
    monkeypatch.setattr(psycopg.Connection, "connect", forbid_external)
    monkeypatch.setattr(edge, "_load_key", forbid_external)


def fabricated_response(table, key=DUMMY):
    """Independent frozen schema, mapping and toy operands, no captured bytes."""
    mom = table == "T20807"
    rows = []
    for line, series, description in (
        ("1", "DPCERGM" if mom else "DPCERG", "Personal consumption expenditures"),
        ("25", "DPCCRGM" if mom else "DPCCRG", "PCE excluding food and energy"),
    ):
        for period, value in (("2026M07", "103.000"), ("2025M07", "100.000")):
            rows.append(
                {
                    "TableName": table,
                    "LineNumber": line,
                    "SeriesCode": series,
                    "LineDescription": description,
                    "TimePeriod": period,
                    "METRIC_NAME": "Fisher Price Index",
                    "CL_UNIT": "Percent change" if mom else "Level",
                    "UNIT_MULT": "0",
                    "NoteRef": table if line == "1" else table + "," + table + ".4",
                    "DataValue": "0.2" if mom else value,
                }
            )
    return {
        "BEAAPI": {
            "Request": {
                "RequestParam": [
                    {"ParameterName": name, "ParameterValue": value}
                    for name, value in {
                        **PARAMS,
                        "TableName": table,
                        "UserID": key,
                    }.items()
                ]
            },
            "Results": {
                "Data": rows,
                "Statistic": "NIPA Table",
                "Dimensions": [
                    {
                        "Name": name,
                        "DataType": "numeric"
                        if name in {"LineNumber", "UNIT_MULT", "DataValue"}
                        else "string",
                        "IsValue": "1" if name == "DataValue" else "0",
                        "Ordinal": str(ordinal),
                    }
                    for ordinal, name in enumerate(
                        (
                            "TableName",
                            "SeriesCode",
                            "LineNumber",
                            "LineDescription",
                            "TimePeriod",
                            "CL_UNIT",
                            "UNIT_MULT",
                            "METRIC_NAME",
                            "DataValue",
                        ),
                        1,
                    )
                ],
                "UTCProductionTime": "2026-10-16T13:24:43.613",
                "Notes": [
                    {
                        "NoteRef": table,
                        "NoteText": (
                            "Table 2.8.7. Percent Change From Preceding Period "
                            "in Prices "
                            "for Personal Consumption Expenditures by Major Type of "
                            "Product, Monthly [Percent] - LastRevised: "
                            "September 30, 2026"
                        )
                        if mom
                        else (
                            "Table 2.8.4. Price Indexes for Personal Consumption "
                            "Expenditures by Major Type of Product, Monthly [Index "
                            "numbers, 2017=100; seasonally adjusted] - LastRevised: "
                            "September 30, 2026"
                        ),
                    }
                ],
            },
        }
    }


def serialize(value):
    return json.dumps(value, separators=(",", ":")).encode()


class Stream:
    def __init__(self, body, *, headers=None, status=200, clock=None, drip=0):
        self.body = body
        self.position = 0
        self.status = status
        self.headers = (
            headers
            if headers is not None
            else [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ]
        )
        self.reads = []
        self.clock = clock
        self.drip = drip

    def getheaders(self):
        return self.headers

    def read1(self, amount):
        assert 0 < amount <= edge.CHUNK
        self.reads.append(amount)
        if self.clock:
            self.clock[0] += self.drip
        chunk = self.body[self.position : self.position + amount]
        self.position += len(chunk)
        return chunk


@dataclass
class SyntheticTransport:
    scenario: str = "success"
    fail_table: str = "T20807"
    delay: float = 0.0
    transient_attempts: int = 0

    def exchange(self, table, key, deadline):
        assert key == DUMMY
        if self.delay:
            time.sleep(self.delay)
        document = fabricated_response(table, key)
        if table == self.fail_table:
            if self.scenario == "slow_drip":
                # Keeps making progress while exceeding the absolute deadline.
                while True:
                    time.sleep(0.005)
            if self.scenario == "exception":
                raise RuntimeError("https://apps.bea.gov/?UserID=" + key)
            if self.scenario == "timeout":
                raise edge.EdgeRejected("deadline_unknown")
            if self.scenario == "wrong_request":
                document["BEAAPI"]["Request"]["RequestParam"][0]["ParameterValue"] = (
                    "other"
                )
            if self.scenario == "wrong_mapping":
                document["BEAAPI"]["Results"]["Data"][0]["SeriesCode"] = "other"
            if self.scenario == "missing_operand":
                document["BEAAPI"]["Results"]["Data"] = []
            if self.scenario == "echo":
                document["BEAAPI"]["Results"]["Notes"][0]["NoteText"] += key
        if self.scenario == "bundle_oversize":
            document["BEAAPI"]["Results"]["UTCProductionTime"] = "X" * 1_050_000
        body = serialize(document)
        if self.scenario == "http_status_then_success" and table == self.fail_table:
            self.transient_attempts += 1
            status = 503 if self.transient_attempts <= 2 else 200
        else:
            status = (
                503
                if self.scenario == "http_status" and table == self.fail_table
                else 200
            )
        headers = None
        if self.scenario == "partial" and table == self.fail_table:
            headers = [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body) + 1)),
            ]
        if self.scenario == "invalid_header" and table == self.fail_table:
            headers = [
                ("Content-Type", "text/html"),
                ("Content-Length", str(len(body))),
            ]
        return edge._read_stream(
            Stream(body, headers=headers, status=status), deadline=deadline, key=key
        )


def acquire(transport=None, **kwargs):
    return edge.acquire(
        IDENTITY,
        enabled=True,
        provider_budget=2,
        transport=transport or SyntheticTransport(),
        credential_loader=dummy_loader,
        **kwargs,
    )


def test_default_off_and_exact_budget_do_not_load_or_start(monkeypatch):
    monkeypatch.setattr(edge, "_supervise", forbid_external)
    for enabled, budget in (
        (False, 0),
        (False, 2),
        (True, 0),
        (True, 1),
        (True, 3),
        (True, True),
        (True, 2.0),
        (True, "2"),
        (1, 2),
    ):
        result = edge.acquire(IDENTITY, enabled=enabled, provider_budget=budget)
        assert result.classification == "disabled_budget_zero"
        assert result.observations == () and result.projection is None


def test_reject_unbound_identity_and_unpaired_loader_before_effects(monkeypatch):
    monkeypatch.setattr(edge, "_supervise", forbid_external)
    result = edge.acquire(
        replace(IDENTITY, exact_plan_sha256="c" * 64), enabled=True, provider_budget=2
    )
    assert result.classification == "context_rejected"
    result = edge.acquire(
        IDENTITY, enabled=True, provider_budget=2, credential_loader=dummy_loader
    )
    assert result.classification == "context_rejected"
    assert (
        edge.acquire(object(), enabled=True, provider_budget=2).classification
        == "context_rejected"
    )


def test_explicit_acquire_selects_real_defaults_without_offline_boundary(monkeypatch):
    from market_data_foundation.historical import bea_pce_boundary as historical

    calls = []

    def supervise(identity, transport, loader):
        calls.append((identity, type(transport), loader))
        return edge.AcquisitionResult("credential_unavailable")

    monkeypatch.setattr(historical, "dry_run", forbid_external)
    monkeypatch.setattr(historical, "binding", forbid_external)
    monkeypatch.setattr(edge, "_supervise", supervise)
    # The loader is selected, never called in this dispatch check.
    result = edge.acquire(IDENTITY, enabled=True, provider_budget=2)
    assert result.classification == "credential_unavailable"
    assert calls == [(IDENTITY, edge._HttpsTransport, edge._load_key)]


def https_fixture_worker(
    pipe, buffer, sizes, identity, transport, loader, *, fail_table
):
    """Spawn instrumentation: real loader/HTTPS code, fabricated file and wire.

    The real secret file and all external IO are prohibited inside the child.
    This fixture exercises dispatch and protocol, not actual Source identity.
    """
    assert type(transport) is edge._HttpsTransport and loader is edge._load_key
    dummy_loader()
    loaded, closed = [], []

    def open_config(path, mode):
        assert str(path) == "/srv/market-data-foundation/config/local/secret.toml"
        assert mode == "rb" and not loaded
        loaded.append(True)
        return BytesIO(('[bea]\napi_key = "' + DUMMY + '"\n').encode())

    class Connection:
        def __init__(self, host, **kwargs):
            assert host == "apps.bea.gov" and loaded == [True]
            assert 0 < kwargs["timeout"] <= edge.REQUEST_SECONDS

        def request(self, method, path, *, headers):
            assert method == "GET" and urlsplit(path).path == "/api/data/"
            parsed = parse_qs(urlsplit(path).query)
            self.table = parsed["TableName"][0]
            assert self.table in {"T20807", "T20804"}
            assert parsed == {
                name: [value]
                for name, value in {
                    **PARAMS,
                    "TableName": self.table,
                    "UserID": DUMMY,
                }.items()
            }
            assert headers == {
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            }

        def getresponse(self):
            return Stream(
                serialize(fabricated_response(self.table)),
                status=503 if self.table == fail_table else 200,
            )

        def close(self):
            closed.append(self.table)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "open", open_config)
        patch.setattr(edge.http.client, "HTTPSConnection", Connection)
        patch.setattr(edge.ssl, "create_default_context", lambda: None)
        patch.setattr(edge.time, "sleep", lambda _seconds: None)
        edge._worker(pipe, buffer, sizes, identity, transport, loader)


@pytest.mark.parametrize("fail_table,count", [(None, 2), ("T20807", 1), ("T20804", 2)])
def test_public_default_pair_uses_spawn_loader_and_https_sequentially(
    monkeypatch, fail_table, count
):
    monkeypatch.setattr(edge, "_load_key", REAL_KEY_LOADER)
    monkeypatch.setattr(
        edge, "_worker", partial(https_fixture_worker, fail_table=fail_table)
    )
    result = edge.acquire(IDENTITY, enabled=True, provider_budget=2)
    assert [o.table for o in result.observations] == ["T20807", "T20804"][:count]
    assert DUMMY not in repr(result) and not result.source_qualified
    assert not multiprocessing.active_children()
    if fail_table is not None:
        assert result.classification == "http_status_rejected"
        assert result.observations[-1].sent == "unknown"
        assert result.observations[-1].transport_attempts == 3
        assert not result.observations[-1].response_complete
        assert result.projection is result.receipt is None
    else:
        assert result.classification == "candidate_pending_independent_readback"
        assert all(o.response_complete for o in result.observations)
        assert DUMMY.encode() not in result.projection + result.receipt
        assert b"UserID" not in result.projection + result.receipt
        check = edge.inspect_candidate(
            result.projection, result.receipt, expected=IDENTITY
        )
        assert not check["source_qualified"]


def test_first_table_503_503_200_retries_then_second_table_runs_once():
    result = acquire(SyntheticTransport("http_status_then_success", "T20807"))
    assert result.classification == "candidate_pending_independent_readback"
    assert [o.table for o in result.observations] == ["T20807", "T20804"]
    assert [o.transport_attempts for o in result.observations] == [3, 1]
    receipt = pce.decode(result.receipt)
    assert [row["transport_attempts"] for row in receipt["requests"]] == [3, 1]
    edge.inspect_candidate(result.projection, result.receipt, expected=IDENTITY)


@pytest.fixture(scope="module")
def successful_candidate():
    return acquire()


@pytest.mark.parametrize(
    "field,size", [("exact_code_git_sha", 40), ("code_manifest_sha256", 64)]
)
def test_valid_identity_hex_with_nul_byte_is_not_credential_echo(field, size):
    identity = replace(IDENTITY, **{field: "7800" + "a" * (size - 4)})
    result = edge._supervise(identity, SyntheticTransport(), dummy_loader)
    assert result.classification == "candidate_pending_independent_readback"
    assert len(result.observations) == 2
    assert all(observation.response_complete for observation in result.observations)
    assert json.loads(result.receipt)[field] == getattr(identity, field)
    edge.inspect_candidate(result.projection, result.receipt, expected=identity)


def test_independent_replay_and_safe_provenance(successful_candidate, tmp_path):
    result = successful_candidate
    assert result.classification == "candidate_pending_independent_readback"
    assert not result.source_qualified and not result.exact_original_retained
    assert [o.table for o in result.observations] == ["T20807", "T20804"]
    assert all(o.response_complete for o in result.observations)
    receipt = json.loads(result.receipt)
    assert receipt["execution_class"] == "synthetic"
    assert (
        receipt["source_projection_sha256"]
        == hashlib.sha256(result.projection).hexdigest()
    )
    assert receipt["source_projection_size"] == len(result.projection) <= 2_000_000
    assert receipt["requests"][0]["parameters"] == {**PARAMS, "TableName": "T20807"}
    assert receipt["requests"][1]["parameters"] == {**PARAMS, "TableName": "T20804"}
    for first, second in zip(result.observations, result.observations[1:]):
        assert first.obtained_at_utc <= second.requested_at_utc
    for response in receipt["requests"]:
        assert datetime.fromisoformat(
            response["requested_at_utc"]
        ).utcoffset() == timedelta(0)
        assert response["requested_at_utc"] <= response["obtained_at_utc"]
    assert DUMMY.encode() not in result.projection + result.receipt
    assert b"UserID" not in result.projection + result.receipt
    assert b"RequestParam" not in result.projection + result.receipt
    assert DUMMY not in repr(result)
    # A separate byte read and replay, not a claim of independent real identity.
    path = tmp_path / "synthetic-projection.bin"
    path.write_bytes(result.projection)
    check = edge.inspect_candidate(path.read_bytes(), result.receipt, expected=IDENTITY)
    assert not check["source_qualified"]
    assert check["independent_source_readback_required"]
    measurements = check["replay"]["measurements"]
    assert measurements["headline_mom_sa"]["value"] == "0.2"
    assert (
        measurements["core_mom_sa"]["classification"] == "provider_returned_calculation"
    )
    assert measurements["headline_yoy_sa"]["value"] == "3.000000"
    assert measurements["core_yoy_sa"]["classification"] == "product_derived"
    assert [v["reference"] for v in measurements["core_yoy_sa"]["inputs"]] == [
        "2026-07",
        "2025-07",
    ]
    assert all(v["adjustment"] == "seasonally_adjusted" for v in measurements.values())
    assert (
        check["replay"]["release_at"] is None
        and check["replay"]["provider_vintage"] is None
    )
    obtained = datetime.fromisoformat(check["replay"]["obtained_at"])
    plan = pce.build_plan(
        pce.PceRetainedEvidence(result.projection, "offline:acq", "synthetic:receipt"),
        identity="offline",
        finished_at=obtained,
        frozen_at=obtained,
    )
    assert len(plan.content) <= 32_000_000
    assert plan.run.budget == 0
    assert "candidate_only_not_active" in plan.binding_candidate.limitations


@pytest.mark.parametrize("table,count", [("T20807", 1), ("T20804", 2)])
@pytest.mark.parametrize(
    "scenario",
    [
        "exception",
        "timeout",
        "http_status",
        "partial",
        "invalid_header",
        "wrong_request",
        "wrong_mapping",
        "missing_operand",
        "echo",
    ],
)
def test_failure_or_unknown_stops_without_orphan(table, count, scenario):
    result = acquire(SyntheticTransport(scenario, table))
    assert len(result.observations) == count
    assert result.observations[-1].classification in edge._FAILURES
    assert result.projection is None and result.receipt is None
    assert not result.source_qualified
    assert DUMMY not in repr(result)


def test_oversize_free_metadata_is_rejected_before_second_request():
    result = acquire(SyntheticTransport("bundle_oversize", "T20804"))
    # Each body fits the wire cap, but metadata no longer permits opaque padding.
    assert result.classification == "source_shape_rejected"
    assert len(result.observations) == 1
    assert not result.observations[0].response_complete
    assert result.projection is None and result.receipt is None


@pytest.mark.parametrize(
    "scenario,table,expected",
    [
        ("slow_drip", "T20807", 1),
        ("slow_drip", "T20804", 2),
    ],
)
def test_parent_kills_slow_drip_no_idle_timeout_substitute(scenario, table, expected):
    before = time.monotonic()
    result = edge._supervise(
        IDENTITY,
        SyntheticTransport(scenario, table),
        dummy_loader,
        request_seconds=0.08,
        network_seconds=0.16,
        attempt_seconds=3.0,
    )
    assert result.classification == "deadline_unknown"
    assert len(result.observations) == expected
    assert result.projection is None
    assert time.monotonic() - before < 3.0
    assert not multiprocessing.active_children()


def test_total_attempt_deadline_includes_credential_loading():
    result = edge._supervise(
        IDENTITY, SyntheticTransport(), slow_loader, attempt_seconds=0.08
    )
    assert result.classification == "deadline_unknown"
    assert not result.observations and result.projection is None
    assert not multiprocessing.active_children()


def slow_loader():
    time.sleep(2)
    return dummy_loader()


@pytest.mark.parametrize("size", [1_999_999, 2_000_000, 2_000_001])
@pytest.mark.parametrize("compressed", [False, True])
def test_wire_and_decoded_inclusive_size_bound(size, compressed):
    plain = b"x" * size
    body = gzip.compress(plain) if compressed else plain
    headers = [("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
    if compressed:
        headers.append(("Content-Encoding", "gzip"))
    stream = Stream(body, headers=headers)
    if size > 2_000_000:
        with pytest.raises(edge.EdgeRejected, match="size_rejected"):
            edge._read_stream(stream, deadline=time.monotonic() + 10)
        if not compressed:
            assert not stream.reads  # Content-Length rejects before body IO.
    else:
        assert edge._read_stream(stream, deadline=time.monotonic() + 10) == plain
    assert all(amount <= 16_384 for amount in stream.reads)


def test_wire_actual_over_limit_and_partial_and_truncated_gzip():
    stream = Stream(
        b"x" * 2_000_001,
        headers=[("Content-Type", "application/json"), ("Content-Length", "2000000")],
    )
    with pytest.raises(edge.EdgeRejected, match="size_rejected"):
        edge._read_stream(stream, deadline=time.monotonic() + 10)
    body = gzip.compress(b"hi")[:-2]
    with pytest.raises(edge.EdgeRejected, match="decode_rejected"):
        edge._read_stream(
            Stream(
                body,
                headers=[
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                    ("Content-Encoding", "gzip"),
                ],
            ),
            deadline=time.monotonic() + 10,
        )


@pytest.mark.parametrize(
    "headers",
    [
        [("Content-Length", "-1")],
        [("Content-Length", "2000001")],
        [("Content-Length", "3"), ("Content-Length", "3")],
        [("Content-Length", "3"), ("Location", "https://other.invalid/")],
        [("Content-Length", "3"), ("Transfer-Encoding", "chunked")],
        [("Bad Header", "x")],
        [("X-Test", "bad\r\nvalue")],
        [("Content-Encoding", "br")],
    ],
)
def test_invalid_headers_rejected_without_body_read(headers):
    stream = Stream(b"abc", headers=[("Content-Type", "application/json"), *headers])
    with pytest.raises(edge.EdgeRejected):
        edge._read_stream(stream, deadline=time.monotonic() + 10)
    assert not stream.reads


def test_duplicate_irrelevant_headers_are_allowed_but_not_used_for_framing():
    stream = Stream(
        b"abc",
        headers=[
            ("Content-Type", "application/json"),
            ("Content-Length", "3"),
            ("Strict-Transport-Security", "max-age=2592000"),
            (
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains; preload",
            ),
        ],
    )
    assert edge._read_stream(stream, deadline=time.monotonic() + 10) == b"abc"


@pytest.mark.parametrize("status", [201, 301, 302, 307, 400, 429, 500, 503])
def test_http_status_no_redirect_retry_fallback(status):
    stream = Stream(b"abc", status=status)
    with pytest.raises(edge.EdgeRejected, match="http_status_rejected"):
        edge._read_stream(stream, deadline=time.monotonic() + 10)
    assert not stream.reads


def test_slow_stream_absolute_monotonic_deadline():
    clock = [0.0]
    with pytest.raises(edge.EdgeRejected, match="deadline_unknown"):
        edge._read_stream(
            Stream(b"x" * 50_000, clock=clock, drip=3.4),
            monotonic=lambda: clock[0],
            deadline=10,
        )
    assert clock[0] == pytest.approx(10.2)


@pytest.mark.parametrize(
    "echo",
    [
        DUMMY,
        quote(DUMMY, safe=""),
        "".join("%%%02X" % b for b in DUMMY.encode()),
        quote("".join("%%%02X" % b for b in DUMMY.encode()), safe=""),
        base64.b64encode(DUMMY.encode()).decode(),
        DUMMY.encode().hex(),
        "".join("&#%d;" % b for b in DUMMY.encode()),
        "".join("&#x%x;" % b for b in DUMMY.encode()),
        "".join("\\u%04x" % b for b in DUMMY.encode()),
        base64.b64encode(("prefix=" + DUMMY + "&x=y").encode()).decode(),
        "https://apps.bea.gov/api/data/?UserID=" + DUMMY,
        {"Request": {"RequestParam": "unexpected"}},
    ],
)
def test_residual_encoded_echo_rejected_before_digest(echo, monkeypatch):
    document = fabricated_response("T20807")
    document["BEAAPI"]["Results"]["Unexpected"] = echo
    monkeypatch.setattr(hashlib, "sha256", forbid_external)
    with pytest.raises(edge.EdgeRejected, match="credential_echo_rejected"):
        edge._clean_capture("T20807", serialize(document), DUMMY)


def test_allowed_request_auth_slot_only_and_duplicate_key():
    raw = serialize(fabricated_response("T20807"))
    assert edge._clean_capture("T20807", raw, DUMMY) == raw
    document = fabricated_response("T20807")
    document["BEAAPI"]["Request"]["RequestParam"].append(
        {"ParameterName": "extra", "ParameterValue": DUMMY}
    )
    with pytest.raises(edge.EdgeRejected):
        edge._clean_capture("T20807", serialize(document), DUMMY)
    with pytest.raises(edge.EdgeRejected):
        edge._clean_capture("T20807", b'{"BEAAPI":{},"BEAAPI":{}}', DUMMY)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_qualified", True),
        ("exact_original_retained", True),
        ("source_projection_size", 1),
        ("source_projection_sha256", "0" * 64),
        ("execution_class", "qualified"),
        ("endpoint", "https://other.invalid/"),
        ("controlled_observation_id", "arbitrary-nonempty"),
    ],
)
def test_arbitrary_receipt_cannot_qualify(successful_candidate, field, value):
    receipt = json.loads(successful_candidate.receipt)
    receipt[field] = value
    with pytest.raises(edge.EdgeRejected):
        edge.inspect_candidate(
            successful_candidate.projection, serialize(receipt), expected=IDENTITY
        )


def test_no_nonempty_string_receipt_or_changed_projection(successful_candidate):
    with pytest.raises((ValueError, edge.EdgeRejected)):
        edge.inspect_candidate(
            successful_candidate.projection, b'"nonempty"', expected=IDENTITY
        )
    with pytest.raises(edge.EdgeRejected):
        edge.inspect_candidate(
            successful_candidate.projection + b" ",
            successful_candidate.receipt,
            expected=IDENTITY,
        )


def test_real_transport_constructs_only_named_wire_request(monkeypatch):
    connections = []

    class Connection:
        def __init__(self, host, **kwargs):
            assert host == "apps.bea.gov"
            connections.append(self)
            self.closed = False

        def request(self, method, path, *, headers):
            assert method == "GET" and path.startswith("/api/data/?")
            assert "UserID=" + DUMMY in path
            assert "Year=2025%2C2026" in path
            assert headers["Accept-Encoding"] == "identity"

        def getresponse(self):
            return Stream(serialize(fabricated_response("T20807")))

        def close(self):
            self.closed = True

    # Entire HTTPS surface is replaced; no socket/DNS/TLS/network is performed.
    monkeypatch.setattr(edge.http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(edge.ssl, "create_default_context", lambda: SimpleNamespace())
    monkeypatch.setattr(edge.http.client, "_MAXLINE", 8192)
    monkeypatch.setattr(edge.http.client, "_MAXHEADERS", 64)
    edge._HttpsTransport().exchange("T20807", DUMMY, time.monotonic() + 10)
    assert len(connections) == 1 and connections[0].closed


def test_combined_network_budget_stops_second_exchange():
    # Each 65ms response fits 100ms; together they exceed the 110ms network cap.
    # Spawn/import time is not the budget under test. Leave startup headroom on
    # contended hosts, while keeping both network limits and completion assertions.
    # The separate total-attempt test below still exercises its tight deadline.
    result = edge._supervise(
        IDENTITY,
        SyntheticTransport(delay=0.065),
        dummy_loader,
        request_seconds=0.10,
        network_seconds=0.11,
        attempt_seconds=10.0,
    )
    assert result.classification == "deadline_unknown"
    assert len(result.observations) == 2
    assert result.observations[0].response_complete
    assert not result.observations[1].response_complete
    assert result.projection is None


def test_total_attempt_deadline_during_local_sanitization(monkeypatch):
    # The same parent supervision remains active after both network exchanges.
    # The fake worker uses the real handshake then intentionally stalls locally.
    monkeypatch.setattr(edge, "_worker", stall_after_two)
    result = edge._supervise(
        IDENTITY,
        SyntheticTransport(),
        dummy_loader,
        request_seconds=0.10,
        network_seconds=0.20,
        # Spawn/import startup can exceed 0.5s on isolated CI. Keep the network
        # budget tight and stall far beyond the total test deadline below.
        attempt_seconds=3.0,
    )
    assert result.classification == "deadline_unknown"
    assert len(result.observations) == 2
    assert all(o.response_complete for o in result.observations)
    assert result.projection is None and result.receipt is None


def stall_after_two(pipe, _buffer, _size, _identity, _transport, _loader):
    for _ in range(2):
        pipe.send_bytes(b"ready")
        pipe.recv_bytes(1024)
        pipe.send_bytes(b"complete")
        pipe.recv_bytes(1024)
    time.sleep(10)


@pytest.mark.parametrize("stage", ["dns", "tls", "headers", "body"])
def test_parent_kills_every_blocking_exchange_stage(stage):
    result = edge._supervise(
        IDENTITY,
        BlockingStageTransport(stage),
        dummy_loader,
        request_seconds=0.05,
        network_seconds=0.10,
        attempt_seconds=3.0,
    )
    assert result.classification == "deadline_unknown"
    assert len(result.observations) == 1
    assert result.projection is None
    assert not multiprocessing.active_children()


@dataclass
class BlockingStageTransport:
    stage: str

    def exchange(self, table, key, deadline):
        # Simulates the same opaque blocking call at each named stage; no DNS,
        # socket, TLS or HTTP request actually happens in this qualification.
        assert self.stage in {"dns", "tls", "headers", "body"}
        time.sleep(2)


def test_header_echo_and_invalid_compression_never_leak():
    for headers, body, classification in (
        ([("X-Test", DUMMY)], b"{}", "credential_echo_rejected"),
        ([("Content-Encoding", "gzip")], b"not gzip", "decode_rejected"),
    ):
        stream = Stream(
            body,
            headers=[
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
                *headers,
            ],
        )
        with pytest.raises(edge.EdgeRejected, match=classification):
            edge._read_stream(stream, deadline=time.monotonic() + 10, key=DUMMY)


def test_context_echo_rejected_without_any_send():
    identity = replace(IDENTITY, producer_execution_identity=DUMMY)
    result = edge._supervise(
        identity,
        SyntheticTransport(),
        dummy_loader,
    )
    assert result.classification == "credential_echo_rejected"
    assert not result.observations and result.projection is None
    assert DUMMY not in repr(result)


def test_synthetic_transport_cannot_load_real_config():
    result = edge.acquire(
        IDENTITY, enabled=True, provider_budget=2, transport=SyntheticTransport()
    )
    assert result.classification == "context_rejected"


def test_receipt_unknown_envelope_rejected(successful_candidate):
    receipt = json.loads(successful_candidate.receipt)
    receipt["Request"] = "arbitrary"
    with pytest.raises((ValueError, edge.EdgeRejected)):
        edge.inspect_candidate(
            successful_candidate.projection, serialize(receipt), expected=IDENTITY
        )


def test_utc_regression_stops_before_second_send(monkeypatch):
    dates = iter(
        [
            "2026-10-16T08:00:01+00:00",
            "2026-10-16T08:00:02+00:00",
            "2026-10-16T08:00:00+00:00",
        ]
    )
    monkeypatch.setattr(edge, "_utc", lambda: next(dates))
    result = acquire()
    assert result.classification == "source_shape_rejected"
    assert len(result.observations) == 1 and result.observations[0].response_complete
    assert result.projection is None


def test_parent_start_failure_is_sanitized_and_closed(monkeypatch):
    context = multiprocessing.get_context("spawn")
    monkeypatch.setattr(context.Process, "start", forbid_external)
    result = acquire()
    assert result.classification == "worker_unknown"
    assert not result.observations and result.projection is None


def test_real_wire_transport_does_not_retry_exception_or_emit_url(monkeypatch):
    calls = []

    class Connection:
        def __init__(self, host, **kwargs):
            calls.append(host)

        def request(self, *args, **kwargs):
            raise TimeoutError("https://apps.bea.gov/?UserID=" + DUMMY)

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(edge.http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(edge.ssl, "create_default_context", lambda: None)
    monkeypatch.setattr(edge.http.client, "_MAXLINE", 8192)
    monkeypatch.setattr(edge.http.client, "_MAXHEADERS", 64)
    with pytest.raises(edge.EdgeRejected, match="^transport_unknown$") as caught:
        edge._HttpsTransport().exchange("T20807", DUMMY, time.monotonic() + 10)
    assert DUMMY not in str(caught.value)
    assert calls == ["apps.bea.gov", "closed"]


def test_real_wire_header_parse_failure_is_terminal_one_attempt(monkeypatch):
    calls = []

    class Connection:
        def __init__(self, host, **kwargs):
            assert host == "apps.bea.gov"
            calls.append("connect")

        def request(self, *args, **kwargs):
            calls.append("request")

        def getresponse(self):
            calls.append("getresponse")
            raise edge.http.client.LineTooLong("synthetic header line")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(edge.http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(edge.ssl, "create_default_context", lambda: None)
    pipe = CapturingPipe()
    with pytest.raises(edge.EdgeRejected, match="^header_rejected$"):
        edge._request_table(
            pipe,
            edge._HttpsTransport(),
            "T20807",
            DUMMY,
            logical_deadline=time.monotonic() + 10,
            per_attempt_seconds=10,
        )
    assert calls == ["connect", "request", "getresponse", "close"]
    assert pipe.frames == []


def test_real_wire_short_body_retries_three_transport_attempts(monkeypatch):
    connections = []

    class Connection:
        def __init__(self, host, **kwargs):
            assert host == "apps.bea.gov"
            connections.append(self)

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Stream(
                b"{}",
                headers=[
                    ("Content-Type", "application/json"),
                    ("Content-Length", "20"),
                ],
            )

        def close(self):
            pass

    monkeypatch.setattr(edge.http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(edge.ssl, "create_default_context", lambda: None)
    monkeypatch.setattr(edge.time, "sleep", lambda _seconds: None)
    pipe = CapturingPipe()
    with pytest.raises(edge.EdgeRejected, match="^transport_unknown$"):
        edge._request_table(
            pipe,
            edge._HttpsTransport(),
            "T20807",
            DUMMY,
            logical_deadline=time.monotonic() + 10,
            per_attempt_seconds=10,
        )
    assert len(connections) == 3
    assert pipe.frames == [b"retry", b"retry"]


def test_retry_count_not_incremented_until_next_transport_enters(monkeypatch):
    calls = []

    def exchange(self, table, key, deadline):
        calls.append(table)
        raise edge.EdgeRejected("http_status_rejected", 503)

    def cancel_during_backoff(_seconds):
        raise edge.EdgeRejected("deadline_unknown")

    monkeypatch.setattr(edge._HttpsTransport, "exchange", exchange)
    monkeypatch.setattr(edge.time, "sleep", cancel_during_backoff)
    pipe = CapturingPipe()
    with pytest.raises(edge.EdgeRejected, match="^deadline_unknown$"):
        edge._request_table(
            pipe,
            edge._HttpsTransport(),
            "T20807",
            DUMMY,
            logical_deadline=time.monotonic() + 10,
            per_attempt_seconds=10,
        )
    assert calls == ["T20807"]
    assert pipe.frames == []


class SecretTypedError(edge.EdgeRejected):
    def __str__(self):
        raise AssertionError("untrusted_str_must_not_run")

    def __repr__(self):
        raise AssertionError("untrusted_repr_must_not_run")


def typed_failure(kind):
    if kind == "subclass":
        return SecretTypedError("size_rejected")
    failure = edge.EdgeRejected("size_rejected")
    failure.args = (
        DUMMY
        if kind == "args"
        else DUMMY * 10000
        if kind == "long"
        else "https://apps.bea.gov/?UserID=" + DUMMY,
    )
    return failure


@dataclass
class CorrectionTransport:
    echo: str | None = None
    failure: str | None = None
    fail_table: str = "T20807"
    calls: list | None = None

    def exchange(self, table, key, deadline):
        if self.calls is not None:
            self.calls.append(table)
        if table == self.fail_table and self.failure:
            if self.failure == "partial":
                return edge._read_stream(
                    Stream(
                        b"{}",
                        headers=[
                            ("Content-Type", "application/json"),
                            ("Content-Length", "3"),
                        ],
                    ),
                    deadline=deadline,
                    key=key,
                )
            if self.failure == "unknown":
                raise RuntimeError("https://apps.bea.gov/?UserID=" + key)
            raise typed_failure(self.failure)
        document = fabricated_response(table, key)
        if table == self.fail_table and self.echo is not None:
            # Credential probes use a retained free-text field. Malformed
            # production timestamps now fail their field contract first.
            note = document["BEAAPI"]["Results"]["Notes"][0]
            note["NoteText"] = note["NoteText"].replace(
                " - LastRevised:", " " + self.echo + " - LastRevised:"
            )
        return serialize(document)


class CapturingPipe:
    """Bytes-only in-memory worker protocol, captures the actual IPC frames."""

    def __init__(self):
        self.frames = []
        self.closed = False

    def send_bytes(self, frame):
        assert type(frame) is bytes
        self.frames.append(frame)

    def recv_bytes(self, maximum):
        assert maximum == 1024
        second = 2 * (self.frames.count(b"ready") - 1)
        if self.frames[-1] == b"ready":
            return serialize(
                {
                    "deadline": time.monotonic() + 10,
                    "requested": f"2026-10-16T08:00:0{second}+00:00",
                }
            )
        assert self.frames[-1] == b"complete"
        return serialize({"obtained": f"2026-10-16T08:00:0{second + 1}+00:00"})

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "field",
    ["producer_execution_identity", "attempt_identity", "controlled_observation_id"],
)
@pytest.mark.parametrize("encoding", ["literal", "hex", "base64", "urlsafe_base64"])
def test_identity_encoded_credential_rejected_before_transport(field, encoding):
    # A 27-byte fabricated key keeps base64 unpadded and valid as identity text.
    key = DUMMY + "XX"
    encoded = {
        "literal": key,
        "hex": key.encode().hex(),
        "base64": base64.b64encode(key.encode()).decode(),
        "urlsafe_base64": base64.urlsafe_b64encode(key.encode()).decode(),
    }[encoding]
    identity = replace(IDENTITY, **{field: "prefix-" + encoded + "-suffix"})
    identity.validate()
    pipe, output, sizes = CapturingPipe(), bytearray(edge.OUTPUT_LIMIT), [0, 0]
    transport = CorrectionTransport(calls=[])
    edge._worker(pipe, output, sizes, identity, transport, lambda: key)
    assert transport.calls == []
    assert pipe.closed and pipe.frames == [b"credential_echo_rejected"]
    assert sizes == [0, 0] and not any(output)


@pytest.mark.parametrize(
    "field",
    ["producer_execution_identity", "attempt_identity", "controlled_observation_id"],
)
@pytest.mark.parametrize("encoding", ["base64", "urlsafe_base64"])
def test_unpadded_identity_credential_rejected_before_transport(field, encoding):
    key = DUMMY
    encoded = {
        "base64": base64.b64encode(key.encode()).decode().rstrip("="),
        "urlsafe_base64": base64.urlsafe_b64encode(key.encode()).decode().rstrip("="),
    }[encoding]
    identity = replace(IDENTITY, **{field: "prefix-" + encoded + "-suffix"})
    identity.validate()
    pipe, output, sizes = CapturingPipe(), bytearray(edge.OUTPUT_LIMIT), [0, 0]
    transport = CorrectionTransport(calls=[])
    edge._worker(pipe, output, sizes, identity, transport, lambda: key)
    assert transport.calls == []
    assert pipe.closed and pipe.frames == [b"credential_echo_rejected"]
    assert sizes == [0, 0] and not any(output)


@pytest.mark.parametrize(
    "field",
    ["producer_execution_identity", "attempt_identity", "controlled_observation_id"],
)
@pytest.mark.parametrize(
    "codec", ["utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"]
)
@pytest.mark.parametrize("prefix", ["", "x:"])
def test_identity_wrapped_credential_rejected_before_transport(field, codec, prefix):
    # Includes the independent x:key and UTF-16LE unpadded urlsafe counterexamples.
    key = DUMMY
    encoded = base64.urlsafe_b64encode((prefix + key).encode(codec)).decode()
    identity = replace(IDENTITY, **{field: encoded.rstrip("=")})
    identity.validate()
    pipe, output, sizes = CapturingPipe(), bytearray(edge.OUTPUT_LIMIT), [0, 0]
    transport = CorrectionTransport(calls=[])
    edge._worker(pipe, output, sizes, identity, transport, lambda: key)
    assert transport.calls == []
    assert pipe.closed and pipe.frames == [b"credential_echo_rejected"]
    assert sizes == [0, 0] and not any(output)


@pytest.mark.parametrize("field", list(IDENTITY.__dataclass_fields__))
def test_all_six_identity_fields_scanned_before_transport(field):
    key = getattr(IDENTITY, field)[:16]
    pipe, output, sizes = CapturingPipe(), bytearray(edge.OUTPUT_LIMIT), [0, 0]
    transport = CorrectionTransport(calls=[])
    edge._worker(pipe, output, sizes, IDENTITY, transport, lambda: key)
    assert transport.calls == []
    assert pipe.closed and pipe.frames == [b"credential_echo_rejected"]
    assert sizes == [0, 0] and not any(output)


@pytest.mark.parametrize(
    "codec", ["utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"]
)
@pytest.mark.parametrize("wrapper", ["hex", "base64", "urlsafe_base64", "nested"])
def test_identity_scanner_decodes_actual_credentials(codec, wrapper):
    raw = ("x:" + DUMMY.swapcase()).encode(codec)
    encoded = {
        "hex": raw.hex(),
        "base64": base64.b64encode(raw).decode().rstrip("="),
        "urlsafe_base64": base64.urlsafe_b64encode(raw).decode().rstrip("="),
        "nested": quote("".join(f"&#{ord(char)};" for char in raw.hex()), safe=""),
    }[wrapper]
    with pytest.raises(edge.EdgeRejected, match="credential_echo_rejected"):
        edge._identity_credential_scan(encoded, DUMMY)


@pytest.mark.parametrize(
    "value",
    [
        "token-credential-secret",
        "https://example.invalid/?api_key=unrelated",
        "7800" + "a" * 60,
        base64.urlsafe_b64encode(b"\xff\x00unrelated\xfe").decode().rstrip("="),
        base64.urlsafe_b64encode("unrelated".encode("utf-32-le")).decode(),
    ],
)
def test_identity_scanner_allows_noncredential_text_and_binary(value):
    edge._identity_credential_scan(value, DUMMY)


def test_identity_scanner_encoding_budget_exhaustion_fails_closed():
    nested = "unrelated"
    for _ in range(12):
        nested = base64.urlsafe_b64encode(nested.encode()).decode()
    with pytest.raises(edge.EdgeRejected, match="credential_echo_rejected"):
        edge._identity_credential_scan(nested, DUMMY)


@pytest.mark.parametrize(
    "field,size", [("exact_code_git_sha", 40), ("code_manifest_sha256", 64)]
)
@pytest.mark.parametrize("encoding", ["hex", "base64"])
def test_digest_equal_to_encoded_credential_rejected_before_transport(
    field, size, encoding
):
    if encoding == "hex":
        key = "offline-" + "x" * (size // 2 - len("offline-"))
        encoded = key.encode().hex()
    else:
        # This base64 fragment is also valid lowercase hex; its decoded dummy
        # key is printable ASCII, so digest shape validation alone cannot help.
        encoded = "a0dd" * (size // 4)
        key = base64.b64decode(encoded).decode()
    assert len(encoded) == size
    identity = replace(IDENTITY, **{field: encoded})
    identity.validate()
    pipe, output, sizes = CapturingPipe(), bytearray(edge.OUTPUT_LIMIT), [0, 0]
    transport = CorrectionTransport(calls=[])
    edge._worker(pipe, output, sizes, identity, transport, lambda: key)
    assert transport.calls == []
    assert pipe.closed and pipe.frames == [b"credential_echo_rejected"]
    assert sizes == [0, 0] and not any(output)


def capture_failed_worker(transport, monkeypatch, expected_calls):
    pipe, output, sizes = CapturingPipe(), bytearray(edge.OUTPUT_LIMIT), [0, 0]
    # A rejected response must never reach a digest, retained bundle or file IO.
    with monkeypatch.context() as guard:
        guard.setattr(hashlib, "sha256", forbid_external)
        guard.setattr(pce, "retain_sanitized_bundle", forbid_external)
        guard.setattr(edge.Path, "open", forbid_external)
        guard.setattr(edge.Path, "write_bytes", forbid_external)
        guard.setattr(edge.Path, "write_text", forbid_external)
        edge._worker(pipe, output, sizes, IDENTITY, transport, dummy_loader)
    assert pipe.closed
    assert transport.calls == expected_calls
    assert sizes == [0, 0] and not any(output)
    assert pipe.frames[-1] in edge._FAILURE_FRAMES.values()
    assert set(pipe.frames) <= {b"ready", b"complete", b"retry"} | set(
        edge._FAILURE_FRAMES.values()
    )
    assert DUMMY.encode() not in b"".join(pipe.frames)
    return pipe.frames


@pytest.mark.parametrize("failure", ["partial", "unknown"])
@pytest.mark.parametrize("table", ["T20807", "T20804"])
def test_partial_unknown_worker_never_hashes_persists_or_emits_orphan(
    failure, table, monkeypatch
):
    if failure == "partial":
        expected = ["T20807"] * 3 if table == "T20807" else ["T20807"] + ["T20804"] * 3
    else:
        expected = ["T20807"] if table == "T20807" else ["T20807", "T20804"]
    capture_failed_worker(
        CorrectionTransport(failure=failure, fail_table=table, calls=[]),
        monkeypatch,
        expected,
    )


ENCODINGS = (
    "utf-8",
    "utf-16",
    "utf-16-le",
    "utf-16-be",
    "utf-32",
    "utf-32-le",
    "utf-32-be",
)


@pytest.mark.parametrize("codec", ENCODINGS)
@pytest.mark.parametrize("wrapper", ["base64", "hex", "nested"])
def test_wide_encoded_echo_never_reaches_worker_ipc_or_digest(
    codec, wrapper, monkeypatch
):
    encoded = DUMMY.encode(codec)
    echo = encoded.hex() if wrapper == "hex" else base64.b64encode(encoded).decode()
    if wrapper == "nested":
        echo = quote(base64.b64encode(echo.encode()).decode(), safe="")
    frames = capture_failed_worker(
        CorrectionTransport(echo=echo, calls=[]), monkeypatch, ["T20807"]
    )
    assert frames == [b"ready", b"credential_echo_rejected"]


@pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"])
def test_nul_interleaved_echo_stops_first_exchange(codec, monkeypatch):
    echo = DUMMY.encode(codec).decode("utf-8")
    frames = capture_failed_worker(
        CorrectionTransport(echo=echo, calls=[]), monkeypatch, ["T20807"]
    )
    assert frames == [b"ready", b"credential_echo_rejected"]


@pytest.mark.parametrize("failure", ["subclass", "args", "long", "url"])
@pytest.mark.parametrize("table", ["T20807", "T20804"])
def test_typed_exception_ipc_frames_are_only_trusted_constants(
    failure, table, monkeypatch
):
    expected = ["T20807"] if table == "T20807" else ["T20807", "T20804"]
    frames = capture_failed_worker(
        CorrectionTransport(failure=failure, fail_table=table, calls=[]),
        monkeypatch,
        expected,
    )
    assert frames[-1] == b"worker_unknown"


@pytest.mark.parametrize("failure", ["subclass", "args", "long", "url"])
@pytest.mark.parametrize("table,count", [("T20807", 1), ("T20804", 2)])
def test_typed_exception_spawn_protocol_has_no_orphan(failure, table, count):
    result = acquire(CorrectionTransport(failure=failure, fail_table=table))
    assert result.classification == "worker_unknown"
    assert len(result.observations) == count
    assert result.projection is None and result.receipt is None
    assert not result.source_qualified and DUMMY not in repr(result)
    assert not multiprocessing.active_children()


@pytest.mark.parametrize("codec", ENCODINGS)
@pytest.mark.parametrize("field", ["production", "note", "description", "value"])
def test_projection_schema_rejects_opaque_source_fields(field, codec):
    document = fabricated_response("T20807")
    results = document["BEAAPI"]["Results"]
    # Deliberately unrelated opaque data, so this tests the schema, not key search.
    opaque = base64.b64encode("unreviewed opaque metadata".encode(codec)).decode()
    if field == "production":
        results["UTCProductionTime"] = opaque
    elif field == "note":
        results["Notes"][0]["NoteText"] += opaque
    else:
        row = results["Data"][0]
        row[
            {
                "description": "LineDescription",
                "value": "DataValue",
            }[field]
        ] = opaque
    with pytest.raises(edge.EdgeRejected):
        edge._clean_capture("T20807", serialize(document), DUMMY)


@pytest.mark.parametrize(
    "codec", ["utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"]
)
def test_non_utf8_json_wire_fails_closed(codec):
    raw = serialize(fabricated_response("T20807")).decode().encode(codec)
    with pytest.raises(edge.EdgeRejected):
        edge._clean_capture("T20807", raw, DUMMY)


def test_admitted_iso_metadata_and_values_keep_two_response_replay():
    class IsoTransport:
        def exchange(self, table, key, deadline):
            document = fabricated_response(table, key)
            results = document["BEAAPI"]["Results"]
            results["UTCProductionTime"] = "2026-08-28T12:30:00Z"
            results["Notes"][0]["NoteText"] = results["Notes"][0]["NoteText"].replace(
                "September 30, 2026", "August 28, 2026"
            )
            return serialize(document)

    # Use the real worker in memory; local class does not cross a spawn boundary.
    pipe, output, sizes = CapturingPipe(), bytearray(edge.OUTPUT_LIMIT), [0, 0]
    edge._worker(pipe, output, sizes, IDENTITY, IsoTransport(), dummy_loader)
    assert pipe.frames == [b"ready", b"complete", b"ready", b"complete", b"candidate"]
    projection = bytes(output[: sizes[0]])
    receipt = bytes(output[sizes[0] : sum(sizes)])
    replay = edge.inspect_candidate(projection, receipt, expected=IDENTITY)
    assert replay["replay"]["measurements"]["headline_yoy_sa"]["value"] == "3.000000"
    assert not replay["source_qualified"]


def test_encoding_budget_exhaustion_and_unknown_metadata_fail_closed():
    nested = DUMMY
    for _ in range(12):
        nested = base64.b64encode(nested.encode()).decode()
    with pytest.raises(edge.EdgeRejected, match="credential_echo_rejected"):
        edge._echo_scan(nested, DUMMY)
    for value in ("unreviewed prose", "2026-99-99", "2026-08-28T99:00:00Z"):
        document = fabricated_response("T20807")
        document["BEAAPI"]["Results"]["UTCProductionTime"] = value
        with pytest.raises(edge.EdgeRejected, match="source_shape_rejected"):
            edge._clean_capture("T20807", serialize(document), DUMMY)


def test_replay_applies_projection_schema_before_digest(
    successful_candidate, monkeypatch
):
    document = json.loads(successful_candidate.projection)
    document["responses"][0]["results"]["UTCProductionTime"] = base64.b64encode(
        DUMMY.encode("utf-32-be")
    ).decode()
    with monkeypatch.context() as guard:
        guard.setattr(hashlib, "sha256", forbid_external)
        with pytest.raises(edge.EdgeRejected, match="source_shape_rejected"):
            edge.inspect_candidate(
                serialize(document), successful_candidate.receipt, expected=IDENTITY
            )


@pytest.mark.parametrize(
    "field,size", [("exact_code_git_sha", 40), ("code_manifest_sha256", 64)]
)
def test_digest_equal_to_unpadded_base64_credential_rejected(field, size):
    encoded = "a0dd" * (size // 4 - 1) + "a0c"
    key = base64.b64decode(encoded + "=").decode()
    assert base64.b64encode(key.encode()).decode().rstrip("=") == encoded
    identity = replace(IDENTITY, **{field: "a" + encoded})
    identity.validate()
    pipe, output, sizes = CapturingPipe(), bytearray(edge.OUTPUT_LIMIT), [0, 0]
    transport = CorrectionTransport(calls=[])
    edge._worker(pipe, output, sizes, identity, transport, lambda: key)
    assert transport.calls == []
    assert pipe.closed and pipe.frames == [b"credential_echo_rejected"]
    assert sizes == [0, 0] and not any(output)


def test_bea_request_echo_canonicalized_value_case_is_accepted():
    document = fabricated_response("T20807")
    for row in document["BEAAPI"]["Request"]["RequestParam"]:
        if row["ParameterName"].casefold() != "userid":
            row["ParameterValue"] = row["ParameterValue"].upper()
    raw = serialize(document)
    assert edge._clean_capture("T20807", raw, DUMMY) == raw


def test_bea_request_echo_wrong_value_or_userid_is_rejected():
    for name, value in (("Year", "2024"), ("UserID", DUMMY + "x")):
        document = fabricated_response("T20807")
        for row in document["BEAAPI"]["Request"]["RequestParam"]:
            if row["ParameterName"].casefold() == name.casefold():
                row["ParameterValue"] = value
        with pytest.raises(edge.EdgeRejected, match="source_shape_rejected"):
            edge._clean_capture("T20807", serialize(document), DUMMY)


def test_provider_metadata_prefixed_base64_credential_never_emits_candidate():
    encoded = "a" + base64.urlsafe_b64encode(("x:" + DUMMY).encode()).decode().rstrip(
        "="
    )

    class CredentialEchoTransport:
        def exchange(self, table, key, deadline):
            document = fabricated_response(table, key)
            if table == "T20807":
                note = document["BEAAPI"]["Results"]["Notes"][0]
                note["NoteText"] = note["NoteText"].replace(
                    " - LastRevised:", " " + encoded + " - LastRevised:"
                )
            return serialize(document)

    pipe, output, sizes = CapturingPipe(), bytearray(edge.OUTPUT_LIMIT), [0, 0]
    transport = CredentialEchoTransport()
    edge._worker(pipe, output, sizes, IDENTITY, transport, dummy_loader)
    assert pipe.frames == [b"ready", b"credential_echo_rejected"]
    assert sizes == [0, 0] and not any(output)


def representative_provider_response(table, key=DUMMY):
    """620-row real shape with the Owner-supplied July samples encoded here.

    Other months/lines are fabricated; this is deterministic shape evidence,
    never a claim to be a captured original or an independently qualified Source.
    """
    document = fabricated_response(table, key)
    results = document["BEAAPI"]["Results"]
    templates = {r["LineNumber"]: r for r in results["Data"]}
    samples = {
        "T20807": {"1": ("0.1", "0.1"), "25": ("0.2", "0.1")},
        "T20804": {"1": ("126.906", "131.172"), "25": ("126.363", "130.133")},
    }
    rows = []
    for number in range(1, 32):
        line = str(number)
        for year, months in ((2025, 12), (2026, 8)):
            for month in range(1, months + 1):
                row = dict(templates.get(line, templates["1"]))
                row.update(LineNumber=line, TimePeriod=f"{year}M{month:02d}")
                if line in samples[table]:
                    if line == "1":
                        row["LineDescription"] = (
                            "Personal consumption expenditures (PCE)"
                        )
                    if month == 7:
                        row["DataValue"] = samples[table][line][year - 2025]
                else:
                    row.update(
                        SeriesCode="OTHER",
                        LineDescription="Other product",
                        DataValue="(NA)",
                    )
                rows.append(row)
    results["Data"] = rows
    results["Notes"].extend(
        {"NoteRef": table + "." + str(i), "NoteText": "Other provider footnote."}
        for i in range(1, 7)
    )
    return document


class RepresentativeProviderTransport:
    def exchange(self, table, key, deadline):
        return serialize(representative_provider_response(table, key))


def test_real_shape_pair_projects_small_bundle_and_replays_exact_samples():
    result = acquire(RepresentativeProviderTransport())
    assert result.classification == "candidate_pending_independent_readback"
    assert len(result.observations) == 2
    assert all(o.response_complete for o in result.observations)
    document = json.loads(result.projection)
    assert len(result.projection) < 30_000
    assert document["exact_original_retained"] is False
    assert DUMMY.encode() not in result.projection + result.receipt
    assert (
        b"RequestParam" not in result.projection and b"OTHER" not in result.projection
    )
    for table, response in zip(pce.TABLES, document["responses"], strict=True):
        original = representative_provider_response(table)["BEAAPI"]["Results"]
        projected = response["results"]
        assert len(original["Data"]) == 620 and len(projected["Data"]) == 40
        assert projected["Statistic"] == "NIPA Table"
        assert projected["Dimensions"] == original["Dimensions"]
        assert projected["UTCProductionTime"] == original["UTCProductionTime"]
        assert projected["Notes"] == [
            original["Notes"][0],
            {"NoteRef": "LastRevised", "NoteText": "September 30, 2026"},
        ]
        assert projected["Data"] == [
            r for r in original["Data"] if r["LineNumber"] in {"1", "25"}
        ]
    parsed = edge.inspect_candidate(
        result.projection, result.receipt, expected=IDENTITY
    )
    measurements = parsed["replay"]["measurements"]
    assert {k: v["value"] for k, v in measurements.items()} == {
        "headline_mom_sa": "0.1",
        "core_mom_sa": "0.1",
        "headline_yoy_sa": "3.361543",
        "core_yoy_sa": "2.983468",
    }
    for family in ("headline", "core"):
        for operand in measurements[family + "_yoy_sa"]["inputs"]:
            row_index = int(operand["value_pointer"].split("/")[-2])
            row = document["responses"][1]["results"]["Data"][row_index]
            assert operand["value"] == row["DataValue"]
            assert operand["series"] == row["SeriesCode"]
            assert (
                operand["retained_sha256"]
                == hashlib.sha256(result.projection).hexdigest()
            )
    assert not parsed["source_qualified"]


@pytest.mark.parametrize("table", pce.TABLES)
def test_provider_projection_discards_unrelated_fields_rows_and_notes(table):
    document = representative_provider_response(table)
    results = document["BEAAPI"]["Results"]
    expected = pce.project_provider_results(table, results)
    results["AdditionalMetadata"] = "Unused prose"
    results["Data"][0]["AdditionalColumn"] = "Unused column"
    results["Dimensions"][0]["AdditionalDimensionMetadata"] = "Unused prose"
    results["Notes"][0]["AdditionalNoteMetadata"] = "Unused prose"
    # Unrelated note prose and nonnumeric values do not define the PCE mapping.
    results["Notes"][1]["NoteText"] = "Another definition from the provider."
    assert pce.project_provider_results(table, results) == expected
    assert pce.project_provider_results(table, expected) == expected
    assert edge._clean_capture(table, serialize(document), DUMMY)


@pytest.mark.parametrize("table", pce.TABLES)
@pytest.mark.parametrize(
    "case",
    [
        "statistic",
        "dimensions_missing",
        "dimension_name",
        "dimension_duplicate",
        "production",
        "table_note",
        "revision_missing",
        "revision_invalid",
        "metric",
        "unit",
        "series",
        "note_ref",
        "target",
        "comparison",
        "duplicate",
        "note_type",
        "row_type",
        "row_limit",
        "note_limit",
    ],
)
def test_real_provider_shape_required_semantics_fail_closed(table, case):
    document = fabricated_response(table)
    results = document["BEAAPI"]["Results"]
    if case == "statistic":
        results["Statistic"] = "Another statistic"
    elif case == "dimensions_missing":
        del results["Dimensions"]
    elif case == "dimension_name":
        results["Dimensions"][0]["Name"] = "AnotherName"
    elif case == "dimension_duplicate":
        results["Dimensions"][0] = results["Dimensions"][1]
    elif case == "production":
        results["UTCProductionTime"] = "2026-99-99T13:24:43.613"
    elif case == "table_note":
        results["Notes"][0]["NoteRef"] = "AnotherTable"
    elif case == "revision_missing":
        results["Notes"][0]["NoteText"] = results["Notes"][0]["NoteText"].split(
            " - LastRevised:"
        )[0]
    elif case == "revision_invalid":
        results["Notes"][0]["NoteText"] = results["Notes"][0]["NoteText"].replace(
            "September 30", "September 99"
        )
    elif case in {"metric", "unit", "series", "note_ref"}:
        field = {
            "metric": "METRIC_NAME",
            "unit": "CL_UNIT",
            "series": "SeriesCode",
            "note_ref": "NoteRef",
        }[case]
        results["Data"][0][field] = "Wrong"
    elif case == "target":
        results["Data"].pop(0)
    elif case == "comparison":
        results["Data"].pop(1)
    elif case == "duplicate":
        results["Data"].append(results["Data"][0])
    elif case == "note_type":
        results["Notes"].append("Invalid note")
    elif case == "row_type":
        results["Data"].append("Invalid row")
    elif case == "row_limit":
        results["Data"] *= 501
    elif case == "note_limit":
        results["Notes"] *= 129
    with pytest.raises(edge.EdgeRejected, match="source_shape_rejected"):
        edge._clean_capture(table, serialize(document), DUMMY)


def test_retained_replay_requires_extracted_revision_consistent_with_table_note(
    successful_candidate,
):
    document = json.loads(successful_candidate.projection)
    for response in document["responses"]:
        response["results"]["Notes"][1]["NoteText"] = "October 01, 2026"
    with pytest.raises(edge.EdgeRejected, match="source_shape_rejected"):
        edge.inspect_candidate(
            serialize(document), successful_candidate.receipt, expected=IDENTITY
        )


def test_provider_pair_revision_mismatch_emits_no_candidate():
    class RevisionMismatchTransport:
        def exchange(self, table, key, deadline):
            document = fabricated_response(table, key)
            if table == "T20804":
                note = document["BEAAPI"]["Results"]["Notes"][0]
                note["NoteText"] = note["NoteText"].replace(
                    "September 30, 2026", "October 01, 2026"
                )
            return serialize(document)

    pipe, output, sizes = CapturingPipe(), bytearray(edge.OUTPUT_LIMIT), [0, 0]
    edge._worker(
        pipe, output, sizes, IDENTITY, RevisionMismatchTransport(), dummy_loader
    )
    assert pipe.frames == [
        b"ready",
        b"complete",
        b"ready",
        b"complete",
        b"source_shape_rejected",
    ]
    assert sizes == [0, 0] and not any(output)


@pytest.mark.parametrize("wrapper", ["base64", "hex"])
def test_unrelated_binary_metadata_nul_is_not_a_credential(wrapper):
    # Before R2 both wrappers were rejected solely due to guessed binary NULs.
    raw = b"\xff\x00unrelated-cdn-metadata\xfe"
    value = base64.b64encode(raw).decode() if wrapper == "base64" else raw.hex()
    edge._echo_scan({"X-CDN-Trace": value}, DUMMY)


@pytest.mark.parametrize("codec", ENCODINGS)
@pytest.mark.parametrize("wrapper", ["base64", "hex"])
def test_binary_metadata_containing_actual_key_still_rejected(codec, wrapper):
    raw = b"\xff\x00" + DUMMY.encode(codec) + b"\xfe"
    value = base64.b64encode(raw).decode() if wrapper == "base64" else raw.hex()
    with pytest.raises(edge.EdgeRejected, match="credential_echo_rejected"):
        edge._echo_scan({"X-CDN-Trace": value}, DUMMY)


@pytest.mark.parametrize("prefix", ["a", "ab", "abc"])
@pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"])
def test_nul_exception_never_accepts_prefixed_wide_encoded_key(prefix, codec):
    value = prefix + base64.b64encode(("x:" + DUMMY).encode(codec)).decode()
    with pytest.raises(edge.EdgeRejected, match="credential_echo_rejected"):
        edge._echo_scan({"X-CDN-Trace": value}, DUMMY)


@pytest.mark.parametrize("prefix", ["", "a", "ab", "abc"])
def test_binary_nested_encoded_identity_no_ipc_candidate(prefix, monkeypatch):
    """Regression: an encoded identity inside binary CDN metadata stays secret."""
    inner = base64.b64encode(DUMMY.encode()).decode()
    wrapped = base64.b64encode(b"\xff\x00" + inner.encode("ascii") + b"\xfe").decode()
    frames = capture_failed_worker(
        CorrectionTransport(echo=prefix + wrapped, calls=[]),
        monkeypatch,
        ["T20807"],
    )
    assert frames == [b"ready", b"credential_echo_rejected"]


@pytest.mark.parametrize(
    "case",
    [
        "odd_hex",
        "binary_percent",
        "nested_utf16le",
        "nested_utf16be",
        "nested_utf32le",
        "nested_utf32be",
    ],
)
def test_r5_nested_binary_wrapped_credential_never_leaves_worker(case):
    """Fabricated key cannot survive nested opaque binary provider notes."""
    from urllib.parse import quote as encode_url

    inner = base64.b64encode(DUMMY.encode())
    wrapped = {
        "odd_hex": "a" + (b"\xff\x00" + inner + b"\xfe").hex(),
        "binary_percent": base64.b64encode(
            b"\xff\x00" + encode_url(DUMMY).encode() + b"\xfe"
        ).decode(),
        **{
            "nested_" + codec.replace("-", ""): base64.b64encode(
                b"\xff\x00" + base64.b64encode(DUMMY.encode(codec)) + b"\xfe"
            ).decode()
            for codec in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")
        },
    }[case]

    class NoteTransport:
        def exchange(self, table, key, deadline):
            doc = fabricated_response(table, key)
            if table == "T20807":
                note = doc["BEAAPI"]["Results"]["Notes"][0]
                assert " - LastRevised:" in note["NoteText"]
                note["NoteText"] = note["NoteText"].replace(
                    " - LastRevised:", " " + wrapped + " - LastRevised:"
                )
            return serialize(doc)

    pipe = CapturingPipe()
    buffer = bytearray(edge.OUTPUT_LIMIT)
    sizes = [0, 0]
    edge._worker(pipe, buffer, sizes, IDENTITY, NoteTransport(), dummy_loader)
    assert pipe.closed
    assert pipe.frames == [b"ready", b"credential_echo_rejected"]
    assert sizes == [0, 0] and not any(buffer)


@pytest.mark.parametrize("prefix", ["", "a", "ab", "abc"])
def test_r5_nested_binary_identity_never_enters_receipt(prefix):
    """An unpadded opaque identity cannot encode a recoverable key."""
    inner = base64.b64encode(DUMMY.encode())
    encoded = (
        base64.urlsafe_b64encode(b"\xff\x00" + inner + b"\xfe").decode().rstrip("=")
    )
    identity = replace(IDENTITY, controlled_observation_id=prefix + encoded)
    identity.validate()
    pipe = CapturingPipe()
    buffer = bytearray(edge.OUTPUT_LIMIT)
    sizes = [0, 0]
    transport = CorrectionTransport(calls=[])
    edge._worker(pipe, buffer, sizes, identity, transport, dummy_loader)
    assert transport.calls == []
    assert pipe.closed
    assert pipe.frames == [b"credential_echo_rejected"]
    assert sizes == [0, 0] and not any(buffer)


@pytest.mark.parametrize("separator", [b"\n", b"\r\n", b" ", b"\t"])
@pytest.mark.parametrize("prefix", ["", "a", "ab", "abc"])
@pytest.mark.parametrize("location", ["note", "identity"])
def test_r6_segmented_binary_base64_worker_rejects_before_digest_or_ipc(
    separator, prefix, location, monkeypatch
):
    inner = base64.b64encode(DUMMY.encode())
    split = separator.join(inner[i : i + 12] for i in range(0, len(inner), 12))
    encoded = prefix + base64.urlsafe_b64encode(
        b"\xff\x00" + split + b"\xfe"
    ).decode().rstrip("=")
    identity = (
        replace(IDENTITY, controlled_observation_id=encoded)
        if location == "identity"
        else IDENTITY
    )
    identity.validate()
    transport = CorrectionTransport(
        echo=encoded if location == "note" else None, calls=[]
    )
    pipe, buffer, sizes = CapturingPipe(), bytearray(edge.OUTPUT_LIMIT), [0, 0]
    forbidden_calls = []

    def forbid_output(*_args, **_kwargs):
        forbidden_calls.append(True)
        raise AssertionError("rejected_payload_reached_output")

    with monkeypatch.context() as guard:
        guard.setattr(hashlib, "sha256", forbid_output)
        guard.setattr(pce, "retain_sanitized_bundle", forbid_output)
        guard.setattr(builtins, "open", forbid_output)
        guard.setattr(Path, "open", forbid_output)
        edge._worker(pipe, buffer, sizes, identity, transport, dummy_loader)
    assert not forbidden_calls
    assert transport.calls == (["T20807"] if location == "note" else [])
    assert pipe.frames == (
        [b"ready", b"credential_echo_rejected"]
        if location == "note"
        else [b"credential_echo_rejected"]
    )
    assert pipe.closed and sizes == [0, 0] and not any(buffer)


@pytest.mark.parametrize("size", [4096, 8192, 65536, 65537, 1050000])
def test_r6_invalid_production_uses_field_contract_before_scan(size, monkeypatch):
    class InvalidTimeTransport:
        def exchange(self, table, key, deadline):
            document = fabricated_response(table, key)
            document["BEAAPI"]["Results"]["UTCProductionTime"] = "a" * size
            return serialize(document)

    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("invalid_time_reached_scan_or_digest")

    pipe, buffer, sizes = CapturingPipe(), bytearray(edge.OUTPUT_LIMIT), [0, 0]
    with monkeypatch.context() as guard:
        guard.setattr(edge, "_echo_scan", forbidden)
        guard.setattr(hashlib, "sha256", forbidden)
        edge._worker(
            pipe, buffer, sizes, IDENTITY, InvalidTimeTransport(), dummy_loader
        )
    assert not calls
    assert pipe.frames == [b"ready", b"source_shape_rejected"]
    assert pipe.closed and sizes == [0, 0] and not any(buffer)


@pytest.mark.parametrize("kind", ["text", "binary", "random_binary"])
@pytest.mark.parametrize("size", [4096, 8192])
def test_r6_benign_long_metadata_preserves_projection_and_replay(kind, size):
    if kind == "text":
        payload = "a" * size
    else:
        raw = (
            random.Random(7).randbytes(size)
            if kind == "random_binary"
            else bytes(i % 256 for i in range(size))
        )
        payload = base64.b64encode(raw).decode()
    expected = {}

    class MetadataTransport:
        def exchange(self, table, key, deadline):
            document = fabricated_response(table, key)
            results = document["BEAAPI"]["Results"]
            note = results["Notes"][0]
            note["NoteText"] = note["NoteText"].replace(
                " - LastRevised:", " " + payload + " - LastRevised:"
            )
            expected[table] = pce.project_provider_results(table, results)
            return serialize(document)

    pipe, buffer, sizes = CapturingPipe(), bytearray(edge.OUTPUT_LIMIT), [0, 0]
    edge._worker(pipe, buffer, sizes, IDENTITY, MetadataTransport(), dummy_loader)
    assert pipe.frames == [b"ready", b"complete", b"ready", b"complete", b"candidate"]
    projection = bytes(buffer[: sizes[0]])
    receipt = bytes(buffer[sizes[0] : sum(sizes)])
    for table, response in zip(
        pce.TABLES, pce.decode(projection)["responses"], strict=True
    ):
        assert response["results"] == expected[table]
    replay = edge.inspect_candidate(projection, receipt, expected=IDENTITY)
    assert not replay["source_qualified"]
    assert replay["replay"]["measurements"]["headline_yoy_sa"]["value"] == "3.000000"


@pytest.mark.parametrize("scan", [edge._identity_credential_scan, edge._echo_scan])
def test_r7_unique_decode_work_exhaustion_still_fails_closed(scan):
    # Independent distinct tokens, not a repeated traversal of the same blob.
    values = [
        base64.b64encode(random.Random(i).randbytes(4096)).decode() for i in range(20)
    ]
    with pytest.raises(edge.EdgeRejected, match="^credential_echo_rejected$"):
        scan(";".join(values), DUMMY)


@pytest.mark.parametrize(
    "codec,width",
    [("utf-16-le", 2), ("utf-16-be", 2), ("utf-32-le", 4), ("utf-32-be", 4)],
)
@pytest.mark.parametrize("separator", ["\n", "\r\n", " ", "\t"])
def test_r7_segmented_nested_wide_text_inside_binary_all_alignments(
    codec, width, separator
):
    inner = base64.b64encode(DUMMY.encode()).decode()
    split = separator.join(inner[i : i + 5] for i in range(0, len(inner), 5))
    for offset in range(width):
        raw = b"\xff" * offset + split.encode(codec) + b"\xfe"
        value = base64.b64encode(raw).decode().rstrip("=")
        with pytest.raises(edge.EdgeRejected, match="^credential_echo_rejected$"):
            edge._identity_credential_scan(value, DUMMY)


@pytest.mark.parametrize("separator", [b"\n", b"\r\n", b" ", b"\t"])
@pytest.mark.parametrize("location", ["note", "identity"])
def test_r7_segmented_binary_noncredential_preserved_in_candidate(separator, location):
    inner = base64.b64encode(b"public-CDN-23456789-abcd")
    split = separator.join(inner[i : i + 12] for i in range(0, len(inner), 12))
    encoded = "abc" + base64.urlsafe_b64encode(
        b"\xff\x00" + split + b"\xfe"
    ).decode().rstrip("=")
    identity = (
        replace(IDENTITY, controlled_observation_id=encoded)
        if location == "identity"
        else IDENTITY
    )
    pipe, buffer, sizes = CapturingPipe(), bytearray(edge.OUTPUT_LIMIT), [0, 0]
    transport = CorrectionTransport(echo=encoded if location == "note" else None)
    edge._worker(pipe, buffer, sizes, identity, transport, dummy_loader)
    assert pipe.closed
    assert pipe.frames == [b"ready", b"complete", b"ready", b"complete", b"candidate"]
    projection = bytes(buffer[: sizes[0]])
    receipt = bytes(buffer[sizes[0] : sum(sizes)])
    if location == "note":
        note = pce.decode(projection)["responses"][0]["results"]["Notes"][0]
        assert " " + encoded + " - LastRevised:" in note["NoteText"]
    else:
        assert pce.decode(receipt)["controlled_observation_id"] == encoded
    assert not edge.inspect_candidate(projection, receipt, expected=identity)[
        "source_qualified"
    ]
