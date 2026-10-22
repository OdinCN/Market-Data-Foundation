"""Final three P2 regressions: local pipes, invented frames, no Provider access."""

import base64
import io
import json
import os
import signal
import struct
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_i2_market_corrections import NOW, collect_kwargs
from test_i2_market_source import plan, source_line

from market_data_foundation.qualification import tdx_market_collect as runner
from market_data_foundation.qualification.tdx_market_evidence import request_wire
from market_data_foundation.qualification.tdx_market_plan import (
    UPSTREAM,
    Request,
    encoded,
)


def handshake():
    response = (
        b"\xb1\xcb\x74\x00\x1c"
        + struct.pack("<I", 1)
        + b"\x00"
        + struct.pack("<HHH", 13, 1, 1)
        + b"\x00"
    )
    row = {
        "index": -1,
        "upstream_revision": UPSTREAM,
        "request": {
            "operation": "MConnect",
            "market": 0,
            "code": "",
            "start": 0,
            "count": 0,
        },
        "requested_at": "2026-10-22T08:00:00Z",
        "received_at": "2026-10-22T08:00:00Z",
        "request_wire": base64.b64encode(request_wire(None, -1)).decode(),
        "response_wire": base64.b64encode(response).decode(),
        "classification": "retained_raw",
        "source_projection": None,
    }
    raw = json.dumps(row, separators=(",", ":")).encode()
    assert len(raw) < 404
    return raw + b" " * (403 - len(raw)) + b"\n"


@contextmanager
def sealed(*args):
    yield "unused-test-candidate", 123


def setup_collection(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "utc_now", lambda: NOW + timedelta(seconds=2))
    monkeypatch.setattr(runner, "verify_endpoint", lambda *a: None)
    monkeypatch.setattr(runner, "sealed_candidate", sealed)


def assert_unknown(ledger, p, *, stage="HANDSHAKE_ONLY", request=13, response=17):
    assert ledger["actual_request_bytes"] == "UNKNOWN"
    assert ledger["actual_response_bytes"] == "UNKNOWN"
    effect = ledger["source_effect"]
    assert effect["total_effect"] == "UNKNOWN"
    assert effect["stage"] == stage
    assert effect["run_id"] == p.run_id and effect["plan_revision"] == p.revision
    assert effect["observed_confirmed_lower_bound"] == {
        "request_bytes": request,
        "response_bytes": response,
    }
    assert len(effect["jobs"]) == len(p.requests) + 1


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, runner.TerminationRequested])
def test_unread_404_pipe_bytes_survive_stop_and_fsync(tmp_path, monkeypatch, interrupt):
    setup_collection(tmp_path, monkeypatch)
    raw = handshake()
    output = collect_kwargs(tmp_path)["output"]
    children = []
    real_popen = subprocess.Popen
    real_selector = runner.selectors.DefaultSelector
    real_read = os.read
    real_fsync = os.fsync
    fsynced = []
    reads = []

    def spawn(*args, **kwargs):
        # Only a local inert writer. No socket, provider binary or network.
        child = real_popen(
            [
                sys.executable,
                "-c",
                "import os,signal; os.write(1, " + repr(raw) + "); signal.pause()",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        children.append(child)
        return child

    def read(fd, size):
        if children and fd == children[0].stdout.fileno():
            reads.append(size)
            assert children[0].poll() is not None  # stop/reap precedes drain
            assert not os.get_blocking(fd)
        return real_read(fd, size)

    def fsync(fd):
        real_fsync(fd)
        target = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if target.is_file():
            fsynced.append((target.name, target.read_bytes()))

    class InterruptBeforeRead:
        def __enter__(self):
            self.selector = real_selector()
            return self

        def __exit__(self, *args):
            self.selector.close()

        def register(self, *args):
            self.selector.register(*args)

        def select(self, timeout):
            assert self.selector.select(min(timeout, 3)), "local child did not write"
            assert not reads
            assert (output / "source-stdout-progress.jsonl").read_bytes() == b""
            if interrupt is runner.TerminationRequested:
                signal.raise_signal(signal.SIGTERM)
            raise interrupt()

    monkeypatch.setattr(runner.subprocess, "Popen", spawn)
    monkeypatch.setattr(runner.selectors, "DefaultSelector", InterruptBeforeRead)
    monkeypatch.setattr(runner.os, "read", read)
    monkeypatch.setattr(runner.os, "fsync", fsync)
    monkeypatch.setattr(
        runner,
        "DeadlineFramedProcess",
        lambda *a, **kw: pytest.fail("quote after stop"),
    )
    before = time.monotonic()
    try:
        with pytest.raises(interrupt):
            runner.collect(plan(), **collect_kwargs(tmp_path))
        assert time.monotonic() - before < 5
        assert len(children) == 1 and children[0].poll() == -signal.SIGKILL
        assert (output / "source-stdout-partial.jsonl").read_bytes() == raw
        assert (output / "source-stdout-progress.jsonl").read_bytes() == raw
        ledger = json.loads((output / "effect-ledger.json").read_bytes())
        assert_unknown(ledger, plan())
        assert ledger["source_cleanup"] == {
            "child_reaped": True,
            "drain": "EOF",
            "errors": [],
        }
        assert (
            ledger["source_effect"]["jobs"][0]["record_state"] == "CONFIRMED_COMPLETE"
        )
        assert all(
            j["record_state"] == "UNCONFIRMED_EFFECT_UNKNOWN"
            for j in ledger["source_effect"]["jobs"][1:]
        )
        assert ledger["materials"][0]["sha256"] == sha256(raw).hexdigest()
        assert ledger["quote"] == "NOT_ATTEMPTED"
        assert ("source-stdout-progress.jsonl", raw) in fsynced
        assert ("source-stdout-partial.jsonl", raw) in fsynced
        assert any(n == "effect-ledger.json" for n, _ in fsynced)
        intent = json.loads(next(b for n, b in fsynced if n == "source-intent.json"))
        assert intent["source_effect"]["total_effect"] == "UNKNOWN"
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)


@pytest.mark.parametrize("mode", ["open_writer", "byte_limit", "reap_timeout"])
def test_cleanup_never_blocks_on_open_pipe_and_respects_budget(monkeypatch, mode):
    reader, writer = os.pipe()
    raw = handshake()
    os.write(writer, raw)
    stdout = os.fdopen(reader, "rb", buffering=0)
    calls = []

    def wait(*, timeout):
        calls.append(("wait", timeout))
        if mode == "reap_timeout":
            raise subprocess.TimeoutExpired("local fixture", timeout)
        return -9

    child = SimpleNamespace(
        stdin=io.BytesIO(),
        stdout=stdout,
        poll=lambda: None,
        kill=lambda: calls.append("kill"),
        wait=wait,
    )
    monkeypatch.setattr(runner, "sealed_candidate", sealed)
    monkeypatch.setattr(runner, "utc_now", lambda: NOW)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **kw: child)
    if mode == "byte_limit":
        monkeypatch.setattr(runner, "SOURCE_STDOUT_LIMIT", 200, raising=False)

    class InterruptSelector:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def register(self, *args):
            pass

        def select(self, timeout):
            raise KeyboardInterrupt()

    monkeypatch.setattr(runner.selectors, "DefaultSelector", InterruptSelector)
    chunks = []
    start = time.monotonic()
    try:
        with pytest.raises(runner.SourceStopped) as caught:
            runner.source_once(
                plan(),
                Path("unused"),
                deadline=NOW + timedelta(minutes=5),
                on_chunk=chunks.append,
            )
        assert time.monotonic() - start < 1
        expected = raw[:200] if mode == "byte_limit" else raw
        assert caught.value.raw == b"".join(chunks) == expected
        assert calls == ["kill", ("wait", 2)]
        assert caught.value.cleanup["drain"] == (
            "BYTE_LIMIT" if mode == "byte_limit" else "AVAILABLE_BYTES_EXHAUSTED"
        )
        assert caught.value.cleanup["child_reaped"] is (mode != "reap_timeout")
    finally:
        os.close(writer)
        stdout.close()


@pytest.mark.parametrize(
    "status,shape",
    [(-9, "handshake"), (0, "handshake"), (-9, "full"), (0, "full"), (-9, "partial")],
)
def test_collect_accounts_confirmed_bounds_separately_from_unknown_total(
    tmp_path, monkeypatch, status, shape
):
    setup_collection(tmp_path, monkeypatch)
    p = plan(requests=(Request("GetCount", 1),))
    raw = handshake()
    if shape == "full":
        raw += source_line(p, 0, {"Count": 3}) + b"\n"
    if shape == "partial":
        row = json.loads(source_line(p, 0, {"Count": 3}))
        row.update(
            classification="truncated_response",
            response_wire=base64.b64encode(b"abc").decode(),
        )
        raw += encoded(row) + b"\n"

    def source(*args, on_chunk, **kwargs):
        on_chunk(raw)
        return raw, status

    monkeypatch.setattr(runner, "source_once", source)

    # Stop the independently budgeted quote before any process starts.
    def no_quote(*a, **kw):
        raise RuntimeError("local fixture stop")

    monkeypatch.setattr(runner, "DeadlineFramedProcess", no_quote)
    ledger = runner.collect(p, **collect_kwargs(tmp_path))
    assert ledger["source_exit"] == status
    if status != 0 or shape != "full":
        assert (ledger["actual_request_bytes"], ledger["actual_response_bytes"]) == (
            "UNKNOWN",
            "UNKNOWN",
        )
    effect = ledger["source_effect"]
    if status == 0 and shape == "full":
        assert ledger["actual_request_bytes"] == 31
        assert ledger["actual_response_bytes"] == 35
        assert effect["total_effect"] == "EXACT" and effect["stage"] == "COMPLETE"
        assert ledger["source"] == "BOUNDED_READ_COMPLETE"
    else:
        assert_unknown(
            ledger,
            p,
            stage="HANDSHAKE_ONLY" if shape == "handshake" else "PARTIAL",
            request=13 if shape == "handshake" else 31,
            response={"handshake": 17, "full": 35, "partial": 20}[shape],
        )
        assert ledger["source"] == "STOPPED_CLASSIFIED"
    assert effect["jobs"][0]["record_state"] == "CONFIRMED_COMPLETE"
    expected = {
        "handshake": "UNCONFIRMED_EFFECT_UNKNOWN",
        "full": "CONFIRMED_COMPLETE",
        "partial": "CONFIRMED_PARTIAL",
    }[shape]
    assert effect["jobs"][1]["record_state"] == expected
    assert effect["jobs"][1]["request"] == {
        "operation": "GetCount",
        "market": 1,
        "code": "",
        "start": 0,
        "count": 0,
    }
