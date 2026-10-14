"""Orchestration contracts using invented envelopes, clocks and prices only."""

import importlib
import json
import math
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from market_data_foundation.candidates import periodic_watchlist as module
from market_data_foundation.canonical.model import ContractError

BASE = datetime(2026, 2, 1, tzinfo=UTC)
MEMBERS = ("sh600519", "sz000001")


class Time:
    def __init__(self):
        self.seconds = 0.0
        self.events = []

    @property
    def at(self):
        return BASE + timedelta(seconds=self.seconds)

    def monotonic(self):
        return 100.0 + self.seconds

    def sleep(self, delay):
        self.events.append(("sleep", delay))
        self.seconds += delay

    def clock(self):
        return self.at


def envelope(request, time, *, price=17000, duration=0.5, maximum=3):
    """Author-created decoded shape; no retained source material is read."""
    requested = time.at
    time.seconds += duration / 2
    received = time.at
    rows = [
        {
            "Exchange": exchange,
            "Code": code,
            "Kline": {
                **dict.fromkeys(("Last", "Open", "High", "Low", "Close"), price),
                "Amount": 123456,
                "Volume": 17,
                "Time": BASE.isoformat(),
            },
            "BuyLevel": [
                {"Buy": True, "Price": price - i, "Number": i} for i in range(1, 6)
            ],
            "SellLevel": [
                {"Buy": False, "Price": price + i, "Number": i} for i in range(1, 6)
            ],
            "ServerTime": "invented-opaque",
            "Active1": 1,
            "Active2": 2,
        }
        for exchange, code in ((1, "600519"), (0, "000001"))
    ]
    time.seconds += duration / 2
    return {
        "schema": "mdf.candidate.go-stock.real-observation.r1",
        "input_origin": "author_created_offline_fixture",
        "clock_provenance": "author_created_offline_fixture",
        "sdk_release": "injoyai/tdx v0.0.87@51c1ed51020fa43727300339dd92cbecf06a7d2e",
        "endpoint": "139.9.52.158:7709",
        "securities": list(MEMBERS),
        "batch_id": f"go-real-r1-{request.round}",
        "round": request.round,
        "max_rounds": maximum,
        "connection": {
            "requested": (BASE - timedelta(seconds=2)).isoformat(),
            "received": (BASE - timedelta(seconds=1)).isoformat(),
            "elapsed_ns": 1_000_000_000,
            "redial": False,
            "attempts": 1,
        },
        "clocks": {
            "requested": requested.isoformat(),
            "received": received.isoformat(),
            "frozen": time.at.isoformat(),
        },
        "quote_elapsed_ns": int(duration * 1_000_000_000),
        "source_event_clock": "UNKNOWN",
        "kline_time_semantics": "SDK_CLIENT_SAMPLED",
        "server_time_semantics": "OPAQUE",
        "units": ["integer_li", "sdk_volume_count_UNKNOWN", "sdk_level_count_UNKNOWN"],
        "failure": None,
        "rows": rows,
    }


def wire(body):
    return json.dumps(body, allow_nan=False).encode()


def session(time, **overrides):
    return module.PeriodicWatchlistSession(
        **{
            "monotonic": time.monotonic,
            "sleep": time.sleep,
            "clock": time.clock,
            **overrides,
        }
    )


def sampler(time, *, maximum=3, mutate=None, price=lambda n: 17000 + n):
    def sample(request):
        time.events.append(("sample", request.round))
        body = envelope(request, time, maximum=maximum, price=price(request.round))
        if mutate is not None:
            mutate(body, request.round)
        data = wire(body)
        time.events.append(("frozen", request.round))
        return data

    return sample


def instrument(monkeypatch, time):
    instances = []
    original = module.GoStockRealObservationIngress

    class Ingress(original):
        def __init__(self, **kwargs):
            assert kwargs["offline_fixture_mode"] is True
            assert kwargs["capacity"] == 3
            super().__init__(**kwargs)
            instances.append(self)

        def ingest(self, data, **kwargs):
            time.events.append(("ingest", data))
            return super().ingest(data, **kwargs)

        def read(self, security, **kwargs):
            time.events.append(("read", security, kwargs["batch_id"]))
            return super().read(security, **kwargs)

    monkeypatch.setattr(module, "GoStockRealObservationIngress", Ingress)
    return instances


def assert_consumed(target, time):
    before = list(time.events)
    with pytest.raises(ContractError, match="^session_already_used$"):
        target.run(sampler(time))
    assert time.events == before


def test_three_rounds_are_immediate_serial_frozen_ingress_then_exact_pair(monkeypatch):
    time = Time()
    instances = instrument(monkeypatch, time)

    def clock():
        time.seconds += 0.125
        return time.at

    target = session(time, clock=clock)
    assert time.events == [] and len(instances) == 1
    report = target.run(sampler(time))
    assert report.stop_reason == "budget_exhausted"
    assert len(instances) == 1 and len(instances[0].history) == 3
    assert [r.started_monotonic for r in report.rounds] == [100, 112, 124]
    assert [r.request.deadline_monotonic for r in report.rounds] == [110, 122, 134]
    assert [r.outcome for r in report.rounds] == ["sample"] * 3
    assert [r.stop_reason for r in report.rounds] == [None, None, "budget_exhausted"]
    assert [e[0] for e in time.events] == [
        "sample",
        "frozen",
        "ingest",
        "read",
        "read",
        "sleep",
        "sample",
        "frozen",
        "ingest",
        "read",
        "read",
        "sleep",
        "sample",
        "frozen",
        "ingest",
        "read",
        "read",
    ]
    frozen = [e[1] for e in time.events if e[0] == "ingest"]
    for n, (result, data) in enumerate(zip(report.rounds, frozen, strict=True), 1):
        obs = result.observation
        assert obs is instances[0].history[n - 1]
        assert result.request.securities == MEMBERS and result.request.max_rounds == 3
        assert obs.batch_id == f"go-real-r1-{n}"
        assert obs.record.acquisition == f"sample-{n}"
        assert obs.envelope_sha256 == sha256(data).hexdigest()
        assert (
            obs.input_origin == obs.known_provenance == "author_created_offline_fixture"
        )
        assert result.local_known_age == timedelta(seconds=0.125)
        assert result.read_at == obs.record.clocks.known + result.local_known_age
        assert tuple(r.security for r in result.reads) == MEMBERS
        for item in result.reads:
            assert item.view.status == "observed"
            assert item.view.age == timedelta(seconds=0.5)
            assert item.view.state.acquisition == obs.record.acquisition
            assert item.view.source_event_at is None
            assert item.view.source_freshness == "UNKNOWN"
            assert item.view.rights == "UNKNOWN / EXCLUDED_FROM_PRODUCT_EFFECT"
    with pytest.raises(FrozenInstanceError):
        report.rounds[0].request.round = 4
    assert_consumed(target, time)


@pytest.mark.parametrize("maximum", [1, 2, 3])
def test_earlier_ingress_cap_stops_before_another_sample(maximum):
    time = Time()
    target = session(time)
    report = target.run(sampler(time, maximum=maximum, price=lambda n: 17000))
    assert report.stop_reason == "budget_exhausted"
    assert len(report.rounds) == maximum
    assert [r.outcome for r in report.rounds] == ["sample"] + ["duplicate"] * (
        maximum - 1
    )
    assert len([e for e in time.events if e[0] == "sleep"]) == maximum - 1
    assert all(
        r.reads[0].view.state.acquisition == f"sample-{i}"
        for i, r in enumerate(report.rounds, 1)
    )
    assert_consumed(target, time)


@pytest.mark.parametrize(
    "kind,outcome,reason",
    [
        ("timeout", "failed", "observation_failed"),
        ("incomplete_pair", "failed", "observation_failed"),
        ("late", "late", "observation_late"),
        ("equal", "order_uncertain", "observation_order_uncertain"),
    ],
)
def test_ingested_failure_or_ignored_order_is_read_then_terminal(kind, outcome, reason):
    time = Time()

    def mutate(body, n):
        if n != 2:
            return
        if kind == "timeout":
            body.update(failure="timeout", rows=None)
        elif kind == "incomplete_pair":
            body["rows"].pop()
        else:
            body["clocks"]["requested"] = (
                BASE - timedelta(seconds=0.5 if kind == "late" else 0)
            ).isoformat()

    target = session(time)
    report = target.run(sampler(time, mutate=mutate))
    assert report.stop_reason == reason and len(report.rounds) == 2
    first, last = report.rounds
    assert last.outcome == outcome and len(last.reads) == 2
    assert all(
        r.view.state.acquisition == first.observation.record.acquisition
        for r in last.reads
    )
    if outcome == "failed":
        assert all(r.view.status == "unavailable" for r in last.reads)
        assert last.observation.classification in ("timeout", "two_members_required")
    assert_consumed(target, time)


def test_stale_read_is_reported_and_stops_even_after_valid_ingestion():
    time = Time()
    report = session(time, ttl=timedelta(seconds=0.1)).run(sampler(time))
    assert report.stop_reason == "read_not_observed" and len(report.rounds) == 1
    assert report.rounds[0].outcome == "sample"
    assert all(r.view.status == "stale" for r in report.rounds[0].reads)


@pytest.mark.parametrize("returned", [None, "{}", {}, bytearray(b"{}"), iter(())])
def test_incomplete_callback_cannot_reach_ingress(monkeypatch, returned):
    time = Time()
    instances = instrument(monkeypatch, time)
    target = session(time)
    report = target.run(lambda request: returned)
    assert report.stop_reason == "frozen_bytes_required"
    assert report.rounds[0].observation is None and report.rounds[0].reads == ()
    assert instances[0].history == () and time.events == []
    assert_consumed(target, time)


@pytest.mark.parametrize("kind", ["round", "member", "real_origin", "malformed", "cap"])
def test_invalid_batch_is_terminal_without_a_replacement_attempt(monkeypatch, kind):
    time = Time()
    instances = instrument(monkeypatch, time)

    def sample(request):
        body = envelope(request, time)
        if kind == "round":
            body.update(round=2, batch_id="go-real-r1-2")
        elif kind == "member":
            body["securities"][1] = "sz000002"
        elif kind == "real_origin":
            body.update(
                input_origin="real_go_sdk_decoded_quote",
                clock_provenance="local_probe_wall_clock_utc",
            )
        elif kind == "cap":
            body["max_rounds"] = 4
        return b"{" if kind == "malformed" else wire(body)

    target = session(time)
    report = target.run(sample)
    assert report.stop_reason == "ingress_rejected" and len(report.rounds) == 1
    assert report.rounds[0].outcome == "rejected"
    assert instances[0].history == ()
    assert [e[0] for e in time.events] == ["ingest"]
    assert_consumed(target, time)


@pytest.mark.parametrize(
    "exception,reason",
    [
        (TimeoutError, "sample_timeout"),
        (RuntimeError, "sample_error"),
        (ContractError, "sample_error"),
    ],
)
def test_callback_exceptions_are_fixed_classifications_without_messages(
    exception, reason
):
    time = Time()

    def sample(request):
        raise exception("private callback exception text")

    target = session(time)
    report = target.run(sample)
    assert report.stop_reason == reason and len(report.rounds) == 1
    assert "private" not in repr(report)
    assert time.events == []
    assert_consumed(target, time)


@pytest.mark.parametrize(
    "duration,reason,ingested",
    [
        (10.0, "budget_exhausted", True),
        (10.000001, "round_timeout", False),
    ],
)
def test_callback_deadline_is_checked_before_ingress(
    monkeypatch, duration, reason, ingested
):
    time = Time()
    instances = instrument(monkeypatch, time)
    target = session(time)
    report = target.run(lambda r: wire(envelope(r, time, duration=duration, maximum=1)))
    assert report.stop_reason == reason and len(report.rounds) == 1
    assert bool(instances[0].history) is ingested
    assert bool(report.rounds[0].reads) is ingested
    assert_consumed(target, time)


def test_read_overrun_keeps_accepted_observation_but_never_starts_next_round(
    monkeypatch,
):
    time = Time()
    original = module.GoStockRealObservationIngress.read

    def read(self, security, **kwargs):
        time.seconds += 6
        return original(self, security, **kwargs)

    monkeypatch.setattr(module.GoStockRealObservationIngress, "read", read)
    report = session(time).run(sampler(time))
    assert report.stop_reason == "round_timeout" and len(report.rounds) == 1
    assert report.rounds[0].observation is not None
    assert len(report.rounds[0].reads) == 2
    assert not any(e[0] == "sleep" for e in time.events)


def test_partial_read_failure_does_not_publish_a_partial_pair(monkeypatch):
    time = Time()
    original = module.GoStockRealObservationIngress.read

    def read(self, security, **kwargs):
        if security == MEMBERS[1]:
            raise RuntimeError("private read exception text")
        return original(self, security, **kwargs)

    monkeypatch.setattr(module.GoStockRealObservationIngress, "read", read)
    report = session(time).run(sampler(time))
    assert report.stop_reason == "read_error" and len(report.rounds) == 1
    assert report.rounds[0].observation is not None and report.rounds[0].reads == ()
    assert "private" not in repr(report)


@pytest.mark.parametrize("offset", [math.ulp(112.0), 0.000001, 0.001, 0.5])
def test_bounded_late_start_keeps_fixed_slots_deadlines_and_serial_reads(
    monkeypatch, offset
):
    time = Time()
    instances = instrument(monkeypatch, time)

    def sleep(delay):
        time.events.append(("sleep", delay))
        time.seconds += delay + offset

    target = session(time, sleep=sleep)
    starts = []
    sample_fixture = sampler(time)

    def sample(request):
        starts.append(time.monotonic())
        return sample_fixture(request)

    report = target.run(sample)
    assert report.stop_reason == "budget_exhausted" and len(report.rounds) == 3
    assert [r.request.scheduled_monotonic for r in report.rounds] == [100, 112, 124]
    assert [r.request.deadline_monotonic for r in report.rounds] == [110, 122, 134]
    assert [r.started_monotonic for r in report.rounds] == starts
    assert starts == pytest.approx([100, 112 + offset, 124 + offset], rel=0, abs=1e-12)
    assert 112 < starts[1] <= 112.5 and 124 < starts[2] <= 124.5
    assert len(instances[0].history) == 3
    assert [e[0] for e in time.events] == (
        ["sample", "frozen", "ingest", "read", "read"]
        + ["sleep", "sample", "frozen", "ingest", "read", "read"] * 2
    )
    assert all(
        tuple(r.security for r in result.reads) == MEMBERS for result in report.rounds
    )
    assert_consumed(target, time)


@pytest.mark.parametrize("offset", [0.500001, 12.0])
def test_excess_lateness_cancels_slot_without_catch_up(offset):
    time = Time()

    def sleep(delay):
        time.events.append(("sleep", delay))
        time.seconds += delay + offset

    target = session(time, sleep=sleep)
    report = target.run(sampler(time))
    assert report.stop_reason == "cadence_missed" and len(report.rounds) == 2
    assert report.rounds[-1].outcome == "not_started"
    assert report.rounds[-1].started_monotonic is None
    assert len([e for e in time.events if e[0] == "sample"]) == 1
    assert_consumed(target, time)


@pytest.mark.parametrize("early", [0.000001, 0.001])
def test_early_wake_gets_one_remaining_wait_without_sampling_early(early):
    time = Time()
    waits = []

    def sleep(delay):
        waits.append(delay)
        time.seconds += delay - early if len(waits) == 1 else delay

    target = session(time, sleep=sleep)
    report = target.run(sampler(time))
    assert report.stop_reason == "budget_exhausted" and len(report.rounds) == 3
    assert waits == pytest.approx([11.5, early, 11.5])
    assert [r.started_monotonic for r in report.rounds] == [100, 112, 124]
    assert [e for e in time.events if e[0] == "sample"] == [
        ("sample", 1),
        ("sample", 2),
        ("sample", 3),
    ]
    assert_consumed(target, time)


@pytest.mark.parametrize("progress", [False, True])
def test_unreachable_slot_stops_after_two_waits_without_more_sampling(progress):
    time = Time()
    waits = []

    def sleep(delay):
        waits.append(delay)
        if progress:
            time.seconds += delay / 2

    target = session(time, sleep=sleep)
    report = target.run(sampler(time))
    assert waits == [11.5, 5.75 if progress else 11.5]
    assert report.stop_reason == "cadence_missed" and len(report.rounds) == 2
    assert report.rounds[-1].request.scheduled_monotonic == 112
    assert report.rounds[-1].started_monotonic is None
    assert report.rounds[-1].observation is None and report.rounds[-1].reads == ()
    assert report.rounds[-1].outcome == "not_started"
    assert [e for e in time.events if e[0] == "sample"] == [("sample", 1)]
    assert_consumed(target, time)


@pytest.mark.parametrize(
    "duration,reason,ingested",
    [(9.5, "budget_exhausted", True), (9.500001, "round_timeout", False)],
)
def test_lateness_consumes_target_based_timeout(
    monkeypatch, duration, reason, ingested
):
    time = Time()
    instances = instrument(monkeypatch, time)

    def sleep(delay):
        time.seconds += delay + 0.5

    def sample(request):
        time.events.append(("sample", request.round))
        return wire(
            envelope(
                request,
                time,
                duration=0.5 if request.round == 1 else duration,
                maximum=2,
            )
        )

    target = session(time, sleep=sleep)
    report = target.run(sample)
    assert report.stop_reason == reason and len(report.rounds) == 2
    last = report.rounds[-1]
    assert last.request.scheduled_monotonic == 112
    assert last.started_monotonic == 112.5
    assert last.request.deadline_monotonic == 122
    assert len(instances[0].history) == 1 + ingested
    assert bool(last.reads) is ingested
    assert_consumed(target, time)


def test_sleep_exception_is_terminal_and_redacted():
    time = Time()

    def sleep(delay):
        raise RuntimeError("private sleep exception text")

    report = session(time, sleep=sleep).run(sampler(time))
    assert report.stop_reason == "sleep_error" and len(report.rounds) == 2
    assert report.rounds[-1].observation is None and "private" not in repr(report)


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "100"])
def test_invalid_monotonic_consumes_session_without_sampling(value):
    time = Time()
    target = session(time, monotonic=lambda: value)
    report = target.run(sampler(time))
    assert report.stop_reason == "monotonic_error" and report.rounds == ()
    assert time.events == []
    assert_consumed(target, time)


def test_regressed_monotonic_stops_after_reads_without_more_sampling():
    time = Time()
    values = iter((100, 100.5, 100.4))
    report = session(time, monotonic=lambda: next(values)).run(sampler(time))
    assert report.stop_reason == "monotonic_error" and len(report.rounds) == 1
    assert len(report.rounds[0].reads) == 2


@pytest.mark.parametrize(
    "clocks,reason,accepted",
    [
        ([datetime(2026, 2, 1)], "wall_clock_error", False),
        ([BASE], "ingress_rejected", False),
        ([BASE + timedelta(seconds=0.5), BASE], "wall_clock_error", True),
    ],
)
def test_invalid_or_regressed_wall_clock_fails_closed(clocks, reason, accepted):
    time = Time()
    values = iter(clocks)
    target = session(time, clock=lambda: next(values))
    report = target.run(sampler(time))
    assert report.stop_reason == reason and len(report.rounds) == 1
    assert (report.rounds[0].observation is not None) is accepted
    assert report.rounds[0].reads == ()
    assert_consumed(target, time)


def test_no_reentrant_session_and_no_resume_after_cancellation():
    time = Time()
    target = session(time)

    def sample(request):
        assert_consumed(target, time)
        return wire(envelope(request, time, maximum=1))

    assert target.run(sample).stop_reason == "budget_exhausted"
    cancelled = session(time)

    def cancel(request):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        cancelled.run(cancel)
    assert_consumed(cancelled, time)


def test_import_construction_and_run_have_no_external_execution_paths(monkeypatch):
    import socket
    import subprocess
    import threading
    import time as system_time
    from pathlib import Path

    def forbidden(*args, **kwargs):
        pytest.fail("external side effect")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(threading.Thread, "start", forbidden)
    monkeypatch.setattr(threading, "Timer", forbidden)
    monkeypatch.setattr(system_time, "sleep", forbidden)
    for name in ("read_bytes", "read_text", "write_bytes", "write_text"):
        monkeypatch.setattr(Path, name, forbidden)
    importlib.reload(module)
    time = Time()
    target = session(time)
    assert time.events == []
    assert target.run(sampler(time)).stop_reason == "budget_exhausted"
    for extra in (
        {"offline_fixture_mode": False},
        {"max_rounds": 4},
        {"securities": ()},
    ):
        with pytest.raises(TypeError):
            session(time, **extra)


@pytest.mark.parametrize("hook", ["monotonic", "sleep", "clock"])
def test_execution_hooks_are_required_without_invocation(hook):
    time = Time()
    with pytest.raises(ContractError, match="^execution_hooks_required$"):
        session(time, **{hook: None})
    assert time.events == []
