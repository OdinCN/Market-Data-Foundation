"""Deterministic counterexamples for the single prebound CPI release slot."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from market_data_foundation.historical import bls_cpi_release_runner as runner


class FakeClock:
    def __init__(self, value=runner.RELEASE):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def good_probe(clock):
    return lambda: runner.ClockAttestation(runner.CLOCK_SOURCE, clock(), 0.25, True)


def success(_request):
    return runner.CaptureCompletion("success", canonical_verified=True)


def execute(clock=None, capture=success, store=None, probe=None, enabled=True):
    clock = clock or FakeClock()
    store = store or runner.InMemorySlotStateStore()
    result = runner.run_cpi_release(
        enabled=enabled,
        clock=clock,
        probe=probe or good_probe(clock),
        capture=capture,
        state_store=store,
        wait=clock.advance,
    )
    return result, store


@pytest.mark.parametrize(
    "enabled,offset,phase,classification",
    [
        (False, 0, "disabled", runner.RunnerClassification.DISABLED),
        (True, -1, "early", runner.RunnerClassification.NOT_DUE),
        (True, 121, "late", runner.RunnerClassification.LATE_NO_CATCHUP),
        (True, 1200, "late", runner.RunnerClassification.LATE_NO_CATCHUP),
        (True, 1201, "missed", runner.RunnerClassification.MISSED_NO_BACKFILL),
    ],
)
def test_nonadmitted_times_never_capture(enabled, offset, phase, classification):
    clock = FakeClock(runner.RELEASE + timedelta(seconds=offset))
    captures = []
    result, store = execute(
        clock,
        capture=lambda request: captures.append(request),
        enabled=enabled,
    )
    assert (result.phase, result.classification) == (phase, classification)
    assert captures == []
    assert store.state is None


@pytest.mark.parametrize("offset", [0, 120])
def test_exact_due_and_t_plus_120_are_admitted(offset):
    clock = FakeClock(runner.RELEASE + timedelta(seconds=offset))
    result, store = execute(clock)
    assert result.classification == runner.RunnerClassification.CANONICAL_VERIFIED
    assert result.attempts == 1
    assert store.state.status == runner.SlotStatus.COMPLETED


@pytest.mark.parametrize(
    "change",
    [
        {"source_identity": "ntp://other.invalid:123"},
        {"offset_seconds": 2.01},
        {"read_only": False},
        {"observed_at": runner.RELEASE - timedelta(seconds=61)},
        {"observed_at": runner.RELEASE + timedelta(seconds=1)},
    ],
)
def test_clock_invalid_before_first_attempt_stops_without_capture(change):
    clock = FakeClock()
    evidence = runner.ClockAttestation(runner.CLOCK_SOURCE, clock(), 0.0, True)
    captures = []
    result, store = execute(
        clock,
        capture=lambda request: captures.append(request),
        probe=lambda: replace(evidence, **change),
    )
    assert result.classification == runner.RunnerClassification.CLOCK_UNCERTAIN
    assert captures == []
    assert store.state.status == runner.SlotStatus.STOPPED
    assert store.stop_marker["classification"] == "clock_uncertain"


def test_clock_invalid_before_release_poll_stops_after_one_capture():
    clock = FakeClock()
    probes = 0
    captures = []

    def probe():
        nonlocal probes
        probes += 1
        source = runner.CLOCK_SOURCE if probes == 1 else "wrong"
        return runner.ClockAttestation(source, clock(), 0, True)

    def capture(request):
        captures.append(request)
        return runner.CaptureCompletion("no_new_release/reference_not_available")

    result, _store = execute(clock, capture=capture, probe=probe)
    assert result.classification == runner.RunnerClassification.CLOCK_UNCERTAIN
    assert len(captures) == 1


def test_slot_is_claimed_before_capture_and_identity_is_exact():
    store = runner.InMemorySlotStateStore()
    requests = []

    def capture(request):
        assert store.state.status == runner.SlotStatus.RUNNING
        assert store.state.attempts == 1
        requests.append(request)
        return runner.CaptureCompletion("success", canonical_verified=True)

    result, _store = execute(capture=capture, store=store)
    assert result.classification == runner.RunnerClassification.CANONICAL_VERIFIED
    assert requests == [
        runner.CaptureRequest(
            runner.PRODUCT,
            runner.CAPABILITY,
            runner.REFERENCE,
            runner.RELEASE,
            runner.SLOT_IDENTITY,
            runner.EXECUTION_IDENTITY,
            1,
        )
    ]


def test_completed_slot_forbids_recapture():
    _first, store = execute()
    captures = []
    second, _store = execute(
        store=store, capture=lambda request: captures.append(request)
    )
    assert second.classification == runner.RunnerClassification.COMPLETED_SLOT
    assert captures == []


def test_interrupted_running_slot_is_stopped_without_recapture():
    existing = runner.SlotState(
        runner.PRODUCT,
        runner.CAPABILITY,
        runner.REFERENCE,
        "2026-10-14T12:30:00Z",
        runner.SLOT_IDENTITY,
        runner.EXECUTION_IDENTITY,
        runner.SlotStatus.RUNNING,
        1,
        "capturing",
        "2026-10-14T12:30:00Z",
    )
    store = runner.InMemorySlotStateStore(existing)
    captures = []
    result, _store = execute(
        store=store, capture=lambda request: captures.append(request)
    )
    assert result.classification == (
        runner.RunnerClassification.INTERRUPTED_SLOT_UNCERTAIN
    )
    assert captures == []
    assert store.state.status == runner.SlotStatus.STOPPED
    assert store.stop_marker is not None


def test_only_reference_not_available_polls_then_succeeds():
    completions = iter(
        [
            runner.CaptureCompletion("no_new_release/reference_not_available"),
            runner.CaptureCompletion("success", True),
        ]
    )
    requests = []

    def capture(request):
        requests.append(request)
        return next(completions)

    result, _store = execute(capture=capture)
    assert result.classification == runner.RunnerClassification.CANONICAL_VERIFIED
    assert [request.attempt for request in requests] == [1, 2]
    assert {request.slot_identity for request in requests} == {runner.SLOT_IDENTITY}
    assert {request.execution_identity for request in requests} == {
        runner.EXECUTION_IDENTITY
    }


@pytest.mark.parametrize(
    "completion",
    [
        runner.CaptureCompletion("transport_failure"),
        runner.CaptureCompletion("source_transport_exhausted"),
        runner.CaptureCompletion("http_error", http_status=408),
        runner.CaptureCompletion("http_error", http_status=429),
        runner.CaptureCompletion("http_error", http_status=500),
        runner.CaptureCompletion("http_error", http_status=502),
        runner.CaptureCompletion("http_error", http_status=503),
        runner.CaptureCompletion("http_error", http_status=504),
        runner.CaptureCompletion("http_error", http_status=400),
        runner.CaptureCompletion("http_error", http_status=401),
        runner.CaptureCompletion("semantic_failure"),
        runner.CaptureCompletion("integrity_failure"),
        runner.CaptureCompletion("auth_failure"),
        runner.CaptureCompletion("policy_failure"),
        runner.CaptureCompletion("transport_failure", http_status=500),
    ],
)
def test_transport_http_semantic_integrity_auth_policy_are_terminal(completion):
    captures = []

    def capture(request):
        captures.append(request)
        return completion

    result, store = execute(capture=capture)
    assert result.classification == runner.RunnerClassification.CAPTURE_NONRETRYABLE
    assert len(captures) == 1
    assert store.state.status == runner.SlotStatus.STOPPED


def test_max_attempts_is_terminal():
    attempts = []

    def capture(request):
        attempts.append(request.attempt)
        return runner.CaptureCompletion("no_new_release/reference_not_available")

    result, _store = execute(capture=capture)
    assert result.classification == runner.RunnerClassification.MAX_ATTEMPTS_EXHAUSTED
    assert attempts == list(range(1, 41))


def test_remaining_window_must_cover_retry_and_attempt_budgets():
    clock = FakeClock(runner.RELEASE + timedelta(seconds=120))

    result, _store = execute(
        clock,
        capture=lambda _request: runner.CaptureCompletion(
            "no_new_release/reference_not_available"
        ),
    )
    assert result.classification == runner.RunnerClassification.RETRY_BUDGET_EXHAUSTED
    assert result.attempts == 36


def test_attempt_duration_budget_is_terminal():
    clock = FakeClock()

    def capture(_request):
        clock.advance(21)
        return runner.CaptureCompletion("transport_failure")

    result, _store = execute(clock, capture=capture)
    assert result.classification == (
        runner.RunnerClassification.ATTEMPT_DURATION_EXCEEDED
    )


def test_window_expiry_after_capture_is_terminal():
    clock = FakeClock()

    def capture(_request):
        clock.advance(1201)
        return runner.CaptureCompletion("transport_failure")

    result, _store = execute(clock, capture=capture)
    assert result.classification == runner.RunnerClassification.WINDOW_EXPIRED


def test_raw_success_without_explicit_canonical_verification_is_terminal():
    result, store = execute(
        capture=lambda _request: runner.CaptureCompletion("success")
    )
    assert result.classification == (
        runner.RunnerClassification.CANONICAL_VERIFICATION_REQUIRED
    )
    assert store.state.status == runner.SlotStatus.STOPPED


def test_unclassified_capture_exception_is_terminal_and_not_retried():
    calls = 0

    def capture(_request):
        nonlocal calls
        calls += 1
        raise TimeoutError("must not enter state")

    result, store = execute(capture=capture)
    assert result.classification == runner.RunnerClassification.CAPTURE_NONRETRYABLE
    assert store.state.status == runner.SlotStatus.STOPPED
    assert calls == 1


def test_waiter_cannot_bypass_thirty_second_retry_spacing():
    clock = FakeClock()
    captures = []
    store = runner.InMemorySlotStateStore()

    def capture(request):
        captures.append(request)
        return runner.CaptureCompletion("no_new_release/reference_not_available")

    result = runner.run_cpi_release(
        enabled=True,
        clock=clock,
        probe=good_probe(clock),
        capture=capture,
        state_store=store,
        wait=lambda _seconds: None,
    )
    assert result.classification == runner.RunnerClassification.CLOCK_UNCERTAIN
    assert len(captures) == 1


def test_malformed_injected_clock_fails_closed_without_capture():
    captures = []
    store = runner.InMemorySlotStateStore()
    result = runner.run_cpi_release(
        enabled=True,
        clock=lambda: "not-a-clock",
        probe=lambda: None,
        capture=lambda request: captures.append(request),
        state_store=store,
    )
    assert result.classification == runner.RunnerClassification.CLOCK_UNCERTAIN
    assert captures == []
    assert store.stop_marker["classification"] == "clock_uncertain"


def test_stop_marker_without_state_forbids_a_later_claim():
    store = runner.InMemorySlotStateStore()
    store.mark_stop("clock_uncertain", runner.RELEASE)
    captures = []
    result, _store = execute(
        store=store, capture=lambda request: captures.append(request)
    )
    assert result.classification == runner.RunnerClassification.SLOT_STATE_INVALID
    assert captures == []


def test_filesystem_claim_is_exclusive_and_updates_are_durable(tmp_path):
    first = runner.FilesystemSlotStateStore(tmp_path)
    second = runner.FilesystemSlotStateStore(tmp_path)
    now = runner.RELEASE
    state = runner.SlotState(
        runner.PRODUCT,
        runner.CAPABILITY,
        runner.REFERENCE,
        "2026-10-14T12:30:00Z",
        runner.SLOT_IDENTITY,
        runner.EXECUTION_IDENTITY,
        runner.SlotStatus.RUNNING,
        0,
        "admitted",
        "2026-10-14T12:30:00Z",
    )
    assert first.claim(state) is None
    assert second.claim(state) == state
    replacement = replace(state, attempts=1, classification="capturing")
    first.replace(state, replacement)
    assert second.claim(state) == replacement
    first.mark_stop("integrity_failure", now)
    assert json.loads(first.stop_path.read_text())["classification"] == (
        "integrity_failure"
    )


def test_malformed_filesystem_state_fails_closed_with_stop_marker(tmp_path):
    store = runner.FilesystemSlotStateStore(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    store.state_path.write_text("{not-json")
    captures = []
    result, _store = execute(
        store=store, capture=lambda request: captures.append(request)
    )
    assert result.classification == runner.RunnerClassification.SLOT_STATE_INVALID
    assert captures == []
    marker = json.loads(store.stop_path.read_text())
    assert marker["classification"] == "slot_state_invalid"


def test_state_contains_no_callback_payload_credentials_or_raw_exception(tmp_path):
    store = runner.FilesystemSlotStateStore(tmp_path)
    secret = "credential-value-that-must-not-be-stored"
    clock = FakeClock(runner.RELEASE + timedelta(seconds=120))

    def capture(_request):
        raise RuntimeError(secret)

    result, _store = execute(clock, capture=capture, store=store)
    assert result.classification == runner.RunnerClassification.CAPTURE_NONRETRYABLE
    persisted = store.state_path.read_text() + store.stop_path.read_text()
    assert secret not in persisted
    assert "raw" not in persisted.lower()
    assert set(json.loads(store.state_path.read_text())) == {
        "product",
        "capability",
        "reference",
        "release",
        "slot_identity",
        "execution_identity",
        "status",
        "attempts",
        "classification",
        "updated_at",
    }


def test_stopped_slot_never_hiddenly_recaptures():
    _result, store = execute(
        capture=lambda _request: runner.CaptureCompletion("integrity_failure")
    )
    captures = []
    result, _store = execute(
        store=store, capture=lambda request: captures.append(request)
    )
    assert result.classification == runner.RunnerClassification.CAPTURE_NONRETRYABLE
    assert captures == []


def test_malformed_semantic_state_stops_without_capture():
    _result, store = execute()
    store.state = replace(store.state, attempts=41)
    captures = []
    result, _store = execute(
        store=store, capture=lambda request: captures.append(request)
    )
    assert result.classification == runner.RunnerClassification.SLOT_STATE_INVALID
    assert captures == []
