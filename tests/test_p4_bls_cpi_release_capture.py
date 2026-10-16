"""Zero-effect composition tests for the September CPI release callback."""

import copy
from dataclasses import replace
from datetime import timedelta

import pytest
from test_p4_bls_cpi_direct import encode, response, september_response

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import Key, Kind
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical import bls_cpi_direct_live as live
from market_data_foundation.historical import bls_cpi_release_runner as runner
from market_data_foundation.historical.bls_cpi_material_store import (
    InMemoryMaterialStore,
    InMemoryWriterProtection,
)
from market_data_foundation.historical.bls_cpi_release_capture import (
    SeptemberReleaseCapture,
)


class FakeTransport:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def post(self, endpoint, body, headers, *, timeout, max_response_bytes):
        self.calls.append((endpoint, body, headers, timeout, max_response_bytes))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class SequenceClock:
    def __init__(self, *values):
        self.values = iter(values)

    def __call__(self):
        return next(self.values)


def exact_request(attempt=1):
    return runner.CaptureRequest(
        runner.PRODUCT,
        runner.CAPABILITY,
        runner.REFERENCE,
        runner.RELEASE,
        runner.SLOT_IDENTITY,
        runner.EXECUTION_IDENTITY,
        attempt,
    )


def secret(tmp_path):
    path = tmp_path / "synthetic-secret.toml"
    path.write_text('[bls]\napi_key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"\n')
    return path


def live_clock(count=4):
    return SequenceClock(
        *(runner.RELEASE + timedelta(milliseconds=index) for index in range(count))
    )


def callback(
    tmp_path,
    transport,
    *,
    ledger=None,
    store=None,
    clock=None,
    tokens=None,
    readback_ledger_factory=None,
):
    ledger = ledger or InMemoryLedger()
    token_values = iter(tokens or ("release-attempt-1",))
    readback_ledger_factory = readback_ledger_factory or (
        lambda: InMemoryLedger.from_snapshot(ledger.snapshot())
    )
    return (
        SeptemberReleaseCapture(
            CanonicalKernel(ledger, lambda: runner.RELEASE + timedelta(seconds=1)),
            store or InMemoryMaterialStore(),
            InMemoryWriterProtection(),
            readback_ledger_factory,
            secret_path=secret(tmp_path),
            artifact_root=tmp_path / "artifacts",
            transport=transport,
            clock=clock or live_clock(),
            identity_factory=lambda: next(token_values),
            source_retry_sleep=lambda _seconds: None,
        ),
        ledger,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"product": "PPI"},
        {"capability": "wrong"},
        {"reference": "2026-08"},
        {"release": runner.RELEASE + timedelta(seconds=1)},
        {"slot_identity": "wrong"},
        {"execution_identity": "wrong"},
        {"attempt": 0},
        {"attempt": runner.MAX_ATTEMPTS + 1},
        {"attempt": True},
    ],
)
def test_wrong_runner_identity_is_terminal_before_provider(tmp_path, change):
    transport = FakeTransport()
    capture, _ledger = callback(tmp_path, transport)
    assert capture(replace(exact_request(), **change)) == runner.CaptureCompletion(
        "request_invalid"
    )
    assert transport.calls == []


def test_runner_to_september_material_publication_readback_success(tmp_path):
    transport = FakeTransport(
        live.HttpResult(
            200, encode(september_response()), {"content-encoding": "identity"}
        )
    )
    capture, ledger = callback(tmp_path, transport)

    def runner_clock():
        return runner.RELEASE

    result = runner.run_cpi_release(
        enabled=True,
        clock=runner_clock,
        probe=lambda: runner.ClockAttestation(
            runner.CLOCK_SOURCE, runner.RELEASE, 0, True
        ),
        capture=capture,
        state_store=runner.InMemorySlotStateStore(),
    )
    assert result == runner.RunnerResult(
        "completed",
        runner.RunnerClassification.CANONICAL_VERIFIED,
        1,
        runner.EXECUTION_IDENTITY,
    )
    assert len(transport.calls) == 1

    prefix = "p4-bls-cpi-direct-live-release-attempt-1"
    run = ledger.get(Key(Kind.RUN, prefix + ":run"))
    acquisition = ledger.get(
        Key(Kind.ACQUISITION, "mdf-bls-cpi-direct-release-attempt-1")
    )
    version = ledger.get(Key(Kind.VERSION, prefix + ":version"))
    publication = ledger.publication_by_occurrence(prefix + ":occurrence")
    expected = {
        "release_attempt": "1",
        "release_execution_identity": runner.EXECUTION_IDENTITY,
        "release_instant": runner.RELEASE.isoformat(),
        "release_reference": runner.REFERENCE,
        "release_slot_identity": runner.SLOT_IDENTITY,
    }
    assert dict(run.transform.parameters) | expected == dict(run.transform.parameters)
    assert dict(acquisition.parameters) | expected == dict(acquisition.parameters)
    assert version.run == run.key
    assert version.acquisitions == (acquisition.key,)
    assert ledger.get_content(version.key) is not None
    assert publication.request.version == version.key


def test_transport_retry_stays_inside_first_release_attempt(tmp_path):
    transport = FakeTransport(
        OSError("synthetic transport failure"),
        live.HttpResult(
            200, encode(september_response()), {"content-encoding": "identity"}
        ),
    )
    capture, ledger = callback(
        tmp_path,
        transport,
        clock=live_clock(),
        tokens=("attempt-1",),
    )
    runner_now = [runner.RELEASE]

    result = runner.run_cpi_release(
        enabled=True,
        clock=lambda: runner_now[0],
        probe=lambda: runner.ClockAttestation(
            runner.CLOCK_SOURCE, runner_now[0], 0, True
        ),
        capture=capture,
        state_store=runner.InMemorySlotStateStore(),
        wait=lambda seconds: runner_now.__setitem__(
            0, runner_now[0] + timedelta(seconds=seconds)
        ),
    )
    assert result.attempts == 1
    assert len(transport.calls) == 2
    run = ledger.get(Key(Kind.RUN, "p4-bls-cpi-direct-live-attempt-1:run"))
    acquisition = ledger.get(Key(Kind.ACQUISITION, "mdf-bls-cpi-direct-attempt-1"))
    parameters = dict(run.transform.parameters)
    assert parameters["release_attempt"] == "1"
    assert parameters["release_slot_identity"] == runner.SLOT_IDENTITY
    assert parameters["release_execution_identity"] == runner.EXECUTION_IDENTITY
    assert acquisition.provider_data_attempts == 2


def test_valid_http_200_without_september_is_reference_not_available(tmp_path):
    transport = FakeTransport(
        live.HttpResult(200, encode(response()), {"content-encoding": "identity"})
    )
    capture, _ledger = callback(tmp_path, transport)
    assert capture(exact_request()) == runner.CaptureCompletion(
        "no_new_release/reference_not_available"
    )
    assert len(transport.calls) == 1


def test_partial_september_presence_is_terminal_and_not_retryable(tmp_path):
    payload = copy.deepcopy(september_response())
    payload["Results"]["series"][0]["data"].pop(0)
    transport = FakeTransport(
        live.HttpResult(200, encode(payload), {"content-encoding": "identity"})
    )
    capture, _ledger = callback(tmp_path, transport)
    completion = capture(exact_request())
    assert completion == runner.CaptureCompletion("terminal")
    assert runner._retryable(completion) is False


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_shared_source_http_retry_exhaustion_is_terminal_to_release_runner(
    tmp_path, status
):
    transport = FakeTransport(
        live.HttpResult(status, b"synthetic", {}),
        live.HttpResult(status, b"synthetic", {}),
        live.HttpResult(status, b"synthetic", {}),
    )
    capture, _ledger = callback(tmp_path, transport)
    completion = capture(exact_request())
    assert completion == runner.CaptureCompletion(
        "source_transport_exhausted", http_status=status
    )
    assert len(transport.calls) == 3
    assert runner._retryable(completion) is False


def test_shared_source_transport_retry_exhaustion_is_terminal_to_release_runner(
    tmp_path,
):
    transport = FakeTransport(
        TimeoutError("synthetic"),
        TimeoutError("synthetic"),
        TimeoutError("synthetic"),
    )
    capture, _ledger = callback(tmp_path, transport)
    completion = capture(exact_request())
    assert completion == runner.CaptureCompletion("source_transport_exhausted")
    assert len(transport.calls) == 3
    assert runner._retryable(completion) is False


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 501])
def test_other_http_status_is_nonretryable(tmp_path, status):
    transport = FakeTransport(live.HttpResult(status, b"synthetic", {}))
    capture, _ledger = callback(tmp_path, transport)
    completion = capture(exact_request())
    assert completion == runner.CaptureCompletion("terminal")
    assert runner._retryable(completion) is False


def test_missing_comparison_after_target_exists_is_terminal(tmp_path):
    payload = copy.deepcopy(september_response())
    payload["Results"]["series"][0]["data"].pop(1)
    transport = FakeTransport(
        live.HttpResult(200, encode(payload), {"content-encoding": "identity"})
    )
    capture, _ledger = callback(tmp_path, transport)
    completion = capture(exact_request())
    assert completion == runner.CaptureCompletion("terminal")
    assert runner._retryable(completion) is False


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        encode({**september_response(), "status": "REQUEST_FAILED"}),
    ],
)
def test_semantic_or_integrity_failure_is_terminal(tmp_path, body):
    transport = FakeTransport(
        live.HttpResult(200, body, {"content-encoding": "identity"})
    )
    capture, _ledger = callback(tmp_path, transport)
    assert capture(exact_request()) == runner.CaptureCompletion("terminal")


def test_material_failure_is_terminal_and_never_provider_retryable(tmp_path):
    class FailingStore(InMemoryMaterialStore):
        def create_if_absent(self, identity, payload):
            raise OSError("synthetic material failure")

    transport = FakeTransport(
        live.HttpResult(
            200, encode(september_response()), {"content-encoding": "identity"}
        )
    )
    capture, _ledger = callback(tmp_path, transport, store=FailingStore())
    completion = capture(exact_request())
    assert completion == runner.CaptureCompletion("terminal")
    assert runner._retryable(completion) is False


def test_credential_failure_is_terminal_before_provider(tmp_path):
    transport = FakeTransport()
    capture, _ledger = callback(tmp_path, transport)
    capture = replace(capture, secret_path=tmp_path / "missing-secret.toml")
    completion = capture(exact_request())
    assert completion == runner.CaptureCompletion("terminal")
    assert transport.calls == []


@pytest.mark.parametrize("failure", ["canonical", "publication", "outcome_unknown"])
def test_canonical_publication_and_unknown_outcome_are_terminal(tmp_path, failure):
    class FailingLedger(InMemoryLedger):
        def put(self, record):
            if failure == "canonical" and record.key.kind == Kind.RUN:
                raise OSError("synthetic canonical failure")
            return super().put(record)

        def publish(self, request, clock):
            if failure == "publication":
                raise OSError("synthetic publication failure")
            return super().publish(request, clock)

    transport = FakeTransport(
        live.HttpResult(
            200, encode(september_response()), {"content-encoding": "identity"}
        )
    )
    capture, _ledger = callback(tmp_path, transport, ledger=FailingLedger())
    if failure == "outcome_unknown":
        capture = replace(
            capture,
            authoritative_commit=lambda: (_ for _ in ()).throw(
                OSError("synthetic unknown outcome")
            ),
        )
    completion = capture(exact_request())
    assert completion == runner.CaptureCompletion("terminal")
    assert runner._retryable(completion) is False


def test_successful_provider_without_exact_readback_cannot_verify(tmp_path):
    transport = FakeTransport(
        live.HttpResult(
            200, encode(september_response()), {"content-encoding": "identity"}
        )
    )
    capture, _ledger = callback(
        tmp_path,
        transport,
        readback_ledger_factory=InMemoryLedger,
    )
    completion = capture(exact_request())
    assert completion == runner.CaptureCompletion("terminal")
    assert completion.canonical_verified is False


@pytest.mark.parametrize(
    "failure",
    [
        "version_missing",
        "version_mismatch",
        "content_missing",
        "content_mismatch",
        "publication_missing",
        "publication_mismatch",
    ],
)
def test_independent_exact_readback_mismatch_is_terminal(tmp_path, failure):
    class FaultyReadbackLedger:
        def __init__(self, source):
            self.delegate = InMemoryLedger.from_snapshot(source.snapshot())

        def get(self, key):
            value = self.delegate.get(key)
            if key.kind == Kind.VERSION:
                if failure == "version_missing":
                    return None
                if failure == "version_mismatch":
                    return object()
            return value

        def get_content(self, key):
            if failure == "content_missing":
                return None
            if failure == "content_mismatch":
                return b"synthetic mismatch"
            return self.delegate.get_content(key)

        def publication_by_occurrence(self, occurrence):
            if failure == "publication_missing":
                return None
            if failure == "publication_mismatch":
                return object()
            return self.delegate.publication_by_occurrence(occurrence)

    transport = FakeTransport(
        live.HttpResult(
            200, encode(september_response()), {"content-encoding": "identity"}
        )
    )
    ledger = InMemoryLedger()
    capture, _ledger = callback(
        tmp_path,
        transport,
        ledger=ledger,
        readback_ledger_factory=lambda: FaultyReadbackLedger(ledger),
    )
    completion = capture(exact_request())
    assert completion == runner.CaptureCompletion("terminal")
    assert completion.canonical_verified is False


def test_same_ledger_readback_is_rejected(tmp_path):
    transport = FakeTransport(
        live.HttpResult(
            200, encode(september_response()), {"content-encoding": "identity"}
        )
    )
    ledger = InMemoryLedger()
    capture, _ledger = callback(
        tmp_path,
        transport,
        ledger=ledger,
        readback_ledger_factory=lambda: ledger,
    )
    completion = capture(exact_request())
    assert completion == runner.CaptureCompletion("terminal")
    assert completion.canonical_verified is False


@pytest.mark.parametrize("failure", ["factory", "readback"])
def test_readback_factory_or_ledger_error_is_terminal(tmp_path, failure):
    class FailingReadbackLedger(InMemoryLedger):
        def get(self, key):
            raise OSError("synthetic readback failure")

    def readback_factory():
        if failure == "factory":
            raise OSError("synthetic factory failure")
        return FailingReadbackLedger()

    transport = FakeTransport(
        live.HttpResult(
            200, encode(september_response()), {"content-encoding": "identity"}
        )
    )
    capture, _ledger = callback(
        tmp_path,
        transport,
        readback_ledger_factory=readback_factory,
    )
    completion = capture(exact_request())
    assert completion == runner.CaptureCompletion("terminal")
    assert runner._retryable(completion) is False


def test_ordinary_live_executor_still_defaults_to_august(tmp_path):
    transport = FakeTransport(
        live.HttpResult(200, encode(response()), {"content-encoding": "identity"})
    )
    candidate = live.execute_live_candidate(
        secret_path=secret(tmp_path),
        artifact_root=tmp_path / "august-artifacts",
        transport=transport,
        clock=live_clock(),
        identity_factory=lambda: "ordinary-august",
    )
    assert candidate.evidence.reference_contract.semantics.reference_period == "2026-08"
    assert candidate.plan.version.finite_scope.members == tuple(
        f"2026-08:bls:cpi:{name}"
        for name in sorted(("core_mom", "core_yoy", "headline_mom", "headline_yoy"))
    )
