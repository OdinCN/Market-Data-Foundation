"""Targeted independent-review regressions, all synthetic and offline."""

import base64
import copy
import json
import os
import signal
import struct
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from market_data_foundation.candidates.tdx_exhq_directory import (
    CaptureContext,
    OfflineReceipt,
    PageRequest,
)
from market_data_foundation.candidates.tdx_exhq_directory.schema import EvidenceMode
from market_data_foundation.candidates.tdx_exhq_directory.schema import (
    Operation as DirOp,
)
from market_data_foundation.candidates.tdx_exhq_v1 import decode_ex
from market_data_foundation.candidates.tdx_exhq_v1.retained import (
    directory_associations,
)
from market_data_foundation.candidates.tdx_intraday_v1 import host
from market_data_foundation.candidates.tdx_intraday_v1.model import (
    DecodeError,
    Operation,
    canonical,
    sha,
)
from market_data_foundation.candidates.tdx_intraday_v1.replay import (
    normalized_plan,
    replay,
)
from market_data_foundation.candidates.tdx_intraday_v1.semantics import field_facts
from market_data_foundation.candidates.tdx_intraday_v1.wire import planned_request_wire
from tests.test_i4_exhq import fixture
from tests.test_i4_intraday import fixture as std_fixture
from tests.test_i4_intraday import material
from tests.test_i4_replay import plan, record, transcript
from tests.test_r5_i6_a_ex_dir import (
    instrument_row,
    market_row,
    markets_payload,
    page_payload,
)


def bindings(p, *, mode="synthetic"):
    context = CaptureContext(
        p["source"],
        p["binding"],
        "synthetic.v1",
        "directory-capture",
        EvidenceMode(mode),
        "b" * 64,
    )

    def document(op, raw, request=None):
        receipt = OfflineReceipt(
            context, op, raw, sha(raw), f"retained://named/{op}", request=request
        )
        values = asdict(receipt)
        values["payload"] = base64.b64encode(raw).decode()
        values["identity"] = receipt.material.identity
        return values

    return {
        "schema": "mdf.tdx.i4.directory-bindings.v1",
        "plan_sha256": sha(canonical(normalized_plan(p))),
        "entries": [
            {
                "index": 0,
                "request": p["requests"][0],
                "markets": document(DirOp.MARKETS, markets_payload(market_row())),
                "instruments": document(
                    DirOp.INSTRUMENTS, page_payload(instrument_row()), PageRequest(0, 1)
                ),
            }
        ],
    }


def test_digest_shaped_claim_is_never_verified(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    req, raw = fixture(Operation.EX_QUOTE)
    assert (
        "DIRECTORY_ASSOCIATION_UNKNOWN"
        in decode_ex(material(req, raw, directory_identity="f" * 64)).findings
    )
    p = dict(plan(req), directory_identity="f" * 64)
    r = record(p, req, raw)
    result = replay(
        p,
        transcript(p, [r], complete=True),
        tmp_path / "artifacts" / "unknown",
        producer_sha256="b" * 64,
        evidence_mode="synthetic",
    )
    assert not result["failures"]
    assert "DIRECTORY_ASSOCIATION_UNKNOWN" in result["pages"][0]["findings"]
    assert (
        directory_associations(p, None, mode="retained", producer_sha256="b" * 64) == {}
    )
    with pytest.raises(DecodeError, match="verified_directory"):
        directory_associations(
            p, None, mode="retained", producer_sha256="b" * 64, require_all=True
        )


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "sha",
        "identity",
        "source",
        "market",
        "category",
        "code",
        "request",
        "mode",
        "producer",
        "capture",
        "digest",
    ],
)
def test_exact_retained_directory_replay_binding(tmp_path, monkeypatch, mutation):
    monkeypatch.chdir(tmp_path)
    req, raw = fixture(Operation.EX_QUOTE)
    p = plan(req)
    doc = bindings(p)
    item = doc["entries"][0]
    if mutation == "sha":
        item["instruments"]["payload"] = base64.b64encode(b"wrong").decode()
    if mutation == "identity":
        item["markets"]["identity"] = "f" * 64
    if mutation in ("source", "mode", "producer", "capture"):
        field = {
            "source": "source",
            "mode": "mode",
            "producer": "producer_derived_sha256",
            "capture": "capture_id",
        }[mutation]
        item["instruments"]["context"][field] = {
            "source": "wrong",
            "mode": "retained",
            "producer": "f" * 64,
            "capture": "other",
        }[mutation]
    if mutation in ("market", "category", "code"):
        # Bind a valid receipt to a different exact planned tuple.
        p["requests"][0][mutation] = {"market": 48, "category": 3, "code": "700"}[
            mutation
        ]
        doc["plan_sha256"] = sha(canonical(normalized_plan(p)))
    if mutation == "request":
        item["instruments"]["request"]["start"] = 1
    if mutation == "digest":
        p["directory_identity"] = "f" * 64
        doc["plan_sha256"] = sha(canonical(normalized_plan(p)))
    path = tmp_path / "directory.json"
    path.write_bytes(canonical(doc))
    r = record(p, req, raw)
    result = replay(
        p,
        transcript(p, [r], complete=True),
        tmp_path / "artifacts" / "binding",
        producer_sha256="b" * 64,
        evidence_mode="synthetic",
        directory_receipts=path,
    )
    if mutation:
        assert result["failures"] and result["terminal"] == "STOPPED"
    else:
        assert not result["failures"]
        assert "DIRECTORY_ASSOCIATION_UNKNOWN" not in result["pages"][0]["findings"]
        projection = json.loads(
            (tmp_path / "artifacts/binding/000.projection.json").read_bytes()
        )
        assert len(projection["receipt"]["directory_receipt_identities"]) == 2


def test_multi_request_directory_binding_cannot_be_reused_for_another_index():
    req, _ = fixture(Operation.EX_QUOTE)
    p = plan(req)
    p["requests"] += [replace(req, code="00005").to_dict()]
    p["max_requests"] = 2
    doc = bindings(p, mode="retained")
    with pytest.raises(DecodeError, match="verified_directory"):
        directory_associations(
            p, doc, mode="retained", producer_sha256="b" * 64, require_all=True
        )
    duplicate = copy.deepcopy(doc["entries"][0])
    duplicate.update(index=1, request=p["requests"][1])
    doc["entries"].append(duplicate)
    with pytest.raises(DecodeError, match="not_observed"):
        directory_associations(
            p, doc, mode="retained", producer_sha256="b" * 64, require_all=True
        )


def test_field_packages_and_explicit_projection_rules():
    req, _ = std_fixture(Operation.BARS)
    facts = field_facts(req)
    assert all(
        f["qualified_response_struct"].startswith("github.com/injoyai/tdx/protocol.")
        for f in facts
    )
    assert not {"Unix", "Turnover", "FloatStock", "TotalStock"} & {
        f["source_field_name"] for f in facts
    }
    order = next(f for f in facts if f["source_field_name"] == "Order")
    assert (
        order["projection"]["kind"] == "OMITTED"
        and order["projection"]["target"] is None
    )
    req, raw = fixture(Operation.EX_TRADE)
    facts = {f["source_field_name"]: f for f in field_facts(req)}
    assert facts["Second"]["projection"]["target"] == "row.native.sdk_second"
    assert (
        facts["Second"]["projection"]["observed_rule"]
        == "ALWAYS_NULL_SOURCE_SECONDS_UNKNOWN"
    )
    assert facts["NatureName"]["projection"]["kind"] == "OMITTED"
    assert "natureName" not in decode_ex(material(req, raw)).rows[0].native


@pytest.mark.parametrize(
    "mutation",
    [
        "requested_bytes",
        "written_bytes",
        "response_bytes",
        "decoded_bytes",
        "frames",
        "terminal_total",
        "stop_then_complete",
        "duplicate_terminal",
        "after_terminal",
        "missing_setup",
        "missing_intent",
        "request_order",
        "response_budget",
        "decode_budget",
        "setup_wire",
    ],
)
def test_replay_recomputes_accounting_and_terminal_order(
    tmp_path, monkeypatch, mutation
):
    monkeypatch.chdir(tmp_path)
    req, raw = fixture(Operation.EX_QUOTE)
    p = plan(req)
    if mutation == "response_budget":
        p["max_response_bytes"] = 16
    if mutation == "decode_budget":
        p["max_decoded_bytes"] = 1
    records = list(transcript(p, [record(p, req, raw)], complete=True))
    if mutation in (
        "requested_bytes",
        "written_bytes",
        "response_bytes",
        "decoded_bytes",
        "frames",
    ):
        records[2][mutation] += 1
    if mutation == "terminal_total":
        records[-1]["written_bytes"] += 1
    if mutation == "stop_then_complete":
        records.insert(-1, dict(records[-1], classification="stopped"))
    if mutation == "duplicate_terminal":
        records.append(records[-1])
    if mutation == "after_terminal":
        records.append(records[2])
    if mutation == "missing_setup":
        del records[1]
    if mutation == "missing_intent":
        del records[0]
    if mutation == "request_order":
        records[2]["index"] = 1
    if mutation == "setup_wire":
        records[1]["request_wire"] = base64.b64encode(b"wrong").decode()
    result = replay(
        p,
        tuple(records),
        tmp_path / "artifacts" / "bad",
        producer_sha256="b" * 64,
        evidence_mode="synthetic",
    )
    assert result["failures"] and result["terminal"] == "STOPPED"


@pytest.mark.parametrize("op", ["ExMarkets", "ExCount", "ExInstruments"])
@pytest.mark.parametrize("tampered", [False, True])
def test_all_directory_branches_check_real_outbound_bytes(
    tmp_path, monkeypatch, op, tampered
):
    monkeypatch.chdir(tmp_path)
    req, _ = fixture(Operation.EX_QUOTE)
    p = plan(req)
    request = dict(
        operation=op,
        market=0,
        code="",
        category=None,
        start=0,
        count=1 if op == "ExInstruments" else 0,
        date="",
        period=0,
        kind="",
    )
    p["requests"] = [request]
    payload = {
        "ExMarkets": markets_payload(market_row()),
        "ExCount": bytes(19) + struct.pack("<I", 1),
        "ExInstruments": page_payload(instrument_row()),
    }[op]
    outbound = planned_request_wire(request, 1)
    wire = (
        b"\xb1\xcb\x74\x00\x1c"
        + struct.pack("<I", 1)
        + b"\0"
        + outbound[10:12]
        + struct.pack("<HH", len(payload), len(payload))
        + payload
    )
    r = record(p, req, b"")
    r.update(
        request=request,
        wire=base64.b64encode(wire).decode(),
        wire_sha256=sha(wire),
        payload=base64.b64encode(payload).decode(),
        payload_sha256=sha(payload),
        request_wire=base64.b64encode(outbound[:-1] if tampered else outbound).decode(),
        requested_bytes=len(outbound),
        written_bytes=len(outbound),
        response_bytes=len(wire),
        decoded_bytes=len(payload),
    )
    result = replay(
        p,
        transcript(p, [r], complete=True),
        tmp_path / "artifacts" / "directory",
        producer_sha256="b" * 64,
        evidence_mode="synthetic",
    )
    assert bool(result["failures"]) == tampered
    assert result["terminal"] == (
        "STOPPED" if tampered else "TRANSPORT_BUDGET_COMPLETE_NOT_QUALIFICATION"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "save",
        "persistent_save",
        "keyboard",
        "system_exit",
        "timeout",
        "sigterm",
        "sigint",
        "sighup",
        "sigquit",
    ],
)
def test_host_post_launch_faults_reap_child_once_and_close_streams(
    tmp_path, monkeypatch, fault
):
    monkeypatch.chdir(tmp_path)
    req, _ = std_fixture(Operation.TRADE)
    path = tmp_path / "plan.json"
    path.write_bytes(canonical(plan(req)))
    monkeypatch.setitem(host.DRIVER_BINARY_SHA256, host.I4_RELEASE, "b" * 64)

    @contextmanager
    def verified(*args):
        yield "/unused-synthetic", 0

    monkeypatch.setattr(host, "verified_driver", verified)
    original_popen = subprocess.Popen
    original_write = Path.write_bytes
    children, streams = [], []

    def launch(*args, **kwargs):
        streams.extend((kwargs["stdin"], kwargs["stdout"]))
        child = original_popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        children.append(child)
        if fault == "timeout":
            original_wait = child.wait

            def wait(timeout=None):
                if timeout != 2:
                    raise subprocess.TimeoutExpired("synthetic", timeout)
                return original_wait(timeout=timeout)

            child.wait = wait
        return child

    monkeypatch.setattr(subprocess, "Popen", launch)
    fired = False

    def write(path, raw):
        nonlocal fired
        if (
            path.name == "host-ledger.json"
            and children
            and (not fired or fault == "persistent_save")
        ):
            fired = True
            if fault in ("save", "persistent_save"):
                raise OSError("private-fault-message")
            if fault == "keyboard":
                raise KeyboardInterrupt()
            if fault == "system_exit":
                raise SystemExit(17)
            if fault.startswith("sig"):
                os.kill(os.getpid(), getattr(signal, fault.upper()))
        return original_write(path, raw)

    monkeypatch.setattr(Path, "write_bytes", write)
    previous = {
        sig: signal.getsignal(sig)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT)
    }
    start = time.monotonic()
    if fault in ("keyboard", "system_exit") or fault.startswith("sig"):
        with pytest.raises((KeyboardInterrupt, SystemExit, host.HostSignal)):
            host.run_host(
                path,
                tmp_path / "mock",
                tmp_path / "artifacts" / "run",
                allow_live=True,
                stage="afterclose",
            )
    else:
        result = host.run_host(
            path,
            tmp_path / "mock",
            tmp_path / "artifacts" / "run",
            allow_live=True,
            stage="afterclose",
        )
        assert result["status"] in ("STOP", "HOST_WALL_STOP")
        assert "private-fault-message" not in canonical(result).decode()
    assert time.monotonic() - start < 4
    assert len(children) == 1 and children[0].returncode is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(children[0].pid, os.WNOHANG)
    assert all(
        stream.closed
        for stream in (
            *streams,
            children[0].stdin,
            children[0].stdout,
            children[0].stderr,
        )
    )
    assert {sig: signal.getsignal(sig) for sig in previous} == previous


def test_host_directory_gate_even_with_isolated_mock_pin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    req, _ = fixture(Operation.EX_QUOTE)
    path = tmp_path / "plan.json"
    path.write_bytes(canonical(dict(plan(req), directory_identity="f" * 64)))
    monkeypatch.setitem(host.DRIVER_BINARY_SHA256, host.I4_RELEASE, "b" * 64)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("no child without directory evidence"),
    )
    result = host.run_host(
        path,
        tmp_path / "mock",
        tmp_path / "artifacts" / "run",
        allow_live=True,
        stage="afterclose",
    )
    assert result["status"] == "STOP" and result["subprocess_started"] is False


@pytest.mark.parametrize(
    "mutation",
    [None, "setup_counter", "setup_twice", "cumulative", "frame_budget", "late_index"],
)
def test_handshake_and_multi_request_totals(tmp_path, monkeypatch, mutation):
    monkeypatch.chdir(tmp_path)
    req, raw = fixture(Operation.EX_QUOTE)
    p = plan(req)
    p["requests"] *= 2
    p.update(max_requests=2, max_frames=2 if mutation == "frame_budget" else 3)
    first = record(p, req, raw)
    ack_wire = (
        b"\xb1\xcb\x74\x00\x1c" + bytes(5) + struct.pack("<HHH", 0x2454, 3, 3) + b"ack"
    )
    ack = dict(
        first,
        classification="setup_response",
        wire=base64.b64encode(ack_wire).decode(),
        wire_sha256=sha(ack_wire),
        payload=base64.b64encode(b"ack").decode(),
        payload_sha256=sha(b"ack"),
        response_bytes=len(ack_wire),
        decoded_bytes=3,
        frames=1,
    )
    first.update(
        response_bytes=len(ack_wire) + len(base64.b64decode(first["wire"])),
        decoded_bytes=3 + len(raw),
        frames=2,
    )
    second = copy.deepcopy(first)
    second_wire = bytearray(base64.b64decode(first["wire"]))
    struct.pack_into("<I", second_wire, 5, 2)
    second.update(
        index=1,
        wire=base64.b64encode(second_wire).decode(),
        wire_sha256=sha(second_wire),
        request_wire=base64.b64encode(
            planned_request_wire(p["requests"][1], 2)
        ).decode(),
        response_bytes=first["response_bytes"] + len(second_wire),
        decoded_bytes=3 + 2 * len(raw),
        frames=3,
    )
    records = list(transcript(p, [ack, first, second], complete=True))
    if mutation == "setup_counter":
        records[2]["frames"] = 0
    if mutation == "setup_twice":
        records.insert(3, ack)
    if mutation == "cumulative":
        records[4]["decoded_bytes"] = len(raw)
    if mutation == "late_index":
        records[4]["index"] = 0
    result = replay(
        p,
        tuple(records),
        tmp_path / "artifacts" / "multi",
        producer_sha256="b" * 64,
        evidence_mode="synthetic",
    )
    assert bool(result["failures"]) == bool(mutation)
    if mutation is None:
        assert result["accounting_status"] == "COMPLETE"
        assert result["recomputed_totals"]["requests"] == 2
        assert result["recomputed_totals"]["frames"] == 3
        assert result["recomputed_totals"]["decoded_bytes"] == 3 + 2 * len(raw)


def test_signal_during_popen_return_is_deferred_and_child_reaped(tmp_path, monkeypatch):
    original = subprocess.Popen
    children = []

    def launch(*args, **kwargs):
        child = original(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.PIPE,
        )
        children.append(child)
        os.kill(os.getpid(), signal.SIGTERM)
        return child

    monkeypatch.setattr(subprocess, "Popen", launch)
    started = []
    with pytest.raises(host.HostSignal):
        with host.managed_child([], on_started=lambda: started.append(True)):
            pytest.fail("pending cancellation must abort")
    assert started == [True]
    assert len(children) == 1 and children[0].returncode is not None
    assert children[0].stdout.closed
    with pytest.raises(ChildProcessError):
        os.waitpid(children[0].pid, os.WNOHANG)


class CleanupPipe:
    def __init__(self, fail=False):
        self.closed = False
        self.fail = fail

    def close(self):
        self.closed = True
        if self.fail:
            raise OSError("private-close-message")


class CleanupChild:
    """No OS process: failures cannot leak children, including on old code."""

    def __init__(self, fault, error_type=OSError, persistent=False):
        self.fault = fault
        self.error = error_type("private-cleanup-message")
        self.persistent = persistent
        self.calls = []
        self.killed = self.reaped = False
        self.returncode = None
        self.stdin = CleanupPipe(fault == "close")
        self.stdout = CleanupPipe()
        self.stderr = CleanupPipe()

    def inject(self, operation):
        self.calls.append(operation)
        if self.fault == operation and (
            self.persistent or self.calls.count(operation) == 1
        ):
            raise self.error

    def poll(self):
        self.inject("poll")
        return self.returncode

    def kill(self):
        self.inject("kill")
        self.killed = True

    def wait(self, timeout=None):
        assert timeout is not None and 0 < timeout <= 2
        self.inject("wait")
        if not self.killed or (
            self.fault == "timeout" and self.calls.count("wait") == 1
        ):
            raise subprocess.TimeoutExpired("synthetic", timeout)
        self.reaped = True
        self.returncode = -9
        return self.returncode


@pytest.mark.parametrize("fault", ["poll", "kill", "wait", "close", "timeout"])
@pytest.mark.parametrize("error_type", [OSError, ProcessLookupError])
@pytest.mark.parametrize(
    "primary",
    [
        None,
        ValueError("primary"),
        KeyboardInterrupt(),
        SystemExit(17),
        host.HostSignal(),
    ],
)
def test_cleanup_faults_do_not_short_circuit_or_replace_primary(
    monkeypatch, fault, error_type, primary
):
    child = CleanupChild(fault, error_type)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: child)
    previous = {
        sig: signal.getsignal(sig)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT)
    }
    with pytest.raises(type(primary) if primary is not None else Exception) as caught:
        with host.managed_child([]):
            if primary is not None:
                raise primary
    if primary is not None:
        assert caught.value is primary
    assert child.killed and child.reaped
    assert all(s.closed for s in (child.stdin, child.stdout, child.stderr))
    assert child.calls.count("wait") <= 2
    assert child.calls.count("kill") <= 2
    assert {sig: signal.getsignal(sig) for sig in previous} == previous


@pytest.mark.parametrize("fault", ["kill", "wait"])
def test_persistent_cleanup_failure_is_bounded_and_unknown(monkeypatch, fault):
    child = CleanupChild(fault, persistent=True)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: child)
    cleanup = {}
    with pytest.raises(OSError) as caught:
        with host.managed_child([], cleanup=cleanup):
            pass
    assert caught.value is child.error
    assert child.calls.count("wait") == 2
    assert child.calls.count("kill") == 2
    assert not child.reaped
    assert all(s.closed for s in (child.stdin, child.stdout, child.stderr))
    assert cleanup["reaped"] == "UNKNOWN"
    assert cleanup["status"] == "UNKNOWN"
    assert cleanup["errors"]
    assert "private-" not in canonical(cleanup).decode()


def test_cleanup_fault_in_callers_except_block_is_not_suppressed(monkeypatch):
    child = CleanupChild("poll")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: child)
    try:
        raise ValueError("already handled by caller")
    except ValueError:
        with pytest.raises(OSError) as caught:
            with host.managed_child([]):
                pass
    assert caught.value is child.error
    assert child.killed and child.reaped


def test_ledger_failure_and_poll_failure_reap_and_preserve_primary(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    req, _ = std_fixture(Operation.TRADE)
    path = tmp_path / "plan.json"
    path.write_bytes(canonical(plan(req)))
    monkeypatch.setitem(host.DRIVER_BINARY_SHA256, host.I4_RELEASE, "b" * 64)

    @contextmanager
    def verified(*args):
        yield "/unused-synthetic", 0

    monkeypatch.setattr(host, "verified_driver", verified)
    child = CleanupChild("poll")
    launched = False

    def launch(*args, **kwargs):
        nonlocal launched
        launched = True
        return child

    monkeypatch.setattr(subprocess, "Popen", launch)
    original_write = Path.write_bytes
    fired = False

    def write(destination, raw):
        nonlocal fired
        if destination.name == "host-ledger.json" and launched and not fired:
            fired = True
            raise PermissionError("private-ledger-message")
        return original_write(destination, raw)

    monkeypatch.setattr(Path, "write_bytes", write)
    result = host.run_host(
        path,
        tmp_path / "mock",
        tmp_path / "artifacts" / "run",
        allow_live=True,
        stage="afterclose",
    )
    assert fired and child.killed and child.reaped
    assert result["status"] == "STOP"
    assert result["exception"]["type"] == "PermissionError"
    assert result["subprocess_started"] is True
    assert result["provider_effect"] == "UNKNOWN_UNTIL_EXACT_RECEIPTS"
    assert result["source_freshness"] == "UNKNOWN"
    assert result["active_refresh"] == "NOT_PROVEN"
    assert result["child_cleanup"]["reaped"] == "CONFIRMED"
    assert result["child_cleanup"]["status"] == "UNKNOWN"
    assert result["child_cleanup"]["errors"][0]["operation"] == "poll"
    assert "private-" not in canonical(result).decode()
    assert (
        json.loads((tmp_path / "artifacts/run/host-ledger.json").read_bytes()) == result
    )


@pytest.mark.parametrize("fault", ["poll", "kill", "wait", "close", "timeout"])
def test_host_cleanup_error_stops_before_replay(tmp_path, monkeypatch, fault):
    monkeypatch.chdir(tmp_path)
    req, _ = std_fixture(Operation.TRADE)
    path = tmp_path / "plan.json"
    path.write_bytes(canonical(plan(req)))
    monkeypatch.setitem(host.DRIVER_BINARY_SHA256, host.I4_RELEASE, "b" * 64)

    @contextmanager
    def verified(*args):
        yield "/unused-synthetic", 0

    monkeypatch.setattr(host, "verified_driver", verified)
    child = CleanupChild(fault)
    cleanup_wait = child.wait

    def wait(timeout=None):
        if timeout != 2:
            # Synthetic main wait; leave cleanup's fault injection untouched.
            return 0
        return cleanup_wait(timeout=timeout)

    child.wait = wait
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: child)
    monkeypatch.setattr(host, "replay", lambda *a, **k: pytest.fail("must stop"))
    result = host.run_host(
        path,
        tmp_path / "mock",
        tmp_path / "artifacts" / "run",
        allow_live=True,
        stage="afterclose",
    )
    assert child.killed and child.reaped
    assert result["status"] == "STOP"
    assert result["provider_effect"] == "UNKNOWN_UNTIL_EXACT_RECEIPTS"
    assert result["source_freshness"] == "UNKNOWN"
    assert result["active_refresh"] == "NOT_PROVEN"
    assert result["child_cleanup"]["status"] == "UNKNOWN"
    assert result["child_cleanup"]["reaped"] == "CONFIRMED"
    assert "private-" not in canonical(result).decode()


@pytest.mark.parametrize("fault", ["poll", "kill", "wait", "close", "timeout"])
def test_real_sleep_child_cleanup_faults_leave_no_process(monkeypatch, fault):
    original_popen = subprocess.Popen
    child = original_popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    original_poll, original_kill, original_wait = child.poll, child.kill, child.wait
    original_stdin = child.stdin

    class FailingClose:
        def close(self):
            original_stdin.close()
            raise OSError("private-close-message")

    fired = False

    def fail_once(original):
        def call(*args, **kwargs):
            nonlocal fired
            if not fired:
                fired = True
                if fault == "timeout":
                    raise subprocess.TimeoutExpired("synthetic", kwargs["timeout"])
                raise OSError("private-cleanup-message")
            return original(*args, **kwargs)

        return call

    if fault == "close":
        child.stdin = FailingClose()
    else:
        operation = "wait" if fault == "timeout" else fault
        monkeypatch.setattr(child, operation, fail_once(getattr(child, operation)))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: child)
    start = time.monotonic()
    try:
        with pytest.raises((OSError, subprocess.TimeoutExpired)):
            with host.managed_child([]):
                pass
        assert time.monotonic() - start < 5
        assert child.returncode is not None
        with pytest.raises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)
        assert all(s.closed for s in (original_stdin, child.stdout, child.stderr))
    finally:
        # Independent rescue also runs when testing the deliberately broken code.
        # Bound methods bypass every injected fault; no failed test leaks a child.
        if original_poll() is None:
            original_kill()
        original_wait(timeout=2)
        for stream in (original_stdin, child.stdout, child.stderr):
            stream.close()


CLEANUP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)


@contextmanager
def signal_failures(monkeypatch, failures):
    """Exercise real handler changes, with independent restoration on old code."""
    original = signal.signal
    previous = {sig: signal.getsignal(sig) for sig in CLEANUP_SIGNALS}
    installed, ignored, attempts = set(), set(), []

    def update(sig, handler):
        if sig not in installed:
            phase = "install"
            installed.add(sig)
        elif handler == signal.SIG_IGN and sig not in ignored:
            phase = "ignore"
            ignored.add(sig)
        else:
            phase = "restore"
        attempts.append((phase, sig))
        if (phase, sig) in failures:
            raise failures[phase, sig]
        return original(sig, handler)

    with monkeypatch.context() as patch:
        patch.setattr(signal, "signal", update)
        try:
            yield attempts, previous
        finally:
            for sig, handler in previous.items():
                original(sig, handler)


@pytest.mark.parametrize(
    "faults",
    [
        (("ignore", signal.SIGINT),),
        (("ignore", signal.SIGHUP),),
        (("restore", signal.SIGINT),),
        (("restore", signal.SIGHUP),),
        (
            ("ignore", signal.SIGINT),
            ("ignore", signal.SIGHUP),
            ("restore", signal.SIGINT),
            ("restore", signal.SIGTERM),
            ("restore", signal.SIGQUIT),
        ),
        tuple(("restore", sig) for sig in CLEANUP_SIGNALS),
    ],
    ids=[
        "ignore-first",
        "ignore-middle",
        "restore-first",
        "restore-middle",
        "mixed",
        "all-restore",
    ],
)
@pytest.mark.parametrize("child_fault", [None, "poll"])
@pytest.mark.parametrize(
    "primary",
    [
        None,
        ValueError("primary"),
        KeyboardInterrupt(),
        SystemExit(17),
        host.HostSignal(),
    ],
)
def test_signal_cleanup_failures_are_isolated_and_preserve_priority(
    monkeypatch, faults, child_fault, primary
):
    failures = {key: OSError("private-signal-message") for key in faults}
    child = CleanupChild(child_fault)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: child)
    cleanup = {}
    with signal_failures(monkeypatch, failures) as (attempts, previous):
        with pytest.raises(BaseException) as caught:
            with host.managed_child([], cleanup=cleanup):
                if primary is not None:
                    raise primary
        assert child.killed and child.reaped
        assert all(s.closed for s in (child.stdin, child.stdout, child.stderr))
        assert attempts == [
            (phase, sig)
            for phase in ("install", "ignore", "restore")
            for sig in CLEANUP_SIGNALS
        ]
        # The active exception wins, then the first cleanup fault in execution order.
        ordered = [
            failures["ignore", sig]
            for sig in CLEANUP_SIGNALS
            if ("ignore", sig) in failures
        ]
        if child_fault:
            ordered.append(child.error)
        ordered += [
            failures["restore", sig]
            for sig in CLEANUP_SIGNALS
            if ("restore", sig) in failures
        ]
        assert caught.value is (primary if primary is not None else ordered[0])
        assert cleanup["status"] == "UNKNOWN"
        assert cleanup["reaped"] == "CONFIRMED"
        errors = cleanup["errors"]
        assert len(errors) == len(failures) + bool(child_fault)
        for phase, sig in failures:
            assert {
                "operation": f"signal_{phase}",
                "signal": sig.name,
                "exception": host.classify_exception(failures[phase, sig]),
            } in errors
        for sig in CLEANUP_SIGNALS:
            for phase in ("ignore", "restore"):
                assert cleanup["signals"][sig.name][phase] == (
                    "UNKNOWN" if (phase, sig) in failures else "CONFIRMED"
                )
            if ("restore", sig) in failures and ("ignore", sig) in failures:
                assert signal.getsignal(sig).__name__ == "interrupted"
            else:
                expected = (
                    signal.SIG_IGN if ("restore", sig) in failures else previous[sig]
                )
                assert signal.getsignal(sig) == expected
        assert "private-" not in canonical(cleanup).decode()


@pytest.mark.parametrize("phase", ["ignore", "restore"])
@pytest.mark.parametrize(
    "failed_sig", [signal.SIGINT, signal.SIGHUP], ids=["first", "middle"]
)
@pytest.mark.parametrize(
    "primary_kind", ["ledger", "keyboard", "system_exit", "host_signal", "none"]
)
def test_real_host_signal_failure_reaps_child_and_keeps_unknown(
    tmp_path, monkeypatch, phase, failed_sig, primary_kind
):
    monkeypatch.chdir(tmp_path)
    req, _ = std_fixture(Operation.TRADE)
    path = tmp_path / "plan.json"
    path.write_bytes(canonical(plan(req)))
    monkeypatch.setitem(host.DRIVER_BINARY_SHA256, host.I4_RELEASE, "b" * 64)

    @contextmanager
    def verified(*args):
        yield "/unused-synthetic", 0

    monkeypatch.setattr(host, "verified_driver", verified)
    monkeypatch.setattr(host, "replay", lambda *a, **k: pytest.fail("must stop"))
    primary = {
        "ledger": PermissionError("private-ledger-message"),
        "keyboard": KeyboardInterrupt(),
        "system_exit": SystemExit(17),
        "host_signal": host.HostSignal(),
        "none": None,
    }[primary_kind]
    signal_error = OSError("private-signal-message")
    original_popen, original_write = subprocess.Popen, Path.write_bytes
    child = None
    calls, streams = [], []

    def launch(*args, **kwargs):
        nonlocal child, original_poll, original_kill, original_wait
        streams.extend((kwargs["stdin"], kwargs["stdout"]))
        child = original_popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        original_poll, original_kill, original_wait = child.poll, child.kill, child.wait

        def kill():
            calls.append("kill")
            return original_kill()

        def wait(timeout=None):
            if timeout != 2:
                return 0
            calls.append("wait")
            return original_wait(timeout=timeout)

        child.kill, child.wait = kill, wait
        return child

    original_poll = original_kill = original_wait = None
    monkeypatch.setattr(subprocess, "Popen", launch)
    fired = False

    def write(destination, raw):
        nonlocal fired
        if destination.name == "host-ledger.json" and child is not None and not fired:
            fired = True
            if primary is not None:
                raise primary
        return original_write(destination, raw)

    monkeypatch.setattr(Path, "write_bytes", write)
    started = time.monotonic()
    try:
        with signal_failures(monkeypatch, {(phase, failed_sig): signal_error}) as (
            attempts,
            previous,
        ):
            if primary is not None and not isinstance(primary, Exception):
                with pytest.raises(type(primary)) as caught:
                    host.run_host(
                        path,
                        tmp_path / "mock",
                        tmp_path / "artifacts/run",
                        allow_live=True,
                        stage="afterclose",
                    )
                assert caught.value is primary
                result = json.loads(
                    (tmp_path / "artifacts/run/host-ledger.json").read_bytes()
                )
            else:
                result = host.run_host(
                    path,
                    tmp_path / "mock",
                    tmp_path / "artifacts/run",
                    allow_live=True,
                    stage="afterclose",
                )
            assert time.monotonic() - started < 5
            assert calls == ["kill", "wait"]
            assert child.returncode == -signal.SIGKILL
            with pytest.raises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)
            assert all(
                s.closed for s in (*streams, child.stdin, child.stdout, child.stderr)
            )
            assert [
                sig for operation, sig in attempts if operation == "restore"
            ] == list(CLEANUP_SIGNALS)
            assert result["exception"] == host.classify_exception(
                primary if primary is not None else signal_error
            )
            assert result["status"] == "STOP"
            assert result["subprocess_started"] is True
            assert result["provider_effect"] == "UNKNOWN_UNTIL_EXACT_RECEIPTS"
            assert result["source_freshness"] == "UNKNOWN"
            assert result["active_refresh"] == "NOT_PROVEN"
            assert result["pg_writes"] == result["garage_writes"] == 0
            assert result["timer_installed"] is False
            cleanup = result["child_cleanup"]
            assert cleanup["reaped"] == "CONFIRMED"
            assert cleanup["status"] == "UNKNOWN"
            assert cleanup["errors"] == [
                {
                    "operation": f"signal_{phase}",
                    "signal": failed_sig.name,
                    "exception": host.classify_exception(signal_error),
                }
            ]
            assert cleanup["signals"][failed_sig.name][phase] == "UNKNOWN"
            assert signal.getsignal(failed_sig) == (
                signal.SIG_IGN if phase == "restore" else previous[failed_sig]
            )
            assert "private-" not in canonical(result).decode()
            assert (
                json.loads((tmp_path / "artifacts/run/host-ledger.json").read_bytes())
                == result
            )
    finally:
        # Rescue is independent of managed_child and the injected signal failures.
        if child is not None:
            if original_poll() is None:
                original_kill()
            original_wait(timeout=2)
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()
