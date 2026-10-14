# Go/TDX watchlist Source Adapter Port R1

Transaction: `MDF-P5-WATCHLIST-SOURCE-ADAPTER-R1`. This delivers a static,
nonproduction Source Port contract and an explicitly injected **author-created
offline fixture** execution seam. It is offline compatible with the shipped
`PeriodicWatchlistSession`; the real source is **NOT connected**. P5/P7 remain
DEFINED ONLY / NOT AUTHORIZED. No Source effect, real driver, SourceBinding,
Source/Revision Admission or new live budget is supplied.

The only product additions are this document,
`src/market_data_foundation/candidates/go_tdx_watchlist_source_adapter.py` and
`tests/test_p5_go_tdx_watchlist_source_adapter.py`. Snapshot, Ingress and Periodic
implementations remain unchanged. There are no dependency changes.

## Port and composition

`SourceIdentity` is a frozen declaration of exactly:

- SDK `injoyai/tdx v0.0.87@51c1ed51020fa43727300339dd92cbecf06a7d2e`;
- endpoint `139.9.52.158:7709`;
- ordered members `("sh600519", "sz000001")`;
- `input_origin` and `clock_provenance`, both
  `author_created_offline_fixture` for this executable adapter.

`FrozenBatch(identity, round, wire)` carries one immutable native `bytes`
envelope, using the existing ingress schema, bounded to 32,768 bytes. The
identity and round are the Port's declarations; the ingress independently
validates the envelope. Neither declaration nor a SHA-256 authenticates source
provenance. The adapter never encodes SDK rows, decodes quotes, edits envelope
headers, relabels input, or substitutes an origin. Every fixture row, clock,
numeric value and connection field must be invented by its author. Real R2
quotes and retained SDK responses must never be repackaged as fixtures.

`FrozenBatchSourcePort` is a structural Python Protocol, with no concrete real
Go driver or process bridge:

| Member | Contract |
| --- | --- |
| `identity` | Fixed `SourceIdentity`; inspected before/after fixture open/sample. |
| `open(*, deadline_monotonic) -> None` | Caller-owned fixture readiness only; at most one attempt. Return before the supplied cooperative deadline. |
| `sample(SampleRequest) -> FrozenBatch` | One synchronous callback per attempted round, no retry or replacement. Returns a complete frozen envelope. |
| `close() -> None` | One cleanup attempt, including after open/sample failure or cancellation. No redial, extra sample or retry on close failure. |

Construction invokes no Port or clock hooks. `GoTdxWatchlistSourceAdapter.run`
accepts one existing Periodic session and supplies its public `sample` method
with precisely the existing `sample(SampleRequest) -> bytes` shape. Cleanup is
owned by `run`; `sample` outside that run is rejected. A second/reentrant run
or sampling after terminal failure is rejected. Reports remain inspectable on
the adapter after process-control exceptions.

```python
from market_data_foundation.candidates.go_tdx_watchlist_source_adapter import (
    GoTdxWatchlistSourceAdapter,
)
from market_data_foundation.candidates.periodic_watchlist import (
    PeriodicWatchlistSession,
)


def run_offline_demo(author_created_fixture_port, *, monotonic, sleep, clock):
    session = PeriodicWatchlistSession(
        monotonic=monotonic,
        sleep=sleep,
        clock=clock,
    )
    adapter = GoTdxWatchlistSourceAdapter(
        author_created_fixture_port,
        monotonic=monotonic,
    )
    return adapter.run(session)
```

The test file contains a self-contained, caller-created `FixturePort`, invented
Go decoded pair/envelope and virtual clocks. There is no default Port, live
factory, CLI, SDK import, socket, subprocess, Go probe or file I/O in the
adapter. Caller Port and all hooks are trusted code. This API cannot sandbox
arbitrary callback side effects, prove that an author did not copy real data,
or terminate a blocked/malicious callback. Passing a callback that connects to
a provider violates the offline contract; zero counters do not audit such code.

## Timing, ordering and terminal boundaries

Both objects must receive the same monotonic clock domain. Periodic retains its
existing slots at 0/12/24 seconds, start lateness tolerance 0.5 seconds, up to
two waits per slot, and ten-second target-based round deadline. Adapter open is
lazy within the first sample callback and consumes that same round deadline;
it adds no separate connection budget. Adapter checks native finite,
nonnegative, nonregressing monotonic values and expiry before readiness,
after readiness, before sampling and after sampling. Deadline equality is
allowed. Checks occur after synchronous hooks return, not through an external
wall-time supervisor. No new thread, timer or background task exists.

The serial path is:

1. Validate request/Port identity and fixture readiness; invoke Port sample once.
2. Check return deadline, unchanged identity, matching wrapper round and frozen
   byte bound. Return the **same bytes object**, without rewriting.
3. Periodic stamps known and immediately invokes its one existing
   `GoStockRealObservationIngress(offline_fixture_mode=True)` instance.
4. That ingress validates the envelope, invokes the shared Snapshot parser and
   state machine, and classifies changes/duplicates/failures.
5. Periodic reads both members by the exact accepted batch, then stops or waits
   for the next slot. Both Reads complete before any next sampling invocation.

This demonstrates the local sequence that a future Read/ACK barrier would need.
**Go ACK is DEFERRED / NOT IMPLEMENTED.** There is no ACK hook, transport ACK,
Go consumption receipt, Go backpressure or implicit ACK on Python return. Tests
assert actual Python ingest/Read ordering and equality to a direct fixture
callback through the existing session, without inventing a Go ACK event.

At most three Port sample invocations occur in one adapter/session; an earlier
1/2 cap in an accepted envelope remains enforced by the existing Periodic and
Ingress. An error never triggers retry, catch-up, replacement sampling or a new
session. Wire parsing, source-failure envelopes, atomic wrong-member rejection,
connection/budget invariants, duplicate detection, local TTL and both Reads
remain entirely in those delivered cores. The adapter does not build a second
parser or memory state. A failed pair can retain old values as unavailable;
callback/Port rejection before ingress creates no fabricated Observation.

## Reports and provenance

`AdapterReport` contains the unchanged `SessionReport` and a frozen `PortReport`.
The latter separates fixture lifecycle from actual provider effects:

| Report field | Meaning |
| --- | --- |
| `fixture_readiness` | `NOT_READY` or `FIXTURE_PORT_READY`; readiness history, separate from final close state. |
| `open_calls` | 0/1 fixture readiness attempts, never a TCP/connect count. |
| `samples` | Bounded `SampleCall` records: request round, 0/1 Port sample callback invocations, fixed classification, validated returned identity and envelope SHA-256 when returned. |
| `termination` | Specific adapter classification, otherwise the existing session's stop reason. |
| `close_calls`, `close_state`, `close_error` | 0/1 cleanup attempt; `NOT_CLOSED`, `closed` or `close_failed`; separate classified error. A close failure does not replace the primary termination or increase the budget. |
| `provider_state` | Always `NOT_CONNECTED_TO_PROVIDER`. |
| `actual_tcp_connects`, `actual_getquote_calls` | Always 0 for this offline implementation. Open/sample counters are never converted into these counts. |
| `provider_observation` | Always `NOT_OBSERVED`. |
| `go_ack` | Always `DEFERRED_NOT_IMPLEMENTED`. |

`frozen_batch_returned` means transport bytes were returned, not accepted quotes.
A mismatched real-origin envelope behind a falsely fixture-labelled wrapper is
returned unchanged and rejected by the existing offline ingress as
`ingress_rejected`. The returned wrapper identity and digest are not evidence
of successful parsing. A real-origin **Port or wrapper** is rejected as
`real_origin_rejected` before handing its bytes to the session. Other pinned
identity drift is terminal as `source_identity_drift`; non-identity objects
are `source_identity_invalid`. No fallback provider or fixture encoder exists.

The existing fixture envelope requires `connection.attempts=1`, `redial=false`
and connection/call clocks. These are invented legacy schema fields, not this
run's actual connection/call counters or live timestamps. Ingress Observation
retains offline origin for input, clocks and known provenance. Its schema and
`go-real-r1-N` batch IDs are inherited names, not claims of real origin.

Port `TimeoutError` becomes `fixture_timeout` and session `sample_timeout`;
`ConnectionError` becomes `fixture_disconnected`; other ordinary hook failures
become `fixture_port_error`. Adapter expiry is `deadline_expired`; invalid
clocks are `monotonic_error`. Other adapter gates are `sample_request_invalid`,
`request_not_due`, `adapter_closed`, `frozen_batch_required`,
`batch_round_mismatch` and `frozen_bytes_bound`. These adapter rejections surface
as the existing session's generic `sample_error`, with the specific reason in
`PortReport`. No external exception message, type name or credential is copied
into reports. Cleanup errors are `close_timeout`, `close_disconnected`,
`close_error` or `close_aborted`. Process-control exceptions propagate after
cleanup; classifications remain available via `adapter.report`.

Ingress/session failures retain their original reports (`ingress_rejected`,
`observation_failed`, `read_not_observed`, clock/cadence/read errors, etc.).
Failure is terminal here even if the standalone ingress permits another offline
input. Already accepted memory is not rolled back. Close is attempted even if
no fixture open occurred; caller Ports must tolerate that. A blocking close
cannot be forcibly bounded, and cancellation during close reports
`close_failed/close_aborted` without retry.

## Source qualification and rights evidence matrix

These are read-only, owner-local evidence references, not product artifacts or
new provider executions. No raw real quote was opened for this implementation;
no raw responses or owner-local files are copied into the three product files.
Historical technical results below are attributed to retained adjudication and
completion records, not independently repeated or promoted to current supply.

| Topic | Level | Evidence and limit |
| --- | --- | --- |
| Pinned SDK software license | **EVIDENCED** | Local SDK `LICENSE` is MIT, copyright 2025 injoyai. Applies to software and associated documentation only; **not** a quote-service/data license or market-data authorization. |
| SDK identity | **EVIDENCED** | Local SDK HEAD is the exact pinned revision; tracked source is clean, with pre-existing untracked `cmd/`. Pin/endpoint are static declarations here, not provider authentication. |
| SDK project usage statement | **EVIDENCED statement / unresolved legal interaction** | The *same pinned SDK revision* `README.md` (lines 312–323; SHA-256 `e0f6b22d8a519ec25428d437fa3cfcdea02d0085a8074b8f2a74cc625ad955d7`) says the project is for learning, research and technical exchanges and forbids commercial use, while referring third-party server use to applicable service terms. This sits alongside the separate MIT `LICENSE`; it is **not** a right-holder grant for commercial or source-data uses. Reconciliation and applicability require explicit legal/right-holder review, not inference from either file alone. |
| TDX service terms | **EVIDENCED public terms / applicability needs adjudication** | Official `https://www.tdx.com.cn/about/yhxy/index.html?tabindex=0` (user agreement, published 2025-12-18, effective 2025-12-19) contains restrictions on unauthorized third-party access and client/server interaction data use. This supports a **rights gate**, not a determination that the tested endpoint, owner account or contemplated future path is specifically licensed or prohibited in every context. No signed permission or exception for this MDF Adapter has been verified. |
| Price/Amount representation | **EVIDENCED** | Pinned `protocol/types_price.go`: `Price int64`, integer li and scaling by 1000; `protocol/model_quote.go`: Amount through `Yuan(...)`, five-level prices also `Price`; `protocol/model_kline.go`: Amount is `Price`. Exact decoded representation, not economic accuracy, wire precision or verified decoder correctness. |
| Quote Kline.Time | **EVIDENCED** | Pinned `DecodeKline` in `types_price.go` uses client `time.Now()`. Cannot be used as exchange event time. |
| ServerTime | **EVIDENCED** representation; semantics **UNKNOWN** | `model_quote.go` renders decoded integer as a string. Remains opaque; no exchange time or sequence interpretation. |
| 2026-10-14 bounded R2 Live | **EVIDENCED**, historical limited technical scope | `MDF-P5-LIMITED-WATCHLIST-LIVE-RECOVERY-R2/LIVE_COMPLETION_RETURN.md` and `LIVE_AO_INDEPENDENT_POSTFLIGHT.json`: exactly one TCP, three GetQuote calls, two fixed symbols per round and three immediate same-instance Ingress/Read rounds with ACK. This is limited intraday evidence, not continuing coverage, independent exchange freshness or a new budget. No R2 raw was read/replayed in this Adapter transaction. |
| Distinct 2026-10-13 observation | **EVIDENCED / LIMITED**, separate historical trial | `MDF-P5-GO-STOCK-REAL-OBSERVATION-INGRESS-R1/LIVE_COMPLETION.md` recorded three quote calls followed by later file-to-memory reading (~63 s delay). `MDF-P5-LIMITED-WATCHLIST-SOURCE-CONTINUITY-GATE-R1/DECISION_SUPPORT.md` explicitly says this earlier trial did not establish immediate periodic consumption. It must not be presented as the 10-14 R2 result. |
| Owner lawful use | **LIMITED** | The read completion/adjudication and delivered ingress document record Owner's bounded research lawful-use statement. No broader legal opinion or production grant is inferred. |
| Real server access and bulk use contract | **UNKNOWN / NOT_AUTHORIZED** for this adapter | No service contract or right-holder grant was established. SDK MIT supplies none. |
| Production consumption / redistribution | **UNKNOWN / NOT_AUTHORIZED** | No market-data production or redistribution rights evidenced. |
| Volume and five-level Number trading units | **UNKNOWN** | SDK comments/decoded counts do not establish lots/shares/order-count mappings. Keep `sdk_volume_count_UNKNOWN` and `sdk_level_count_UNKNOWN`. |
| Exchange event time, source freshness, gap-free continuity, recovery, availability/SLA | **UNKNOWN** | Local requested/received/frozen/known and receipt-age TTL cannot prove exchange timing, missing events, continuity or service guarantees. Snapshot duplicates do not prove no source events. |
| Source Admission / Revision / Binding | **NOT_AUTHORIZED** | No authority is created by an interface, test, old technical success or software license. |

Exact owner-local materials actually read:

- `/srv/market-data-foundation-control/transactions/MDF-P5-WATCHLIST-SOURCE-ADAPTER-R1/CONTRACT.md`
- `/srv/market-data-foundation-control/transactions/MDF-P5-WATCHLIST-SOURCE-ADAPTER-R1/STATUS.json`
- `/srv/market-data-foundation-control/transactions/MDF-P5-LIMITED-WATCHLIST-SOURCE-CONTINUITY-GATE-R1/DECISION_SUPPORT.md`
- `/srv/market-data-foundation-control/transactions/MDF-P5-GO-STOCK-REAL-OBSERVATION-INGRESS-R1/LIVE_COMPLETION.md`
- `/srv/market-data-foundation-control/transactions/MDF-P5-LIMITED-WATCHLIST-LIVE-RECOVERY-R2/LIVE_COMPLETION_RETURN.md`
- `/srv/market-data-foundation-control/transactions/MDF-P5-LIMITED-WATCHLIST-LIVE-RECOVERY-R2/LIVE_AO_INDEPENDENT_POSTFLIGHT.json`
- `/var/tmp/mdf-tdx-live-20261011/release-v0.0.87/LICENSE`
- `/var/tmp/mdf-tdx-live-20261011/release-v0.0.87/README.md` (project disclaimer and third-party service terms pointer)
- `/var/tmp/mdf-tdx-live-20261011/release-v0.0.87/protocol/types_price.go`
- `/var/tmp/mdf-tdx-live-20261011/release-v0.0.87/protocol/model_quote.go`
- `/var/tmp/mdf-tdx-live-20261011/release-v0.0.87/protocol/model_kline.go`

Product implementation references are the delivered `periodic_watchlist.py`,
`go_stock_real_observation_ingress.py`, `stock_snapshot.py`, their corresponding
`tests/test_p5_*.py` files and the existing P5 Periodic/Ingress/Snapshot documents.
Local materials are not bundled. No `artifacts/` quote bytes were read. Source
price/amount units are representation evidence only; inherited View source
event time stays absent, freshness stays UNKNOWN, and rights stay
`UNKNOWN / EXCLUDED_FROM_PRODUCT_EFFECT`.

## Offline validation and remaining AO gap

Use qualified uv-managed Python 3.14.8, without dependency synchronization:

```bash
UV_CACHE_DIR=/tmp/mdf-source-adapter-uv-cache uv run --offline --no-sync ruff check .
UV_CACHE_DIR=/tmp/mdf-source-adapter-uv-cache uv run --offline --no-sync ruff format --check .
UV_CACHE_DIR=/tmp/mdf-source-adapter-uv-cache uv run --offline --no-sync pytest -q tests/test_p5_go_tdx_watchlist_source_adapter.py tests/test_p5_periodic_watchlist.py tests/test_p5_go_stock_real_observation_ingress.py tests/test_p5_stock_snapshot_candidate.py
UV_CACHE_DIR=/tmp/mdf-source-adapter-uv-cache uv run --offline --no-sync python scripts/check_repository_hygiene.py
```

Also apply the existing hygiene path/symlink/sensitive-content checks explicitly
to the three new untracked files. Tests use only author-created data and virtual
clocks: port-to-adapter-to-same-ingress/paired-Read equivalence, duplicates,
early caps, identity drift at Port/wrapper/wire boundaries, real origin
rejection, wrong membership, source failure envelopes, expiry/timeouts,
disconnection, cleanup failure, cancellation, single use and no retries. Guards
reject SDK imports, socket/process operations and explicit data file I/O during
module import and an offline execution. This validates this implementation and
fixture path; it is not an audit sandbox for arbitrary caller hooks.

The executable Periodic consumer remains **offline_fixture_mode=True**. Its
real-source input mode is not extended here. A future real Go driver, lawful
service access, bounded execution authority, real provenance-preserving
periodic ingress path and any actual ACK protocol require separate AO gates.
The live binding/timely supply gap identified in `DECISION_SUPPORT.md` remains
open. No database, real TDX/provider execution, R2 raw replay, commit/push,
Hosted CI or control-root modification is included in this implementation
return. Independent professional review, external file identity measurement,
AO acceptance and delivery state must be recorded externally; this document
makes no review PASS or completion-time claim.
