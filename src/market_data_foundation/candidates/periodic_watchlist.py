"""One synchronous, offline-fixture watchlist session; no Source execution."""

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from market_data_foundation.candidates.go_stock_real_observation_ingress import (
    MAX_ROUNDS,
    GoStockRealObservationIngress,
    Observation,
)
from market_data_foundation.candidates.stock_snapshot import MEMBERS, View
from market_data_foundation.canonical.model import require

PERIOD_SECONDS = 12
ROUND_TIMEOUT_SECONDS = 10
START_LATENESS_SECONDS = 0.5
MAX_SLOT_WAITS = 2


@dataclass(frozen=True)
class SampleRequest:
    round: int
    scheduled_monotonic: float
    deadline_monotonic: float
    securities: tuple[str, str] = MEMBERS
    max_rounds: int = MAX_ROUNDS


@dataclass(frozen=True)
class MemberRead:
    security: str
    view: View


@dataclass(frozen=True)
class RoundReport:
    request: SampleRequest
    started_monotonic: float | None
    finished_monotonic: float | None
    observation: Observation | None
    read_at: datetime | None
    reads: tuple[MemberRead, ...]
    outcome: str
    stop_reason: str | None

    @property
    def local_known_age(self) -> timedelta | None:
        """Visibility age at Read, distinct from View.age since local receipt."""
        if self.observation is None or self.read_at is None:
            return None
        return self.read_at - self.observation.record.clocks.known


@dataclass(frozen=True)
class SessionReport:
    rounds: tuple[RoundReport, ...]
    stop_reason: str


class _Stop(Exception):
    """Only module-authored classifications cross into reports."""


class PeriodicWatchlistSession:
    """Single use, serial, max-three rounds through one privately owned ingress.

    All execution hooks are explicit. sample must synchronously return frozen
    fixture-envelope bytes and cooperate with its deadline. No hook is invoked
    by construction. There is no real-mode flag, background worker or reset.
    """

    def __init__(
        self,
        *,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
        clock: Callable[[], datetime],
        ttl: timedelta = timedelta(seconds=30),
    ):
        require(
            all(callable(hook) for hook in (monotonic, sleep, clock)),
            "execution_hooks_required",
        )
        self._monotonic = monotonic
        self._sleep = sleep
        self._clock = clock
        self._ingress = GoStockRealObservationIngress(
            offline_fixture_mode=True, capacity=MAX_ROUNDS, ttl=ttl
        )
        self._used = False
        self._last_monotonic: float | None = None
        self._last_wall: datetime | None = None

    def _now(self) -> float:
        try:
            value = self._monotonic()
            require(type(value) in (int, float), "monotonic_shape")
            value = float(value)
            require(math.isfinite(value) and value >= 0, "monotonic_shape")
            require(
                self._last_monotonic is None or value >= self._last_monotonic,
                "monotonic_regressed",
            )
        except Exception:
            raise _Stop("monotonic_error") from None
        self._last_monotonic = value
        return value

    def _wall(self) -> datetime:
        try:
            value = self._clock()
            require(type(value) is datetime, "wall_clock_shape")
            require(value.utcoffset() == timedelta(0), "utc_clock_required")
            require(
                self._last_wall is None or value >= self._last_wall,
                "wall_clock_regressed",
            )
        except Exception:
            raise _Stop("wall_clock_error") from None
        self._last_wall = value
        return value

    def run(self, sample: Callable[[SampleRequest], bytes]) -> SessionReport:
        """Consume the session even on failure; never retry, catch up or redial.

        Fixed slots are at 0/12/24 seconds, with up to 0.5 seconds of lateness.
        At most two remaining-time waits may reach each slot; otherwise stop.
        Timeouts are detected after synchronous
        hooks return; this API cannot interrupt blocking or malicious hooks.
        """
        require(not self._used, "session_already_used")
        self._used = True
        reports = []
        try:
            t0 = self._now()
        except _Stop as exc:
            return SessionReport((), str(exc))

        for n in range(1, MAX_ROUNDS + 1):
            due = t0 + (n - 1) * PERIOD_SECONDS
            request = SampleRequest(n, due, due + ROUND_TIMEOUT_SECONDS)
            started = finished = None
            observation = read_at = None
            reads = ()
            reason = None
            try:
                if not (math.isfinite(due) and request.deadline_monotonic > due):
                    raise _Stop("monotonic_error")
                now = due if n == 1 else self._now()
                for _ in range(MAX_SLOT_WAITS):
                    if now >= due:
                        break
                    try:
                        self._sleep(due - now)
                    except Exception:
                        raise _Stop("sleep_error") from None
                    now = self._now()
                if now < due or now > due + START_LATENESS_SECONDS:
                    raise _Stop("cadence_missed")
                started = now
                try:
                    wire = sample(request)
                except TimeoutError:
                    raise _Stop("sample_timeout") from None
                except Exception:
                    raise _Stop("sample_error") from None
                finished = self._now()
                if finished > request.deadline_monotonic:
                    raise _Stop("round_timeout")
                if type(wire) is not bytes:
                    raise _Stop("frozen_bytes_required")
                known = self._wall()
                try:
                    observation = self._ingress.ingest(wire, known_at=known)
                except Exception:
                    raise _Stop("ingress_rejected") from None
                read_at = self._wall()
                try:
                    # Publish the pair only when both exact-batch reads succeed.
                    reads = tuple(
                        MemberRead(
                            security,
                            self._ingress.read(
                                security, at=read_at, batch_id=observation.batch_id
                            ),
                        )
                        for security in MEMBERS
                    )
                except Exception:
                    raise _Stop("read_error") from None
                finished = self._now()
                if finished > request.deadline_monotonic:
                    raise _Stop("round_timeout")
                if observation.record.outcome not in ("sample", "duplicate"):
                    raise _Stop(f"observation_{observation.record.outcome}")
                if any(item.view.status != "observed" for item in reads):
                    raise _Stop("read_not_observed")
                # Ingress has validated the whole bounded envelope. Inspect only
                # its declared cap to avoid requesting beyond an earlier 1/2 cap;
                # quote parsing and all state transitions stay in the ingress.
                if n == json.loads(wire)["max_rounds"]:
                    reason = "budget_exhausted"
            except _Stop as exc:
                reason = str(exc)
            reports.append(
                RoundReport(
                    request,
                    started,
                    finished,
                    observation,
                    read_at,
                    reads,
                    observation.record.outcome
                    if observation is not None
                    else "not_started"
                    if started is None
                    else "rejected",
                    reason,
                )
            )
            if reason is not None:
                return SessionReport(tuple(reports), reason)

        # The ingress contract caps every accepted envelope at MAX_ROUNDS.
        raise AssertionError("unreachable_session_budget")
