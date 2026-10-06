"""Deterministic runner for the prebound September 2026 CPI release slot.

This module contains no provider, clock-network, calendar, or storage clients.
Those effects belong behind the injected probe and capture callbacks.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol

PRODUCT = "CPI"
CAPABILITY = "mdf.macro.bls.cpi.headlines"
REFERENCE = "2026-09"
RELEASE = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)
CALENDAR_SOURCE = "https://www.bls.gov/schedule/news_release/cpi.htm"
CALENDAR_BINDING_MODE = "canonical_prebound_official_source"
CLOCK_SOURCE = "ntp://ntp.aliyun.com:123"
WINDOW_SECONDS = 1200
LATE_SECONDS = 120
RETRY_SECONDS = 30
MAX_ATTEMPTS = 40
ATTEMPT_SECONDS = 20
MAX_CLOCK_OFFSET_SECONDS = 2
MAX_CLOCK_EVIDENCE_AGE_SECONDS = 60
SLOT_IDENTITY = "CPI/2026-09/2026-10-14T12:30:00Z"
EXECUTION_IDENTITY = "mdf-bls-cpi-release-2026-09"
STATE_FILENAME = "bls-cpi-2026-09.json"
STOP_FILENAME = "bls-cpi-2026-09.STOP"
MAX_STATE_BYTES = 16_384


class RunnerClassification(StrEnum):
    DISABLED = "disabled"
    NOT_DUE = "not_due"
    LATE_NO_CATCHUP = "late_no_catchup"
    MISSED_NO_BACKFILL = "missed_no_backfill"
    CANONICAL_VERIFIED = "canonical_verified"
    COMPLETED_SLOT = "completed_slot"
    INTERRUPTED_SLOT_UNCERTAIN = "interrupted_slot_uncertain"
    SLOT_STATE_INVALID = "slot_state_invalid"
    CLOCK_UNCERTAIN = "clock_uncertain"
    CAPTURE_NONRETRYABLE = "capture_nonretryable"
    CANONICAL_VERIFICATION_REQUIRED = "canonical_verification_required"
    MAX_ATTEMPTS_EXHAUSTED = "max_attempts_exhausted"
    RETRY_BUDGET_EXHAUSTED = "retry_budget_exhausted"
    WINDOW_EXPIRED = "window_expired"
    ATTEMPT_DURATION_EXCEEDED = "attempt_duration_exceeded"
    RETRY_WAIT_FAILED = "retry_wait_failed"


class SlotStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    STOPPED = "stopped"


@dataclass(frozen=True)
class ClockAttestation:
    source_identity: str
    observed_at: datetime
    offset_seconds: float
    read_only: bool


@dataclass(frozen=True)
class CaptureRequest:
    product: str
    capability: str
    reference: str
    release: datetime
    slot_identity: str
    execution_identity: str
    attempt: int


@dataclass(frozen=True)
class CaptureCompletion:
    """Secret- and payload-free completion classification from a future callback."""

    classification: str
    canonical_verified: bool = False
    http_status: int | None = None


@dataclass(frozen=True)
class RunnerResult:
    phase: str
    classification: RunnerClassification
    attempts: int = 0
    execution_identity: str | None = None


@dataclass(frozen=True)
class SlotState:
    product: str
    capability: str
    reference: str
    release: str
    slot_identity: str
    execution_identity: str
    status: SlotStatus
    attempts: int
    classification: str
    updated_at: str


class StateStoreError(RuntimeError):
    """A state document could not be trusted or durably changed."""


class SlotStateStore(Protocol):
    def claim(self, state: SlotState) -> SlotState | None:
        """Create exclusively, returning None, or return the existing state."""

    def replace(self, expected: SlotState, replacement: SlotState) -> None: ...

    def mark_stop(self, classification: str, at: datetime) -> None: ...


class InMemorySlotStateStore:
    def __init__(self, state: SlotState | None = None) -> None:
        self.state = state
        self.stop_marker: dict[str, str] | None = None

    def claim(self, state: SlotState) -> SlotState | None:
        if self.stop_marker is not None and self.state is None:
            raise StateStoreError("slot_stop_marker_exists")
        if self.state is not None:
            return self.state
        self.state = state
        return None

    def replace(self, expected: SlotState, replacement: SlotState) -> None:
        if self.state != expected:
            raise StateStoreError("slot_state_compare_failed")
        self.state = replacement

    def mark_stop(self, classification: str, at: datetime) -> None:
        if self.stop_marker is None:
            self.stop_marker = _stop_document(classification, at)


class FilesystemSlotStateStore:
    """Exclusive initial claim and atomic, durable JSON replacements."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.state_path = directory / STATE_FILENAME
        self.stop_path = directory / STOP_FILENAME
        self.lock_path = directory / ".bls-cpi-2026-09.lock"

    def claim(self, state: SlotState) -> SlotState | None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self._lock():
                if self.stop_path.exists() and not self.state_path.exists():
                    raise StateStoreError("slot_stop_marker_exists")
                try:
                    descriptor = os.open(
                        self.state_path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                    )
                except FileExistsError:
                    return self._read_state()
                try:
                    _write_descriptor(descriptor, _state_bytes(state))
                finally:
                    os.close(descriptor)
                _fsync_directory(self.directory)
                return None
        except StateStoreError:
            raise
        except OSError:
            raise StateStoreError("slot_state_claim_failed") from None

    def replace(self, expected: SlotState, replacement: SlotState) -> None:
        try:
            with self._lock():
                if self._read_state() != expected:
                    raise StateStoreError("slot_state_compare_failed")
                _atomic_write(self.state_path, _state_bytes(replacement))
        except StateStoreError:
            raise
        except OSError:
            raise StateStoreError("slot_state_write_failed") from None

    def mark_stop(self, classification: str, at: datetime) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self._lock():
                try:
                    descriptor = os.open(
                        self.stop_path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                    )
                except FileExistsError:
                    return
                try:
                    _write_descriptor(
                        descriptor,
                        _json_bytes(_stop_document(classification, at)),
                    )
                finally:
                    os.close(descriptor)
                _fsync_directory(self.directory)
        except StateStoreError:
            raise
        except OSError:
            raise StateStoreError("slot_stop_write_failed") from None

    def _read_state(self) -> SlotState:
        try:
            descriptor = os.open(self.state_path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                raw = os.read(descriptor, MAX_STATE_BYTES + 1)
            finally:
                os.close(descriptor)
            if len(raw) > MAX_STATE_BYTES:
                raise ValueError
            value = json.loads(raw)
            if type(value) is not dict or set(value) != {
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
            }:
                raise ValueError
            if any(type(value[key]) is not str for key in value if key != "attempts"):
                raise ValueError
            if type(value["attempts"]) is not int or value["attempts"] < 0:
                raise ValueError
            value["status"] = SlotStatus(value["status"])
            _parse_instant(value["release"])
            _parse_instant(value["updated_at"])
            return SlotState(**value)
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            raise StateStoreError("slot_state_invalid") from None

    def _lock(self):
        class Lock:
            def __enter__(inner_self):
                self.directory.mkdir(parents=True, exist_ok=True)
                inner_self.descriptor = os.open(
                    self.lock_path,
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                    0o600,
                )
                fcntl.flock(inner_self.descriptor, fcntl.LOCK_EX)
                return inner_self

            def __exit__(inner_self, exc_type, exc, traceback):
                fcntl.flock(inner_self.descriptor, fcntl.LOCK_UN)
                os.close(inner_self.descriptor)

        return Lock()


def run_cpi_release(
    *,
    enabled: bool,
    clock: Callable[[], datetime],
    probe: Callable[[], ClockAttestation],
    capture: Callable[[CaptureRequest], CaptureCompletion],
    state_store: SlotStateStore,
    wait: Callable[[int], None] = lambda _seconds: None,
) -> RunnerResult:
    """Evaluate and, only when admitted, execute the single prebound CPI slot."""
    if not enabled:
        return RunnerResult("disabled", RunnerClassification.DISABLED)
    try:
        now = _trusted_local_time(clock)
    except StateStoreError:
        try:
            state_store.mark_stop(RunnerClassification.CLOCK_UNCERTAIN, RELEASE)
        except StateStoreError:
            pass
        return RunnerResult("stopped", RunnerClassification.CLOCK_UNCERTAIN)
    phase, classification = _admission(now)
    if classification is not None:
        return RunnerResult(phase, classification)

    initial = _state(SlotStatus.RUNNING, 0, "admitted", now)
    try:
        existing = state_store.claim(initial)
    except StateStoreError:
        return _state_failure(state_store, now)
    if existing is not None:
        return _existing_slot(state_store, existing, now)

    current = initial
    retry_not_before: datetime | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            now = _trusted_local_time(clock)
        except StateStoreError:
            return _stop(
                state_store,
                current,
                RELEASE,
                RunnerClassification.CLOCK_UNCERTAIN,
            )
        if retry_not_before is not None and now < retry_not_before:
            return _stop(
                state_store,
                current,
                now,
                RunnerClassification.CLOCK_UNCERTAIN,
            )
        if now > RELEASE + timedelta(seconds=WINDOW_SECONDS):
            return _stop(
                state_store,
                current,
                now,
                RunnerClassification.WINDOW_EXPIRED,
            )
        if not _clock_is_trusted(now, probe):
            return _stop(
                state_store,
                current,
                now,
                RunnerClassification.CLOCK_UNCERTAIN,
            )
        attempting = _state(SlotStatus.RUNNING, attempt, "capturing", now)
        try:
            state_store.replace(current, attempting)
        except StateStoreError:
            return _state_failure(state_store, now, attempts=current.attempts)
        current = attempting
        request = CaptureRequest(
            PRODUCT,
            CAPABILITY,
            REFERENCE,
            RELEASE,
            SLOT_IDENTITY,
            EXECUTION_IDENTITY,
            attempt,
        )
        try:
            completion = capture(request)
        except Exception:
            completion = CaptureCompletion("callback_exception")
        try:
            after = _trusted_local_time(clock)
        except StateStoreError:
            return _stop(
                state_store,
                current,
                now,
                RunnerClassification.CLOCK_UNCERTAIN,
            )
        window_end = RELEASE + timedelta(seconds=WINDOW_SECONDS)
        if after > window_end:
            return _stop(
                state_store,
                current,
                after,
                RunnerClassification.WINDOW_EXPIRED,
            )
        duration = (after - now).total_seconds()
        if duration < 0 or duration > ATTEMPT_SECONDS:
            return _stop(
                state_store,
                current,
                after,
                RunnerClassification.ATTEMPT_DURATION_EXCEEDED,
            )
        if not isinstance(completion, CaptureCompletion):
            return _stop(
                state_store,
                current,
                after,
                RunnerClassification.CAPTURE_NONRETRYABLE,
            )
        if completion.classification == "success":
            if completion.canonical_verified is not True:
                return _stop(
                    state_store,
                    current,
                    after,
                    RunnerClassification.CANONICAL_VERIFICATION_REQUIRED,
                )
            completed = _state(
                SlotStatus.COMPLETED,
                attempt,
                RunnerClassification.CANONICAL_VERIFIED,
                after,
            )
            try:
                state_store.replace(current, completed)
            except StateStoreError:
                return _state_failure(state_store, after, attempts=attempt)
            return RunnerResult(
                "completed",
                RunnerClassification.CANONICAL_VERIFIED,
                attempt,
                EXECUTION_IDENTITY,
            )
        if not _retryable(completion):
            return _stop(
                state_store,
                current,
                after,
                RunnerClassification.CAPTURE_NONRETRYABLE,
            )
        if attempt == MAX_ATTEMPTS:
            return _stop(
                state_store,
                current,
                after,
                RunnerClassification.MAX_ATTEMPTS_EXHAUSTED,
            )
        required = timedelta(seconds=RETRY_SECONDS + ATTEMPT_SECONDS)
        if window_end - after < required:
            return _stop(
                state_store,
                current,
                after,
                RunnerClassification.RETRY_BUDGET_EXHAUSTED,
            )
        waiting = _state(SlotStatus.RUNNING, attempt, "retry_wait", after)
        try:
            state_store.replace(current, waiting)
        except StateStoreError:
            return _state_failure(state_store, after, attempts=attempt)
        current = waiting
        retry_not_before = after + timedelta(seconds=RETRY_SECONDS)
        try:
            wait(RETRY_SECONDS)
        except Exception:
            return _stop(
                state_store,
                current,
                after,
                RunnerClassification.RETRY_WAIT_FAILED,
            )
    raise AssertionError("bounded loop exhausted")


def _admission(now: datetime) -> tuple[str, RunnerClassification | None]:
    elapsed = (now - RELEASE).total_seconds()
    if elapsed < 0:
        return "early", RunnerClassification.NOT_DUE
    if elapsed <= LATE_SECONDS:
        return "due", None
    if elapsed <= WINDOW_SECONDS:
        return "late", RunnerClassification.LATE_NO_CATCHUP
    return "missed", RunnerClassification.MISSED_NO_BACKFILL


def _clock_is_trusted(now: datetime, probe: Callable[[], ClockAttestation]) -> bool:
    try:
        evidence = probe()
        if not isinstance(evidence, ClockAttestation):
            return False
        observed = _trusted_local_time(lambda: evidence.observed_at)
        age = (now - observed).total_seconds()
        return (
            evidence.source_identity == CLOCK_SOURCE
            and evidence.read_only is True
            and type(evidence.offset_seconds) in (int, float)
            and abs(evidence.offset_seconds) <= MAX_CLOCK_OFFSET_SECONDS
            and 0 <= age <= MAX_CLOCK_EVIDENCE_AGE_SECONDS
        )
    except Exception:
        return False


def _retryable(completion: CaptureCompletion) -> bool:
    if completion.canonical_verified:
        return False
    if completion.classification == "no_new_release/reference_not_available":
        return completion.http_status is None
    if completion.classification == "transport_failure":
        return completion.http_status is None
    return completion.classification == "http_error" and completion.http_status in {
        408,
        429,
        500,
        502,
        503,
        504,
    }


def _existing_slot(
    store: SlotStateStore, existing: SlotState, now: datetime
) -> RunnerResult:
    if not _identity_matches(existing) or not _state_semantics_valid(existing):
        return _stop_existing(
            store, existing, now, RunnerClassification.SLOT_STATE_INVALID
        )
    if existing.status == SlotStatus.COMPLETED:
        return RunnerResult(
            "stopped",
            RunnerClassification.COMPLETED_SLOT,
            existing.attempts,
            EXECUTION_IDENTITY,
        )
    if existing.status == SlotStatus.RUNNING:
        return _stop_existing(
            store,
            existing,
            now,
            RunnerClassification.INTERRUPTED_SLOT_UNCERTAIN,
        )
    classification = RunnerClassification(existing.classification)
    try:
        store.mark_stop(classification, now)
    except StateStoreError:
        return _state_failure(store, now, attempts=existing.attempts)
    return RunnerResult(
        "stopped", classification, existing.attempts, EXECUTION_IDENTITY
    )


def _identity_matches(state: SlotState) -> bool:
    return (
        state.product == PRODUCT
        and state.capability == CAPABILITY
        and state.reference == REFERENCE
        and state.release == _format_instant(RELEASE)
        and state.slot_identity == SLOT_IDENTITY
        and state.execution_identity == EXECUTION_IDENTITY
    )


def _state_semantics_valid(state: SlotState) -> bool:
    if not 0 <= state.attempts <= MAX_ATTEMPTS:
        return False
    if state.status == SlotStatus.COMPLETED:
        return (
            state.attempts >= 1
            and state.classification == RunnerClassification.CANONICAL_VERIFIED
        )
    if state.status == SlotStatus.RUNNING:
        return state.classification in {"admitted", "capturing", "retry_wait"}
    try:
        classification = RunnerClassification(state.classification)
    except ValueError:
        return False
    return classification not in {
        RunnerClassification.DISABLED,
        RunnerClassification.NOT_DUE,
        RunnerClassification.LATE_NO_CATCHUP,
        RunnerClassification.MISSED_NO_BACKFILL,
        RunnerClassification.CANONICAL_VERIFIED,
        RunnerClassification.COMPLETED_SLOT,
    }


def _stop_existing(
    store: SlotStateStore,
    existing: SlotState,
    now: datetime,
    classification: RunnerClassification,
) -> RunnerResult:
    stopped = _state(SlotStatus.STOPPED, existing.attempts, classification, now)
    try:
        store.replace(existing, stopped)
        store.mark_stop(classification, now)
    except StateStoreError:
        return _state_failure(store, now, attempts=existing.attempts)
    return RunnerResult(
        "stopped", classification, existing.attempts, EXECUTION_IDENTITY
    )


def _stop(
    store: SlotStateStore,
    current: SlotState,
    now: datetime,
    classification: RunnerClassification,
) -> RunnerResult:
    stopped = _state(SlotStatus.STOPPED, current.attempts, classification, now)
    try:
        store.replace(current, stopped)
        store.mark_stop(classification, now)
    except StateStoreError:
        return _state_failure(store, now, attempts=current.attempts)
    return RunnerResult("stopped", classification, current.attempts, EXECUTION_IDENTITY)


def _state_failure(
    store: SlotStateStore, now: datetime, *, attempts: int = 0
) -> RunnerResult:
    try:
        store.mark_stop(RunnerClassification.SLOT_STATE_INVALID, now)
    except StateStoreError:
        pass
    return RunnerResult(
        "stopped",
        RunnerClassification.SLOT_STATE_INVALID,
        attempts,
        EXECUTION_IDENTITY,
    )


def _state(
    status: SlotStatus,
    attempts: int,
    classification: str | RunnerClassification,
    at: datetime,
) -> SlotState:
    return SlotState(
        PRODUCT,
        CAPABILITY,
        REFERENCE,
        _format_instant(RELEASE),
        SLOT_IDENTITY,
        EXECUTION_IDENTITY,
        status,
        attempts,
        str(classification),
        _format_instant(at),
    )


def _trusted_local_time(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise StateStoreError("runner_clock_invalid")
    return value.astimezone(UTC)


def _format_instant(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError
    return parsed.astimezone(UTC)


def _state_bytes(state: SlotState) -> bytes:
    value = asdict(state)
    value["status"] = state.status.value
    return _json_bytes(value)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _stop_document(classification: str, at: datetime) -> dict[str, str]:
    return {
        "classification": str(classification),
        "execution_identity": EXECUTION_IDENTITY,
        "slot_identity": SLOT_IDENTITY,
        "stopped_at": _format_instant(at),
    }


def _write_descriptor(descriptor: int, content: bytes) -> None:
    with os.fdopen(os.dup(descriptor), "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            _write_descriptor(descriptor, content)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise StateStoreError("slot_state_write_failed") from exc


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
