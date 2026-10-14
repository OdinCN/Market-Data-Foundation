"""Static Go/TDX port contract and offline-only adapter; no Source effects."""

import math
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from market_data_foundation.candidates.go_stock_real_observation_ingress import (
    ENDPOINT,
    MAX_ROUNDS,
    MAX_WIRE_BYTES,
    OFFLINE_ORIGIN,
    REAL_ORIGIN,
)
from market_data_foundation.candidates.periodic_watchlist import (
    ROUND_TIMEOUT_SECONDS,
    PeriodicWatchlistSession,
    SampleRequest,
    SessionReport,
)
from market_data_foundation.candidates.stock_snapshot import MEMBERS, SDK_RELEASE
from market_data_foundation.canonical.model import ContractError, require


@dataclass(frozen=True)
class SourceIdentity:
    """Declared identity, never source authentication or an execution binding."""

    sdk_release: str = SDK_RELEASE
    endpoint: str = ENDPOINT
    securities: tuple[str, str] = MEMBERS
    input_origin: str = OFFLINE_ORIGIN
    clock_provenance: str = OFFLINE_ORIGIN


FIXTURE_IDENTITY = SourceIdentity()


@dataclass(frozen=True)
class FrozenBatch:
    """Caller-authored ingress envelope, unchanged; not a raw Go response encoder."""

    identity: SourceIdentity
    round: int
    wire: bytes


class FrozenBatchSourcePort(Protocol):
    """Trusted caller-owned synchronous port, not implemented as a real driver.

    identity must remain fixed before/after every open/sample. open means only
    fixture readiness. sample must cooperate with the request deadline; close
    is attempted once even after failure. No ACK operation exists in R1.
    A future real driver must preserve real provenance and needs separate AO
    authority and a real consumer path; this adapter rejects real origin.
    """

    @property
    def identity(self) -> SourceIdentity: ...

    def open(self, *, deadline_monotonic: float) -> None: ...

    def sample(self, request: SampleRequest) -> FrozenBatch: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class SampleCall:
    round: int
    callback_calls: int
    classification: str
    returned_identity: SourceIdentity | None
    envelope_sha256: str | None


@dataclass(frozen=True)
class PortReport:
    fixture_readiness: str
    open_calls: int
    samples: tuple[SampleCall, ...]
    termination: str | None
    close_calls: int
    close_state: str
    close_error: str | None
    provider_state: str = "NOT_CONNECTED_TO_PROVIDER"
    actual_tcp_connects: int = 0
    actual_getquote_calls: int = 0
    provider_observation: str = "NOT_OBSERVED"
    go_ack: str = "DEFERRED_NOT_IMPLEMENTED"


@dataclass(frozen=True)
class AdapterReport:
    session: SessionReport
    port: PortReport


class _Stop(Exception):
    """Only adapter-authored classifications may escape into reports."""


def _identity(value: object) -> None:
    if type(value) is not SourceIdentity:
        raise _Stop("source_identity_invalid")
    if value.input_origin == REAL_ORIGIN:
        raise _Stop("real_origin_rejected")
    if value != FIXTURE_IDENTITY:
        raise _Stop("source_identity_drift")


class GoTdxWatchlistSourceAdapter:
    """Single-use offline sample callback wrapped around the existing session.

    Construction invokes no hooks. run(session) owns cleanup and passes sample
    directly to session.run. Supply the same monotonic domain to both objects.
    Hooks/ports are trusted caller code: this API cannot sandbox their effects
    or forcibly interrupt a blocking hook. It supplies no network/SDK driver.
    """

    def __init__(self, port: FrozenBatchSourcePort, *, monotonic: Callable[[], float]):
        require(callable(monotonic), "monotonic_hook_required")
        self._port = port
        self._monotonic = monotonic
        self._last_now: float | None = None
        self._used = self._active = self._sampling = self._stopped = False
        self._readiness = "NOT_READY"
        self._open_calls = self._close_calls = 0
        self._close_state = "NOT_CLOSED"
        self._close_error: str | None = None
        self._termination: str | None = None
        self._calls: list[SampleCall] = []

    @property
    def report(self) -> PortReport:
        return PortReport(
            self._readiness,
            self._open_calls,
            tuple(self._calls),
            self._termination,
            self._close_calls,
            self._close_state,
            self._close_error,
        )

    def _now(self) -> float:
        try:
            value = self._monotonic()
            if type(value) not in (int, float):
                raise ValueError
            value = float(value)
            if not math.isfinite(value) or value < 0:
                raise ValueError
            if self._last_now is not None and value < self._last_now:
                raise ValueError
        except Exception:
            raise _Stop("monotonic_error") from None
        self._last_now = value
        return value

    def _deadline(self, request: SampleRequest) -> None:
        if self._close_calls:
            raise _Stop("adapter_closed")
        now = self._now()
        if now < request.scheduled_monotonic:
            raise _Stop("request_not_due")
        if now > request.deadline_monotonic:
            raise _Stop("deadline_expired")

    def sample(self, request: SampleRequest) -> bytes:
        """Exact Periodic callback shape; no decoding, rewriting or implicit ACK."""
        require(self._active and not self._sampling, "adapter_not_running_or_reentrant")
        require(not self._stopped, "adapter_stopped")
        self._sampling = True
        calls = 0
        classification = "aborted"
        returned_identity = digest = None
        try:
            if (
                type(request) is not SampleRequest
                or type(request.round) is not int
                or request.round != len(self._calls) + 1
                or not 1 <= request.round <= MAX_ROUNDS
                or type(request.max_rounds) is not int
                or request.max_rounds != MAX_ROUNDS
                or request.securities != MEMBERS
                or any(
                    type(v) not in (int, float) or not math.isfinite(v) or v < 0
                    for v in (
                        request.scheduled_monotonic,
                        request.deadline_monotonic,
                    )
                )
                or request.deadline_monotonic
                != request.scheduled_monotonic + ROUND_TIMEOUT_SECONDS
            ):
                raise _Stop("sample_request_invalid")
            self._deadline(request)
            _identity(self._port.identity)
            self._deadline(request)
            if self._open_calls == 0:
                self._open_calls = 1
                self._port.open(deadline_monotonic=request.deadline_monotonic)
                self._deadline(request)
                _identity(self._port.identity)
                self._readiness = "FIXTURE_PORT_READY"
            self._deadline(request)
            calls = 1
            batch = self._port.sample(request)
            self._deadline(request)
            _identity(self._port.identity)
            self._deadline(request)
            if type(batch) is not FrozenBatch:
                raise _Stop("frozen_batch_required")
            _identity(batch.identity)
            if type(batch.round) is not int or batch.round != request.round:
                raise _Stop("batch_round_mismatch")
            if (
                type(batch.wire) is not bytes
                or not 0 < len(batch.wire) <= MAX_WIRE_BYTES
            ):
                raise _Stop("frozen_bytes_bound")
            returned_identity = batch.identity
            digest = sha256(batch.wire).hexdigest()
            classification = "frozen_batch_returned"
            return batch.wire
        except _Stop as exc:
            classification = str(exc)
            raise ContractError(classification) from None
        except TimeoutError:
            classification = "fixture_timeout"
            raise TimeoutError(classification) from None
        except ConnectionError:
            classification = "fixture_disconnected"
            raise ContractError(classification) from None
        except Exception:
            classification = "fixture_port_error"
            raise ContractError(classification) from None
        finally:
            # Invalid requests are never copied into an observation/report.
            n = request.round if type(request) is SampleRequest else 0
            self._calls.append(
                SampleCall(
                    n if type(n) is int and 1 <= n <= MAX_ROUNDS else 0,
                    calls,
                    classification,
                    returned_identity,
                    digest,
                )
            )
            self._sampling = False
            if classification != "frozen_batch_returned":
                self._stopped = True
                self._termination = classification

    def close(self) -> PortReport:
        """Attempt cleanup once; close failure never retries or adds sample budget."""
        if self._close_calls:
            return self.report
        self._stopped = True
        self._close_calls = 1
        self._close_state = "close_failed"
        self._close_error = "close_aborted"
        try:
            self._port.close()
        except TimeoutError:
            self._close_error = "close_timeout"
        except ConnectionError:
            self._close_error = "close_disconnected"
        except Exception:
            self._close_error = "close_error"
        else:
            self._close_state = "closed"
            self._close_error = None
        return self.report

    def run(self, session: PeriodicWatchlistSession) -> AdapterReport:
        """Run one existing offline session; always close, including cancellation.

        Process-control exceptions propagate after cleanup. report remains
        available with safe classifications; no raw hook messages are retained.
        """
        require(not self._used and not self._close_calls, "adapter_already_used")
        require(
            isinstance(session, PeriodicWatchlistSession), "periodic_session_required"
        )
        self._used = self._active = True
        try:
            result = session.run(self.sample)
            if self._termination is None:
                self._termination = result.stop_reason
        except BaseException as exc:
            if self._termination is None:
                self._termination = (
                    "session_error" if isinstance(exc, Exception) else "aborted"
                )
            raise
        finally:
            self._active = False
            self.close()
        return AdapterReport(result, self.report)
