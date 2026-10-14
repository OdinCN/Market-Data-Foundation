# Bounded offline periodic watchlist candidate R1

`PeriodicWatchlistSession` is a synchronous, non-production Python orchestration
candidate for the fixed pair `sh600519` / `sz000001`. It owns one fresh
`GoStockRealObservationIngress(offline_fixture_mode=True, capacity=3)` and uses
its existing ingestion and Snapshot Read semantics. No existing source, SDK,
runner, admission contract or consumer interface changes.

## Interface and sequence

```python
from market_data_foundation.candidates.periodic_watchlist import (
    PeriodicWatchlistSession,
)


def run_fixture_session(author_created_fixture_callback, *, monotonic, sleep, clock):
    session = PeriodicWatchlistSession(
        monotonic=monotonic,
        sleep=sleep,
        clock=clock,
    )
    return session.run(author_created_fixture_callback)
```

All hooks are required, trusted, caller-owned synchronous functions. There are
no default hooks or automatic execution paths. Construction does not invoke
them. Tests provide an in-memory virtual clock and sleep; no real waiting is
needed. `clock()` must return an aware UTC `datetime`; wall clocks must not
regress and accepted known clocks must strictly advance under ingress rules.
`monotonic()` must return finite, nonnegative native int/float seconds without
regression. Wall-clock age and monotonic duration are separate measurements.

`sample(SampleRequest) -> bytes` receives a frozen request with round 1–3,
the immutable two-security tuple, maximum ceiling 3, scheduled monotonic start
and cooperative deadline. It must return one complete, immutable UTF-8 envelope
using the existing ingress schema. Both `input_origin` and `clock_provenance`
must be `author_created_offline_fixture`; all rows, clocks and metadata must be
author-created. The tests show a self-contained fixture builder. Endpoint and
SDK fields are required envelope declarations, never instructions to execute.
No fixture is relabelled as real evidence.

The nominal start slots are `t0`, `t0+12s`, `t0+24s`:

1. Invoke the callback once, with a deadline ten seconds after the slot start.
2. Check its return time and native `bytes` type. Expired results and incomplete
   callbacks are rejected without ingestion. No generator, mutable byte buffer,
   deferred delivery or multi-round collection is accepted.
3. Immediately obtain local known time and hand those bytes to the same ingress.
4. Obtain one local Read clock and Read both fixed members by that exact batch
   identity, in fixed member order. Publish the Read pair only if both succeed.
5. Check the round duration and outcomes, then stop or wait until the next
   fixed slot. The preceding ingestion and both Reads finish before any next
   sampling callback. A terminal round causes no subsequent sleep or sample.

Each target stays fixed relative to `t0`; actual starts never shift subsequent
targets. A start in the inclusive interval `[target, target+0.5s]` is permitted
and recorded as the actual start. Each slot permits at most two sleep calls,
each requesting only its remaining time: an early first wake can receive one
additional wait. If still early after those calls, or more than 0.5 seconds
late, the session stops as `cadence_missed` before sampling. There is no Provider
retry, catch-up or extra sample. The first slot uses the initial monotonic
reading directly. This offline policy does not qualify live/provider polling
or continuous coverage. The ten-second deadline remains `target+10s`, so
lateness consumes the round's available time. The bound includes callback,
known-clock acquisition, ingress and Reads; equality is
permitted. It is checked after synchronous hooks return. A blocked callback,
sleep, clock or ingress cannot be forcibly interrupted here. Callbacks must
cooperate with the deadline. This is a bounded call-count candidate, not a hard
wall-time supervisor; it creates no thread, process, timer, daemon or service.

The ingress independently validates contiguous round/batch identity, fixed
membership, provenance, connection and budget invariants. After accepted
ingestion, orchestration reads only the already-validated envelope's
`max_rounds` header, so an earlier declared 1/2 cap stops before another callback.
The session never expands that cap and never implements a second quote parser,
deduplicator or current-state machine. Ingress rejection is terminal, even
though the underlying ingress alone might permit a corrected offline input.

## Reports and stopping

`run` returns an immutable `SessionReport(rounds, stop_reason)`. Each
`RoundReport` records the request, actual start and last measured finish,
accepted `Observation` if any, Read clock, complete `MemberRead` pair, ingress
outcome and terminal reason. A cancelled slot is `not_started`; an attempted
callback without an accepted observation is `rejected`. A failed clock reading
may leave timing fields absent or at their last successful measurement.

Observation retains the batch ID, acquisition ID, envelope SHA-256, provenance,
requested/received/frozen/known clocks and parser/source classifications.
`local_known_age` is Read time minus observation known time. `MemberRead.view.age`
is inherited local receipt age, potentially belonging to an earlier retained
value after failure or ignored ordering. `View.status`, `stale`, state acquisition
and `observed_at` remain unchanged from Snapshot rules. Duplicate samples can
continue and renew local receipt age; that does not prove source freshness.

Normal exhaustion returns `budget_exhausted`. Ingested `failed`, `late` and
`order_uncertain` outcomes are read and then stop as `observation_<outcome>`.
A non-observed Read (including stale/unavailable) stops as `read_not_observed`
if an ingress outcome has not already supplied the terminal reason. Callback
exceptions become `sample_timeout` or `sample_error`; non-bytes become
`frozen_bytes_required`. Other terminal classifications are `round_timeout`,
`ingress_rejected`, `read_error`, `cadence_missed`, `sleep_error`,
`monotonic_error` and `wall_clock_error`. Arbitrary hook exception messages and
tracebacks are not copied into reports. Underlying accepted Observation
classifications retain their existing meaning.

The session is consumed before any hook runs. Every second or reentrant `run`
raises `ContractError("session_already_used")`, including after rejection,
exhaustion or cancellation. Process-control exceptions such as KeyboardInterrupt
propagate while leaving the session consumed. No resume, reset, reconnect or
replacement-session operation exists. The API is serial and has no concurrent
caller contract. Reconstructing Python objects is not new Source authority.

Fail closed means no subsequent orchestration work after a terminal boundary;
it does not roll back an observation already accepted in memory. In particular,
a Read/clock failure or detected ingress/Read overrun can retain an Observation
without a successful pair or round. Callback timeout before ingress creates no
new unavailable Observation: there are no accepted failure-envelope bytes.
The report records this as controller failure, without fabricating source data.

## Static boundary and validation

There is no real/live mode, SourceBinding/Source Admission, provider selection,
SDK invocation, fallback, network, probe, DB/storage, persistence, deployment,
activation or consumer SLA. Hooks execute caller code; this Python API is not a
sandbox for untrusted callbacks and cannot prevent a caller from supplying a
side-effecting function. The candidate itself performs no Source side effects.
No runtime artifacts or quote data are written. Reports contain fixture quotes
in memory; they are not a publication or logging API.

The existing real ingress entrypoint and its provenance distinction remain
unchanged. Connecting a real callback, qualifying live scheduling,
enforcing a hard runtime deadline or using SourceBinding rights requires a
future separately authorized adapter gate. That adapter is absent. Prior real
ingress research evidence provides no admission and is not replayed as live
positive proof. Source event time is absent, source freshness is `UNKNOWN`, and
rights remain `UNKNOWN / EXCLUDED_FROM_PRODUCT_EFFECT`.

The focused tests cover three-round ordering with the actual ingress and
Snapshot, identity/age/status propagation, duplicates, early caps, stale and
unavailable Reads, late/equal requests, callback failures and incomplete returns,
deadline boundaries, Read overrun/failure, invalid envelope termination,
tiny/one-ULP lateness, inclusive 0.5-second starts, excess lateness STOP,
bounded early-wake waits and unreachable-slot STOP, target-based timeout under
lateness, clock errors, single-use/cancellation and side-effect guards.
Existing parser/state tests are reused through their implementation rather than
copied. No retained live raw input is used.

Run using the qualified uv-managed Python 3.14.8, without dependency sync:

```bash
UV_CACHE_DIR=/tmp/mdf-periodic-uv-cache uv run --offline --no-sync pytest -q tests/test_p5_periodic_watchlist.py
UV_CACHE_DIR=/tmp/mdf-periodic-uv-cache uv run --offline --no-sync ruff check .
UV_CACHE_DIR=/tmp/mdf-periodic-uv-cache uv run --offline --no-sync ruff format --check .
UV_CACHE_DIR=/tmp/mdf-periodic-uv-cache uv run --offline --no-sync python scripts/check_repository_hygiene.py
UV_CACHE_DIR=/tmp/mdf-periodic-uv-cache uv run --offline --no-sync pytest -q
```

Default pytest excludes live-provider and PostgreSQL integration tests. Hygiene
also needs an explicit scan of these three new files while they are untracked.
Any sandbox ptrace failure remains NONPASS; independent host QA and review
belong to PM. This note records interface and limits, with delivery identity,
validation outcomes and acceptance decisions maintained externally.
