"""Invented SDK rows and IPC only. No provider request or TCP probe."""

import json
import struct
import sys
import time
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from test_p5_go_stock_real_observation_ingress import BASE, fixture

from market_data_foundation.candidates.go_stock_real_observation_ingress import (
    SESSION_SCHEMA,
)
from market_data_foundation.canonical.model import ContractError
from market_data_foundation.runtime.tdx_watchlist import (
    DRIVER_SDK_RELEASE,
    FramedProcess,
    RealPeriodicWatchlistSession,
    SessionConfig,
    WatchlistIngress,
)

CONFIG = SessionConfig(
    securities=("sh600519", "sz000001", "sz000002"),
    rounds=4,
    session_id="a" * 32,
    endpoints=("mock-a:7709", "mock-b:7709"),
)


def batch(n=1, *, failure=None, segment=0, endpoint="mock-a:7709"):
    body = fixture(n)
    body.update(
        schema=SESSION_SCHEMA,
        sdk_release=DRIVER_SDK_RELEASE,
        session_id=CONFIG.session_id,
        batch_id=f"{CONFIG.session_id}-{n}",
        max_rounds=CONFIG.rounds,
        segment=segment,
        endpoint=endpoint,
        securities=list(CONFIG.securities),
    )
    body["connection"]["peer"] = "203.0.113.1:7709"
    third = deepcopy(body["rows"][1])
    third["Code"] = "000002"
    body["rows"].append(third)
    body["failure"] = failure
    if failure:
        body["rows"] = None
    if segment:
        body["connection"]["attempts"] = segment + 1
        body["connection"]["requested"] = (BASE + timedelta(seconds=15)).isoformat()
        body["connection"]["received"] = (BASE + timedelta(seconds=16)).isoformat()
    return body


def wire(body):
    return json.dumps(body, allow_nan=False).encode()


def binding(body):
    return {
        k: body[k]
        for k in ("session_id", "sdk_release", "endpoint", "segment", "connection")
    }


def clocks(n):
    return BASE + timedelta(seconds=n * 10 + 3)


def test_dynamic_four_rounds_real_parser_duplicates_and_reads():
    session = RealPeriodicWatchlistSession(CONFIG, offline_fixture_mode=True)
    session.ingress.bind(binding(batch()))
    for n in range(1, 5):
        frozen = wire(batch(n))
        ack = session.consume(frozen, known_at=clocks(n), read_at=clocks(n))
        assert ack["sha256"] == sha256(frozen).hexdigest()
        assert len(ack["reads"]) == 3 and ack["round"] == n
        assert session.rounds[-1]["outcome"] == ("sample" if n == 1 else "duplicate")
        assert all(r["status"] == "observed" for r in session.rounds[-1]["reads"])
        assert session.rounds[-1]["input_origin"] == "author_created_offline_fixture"
        assert session.rounds[-1]["source_event_clock"] == "UNKNOWN"
    with pytest.raises(ContractError, match="round_budget_invalid"):
        session.consume(wire(batch(5)), known_at=clocks(5), read_at=clocks(5))


@pytest.mark.parametrize("failure", ["timeout", "disconnected", "source_error"])
def test_failure_read_barrier_and_new_segment_baseline(failure):
    session = RealPeriodicWatchlistSession(CONFIG, offline_fixture_mode=True)
    first = batch(failure=failure)
    session.ingress.bind(binding(first))
    ack = session.consume(wire(first), known_at=clocks(1), read_at=clocks(1))
    assert ack["accepted"] and all(
        r["status"] == "unavailable" for r in session.rounds[-1]["reads"]
    )
    assert session.rounds[-1]["classification"] == failure
    new = batch(2, segment=1, endpoint="mock-b:7709")
    with pytest.raises(
        ContractError, match="endpoint_mismatch|new_connection_required"
    ):
        session.ingress.ingest(wire(new), known_at=clocks(2))
    session.ingress.bind(binding(new))
    session.consume(wire(new), known_at=clocks(2), read_at=clocks(2))
    assert session.rounds[-1]["segment"] == 1
    assert session.rounds[-1]["endpoint"] == "mock-b:7709"
    assert session.rounds[-1]["outcome"] == "sample"


def test_atomic_bad_member_never_yields_ack_or_partial_publish():
    session = RealPeriodicWatchlistSession(CONFIG, offline_fixture_mode=True)
    session.ingress.bind(binding(batch()))
    session.consume(wire(batch()), known_at=clocks(1), read_at=clocks(1))
    bad = batch(2)
    bad["rows"][2]["BuyLevel"][0]["Number"] = -1
    with pytest.raises(ContractError, match="read_quality_rejected"):
        session.consume(wire(bad), known_at=clocks(2), read_at=clocks(2))
    assert len(session.rounds) == 1
    record = session.ingress.history[-1].record
    assert record.outcome == "failed" and record.quotes == ()
    for symbol in CONFIG.securities:
        view = session.ingress.read(
            symbol, at=clocks(2), batch_id=session.ingress.history[-1].batch_id
        )
        assert view.status == "unavailable"
        assert view.state.acquisition == "sample-1"


def test_real_ingress_forbids_fixture_relabel_and_known_override():
    ingress = WatchlistIngress(CONFIG)
    ingress.bind(binding(batch()))
    with pytest.raises(ContractError, match="real_known_override_forbidden"):
        ingress.ingest(wire(batch()), known_at=clocks(1))
    with pytest.raises(ContractError, match="input_origin_mismatch"):
        ingress.ingest(wire(batch()))
    assert not ingress.history


@pytest.mark.parametrize(
    "change",
    [
        {"securities": ()},
        {"securities": ("sh600519", "sh600519")},
        {"securities": ("sh510300",)},
        {"rounds": 121},
        {"rounds": True},
        {"period_seconds": 0},
        {"period_seconds": float("nan")},
        {"duration_seconds": 901},
        {"max_probes": 9},
        {"max_connections": 9},
        {"max_failovers": 4},
        {"endpoints": ("host:65536",)},
        {"endpoints": ("host:7709/path",)},
        {"start_at": "2026-01-01T00:00:00"},
    ],
)
def test_config_is_bounded(change):
    with pytest.raises(ContractError):
        replace(CONFIG, **change)


class Adapter:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.sent = []
        self.closed = 0

    def receive(self, **kwargs):
        # A second sample must never be consumed ahead of the previous ACK.
        return next(self.messages)

    def send(self, kind, body, **kwargs):
        self.sent.append(body)

    def close(self):
        self.closed += 1


def event(
    classification,
    reason="",
    endpoint="mock-a:7709",
    round=0,
    segment=0,
    attempt=1,
    **extra,
):
    return wire(
        {
            "classification": classification,
            "reason": reason,
            "session_id": CONFIG.session_id,
            "endpoint": endpoint,
            "candidate_origin": "configured",
            "elapsed_ns": 100,
            "attempt": attempt,
            "round": round,
            "segment": segment,
            **extra,
        }
    )


def quote_events(n, segment=0):
    endpoint = "mock-b:7709" if segment else "mock-a:7709"
    return [
        ("E", event("quote_attempt", round=n, segment=segment, endpoint=endpoint)),
        (
            "E",
            event("sdk_getquote_entered", round=n, segment=segment, endpoint=endpoint),
        ),
        (
            "E",
            event(
                "protocol_write_attempt",
                round=n,
                segment=segment,
                endpoint=endpoint,
                requested_bytes=29,
            ),
        ),
        (
            "E",
            event(
                "protocol_write_complete",
                round=n,
                segment=segment,
                endpoint=endpoint,
                requested_bytes=29,
                written_bytes=29,
            ),
        ),
    ]


def test_read_error_stops_without_ack_and_closes(monkeypatch):
    session = RealPeriodicWatchlistSession(CONFIG, offline_fixture_mode=True)
    adapter = Adapter(
        [
            ("E", event("probe_ok")),
            ("E", event("connection_attempt")),
            ("C", wire(binding(batch()))),
            *quote_events(1),
            ("B", wire(batch())),
        ]
    )
    original = session.ingress.read
    calls = []

    def read(symbol, **kwargs):
        calls.append(symbol)
        if symbol == CONFIG.securities[-1]:
            raise RuntimeError("private error must not escape")
        return original(symbol, **kwargs)

    monkeypatch.setattr(session.ingress, "read", read)
    report = session.run(adapter, fixture_clock=lambda: clocks(1))
    assert len(calls) == 3 and not adapter.sent and adapter.closed == 1
    assert report["stop_reason"] == "runtime_error"
    assert "private error" not in json.dumps(report)


def test_mock_adapter_all_reads_precede_ack(monkeypatch):
    session = RealPeriodicWatchlistSession(CONFIG, offline_fixture_mode=True)
    adapter = Adapter(
        [
            ("E", event("probe_ok")),
            ("E", event("connection_attempt")),
            ("C", wire(binding(batch()))),
        ]
        + [
            message
            for n in range(1, 5)
            for message in (
                *quote_events(n),
                ("B", wire(batch(n))),
                ("E", event("ack_received", round=n)),
            )
        ]
        + [("E", event("complete", "budget_exhausted", round=CONFIG.rounds))]
    )
    order = []
    original = session.ingress.read

    def read(symbol, **kwargs):
        order.append(("read", symbol))
        return original(symbol, **kwargs)

    monkeypatch.setattr(session.ingress, "read", read)

    def send(kind, body, **kwargs):
        assert order[-3:] == [("read", m) for m in CONFIG.securities]
        order.append(("ack", body["round"]))
        adapter.sent.append(body)

    adapter.send = send
    now = iter(clocks(n) for n in range(1, 5))
    report = session.run(adapter, fixture_clock=lambda: next(now))
    assert report["stop_reason"] == "budget_exhausted"
    assert len(adapter.sent) == 4 and adapter.closed == 1


# Actual child pipes, fragmented frames and SHA/ACK gating; no network imports.
CHILD = r"""
import sys, json, struct, hashlib
from datetime import datetime, UTC, timedelta

def read(kind):
    header = sys.stdin.buffer.read(5)
    assert header[0] == ord(kind)
    return json.loads(sys.stdin.buffer.read(struct.unpack('>I', header[1:])[0]))

def send(kind, body):
    raw = json.dumps(body).encode()
    frame = kind.encode() + struct.pack('>I', len(raw)) + raw
    for i in range(0, len(frame), 7):
        sys.stdout.buffer.write(frame[i:i+7]); sys.stdout.buffer.flush()
    return raw
c = read('K')
event = dict(session_id=c['session_id'], endpoint='mock-a:7709',
             candidate_origin='configured', elapsed_ns=100, attempt=1, round=0,
             segment=0, classification='probe_ok', reason='')
send('E', event)
send('E', dict(event, classification='connection_attempt'))
conn = dict(requested=datetime.now(UTC).isoformat(),
            received=datetime.now(UTC).isoformat(),
            elapsed_ns=100, redial=False, attempts=1, peer="203.0.113.1:7709")
binding = dict(session_id=c['session_id'], sdk_release=RELEASE, endpoint='mock-a:7709',
               segment=0, connection=conn)
send('C', binding)
for n in range(1, c['rounds']+1):
    body = template(n)
    body.update(binding, max_rounds=c['rounds'], securities=c['securities'])
    body['clocks'] = {k: datetime.now(UTC).isoformat() for k in
                      ('requested','received','frozen')}
    send('E', dict(event, classification='quote_attempt', round=n))
    send('E', dict(event, classification='sdk_getquote_entered', round=n))
    send('E', dict(event, classification='protocol_write_attempt', round=n,
                   requested_bytes=29))
    send('E', dict(event, classification='protocol_write_complete', round=n,
                   requested_bytes=29, written_bytes=29))
    raw = send('B', body)
    ack = read('A')
    assert ack['sha256'] == hashlib.sha256(raw).hexdigest()
    assert ack['round'] == n and len(ack['reads']) == len(c['securities'])
    send('E', dict(event, classification='ack_received', round=n))
send('E', dict(event, classification='complete', reason='budget_exhausted',
               round=c['rounds']))
"""


def test_actual_mock_child_ipc_and_ack(tmp_path):
    from market_data_foundation.runtime.tdx_watchlist import DRIVER_SDK_RELEASE

    child = tmp_path / "child.py"
    child.write_text(
        "RELEASE="
        + repr(DRIVER_SDK_RELEASE)
        + "\n"
        + "bodies="
        + repr([batch(n) for n in range(1, 5)])
        + "\n"
        + "def template(n): return bodies[n-1]\n"
        + CHILD
    )
    config = replace(CONFIG, duration_seconds=5)
    adapter = FramedProcess([sys.executable, str(child)], config)
    session = RealPeriodicWatchlistSession(config, offline_fixture_mode=True)
    report = session.run(adapter, fixture_clock=lambda: datetime.now(UTC))
    assert report["stop_reason"] == "budget_exhausted"
    assert len(report["rounds"]) == 4 and adapter.process.poll() == 0
    assert sum(e["classification"] == "ack_received" for e in report["events"]) == 4


def test_pipe_timeout_kills_and_reaps_child(tmp_path):
    child = tmp_path / "stalled.py"
    child.write_text("import time\ntime.sleep(30)\n")
    adapter = FramedProcess([sys.executable, str(child)], CONFIG)
    with pytest.raises(TimeoutError):
        adapter.receive(deadline=time.monotonic() + 0.05)
    adapter.close()
    adapter.close()
    assert adapter.process.poll() is not None


def test_oversize_frame_rejected_before_body_allocation(tmp_path):
    child = tmp_path / "oversize.py"
    child.write_text(
        "import sys\nsys.stdout.buffer.write(b'B' + "
        + repr(struct.pack(">I", 131073))
        + ")\nsys.stdout.buffer.flush()\n"
    )
    adapter = FramedProcess([sys.executable, str(child)], CONFIG)
    try:
        with pytest.raises(ContractError, match="ipc_frame_invalid"):
            adapter.receive(deadline=time.monotonic() + 2)
    finally:
        adapter.close()


def test_cli_missing_optin_never_launches_child(tmp_path):
    import subprocess

    child = tmp_path / "unavailable"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "market_data_foundation.runtime.tdx_watchlist",
            "--driver",
            str(child),
        ],
        capture_output=True,
        text=True,
    )
    assert (
        result.returncode != 0
        and result.stderr.strip() == "explicit_live_opt_in_required"
    )


def test_same_content_after_gap_is_new_baseline_and_keeps_previous_age():
    session = RealPeriodicWatchlistSession(CONFIG, offline_fixture_mode=True)
    session.ingress.bind(binding(batch()))
    session.consume(wire(batch()), known_at=clocks(1), read_at=clocks(1))
    session.consume(
        wire(batch(2, failure="disconnected")), known_at=clocks(2), read_at=clocks(2)
    )
    failed = session.rounds[-1]
    assert failed["reads"][0]["local_received_age_seconds"] == 12
    recovered = batch(3, segment=1, endpoint="mock-b:7709")
    recovered["connection"]["requested"] = (BASE + timedelta(seconds=24)).isoformat()
    recovered["connection"]["received"] = (BASE + timedelta(seconds=25)).isoformat()
    session.ingress.bind(binding(recovered))
    session.consume(wire(recovered), known_at=clocks(3), read_at=clocks(3))
    assert session.rounds[-1]["outcome"] == "sample"
    assert session.rounds[-1]["segment"] == 1
    assert session.rounds[-1]["reads"][0]["local_received_age_seconds"] == 2


def test_invalid_preflight_still_closes_adapter():
    future = datetime.now(UTC) + timedelta(seconds=500)
    session = RealPeriodicWatchlistSession(
        replace(CONFIG, start_at=future.isoformat()), offline_fixture_mode=True
    )
    adapter = Adapter([])
    report = session.run(adapter)
    assert report["stop_reason"] == "start_wait_bound" and adapter.closed == 1


def test_next_batch_without_go_receipt_is_rejected():
    session = RealPeriodicWatchlistSession(CONFIG, offline_fixture_mode=True)
    adapter = Adapter(
        [
            ("E", event("probe_ok")),
            ("E", event("connection_attempt")),
            ("C", wire(binding(batch()))),
            *quote_events(1),
            ("B", wire(batch())),
            ("B", wire(batch(2))),
        ]
    )
    report = session.run(adapter, fixture_clock=lambda: clocks(1))
    assert report["stop_reason"] == "next_batch_before_ack_receipt"
    assert len(adapter.sent) == 1 and adapter.closed == 1
    assert report["rounds"][0]["ack_sent"]
    assert not report["rounds"][0]["go_ack_received"]


@pytest.mark.parametrize("count", [1, 32])
def test_watchlist_minimum_and_maximum_are_atomic(count):
    members = tuple(f"sz{i:06d}" for i in range(count))
    config = replace(CONFIG, securities=members, rounds=1)
    body = batch()
    template = body["rows"][1]
    body["rows"] = [{**deepcopy(template), "Code": member[2:]} for member in members]
    body.update(securities=list(members), max_rounds=1)
    session = RealPeriodicWatchlistSession(config, offline_fixture_mode=True)
    session.ingress.bind(binding(body))
    ack = session.consume(wire(body), known_at=clocks(1), read_at=clocks(1))
    assert len(ack["reads"]) == count
    assert all(r["status"] == "observed" for r in session.rounds[-1]["reads"])


def stream_prefix(body=None):
    body = body or batch()
    return [
        ("E", event("probe_ok")),
        ("E", event("probe_ok", endpoint="mock-b:7709", attempt=2)),
        ("E", event("connection_attempt")),
        ("C", wire(binding(body))),
    ]


@pytest.mark.parametrize("seconds", [0.1, 10])
@pytest.mark.parametrize("extra_ns", [1, 250_000_000, 250_000_001])
def test_elapsed_kept_with_configured_finite_teardown_tolerance(seconds, extra_ns):
    config = replace(CONFIG, quote_timeout_seconds=seconds)
    body = batch(failure="timeout")
    body["quote_elapsed_ns"] = int(seconds * 1e9) + extra_ns
    session = RealPeriodicWatchlistSession(config, offline_fixture_mode=True)
    session.ingress.bind(binding(body))
    if extra_ns > 250_000_000:
        with pytest.raises(ContractError, match="latency_bound"):
            session.consume(wire(body), known_at=clocks(1), read_at=clocks(1))
        assert not session.ingress.history
    else:
        ack = session.consume(wire(body), known_at=clocks(1), read_at=clocks(1))
        assert ack["accepted"]
        assert session.ingress.history[-1].quote_elapsed_ns == body["quote_elapsed_ns"]
        assert all(r["status"] == "unavailable" for r in session.rounds[-1]["reads"])


def test_timeout_over_ten_seconds_ack_then_failover_and_complete():
    config = replace(CONFIG, rounds=2, quote_timeout_seconds=10)
    failed = batch(failure="timeout")
    failed.update(max_rounds=2)
    failed["quote_elapsed_ns"] = 10_000_000_001
    recovered = batch(2, segment=1, endpoint="mock-b:7709")
    recovered.update(max_rounds=2)
    messages = (
        stream_prefix(failed)
        + quote_events(1)
        + [
            ("E", event("quote_failed", "timeout", round=1)),
            ("B", wire(failed)),
            ("E", event("ack_received", round=1)),
            (
                "E",
                event(
                    "connection_attempt", endpoint="mock-b:7709", segment=1, attempt=2
                ),
            ),
            ("C", wire(binding(recovered))),
        ]
        + quote_events(2, segment=1)
        + [
            ("B", wire(recovered)),
            ("E", event("ack_received", round=2, segment=1)),
            ("E", event("complete", "budget_exhausted", round=2, segment=1)),
        ]
    )
    session = RealPeriodicWatchlistSession(config, offline_fixture_mode=True)
    now = iter((clocks(1), clocks(2)))
    adapter = Adapter(messages)
    report = session.run(adapter, fixture_clock=lambda: next(now))
    assert report["stop_reason"] == "budget_exhausted"
    assert len(adapter.sent) == 2 and report["go_ack_receipts"] == 2
    assert report["rounds"][0]["reads"][0]["status"] == "unavailable"
    assert report["rounds"][1]["outcome"] == "sample"
    assert report["connection_attempt_events"] == 2


@pytest.mark.parametrize("mutation", ["missing", "attempt", "endpoint", "segment"])
def test_binding_must_match_pending_connection_attempt(mutation):
    messages = stream_prefix()
    if mutation == "missing":
        messages.pop(2)
    else:
        body = binding(batch())
        if mutation == "attempt":
            body["connection"]["attempts"] = 2
        elif mutation == "endpoint":
            body["endpoint"] = "mock-b:7709"
        else:
            body["segment"] = 1
        messages[-1] = ("C", wire(body))
    session = RealPeriodicWatchlistSession(CONFIG, offline_fixture_mode=True)
    report = session.run(Adapter(messages))
    assert report["stop_reason"] == "connection_attempt_mismatch"
    assert not report["connections"] and not report["rounds"]


@pytest.mark.parametrize(
    "cls",
    ["quote_attempt", "sdk_getquote_entered", "quote_failed", "protocol_write_attempt"],
)
def test_quote_event_cannot_claim_other_segment(cls):
    messages = stream_prefix()
    if cls != "quote_attempt":
        messages += [("E", event("quote_attempt", round=1))]
    messages += [("E", event(cls, round=1, segment=1))]
    report = RealPeriodicWatchlistSession(CONFIG, offline_fixture_mode=True).run(
        Adapter(messages)
    )
    assert report["stop_reason"] in {"quote_attempt_budget", "quote_event_mismatch"}
    assert not report["rounds"]


@pytest.mark.parametrize("before_batch", [False, True])
def test_next_quote_attempt_requires_current_batch_and_go_ack(before_batch):
    messages = stream_prefix() + quote_events(1)
    if not before_batch:
        messages += [("B", wire(batch()))]
    messages += [("E", event("quote_attempt", round=2))]
    adapter = Adapter(messages)
    report = RealPeriodicWatchlistSession(CONFIG, offline_fixture_mode=True).run(
        adapter, fixture_clock=lambda: clocks(1)
    )
    assert report["stop_reason"] == "quote_attempt_budget"
    assert len(adapter.sent) == (0 if before_batch else 1)


@pytest.mark.parametrize(
    "cls,reason,ack",
    [
        ("stopped", "budget_exhausted", True),
        ("complete", "duration_exhausted", True),
        ("complete", "budget_exhausted", False),
    ],
)
def test_contradictory_terminal_is_rejected(cls, reason, ack):
    config = replace(CONFIG, rounds=1)
    body = batch()
    body["max_rounds"] = 1
    messages = stream_prefix(body) + quote_events(1) + [("B", wire(body))]
    if ack:
        messages += [("E", event("ack_received", round=1))]
    messages += [("E", event(cls, reason, round=1))]
    report = RealPeriodicWatchlistSession(config, offline_fixture_mode=True).run(
        Adapter(messages), fixture_clock=lambda: clocks(1)
    )
    assert report["stop_reason"] == "terminal_state_mismatch"


def test_sdk_not_counted_when_quote_preflight_fails():
    config = replace(CONFIG, rounds=1)
    body = batch(failure="disconnected")
    body["max_rounds"] = 1
    messages = stream_prefix(body) + [
        ("E", event("quote_attempt", round=1)),
        ("E", event("quote_failed", "disconnected", round=1)),
        ("B", wire(body)),
        ("E", event("ack_received", round=1)),
        ("E", event("complete", "budget_exhausted", round=1)),
    ]
    report = RealPeriodicWatchlistSession(config, offline_fixture_mode=True).run(
        Adapter(messages), fixture_clock=lambda: clocks(1)
    )
    assert report["stop_reason"] == "budget_exhausted"
    assert report["quote_sampling_attempts"] == 1
    assert report["observed_sdk_getquote_entries"] == 0
    assert report["observed_quote_protocol_write_attempts"] == 0
    assert "actual_getquote_attempts" not in report


@pytest.mark.parametrize(
    "outcome,written",
    [
        ("protocol_write_partial", 10),
        ("protocol_write_failed", 0),
        ("protocol_write_failed", 29),
    ],
)
def test_write_attempt_and_result_are_separate_facts(outcome, written):
    config = replace(CONFIG, rounds=1)
    body = batch(failure="source_error")
    body["max_rounds"] = 1
    messages = (
        stream_prefix(body)
        + quote_events(1)[:3]
        + [
            ("E", event(outcome, round=1, requested_bytes=29, written_bytes=written)),
            ("E", event("quote_failed", "source_error", round=1)),
            ("B", wire(body)),
            ("E", event("ack_received", round=1)),
            ("E", event("complete", "budget_exhausted", round=1)),
        ]
    )
    report = RealPeriodicWatchlistSession(config, offline_fixture_mode=True).run(
        Adapter(messages), fixture_clock=lambda: clocks(1)
    )
    assert report["stop_reason"] == "budget_exhausted"
    assert report["observed_sdk_getquote_entries"] == 1
    assert report["observed_quote_protocol_write_attempts"] == 1
    assert report["observed_quote_protocol_write_results"][outcome] == 1
    assert (
        report["observed_quote_protocol_write_results"]["protocol_write_complete"] == 0
    )
