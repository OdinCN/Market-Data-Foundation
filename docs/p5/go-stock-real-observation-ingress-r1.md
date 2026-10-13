# Go stock Real Observation Ingress R1

Transaction: `MDF-P5-GO-STOCK-REAL-OBSERVATION-INGRESS-R1`. This is a separate
owner-local bounded research candidate on baseline
`0721717711d2c3d2a5b8dd8d99de3857b4b9a064`. The accepted `stock_snapshot.py`
and `go_stock_observation_bridge.py` remain unchanged. There is no Phase,
SourceBinding, admission, consumer API, durable state, deployment, adoption,
commit, push or CI effect. The owner's lawful-use statement suffices for the
bounded research; production redistribution rights require a separate decision.

## Implementation boundary

`GoStockRealObservationIngress` receives immutable JSON bytes from the separate
Go probe and stamps ingress `known` immediately on entry, before JSON decoding.
It never imports, launches, installs or connects an SDK. Importing this module
has no live effect. There is no live CLI in the Python module.

The candidate delegates typed parsing and state transitions to the existing
`StockSnapshotCandidate.observe`, supplying `go-real-decoded-shape-r1` explicitly.
That shared machine internally calls the existing function named
`parse_synthetic`; this is reuse of its decoded representation checks, not a
synthetic provenance claim. The new envelope and Observation retain their own
real origin. The existing inactive synthetic Definition is not admitted or
revised. No real quote passes through the synthetic bridge or its encoder.

One instance accepts at most three batches in one fixed connection session,
with contiguous round identities, an invariant maximum and connection metadata.
No reconnect, parser correction or replacement session API exists here. History
is bounded (default three; underlying capacity 2–256), and the lifetime batch
counter remains bounded after eviction. Dropping/recreating an instance cannot
confer new live authority. The Go launch claim separately blocks repeat capture.

## Exact envelope

UTF-8 JSON, at most 32,768 bytes, exactly these top-level fields; duplicate keys,
nonfinite constants and exponent overflow are rejected throughout the document,
including unprojected SDK fields. Envelope errors reject without changing state,
history, observation age or acquisition ordinal.

| Field | Required value / meaning |
| --- | --- |
| `schema` | `mdf.candidate.go-stock.real-observation.r1` |
| `input_origin` | `real_go_sdk_decoded_quote` in real mode |
| `clock_provenance` | `local_probe_wall_clock_utc` in real mode |
| `sdk_release` | `injoyai/tdx v0.0.87@51c1ed51020fa43727300339dd92cbecf06a7d2e` |
| `endpoint` | `139.9.52.158:7709`, from prior 2026-10-12 retained ledger |
| `securities` | Exactly `["sh600519", "sz000001"]`, in this order |
| `batch_id`, `round` | `go-real-r1-1` through `go-real-r1-3`, matching integer round |
| `max_rounds` | Integer 1–3, invariant for the session |
| `connection` | Exactly `requested`, `received`, `elapsed_ns`, `redial:false`, `attempts:1` |
| `clocks` | Exactly `requested`, `received`, `frozen`; explicit aware UTC strings |
| `quote_elapsed_ns` | Separate monotonic quote-call duration in nanoseconds |
| `source_event_clock` | `UNKNOWN` |
| `kline_time_semantics` | `SDK_CLIENT_SAMPLED` |
| `server_time_semantics` | `OPAQUE` |
| `units` | `["integer_li", "sdk_volume_count_UNKNOWN", "sdk_level_count_UNKNOWN"]` |
| `failure` | `null`, `timeout`, `disconnected` or `source_error`; classification only |
| `rows` | SDK decoded pair, or `null` on source failure |

Connection and call durations must be integer 0–10,000,000,000 ns, excluding
booleans. They are independent monotonic measurements; they are not derived
from wall-clock differences. Connection wall clocks must precede quote request.
Quote clocks obey `requested <= received <= frozen <= known`, and known time
must strictly advance across accepted records. Wall-clock regressions reject
rather than being silently repaired. Python datetime retains microseconds;
original nanosecond timestamp strings remain only in ignored capture evidence.

Clock boundaries are precise:

- Connection requested/received surround the single TCP dial, excluding the SDK
  handshake. The handshake follows successful dial before `GetQuote`.
- Quote requested is sampled immediately before the call; received immediately
  after `GetQuote` returns the decoded result or a classified failure.
- Frozen is sampled after decoded rows have been JSON serialized to immutable
  bytes. It describes the row payload boundary, not completion of writing the
  later envelope file. On failure the frozen payload is JSON `null`.
- Known is sampled by Python on receipt of the immutable envelope bytes. When
  loading a retained file, it is the later file-ingress clock, not the TCP
  receipt clock. Callers cannot supply a known clock in real mode.

Provenance declarations are structural checks, not cryptographic attestation
of a running provider, authenticated exchange source, trustworthy host clock or
lawful use. `envelope_sha256` fingerprints received bytes in Observation; it
does not authenticate them. Evidence and external host identity checks remain
necessary. The decoder intentionally permits retained real evidence ingestion;
the live session gate belongs to the separate probe and AO execution decision.

## State, quality and unknowns

The shared parser validates exactly two distinct securities, aware SDK sampled
times, five levels per side, side booleans, nonnegative integer fields within
SDK bounds, and bounded opaque ServerTime. It sorts the pair into canonical
member order and constructs immutable typed quotes. A malformed member records
one atomic failed pair, retains both prior quotes and their observation ages,
and marks both unavailable. It never partially updates the good member.
These are representation checks; they do not prove economic consistency, book
ordering, OHLC coherence, tradability or economic lot/share units.

Successful changed content yields `sample`; matching economic content yields
`duplicate`. Client sampled time, ServerTime and Active counters are excluded
from economic deduplication. A duplicate renews local receipt age; this does
not establish exchange freshness. Prior requested clocks yield `late`, equal
requested clocks yield `order_uncertain`; neither replaces current state.
Exact and as-of reads address only retained, already-known records. TTL is
strictly `age > ttl` (default 30 seconds, bounded to five minutes); age begins
at local quote received time, not known time or SDK sampled time.

`timeout`/`disconnected` failures use the existing unavailable transition.
`source_error` also uses its internal `disconnected` transition to mark state
unavailable, but Observation.classification and source_failure remain
`source_error`. The underlying Record classification for that transition is
`disconnected`; it is not evidence of physical socket disconnection. Any source
failure is terminal for this session, with no retry. Offline quality-error
fixtures can exercise a subsequent round; the live probe never automatically
retries a failed SDK call. Empty state is `unknown`; failed state is
`unavailable`; successful state is `observed` or `stale`. A failed sample marks
an observation gap. Silent missing exchange events, gap duration, source
continuity, exchange ordering and source freshness remain UNKNOWN.

`Kline.Time` comes from pinned SDK `DecodeKline`'s `time.Now()`. ServerTime is a
string rendering of an opaque decoded protocol integer. Neither is an exchange
event clock. Integer-li conversion is exact representation arithmetic; volume
and level counts have unproven economic trading units. Source qualification,
authorization/admission, redistribution rights and source freshness are not
auto-proven. The inherited View retains `rights = UNKNOWN /
EXCLUDED_FROM_PRODUCT_EFFECT` and `source_event_at = None`.

## Offline validation and build

All new test fixtures are invented in the new test file, including clocks and
endpoint declarations. `offline_fixture_mode=True` requires both origin and
clock provenance `author_created_offline_fixture` and an explicit UTC
`known_at`. Fixture Observations retain that label for known provenance too.
Real mode rejects those bytes; fixture mode rejects real provenance. No test
reads retained provider material, executes the Go probe or uses a network.
There are no new live pytest tests; any future such test must be separately
opted in and marked `live_provider`.

```bash
UV_CACHE_DIR=/tmp/mdf-ingress-uv-cache uv run --offline --no-sync pytest -q tests/test_p5_go_stock_real_observation_ingress.py
UV_CACHE_DIR=/tmp/mdf-ingress-uv-cache uv run --offline --no-sync pytest -q
UV_CACHE_DIR=/tmp/mdf-ingress-uv-cache uv run --offline --no-sync ruff check .
UV_CACHE_DIR=/tmp/mdf-ingress-uv-cache uv run --offline --no-sync ruff format --check .
UV_CACHE_DIR=/tmp/mdf-ingress-uv-cache uv run --offline --no-sync python scripts/check_repository_hygiene.py
```

The separate source is staged at ignored
`artifacts/mdf-p5-go-real-ingress-20261013/go_capture_once.go`, for AO host copy
to owner-local `transactions/MDF-P5-GO-STOCK-REAL-OBSERVATION-INGRESS-R1/`.
The sandbox does not write that owner-local location. Offline build uses the
existing pinned module, cache and Go 1.25.3, without changing dependencies:

```bash
cd /var/tmp/mdf-tdx-live-20261011/release-v0.0.87
env GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local GOWORK=off \
  GOMODCACHE=/var/tmp/mdf-tdx-live-20261011/pkg/mod \
  GOCACHE=/tmp/mdf-go-real-ingress-cache \
  /var/tmp/mdf-tdx-live-20261011/toolchain/go/bin/go build -mod=readonly -trimpath \
  -o /srv/market-data-foundation/artifacts/mdf-p5-go-real-ingress-20261013/go_capture_once \
  /srv/market-data-foundation/artifacts/mdf-p5-go-real-ingress-20261013/go_capture_once.go
```

Build does not execute Go package initializers or the provider. Build metadata
lists the local SDK dependency as `(devel)`; the explicit source HEAD and clean
tracked-tree checks, rather than a release string alone, identify the pin.
The pinned module has pre-existing untracked `cmd/` material; it is not imported
by this file build. No Go/SDK installation, go.mod, go.sum or uv.lock change is
part of this candidate.

## Separate live opt-in and use

During Shanghai lunch 11:30–13:00 there must be no provider requests. Only an
explicit AO/WebChat follow-up with a fresh time check in the 2026-10-13
13:00–15:00 Asia/Shanghai session can authorize this one launch. A token supplied
by a script is not a substitute for that follow-up. No scheduling, automated
review invocation, pytest, import or preflight may launch the live driver.
If host or end-to-end gates cannot be qualified, stop the live substep and
return offline results. No failed live launch may be repeated.

The binary independently rejects a missing opt-in, a non-UTC/future/stale
(over 60 seconds) fresh check, wrong date/session, wrong host/toolchain, drifted
Git HEAD/origin/main/branch or tracked inputs, missing/unexpected candidate
paths, nonzero owner-local live count, wrong active transaction or budget,
non-ignored/symlinked evidence directories, prior batch files or a launch claim.
AO must separately verify current source/binary identity, owner counter and
actual request history before executing; the probe cannot authenticate AO
authority or determine past unrecorded requests.

After those checks and only after the explicit follow-up, the host may invoke:

```bash
artifacts/mdf-p5-go-real-ingress-20261013/go_capture_once \
  --live-opt-in MDF-P5-GO-STOCK-REAL-OBSERVATION-INGRESS-R1 \
  --fresh-gate-utc "$AO_FRESH_UTC_CHECK" --max-rounds 3
```

`AO_FRESH_UTC_CHECK` is the actual fresh AO time check, in RFC3339 UTC ending
`Z`. This command is documentation, not a live execution performed here.

The probe creates an exclusive fixed `launch.claim` before dialing. Do not
remove it to retry. It uses one fixed-address TCP connection, a three-second
dial timeout, SDK redial disabled and serialized writes with no retry. Before
the SDK invokes OnConnected, the custom dial callback replaces its 30-second
heartbeat handler with the same handshake write alone. The SDK reader remains
necessary for responses; there is no timer writer, automatic GetQuote retry,
sleep loop or background quote poll. At most three calls request only the two
fixed stocks. The whole connection is capped at ten seconds and session end;
the socket deadline, close timer and per-write session gate bound writes.
Classified SDK failures stop the loop. No ETF or other capability is invoked.

Each immutable decoded batch is written exclusively with mode 0600 to ignored
`batch-1.json` through `batch-3.json` in the same artifacts directory. This is
bounded research evidence, not a recording service or durable Current-State
store. Raw decoded prices must never be copied into Git, docs or public logs.
SDK global logs are discarded, client logging is disabled and SDK stack output
is suppressed; stdout contains only classifications, counts and clocks.
Source errors are classified by exception type/identity without raw messages;
an unrecognized SDK error remains `source_error`. A stop diagnostic retains the
attempted SDK call count even if later serialization or evidence writing fails.
An attempted call is not proof of a transmitted or answered provider request;
the host must reconcile partial evidence before any further authority decision.

In a separate Python process, after capture, the following ingests retained
envelopes and prints metadata only. It never calls a provider. Known clocks
are sampled at this later ingress; Current-State disappears at process exit:

```python
import json
from pathlib import Path
from market_data_foundation.candidates.go_stock_real_observation_ingress import (
    GoStockRealObservationIngress,
)
from market_data_foundation.canonical.model import ContractError

candidate = GoStockRealObservationIngress()
directory = Path("artifacts/mdf-p5-go-real-ingress-20261013")
for path in sorted(directory.glob("batch-[1-3].json")):
    try:
        observation = candidate.ingest(path.read_bytes())
    except ContractError as error:
        print(json.dumps({"status": "INGRESS_REJECTED", "classification": str(error)}))
        break
    record = observation.record
    print(
        json.dumps(
            {
                "batch_id": observation.batch_id,
                "outcome": record.outcome,
                "classification": observation.classification,
                "known": record.clocks.known.isoformat(),
                "source_event_clock": observation.source_event_clock,
                "statuses": [
                    candidate.read(s, at=record.clocks.known).status
                    for s in ("sh600519", "sz000001")
                ],
            }
        )
    )
```

## Observed delivery status

The final offline probe build succeeded in the Codex workspace-write sandbox
with Go 1.25.3. The new focused deterministic suite observed 101 passing tests
on uv-managed Python 3.14.8. The full default deterministic run observed
**2,287 passed, 4 failed, 17 deselected**, with three existing WSGI warnings.
The four failures are the three parameterizations of
`test_child_cannot_execute_denied_connection_or_overrun` and
`test_trace_follows_threads_without_provider_access` in the existing
`tests/test_tdx_bounds.py`. They fail before child execution in the ptrace
preexec hook. A separate network-free sandbox diagnostic observed
`PTRACE_TRACEME = -1`, errno 1 (`EPERM`). No accepted test or supervisor was
changed. Full deterministic host validation remains **NOT RUN** here; the
sandbox suite is not reported as fully passing.

Repository-wide Ruff check and format check succeeded (136 files already
formatted). Existing repository hygiene succeeded for 145 tracked files;
an additional scan applied the same forbidden path, symlink and sensitive
content checks to all three untracked candidates. Tracked product bytes,
HEAD/origin/main and tracked pinned SDK bytes remain unchanged. Exactly the
three permitted candidate files are untracked; the probe source and binary
are ignored artifacts. No launch claim or capture batch was created.

Live SDK quote batches executed in this transaction:
**0**; actual live decoded quote count: **0**. No requested/received/frozen/known
live quote clocks exist. Host live execution and the real capture-to-ingress
path remain unobserved. No professional review PASS, AO acceptance, Git
delivery, deployment or admission is claimed. Final artifact identity belongs
in a later external postflight/status record, not in this report's own content.
