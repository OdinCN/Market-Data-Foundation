"""Independent, fabricated BEA edge fixture; never real Source qualification.

Frozen expected parameters and rows do not call the adapter's request/mapping
builders. Spawned doubles prohibit external sockets and real credential loading.
"""

import base64
import gzip
import hashlib
import json
import multiprocessing
import socket
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import quote

import psycopg
import pytest

from market_data_foundation.historical import bea_pce as pce
from market_data_foundation.historical import bea_pce_live as edge

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
                    "METRIC_NAME": "Percent Change" if mom else "Index",
                    "CL_UNIT": "Percent change" if mom else "Index Numbers",
                    "UNIT_MULT": "0",
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
                "UTCProductionTime": "FABRICATED UTC metadata",
                "Notes": [
                    {
                        "NoteRef": "1",
                        "NoteText": (
                            "FABRICATED Table 2.8.7. Monthly price changes "
                            "from preceding "
                            "month, seasonally adjusted."
                            if mom
                            else "FABRICATED Table 2.8.4. Monthly price indexes, "
                            "2017=100, "
                            "seasonally adjusted."
                        ),
                    },
                    {"NoteRef": "LastRevised", "NoteText": "FABRICATED revision A"},
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
        status = (
            503 if self.scenario == "http_status" and table == self.fail_table else 200
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
    # Private laboratory tests retain sanitizer/protocol coverage. Public acquire
    # is closed to all callbacks; supported offline admission is OfflineFixture.
    return edge._supervise(
        IDENTITY,
        transport or SyntheticTransport(),
        dummy_loader,
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
    ):
        result = edge.acquire(IDENTITY, enabled=enabled, provider_budget=budget)
        assert result.classification == "disabled_budget_zero"
        assert result.observations == () and result.projection is None


def test_reject_unbound_identity_and_loader_injection():
    result = edge.acquire(
        replace(IDENTITY, exact_plan_sha256="c" * 64), enabled=True, provider_budget=2
    )
    assert result.classification == "context_rejected"
    result = edge.acquire(
        IDENTITY, enabled=True, provider_budget=2, credential_loader=dummy_loader
    )
    assert result.classification == "context_rejected"


@pytest.fixture(scope="module")
def successful_candidate():
    return acquire()


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
    result = edge._supervise(
        IDENTITY,
        SyntheticTransport(delay=0.065),
        dummy_loader,
        request_seconds=0.10,
        network_seconds=0.11,
        attempt_seconds=3.0,
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
        attempt_seconds=0.5,
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
    time.sleep(2)


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
            document["BEAAPI"]["Results"]["UTCProductionTime"] = self.echo
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
    assert set(pipe.frames) <= {b"ready", b"complete"} | set(
        edge._FAILURE_FRAMES.values()
    )
    assert DUMMY.encode() not in b"".join(pipe.frames)
    return pipe.frames


@pytest.mark.parametrize("failure", ["partial", "unknown"])
@pytest.mark.parametrize("table", ["T20807", "T20804"])
def test_partial_unknown_worker_never_hashes_persists_or_emits_orphan(
    failure, table, monkeypatch
):
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
@pytest.mark.parametrize(
    "field", ["production", "note", "extra_row", "description", "value"]
)
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
                "extra_row": "Unexpected",
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
            results["Notes"][1]["NoteText"] = "2026-08-28"
            results["Notes"][0]["NoteText"] = results["Notes"][0][
                "NoteText"
            ].removeprefix("FABRICATED ")
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
