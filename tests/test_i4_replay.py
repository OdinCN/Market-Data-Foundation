import base64
import json
import struct
import zlib
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from market_data_foundation.candidates.tdx_intraday_v1 import Operation
from market_data_foundation.candidates.tdx_intraday_v1.model import (
    SDK_COMMIT,
    DecodeError,
    canonical,
    sha,
)
from market_data_foundation.candidates.tdx_intraday_v1.plan import validate_plan
from market_data_foundation.candidates.tdx_intraday_v1.replay import (
    normalized_plan,
    replay,
    strict_json,
)
from market_data_foundation.candidates.tdx_intraday_v1.wire import (
    payload_from_wire,
    request_wire,
    setup_wire,
)
from tests.test_i4_exhq import EX_OPERATIONS, fixture
from tests.test_i4_intraday import fixture as std_fixture


def response(req, payload, compressed=False):
    from market_data_foundation.candidates.tdx_intraday_v1.model import CATALOG

    body = zlib.compress(payload, level=0) if compressed else payload
    return (
        b"\xb1\xcb\x74\x00\x1c"
        + struct.pack("<I", 1)
        + b"\0"
        + struct.pack("<HHH", CATALOG[req.operation][1], len(body), len(payload))
        + body
    )


def plan(req):
    return {
        "schema": "mdf.tdx.i4.raw-probe.v1",
        "sdk_commit": SDK_COMMIT,
        "run_id": "a" * 32,
        "source": "synthetic.tdx",
        "binding": "synthetic.binding",
        "endpoint": "116.205.143.214:7727"
        if req.operation.value.startswith("Ex")
        else "124.71.187.122:7709",
        "family": "exhq" if req.operation.value.startswith("Ex") else "standard",
        "directory_identity": "",
        "requests": [req.to_dict()],
        "max_requests": 1,
        "max_request_bytes": 4096,
        "max_response_bytes": 4096,
        "max_decoded_bytes": 4096,
        "max_frames": 2,
        "wall_seconds": 3,
        "operation_seconds": 1,
        "round_interval_seconds": 0,
    }


def record(p, req, raw, compressed=False):
    wire = response(req, raw, compressed)
    stamp = datetime(2026, 10, 22, 10, tzinfo=UTC).isoformat()
    return {
        "schema": p["schema"],
        "sdk_commit": SDK_COMMIT,
        "run_id": p["run_id"],
        "plan_sha256": sha(canonical(normalized_plan(p))),
        "endpoint": p["endpoint"],
        "classification": "payload_retained_not_semantically_qualified",
        "index": 0,
        "request": req.to_dict(),
        "wire": base64.b64encode(wire).decode(),
        "wire_sha256": sha(wire),
        "payload": base64.b64encode(raw).decode(),
        "payload_sha256": sha(raw),
        "request_wire": base64.b64encode(request_wire(req, 1)).decode(),
        "requested_at": stamp,
        "received_at": stamp,
        "requested_bytes": len(request_wire(req, 1)),
        "written_bytes": len(request_wire(req, 1)),
        "response_bytes": len(wire),
        "decoded_bytes": len(raw),
        "frames": 1,
    }


def transcript(p, records, *, complete=False):
    base = {
        key: records[0][key]
        for key in ("schema", "sdk_commit", "run_id", "plan_sha256", "endpoint")
    }
    counters = dict(
        requested_bytes=0, written_bytes=0, response_bytes=0, decoded_bytes=0, frames=0
    )
    intent = dict(base, **counters, classification="", index=-1, plan=p)
    setup = setup_wire(p["family"])
    written = dict(
        base,
        **counters,
        classification="setup_written",
        index=-1,
        request_wire=base64.b64encode(setup).decode(),
    )
    written.update(requested_bytes=len(setup), written_bytes=len(setup))
    result = [intent, written, *records]
    if complete:
        total = len(setup) + sum(
            r["written_bytes"]
            for r in records
            if r["classification"] != "setup_response"
        )
        last = records[-1]
        result.append(
            dict(
                base,
                index=-1,
                classification="budget_complete",
                requested_bytes=total,
                written_bytes=total,
                **{k: last[k] for k in ("response_bytes", "decoded_bytes", "frames")},
            )
        )
    return tuple(result)


@pytest.mark.parametrize("op", list(Operation))
@pytest.mark.parametrize("compressed", [False, True])
def test_full_receipt_raw_wire_payload_projection_readback(
    tmp_path, monkeypatch, op, compressed
):
    monkeypatch.chdir(tmp_path)
    req, raw = fixture(op) if op in EX_OPERATIONS else std_fixture(op)
    p = plan(req)
    r = record(p, req, raw, compressed)
    validate_plan(p)
    output = tmp_path / "artifacts" / "capture"
    result = replay(
        p,
        transcript(p, [r]),
        output,
        producer_sha256="b" * 64,
        evidence_mode="synthetic",
    )
    assert not result["failures"] and result["all_requested_pages_received"]
    assert result["terminal"] == "INCOMPLETE"
    assert (output / "000.payload.bin").read_bytes() == raw
    assert (output / "000.wire.bin").read_bytes() == base64.b64decode(r["wire"])
    projection = json.loads((output / "000.projection.json").read_bytes())
    assert projection["receipt"]["raw_sha256"] == sha(raw)
    assert projection["receipt"]["known_at"] is None
    assert result["source_freshness"] == "UNKNOWN"
    with pytest.raises(FileExistsError):
        replay(
            p,
            transcript(p, [r]),
            output,
            producer_sha256="b" * 64,
            evidence_mode="synthetic",
        )


@pytest.mark.parametrize(
    "mutation",
    ["wire_hash", "request", "payload", "clock", "request_wire", "duplicate"],
)
def test_replay_rejects_mismatch_retains_original_and_no_raw_errors(
    tmp_path, monkeypatch, mutation
):
    monkeypatch.chdir(tmp_path)
    req, raw = fixture(Operation.EX_QUOTE)
    p = plan(req)
    r = record(p, req, raw)
    if mutation == "wire_hash":
        r["wire_sha256"] = "0" * 64
    if mutation == "request":
        r["request"] = replace(req, market=48).to_dict()
    if mutation == "payload":
        r["payload"] = base64.b64encode(b"synthetic-private-message").decode()
    if mutation == "clock":
        r["requested_at"] = "2026-10-22T10:00:00"
    if mutation == "request_wire":
        r["request_wire"] = base64.b64encode(b"wrong").decode()
    records = (r, r) if mutation == "duplicate" else (r,)
    output = tmp_path / "artifacts" / "failed"
    result = replay(
        p,
        transcript(p, records),
        output,
        producer_sha256="b" * 64,
        evidence_mode="synthetic",
    )
    assert result["failures"]
    assert "synthetic-private-message" not in canonical(result).decode()
    assert (output / "records.jsonl").exists()


def test_wire_compression_bomb_tail_control_and_identity():
    req, raw = fixture(Operation.EX_QUOTE)
    wire = response(req, raw, True)
    for changed in (
        wire[:10],
        bytes([0]) + wire[1:],
        wire[:4] + b"\x0c" + wire[5:],
        wire[:5] + struct.pack("<I", 9) + wire[9:],
    ):
        with pytest.raises(DecodeError):
            payload_from_wire(changed, message_id=1, operation=req.operation.value)
    body = zlib.compress(b"x" * 20000)
    bomb = wire[:12] + struct.pack("<HH", len(body), 5) + body
    with pytest.raises(DecodeError):
        payload_from_wire(bomb, message_id=1, operation=req.operation.value)
    tail = (
        wire[:12] + struct.pack("<HH", len(wire) - 16 + 1, len(raw)) + wire[16:] + b"x"
    )
    with pytest.raises(DecodeError):
        payload_from_wire(tail, message_id=1, operation=req.operation.value)


def test_plan_and_json_fail_closed(tmp_path, monkeypatch):
    req, _ = fixture(Operation.EX_QUOTE)
    p = plan(req)
    for changes in (
        {"max_requests": 2},
        {"max_request_bytes": 1},
        {"family": "standard"},
        {"operation_seconds": float("inf")},
        {"max_frames": 99},
        {"wall_seconds": True},
        {"run_id": "x"},
        {"extra": 1},
    ):
        with pytest.raises(DecodeError):
            validate_plan(dict(p, **changes))
    with pytest.raises(DecodeError, match="duplicate"):
        strict_json(b'{"x":1,"x":2}')
    with pytest.raises(DecodeError):
        strict_json(b'{"x":NaN}')
    monkeypatch.chdir(tmp_path)
    with pytest.raises(DecodeError):
        replay(
            p,
            (),
            tmp_path / "outside",
            producer_sha256="b" * 64,
            evidence_mode="synthetic",
        )
