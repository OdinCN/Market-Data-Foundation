"""Invented Go decoded envelopes only; never load SDK or retained market data."""

import builtins
import importlib
import io
import json
import math
import os
import socket
import subprocess
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from market_data_foundation.candidates import (
    go_tdx_watchlist_source_adapter as module,
)
from market_data_foundation.candidates import (
    periodic_watchlist as periodic,
)
from market_data_foundation.canonical.model import ContractError

BASE = datetime(2026, 3, 1, tzinfo=UTC)


class Time:
    def __init__(self):
        self.seconds = 0.0
        self.events = []

    def monotonic(self):
        return 100.0 + self.seconds

    def clock(self):
        return BASE + timedelta(seconds=self.seconds)

    def sleep(self, delay):
        self.events.append(("sleep",))
        self.seconds += delay


def envelope(request, time, *, maximum=3):
    """All numbers/clocks invented here; declarations do not execute a source."""
    requested = time.clock()
    time.seconds += 0.25
    received = time.clock()
    # Rounds 1/2 duplicate economically; round 3 changes both invented prices.
    price = 23456 + (request.round == 3)
    rows = [
        {
            "Exchange": exchange,
            "Code": code,
            "Kline": {
                **dict.fromkeys(("Last", "Open", "High", "Low", "Close"), price),
                "Amount": 987654,
                "Volume": 23,
                "Time": requested.isoformat(),
            },
            "BuyLevel": [
                {"Buy": True, "Price": price - i, "Number": i + 2} for i in range(1, 6)
            ],
            "SellLevel": [
                {"Buy": False, "Price": price + i, "Number": i + 3} for i in range(1, 6)
            ],
            "ServerTime": f"invented-opaque-{request.round}",
            "Active1": request.round,
            "Active2": 4,
        }
        for exchange, code in ((1, "600519"), (0, "000001"))
    ]
    time.seconds += 0.25
    return {
        "schema": "mdf.candidate.go-stock.real-observation.r1",
        "input_origin": "author_created_offline_fixture",
        "clock_provenance": "author_created_offline_fixture",
        "sdk_release": module.SDK_RELEASE,
        "endpoint": "139.9.52.158:7709",
        "securities": ["sh600519", "sz000001"],
        "batch_id": f"go-real-r1-{request.round}",
        "round": request.round,
        "max_rounds": maximum,
        # Required legacy ingress fixture metadata, NOT an actual TCP attempt.
        "connection": {
            "requested": (BASE - timedelta(seconds=2)).isoformat(),
            "received": (BASE - timedelta(seconds=1)).isoformat(),
            "elapsed_ns": 100,
            "redial": False,
            "attempts": 1,
        },
        "clocks": {
            "requested": requested.isoformat(),
            "received": received.isoformat(),
            "frozen": time.clock().isoformat(),
        },
        "quote_elapsed_ns": 250_000_000,
        "source_event_clock": "UNKNOWN",
        "kline_time_semantics": "SDK_CLIENT_SAMPLED",
        "server_time_semantics": "OPAQUE",
        "units": ["integer_li", "sdk_volume_count_UNKNOWN", "sdk_level_count_UNKNOWN"],
        "failure": None,
        "rows": rows,
    }


class FixturePort:
    """Explicit trusted caller injection, no default port in product code."""

    def __init__(self, time, *, maximum=3, mutate=None):
        self.time = time
        self.maximum = maximum
        self.mutate = mutate
        self.identity = module.FIXTURE_IDENTITY
        self.batches = []

    def open(self, *, deadline_monotonic):
        assert self.time.monotonic() <= deadline_monotonic
        self.time.events.append(("fixture_open",))

    def sample(self, request):
        self.time.events.append(("fixture_sample", request.round))
        body = envelope(request, self.time, maximum=self.maximum)
        if self.mutate:
            self.mutate(body, request.round)
        batch = module.FrozenBatch(
            self.identity, request.round, json.dumps(body, allow_nan=False).encode()
        )
        self.batches.append(batch)
        self.time.events.append(("frozen", request.round))
        return batch

    def close(self):
        self.time.events.append(("fixture_close",))


def session(time, **kwargs):
    return periodic.PeriodicWatchlistSession(
        monotonic=time.monotonic, sleep=time.sleep, clock=time.clock, **kwargs
    )


def adapter(port):
    return module.GoTdxWatchlistSourceAdapter(port, monotonic=port.time.monotonic)


def assert_offline(report):
    assert report.provider_state == "NOT_CONNECTED_TO_PROVIDER"
    assert report.provider_observation == "NOT_OBSERVED"
    assert report.actual_tcp_connects == report.actual_getquote_calls == 0
    assert report.go_ack == "DEFERRED_NOT_IMPLEMENTED"


def assert_single_use(target, time):
    before = list(time.events)
    with pytest.raises(ContractError, match="^adapter_already_used$"):
        target.run(session(time))
    with pytest.raises(ContractError, match="^adapter_not_running_or_reentrant$"):
        target.sample(periodic.SampleRequest(1, 100, 110))
    target.close()  # no close retry, including after close failure
    assert time.events == before
    assert_offline(target.report)


def test_port_adapter_same_ingress_pair_reads_match_direct_callback(monkeypatch):
    time = Time()
    port = FixturePort(time)
    target = adapter(port)
    assert time.events == []
    assert target.report.fixture_readiness == "NOT_READY"
    assert target.report.close_state == "NOT_CLOSED"
    assert_offline(target.report)
    instances = []
    original = periodic.GoStockRealObservationIngress

    class Ingress(original):
        def __init__(self, **kwargs):
            assert kwargs["offline_fixture_mode"] is True
            super().__init__(**kwargs)
            instances.append(self)

        def ingest(self, wire, **kwargs):
            n = len(self.history) + 1
            assert wire is port.batches[n - 1].wire  # no reserialization/relabel
            time.events.append(("ingest", n))
            return super().ingest(wire, **kwargs)

        def read(self, security, **kwargs):
            result = super().read(security, **kwargs)
            time.events.append(("read", security, kwargs["batch_id"]))
            return result

    with monkeypatch.context() as scoped:
        scoped.setattr(periodic, "GoStockRealObservationIngress", Ingress)
        result = target.run(session(time))
    assert len(instances) == 1 and len(instances[0].history) == 3
    expected = [("fixture_open",)]
    for n in range(1, 4):
        if n > 1:
            expected.append(("sleep",))
        expected.extend(
            [
                ("fixture_sample", n),
                ("frozen", n),
                ("ingest", n),
                ("read", "sh600519", f"go-real-r1-{n}"),
                ("read", "sz000001", f"go-real-r1-{n}"),
            ]
        )
    assert time.events == expected + [("fixture_close",)]
    # This proves local pair-Read completion before the next request, not Go ACK.
    direct_time = Time()
    direct_port = FixturePort(direct_time)
    direct = session(direct_time).run(lambda r: direct_port.sample(r).wire)
    assert result.session == direct
    assert result.session.stop_reason == result.port.termination == "budget_exhausted"
    assert [r.outcome for r in result.session.rounds] == [
        "sample",
        "duplicate",
        "sample",
    ]
    assert result.port.open_calls == result.port.close_calls == 1
    assert result.port.fixture_readiness == "FIXTURE_PORT_READY"
    assert result.port.close_state == "closed" and result.port.close_error is None
    for n, (call, batch, rr) in enumerate(
        zip(result.port.samples, port.batches, result.session.rounds, strict=True), 1
    ):
        assert call.round == n and call.callback_calls == 1
        assert call.returned_identity == batch.identity == module.FIXTURE_IDENTITY
        assert call.envelope_sha256 == rr.observation.envelope_sha256
        assert call.envelope_sha256 == sha256(batch.wire).hexdigest()
        assert call.classification == "frozen_batch_returned"
        assert rr.observation is instances[0].history[n - 1]
        assert rr.observation.input_origin == module.OFFLINE_ORIGIN
        assert rr.local_known_age == timedelta(0)
        for read in rr.reads:
            assert read.view.status == "observed"
            assert read.view.age == timedelta(seconds=0.25)
            assert read.view.source_event_at is None
            assert read.view.source_freshness == "UNKNOWN"
            assert read.view.rights == "UNKNOWN / EXCLUDED_FROM_PRODUCT_EFFECT"
    with pytest.raises(FrozenInstanceError):
        port.batches[0].identity.endpoint = "other"
    with pytest.raises(FrozenInstanceError):
        port.batches[0].wire = b"other"
    assert_single_use(target, time)


@pytest.mark.parametrize("maximum", [1, 2, 3])
def test_earlier_ingress_budget_never_calls_extra_round(maximum):
    time = Time()
    port = FixturePort(time, maximum=maximum)
    target = adapter(port)
    result = target.run(session(time))
    assert len(result.session.rounds) == len(result.port.samples) == maximum
    assert sum(c.callback_calls for c in result.port.samples) == maximum
    assert result.session.stop_reason == "budget_exhausted"
    assert_single_use(target, time)


@pytest.mark.parametrize(
    "change,classification",
    [
        ({"sdk_release": "injoyai/tdx v0.0.88"}, "source_identity_drift"),
        ({"endpoint": "127.0.0.1:7709"}, "source_identity_drift"),
        ({"securities": ("sh600519", "sz000002")}, "source_identity_drift"),
        ({"input_origin": module.REAL_ORIGIN}, "real_origin_rejected"),
        ({"clock_provenance": "local_probe_wall_clock_utc"}, "source_identity_drift"),
    ],
)
@pytest.mark.parametrize(
    "boundary", ["pre_open", "post_open", "pre_sample", "post_sample", "batch"]
)
def test_source_identity_drift_fails_closed(change, classification, boundary):
    time = Time()
    port = FixturePort(time)
    changed = replace(module.FIXTURE_IDENTITY, **change)
    original_open, original_sample = port.open, port.sample
    if boundary == "pre_open":
        port.identity = changed
    elif boundary == "post_open":

        def open(**kwargs):
            original_open(**kwargs)
            port.identity = changed

        port.open = open
    elif boundary == "pre_sample":
        sleep_original = time.sleep

        # Keep the original bound method to avoid recursively calling this hook.
        def sleep(delay):
            sleep_original(delay)
            port.identity = changed

        time.sleep = sleep
    else:

        def sample(request):
            batch = original_sample(request)
            if boundary == "post_sample":
                port.identity = changed
                return batch
            return replace(batch, identity=changed)

        port.sample = sample
    target = adapter(port)
    result = target.run(session(time))
    assert result.port.termination == classification
    assert result.port.samples[-1].classification == classification
    assert result.port.samples[-1].returned_identity is None
    assert result.session.rounds[-1].observation is None
    assert len(port.batches) == (boundary in ("pre_sample", "post_sample", "batch"))
    assert len(result.session.rounds) == (2 if boundary == "pre_sample" else 1)
    assert result.port.close_calls == 1
    assert_single_use(target, time)


@pytest.mark.parametrize(
    "kind,reason,classification",
    [
        ("real_wire", "ingress_rejected", None),
        ("real_clock", "ingress_rejected", None),
        ("sdk_wire", "ingress_rejected", None),
        ("endpoint_wire", "ingress_rejected", None),
        ("wrong_request_member", "ingress_rejected", None),
        ("wrong_row_member", "observation_failed", "security_out_of_scope"),
        ("timeout", "observation_failed", "timeout"),
        ("disconnected", "observation_failed", "disconnected"),
        ("source_error", "observation_failed", "source_error"),
    ],
)
def test_existing_ingress_enforces_wire_identity_and_failure_semantics(
    kind, reason, classification
):
    time = Time()

    def mutate(body, n):
        if n != 2:
            return
        if kind == "real_wire":
            body.update(
                input_origin="real_go_sdk_decoded_quote",
                clock_provenance="local_probe_wall_clock_utc",
            )
        elif kind == "real_clock":
            body["clock_provenance"] = "local_probe_wall_clock_utc"
        elif kind == "sdk_wire":
            body["sdk_release"] = "injoyai/tdx v0.0.88"
        elif kind == "endpoint_wire":
            body["endpoint"] = "127.0.0.1:7709"
        elif kind == "wrong_request_member":
            body["securities"][1] = "sz000002"
        elif kind == "wrong_row_member":
            body["rows"][1]["Code"] = "000002"
        else:
            body.update(failure=kind, rows=None)

    port = FixturePort(time, mutate=mutate)
    target = adapter(port)
    result = target.run(session(time))
    assert result.session.stop_reason == result.port.termination == reason
    assert len(result.session.rounds) == len(port.batches) == 2
    last = result.session.rounds[-1]
    if classification is None:
        assert last.observation is None and last.reads == ()
    else:
        assert last.observation.classification == classification
        assert len(last.reads) == 2
        assert all(r.view.status == "unavailable" for r in last.reads)
        assert all(r.view.state.acquisition == "sample-1" for r in last.reads)
    if kind == "real_wire":
        assert json.loads(port.batches[-1].wire)["input_origin"] == module.REAL_ORIGIN
    assert_single_use(target, time)


@pytest.mark.parametrize("stage", ["open", "sample"])
@pytest.mark.parametrize(
    "error,classification,session_reason",
    [
        (TimeoutError, "fixture_timeout", "sample_timeout"),
        (ConnectionError, "fixture_disconnected", "sample_error"),
        (RuntimeError, "fixture_port_error", "sample_error"),
    ],
)
def test_port_errors_redacted_terminal_no_retry(
    stage, error, classification, session_reason
):
    time = Time()
    port = FixturePort(time)

    def fail(*args, **kwargs):
        time.events.append(("failing_hook",))
        raise error("private exception payload must not escape")

    setattr(port, stage, fail)
    target = adapter(port)
    result = target.run(session(time))
    assert result.port.termination == classification
    assert result.session.stop_reason == session_reason
    assert result.port.samples[0].callback_calls == (stage == "sample")
    assert [e for e in time.events if e[0] == "failing_hook"] == [("failing_hook",)]
    assert "private exception" not in repr(result)
    assert_single_use(target, time)


@pytest.mark.parametrize("stage", ["before_open", "open", "sample"])
def test_expiry_discards_bytes_and_stops_without_ingress_or_retry(stage):
    time = Time()
    port = FixturePort(time)
    original_open, original_sample = port.open, port.sample
    target = adapter(port)
    if stage == "before_open":
        # The session saw t=100, but the adapter's first deadline check sees t=111.
        def monotonic():
            time.seconds = 11
            return time.monotonic()

        target = module.GoTdxWatchlistSourceAdapter(port, monotonic=monotonic)
    elif stage == "open":

        def open(**kwargs):
            original_open(**kwargs)
            time.seconds += 11

        port.open = open
    else:

        def sample(request):
            batch = original_sample(request)
            time.seconds += 11
            return batch

        port.sample = sample
    result = target.run(session(time))
    assert result.port.termination == "deadline_expired"
    assert result.port.samples[0].callback_calls == (stage == "sample")
    assert result.session.rounds[0].observation is None
    assert result.port.open_calls == (stage != "before_open")
    assert_single_use(target, time)


def test_deadline_equality_allows_immediate_handoff_without_extra_budget():
    time = Time()
    port = FixturePort(time, maximum=1)
    original = port.sample

    def sample(request):
        batch = original(request)
        time.seconds = 10.0
        return batch

    port.sample = sample
    result = adapter(port).run(session(time))
    assert result.session.stop_reason == "budget_exhausted"
    assert result.session.rounds[0].observation is not None
    assert result.port.samples[0].callback_calls == 1
    assert_offline(result.port)


@pytest.mark.parametrize("stage", ["open", "sample"])
def test_explicit_close_during_hook_prevents_any_further_sampling_or_handoff(stage):
    time = Time()
    port = FixturePort(time)
    original = getattr(port, stage)
    target = adapter(port)

    def hook(*args, **kwargs):
        result = original(*args, **kwargs)
        target.close()
        return result

    setattr(port, stage, hook)
    result = target.run(session(time))
    assert result.port.termination == "adapter_closed"
    assert result.port.samples[0].callback_calls == (stage == "sample")
    assert result.session.rounds[0].observation is None
    assert result.port.close_calls == 1
    assert_single_use(target, time)


@pytest.mark.parametrize("error", [TimeoutError, ConnectionError, RuntimeError])
@pytest.mark.parametrize("fail_sample", [False, True])
def test_close_failure_is_separate_reported_once_without_expanding_budget(
    error, fail_sample
):
    time = Time()
    port = FixturePort(time, maximum=1)

    def close():
        time.events.append(("failing_close",))
        raise error("private close message")

    port.close = close
    if fail_sample:
        port.sample = lambda r: (_ for _ in ()).throw(TimeoutError("private"))
    target = adapter(port)
    result = target.run(session(time))
    assert result.port.close_state == "close_failed"
    assert (
        result.port.close_error
        == {
            TimeoutError: "close_timeout",
            ConnectionError: "close_disconnected",
            RuntimeError: "close_error",
        }[error]
    )
    assert result.port.termination == (
        "fixture_timeout" if fail_sample else "budget_exhausted"
    )
    assert len(result.port.samples) == result.port.samples[0].callback_calls == 1
    assert "private" not in repr(result)
    assert_single_use(target, time)


@pytest.mark.parametrize("stage", ["open", "sample", "sleep", "close"])
def test_process_control_abort_still_closes_and_preserves_safe_report(stage):
    time = Time()
    port = FixturePort(time)

    def abort(*args, **kwargs):
        time.events.append(("abort", stage))
        raise KeyboardInterrupt("private cancellation")

    if stage == "sleep":
        time.sleep = abort
    else:
        setattr(port, stage, abort)
    target = adapter(port)
    with pytest.raises(KeyboardInterrupt):
        target.run(session(time))
    assert target.report.close_calls == 1
    assert target.report.close_state == (
        "close_failed" if stage == "close" else "closed"
    )
    assert "private" not in repr(target.report)
    assert_single_use(target, time)


@pytest.mark.parametrize(
    "returned,classification",
    [
        (None, "frozen_batch_required"),
        (b"{}", "frozen_batch_required"),
        (module.FrozenBatch(module.FIXTURE_IDENTITY, 2, b"{}"), "batch_round_mismatch"),
        (
            module.FrozenBatch(module.FIXTURE_IDENTITY, 1, bytearray(b"{}")),
            "frozen_bytes_bound",
        ),
        (module.FrozenBatch(module.FIXTURE_IDENTITY, 1, b""), "frozen_bytes_bound"),
        (
            module.FrozenBatch(module.FIXTURE_IDENTITY, 1, b"x" * 32769),
            "frozen_bytes_bound",
        ),
    ],
)
def test_invalid_frozen_return_never_reaches_ingress(returned, classification):
    time = Time()
    port = FixturePort(time)
    port.sample = lambda r: returned
    target = adapter(port)
    result = target.run(session(time))
    assert result.port.termination == classification
    assert result.port.samples[0].callback_calls == 1
    assert result.session.rounds[0].observation is None
    assert_single_use(target, time)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -1, True, "100"])
def test_invalid_adapter_clock_stops_before_port_open(bad):
    time = Time()
    port = FixturePort(time)
    target = module.GoTdxWatchlistSourceAdapter(port, monotonic=lambda: bad)
    result = target.run(session(time))
    assert result.port.termination == "monotonic_error"
    assert result.port.open_calls == result.port.samples[0].callback_calls == 0
    assert_single_use(target, time)


def test_stale_pair_stops_without_adapter_recovery():
    time = Time()
    port = FixturePort(time)
    target = adapter(port)
    result = target.run(session(time, ttl=timedelta(seconds=0.1)))
    assert result.session.stop_reason == "read_not_observed"
    assert all(r.view.status == "stale" for r in result.session.rounds[0].reads)
    assert len(result.port.samples) == 1
    assert_single_use(target, time)


def test_no_sdk_import_network_process_or_data_file_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected source or file side effect")

    original_import = builtins.__import__
    imports = []

    def checked_import(name, *args, **kwargs):
        imports.append(name)
        if "tdx" in name.lower() and "go_tdx_watchlist_source_adapter" not in name:
            forbidden()
        return original_import(name, *args, **kwargs)

    # Module source loading is interpreter work; explicit data I/O is forbidden.
    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, "__import__", checked_import)
        for obj, names in [
            (socket, ("socket", "create_connection", "getaddrinfo")),
            (subprocess, ("Popen", "run")),
            (os, ("system", "open", "posix_spawn", "fork", "execve")),
            (builtins, ("open",)),
            (io, ("open",)),
            (Path, ("open", "read_bytes", "read_text", "write_bytes", "write_text")),
        ]:
            for name in names:
                scoped.setattr(obj, name, forbidden)
        importlib.reload(module)
        time = Time()
        port = FixturePort(time)
        result = adapter(port).run(session(time))
        assert len(result.session.rounds) == 3
        assert_offline(result.port)
    assert imports  # import guard was exercised
