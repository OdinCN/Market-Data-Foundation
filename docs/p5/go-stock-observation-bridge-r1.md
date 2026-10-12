# Go stock observation bridge R1

`MDF-P5-GO-STOCK-OBSERVATION-BRIDGE-R1` is a bounded workspace candidate under
Owner's 2026-10-13 offline implementation authority. It adds a Python JSON bridge
outside the accepted inactive `stock_snapshot.py` baseline at
`c23d9118f2f0d2380bf81cdfbb7ed88987109b35`. It does not execute Go. It supports
only author-created synthetic decoded shapes for `sh600519` and `sz000001`,
exactly five buy and five sell levels per member. No existing product module,
Definition registry, consumer interface or lifecycle controller is changed.

This candidate is not professional PASS, AO acceptance, Git delivery, Source or
Revision Admission, SourceBinding, MBO admission, Runtime, P5/P7 phase authority,
release, deployment, activation or adoption. The roadmap in
[../roadmap.md](../roadmap.md) is deferred design direction only.

## Offline use

Use the existing uv-managed Python 3.14.8 environment without synchronization,
installation or network access. A writable uv cache avoids the sandbox's
read-only default cache; it does not authorize fetching packages:

```bash
UV_CACHE_DIR=/tmp/mdf-bridge-uv-cache uv run --offline --frozen --no-sync python -m market_data_foundation.candidates.go_stock_observation_bridge
```

The command runs `synthetic_offline_demonstration()`: twelve invented batches
in a capacity-16 instance, with a synthetic ten-second TTL. It emits only a
classified summary: batch/acquisition identities, outcomes, availability and
staleness, plus explicit provenance/UNKNOWN declarations. Its sequence includes
sample, duplicate, changed sample, late, order-uncertain, malformed second-member
failure and recovery, timeout and recovery, declared reconnect, old-segment
rejection and a new baseline. It performs no sleeps, clock capture, provider
calls, file reads/writes or persistent recording. Multiple constructed batches
demonstrate state behavior; they do not prove historical or continuing refresh.

For an in-process review, build an author-created envelope as below, then call
`wire = encode_synthetic_batch(envelope)` and
`observation = GoStockObservationBridge().ingest(wire)`. The test module has a
complete independent fixture generator; neither code nor tests load retained
real quotations. Serialization freezes the mutable caller input into UTF-8
JSON bytes. Ingestion decodes a fresh object; retained observations contain
immutable existing candidate Records/Quotes/States rather than the input dict.

## Candidate envelope, not a public wire contract

All envelope keys are mandatory; extra/missing keys are rejected. This local
schema is unrelated to `mdf.consumer.read.v1` and is not an admitted provider
serialization contract.

| Key | Required meaning/value |
| --- | --- |
| `schema` | `mdf.candidate.go-stock.synthetic-batch.r1` |
| `input_origin` | `author_created_synthetic` |
| `sdk_release` | `injoyai/tdx v0.0.87@51c1ed51020fa43727300339dd92cbecf06a7d2e` |
| `units` | JSON array `["integer_li", "sdk_volume_count_UNKNOWN", "sdk_level_count_UNKNOWN"]` |
| `batch_id` | `synthetic-` followed by 1..54 ASCII alphanumeric/dot/underscore/hyphen characters; first suffix character alphanumeric |
| `clock_provenance` | `author_created_synthetic` |
| `actual_observation_clocks` | `UNKNOWN` |
| `source_event_clock` | `UNKNOWN` |
| `clocks` | Exactly `requested`, `received`, `frozen`, `known`, each an aware ISO datetime string of 1..64 characters |
| `mode` | `observe` or `reconnect` |
| `failure` | JSON null, `timeout` or `disconnected`; non-null requires `rows=null` |
| `parser_revision` | String of 1..64 characters; reconnect must use `synthetic-parser-r1` |
| `supersedes_batch` | JSON null, or a retained batch ID for an existing candidate parser correction |
| `rows` | Synthetic decoded two-member list for a sample, or null for a declared failure |

Reconnect additionally requires `failure=disconnected` and no superseded batch.
It delegates to the existing candidate's fixed-revision reconnect API; a
different revision is rejected rather than discarded. A failed sample is an
offline declared outcome, not evidence that any transport actually timed out
or disconnected.

Input wire must be native Python `bytes`, length 1..32,768 bytes, containing one
UTF-8 JSON document. Duplicate JSON keys are rejected at every object depth;
NaN/Infinity constants are rejected as `nonfinite_json_constant`. Floating-point
tokens that decode to nonfinite values (including `1e999` and `-1e999`) are
rejected as `nonfinite_json_number` at every depth, including unprojected quote,
Kline and level fields, before candidate mutation. Rejection preserves both
histories, state, batch/acquisition ordinals and time visibility; it cannot
refresh synthetic observation age. Finite numbers in unprojected fields retain
their existing ignored behavior. JSON integers remain Python integers
without conversion through floating point, including the int64 maximum. Quote
floats/booleans/strings fail the existing integer gates. Serializer errors,
malformed UTF-8/JSON and decoder recursion failures produce stable
classifications without raw exception text. This is a representation bound,
not a general hostile-input service or authentication boundary.

SDK, unit and synthetic-origin declarations are checked, but labels cannot
prove their own truth. Relabelling a real retained response as synthetic would
violate this transaction even if a parser accepted it. No rights or actual
acquisition-clock provenance can be established by this envelope. Do not supply
real provider material, credentials or raw exception messages.

## Projection, failures and identity

Only existing candidate fields are projected. Price/Amount decoded integer li
and Kline.Volume use 0..2^63-1; level Number uses the SDK decoder's common
nonnegative int32 subset 0..2^31-1; Active1/2 use 0..65535. Native integer type,
identity, two-member atomicity, five levels and side validation remain in
`parse_synthetic`. Exact Decimal yuan projection divides integer li by 1000
without caller-context rounding. Volume and level counts are not mapped to
shares/lots. Opaque ServerTime and Active counters are not event clocks or
exchange sequence numbers. Other decoded fields are outside the projection.

Malformed envelope/control metadata raises classification-only `ContractError`
before either history is changed. Examples include `sdk_release_mismatch`,
`unit_declaration_mismatch`, `synthetic_clocks_required`,
`real_clock_claim_forbidden`, `batch_identity_reused`, `duplicate_json_key` and
`malformed_batch_json`. These rejected attempts have no trustworthy batch to
record and consume no candidate acquisition ordinal. A valid envelope with
malformed quote rows reaches the existing candidate and records its parser
classification, such as `invalid_integer_field` or `malformed_snapshot`.
For a forward request it atomically marks both securities unavailable while
retaining both prior values; a valid later batch recovers availability. Late or
order-uncertain failures retain their classification but do not alter state.
The bridge neither logs input nor catches and prints raw exceptions.

The batch ID identifies an accepted envelope within one bridge's retained
session. The existing `sample-N` acquisition identifies its candidate record;
each Observation links these identities explicitly. Reusing a retained batch ID
is rejected, even with different content or clocks. Content duplicates with
new batch IDs remain separate observations. Default capacity is 32 (2..256);
the bridge's ID mapping and candidate record history use the same capacity and
advance together. There is no unbounded dedup index, durable/global batch
uniqueness, retry protocol or cross-instance identity. Evicted IDs may be reused;
clients needing lasting uniqueness require a separately designed contract.

## Time and reads

All four clocks are deliberately constructed observation/review clocks:
`requested <= received <= frozen <= known`, and accepted known time strictly
increases. There is no `datetime.now()` fallback. Missing, naive, invalid or
misordered clocks are rejected rather than inferred from Kline.Time or
ServerTime. Even syntactically valid clocks do not prove real request, receive,
freeze or availability times; actual clocks stay UNKNOWN. `received` supplies
the candidate's synthetic observation age; `known` supplies candidate-local
visibility, not a canonical Publication decision/usable time or MBO receipt.
Kline.Time is client sampled only; source event clock remains absent/UNKNOWN.
Request time orders these synthetic acquisitions, not exchange events.

The bridge delegates sample/duplicate/late/order-uncertain/failure/recovery,
reconnect segment, watermark, TTL and parser correction behavior to
`StockSnapshotCandidate`. Duplicate content refreshes synthetic observation age
without proving source freshness. TTL is bounded positive..five minutes;
`age > TTL` is stale and equality is still observed. Unavailability can coexist
with a stale retained value. Reconnect is merely a declared offline segment;
requests before its known-time boundary cannot recover it and a new baseline
is needed. It proves neither transport recovery nor gap-free recording.

`exact(batch_id)` returns a retained Observation. `as_of(at)` maps the existing
candidate's last record known by `at` to its batch. `read(security, at=...,
batch_id=...)` delegates the candidate view, including not-yet-known rejection;
omitting the batch ID uses candidate as-of semantics. Evicted exact and
pre-horizon as-of reads fail closed. Initial reads are unknown. Views always
retain `source_event_at=None`, `source_freshness=UNKNOWN` and rights
`UNKNOWN / EXCLUDED_FROM_PRODUCT_EFFECT`.

`supersedes_batch` resolves only retained batch identities to candidate
acquisitions. The existing same-request/current-state/same-segment/different-
parser-revision gates apply; correction creates later knowledge, preserves
original observation age and cannot backdate prior reads. A new parser revision
does not establish SourceRevision Admission or a canonical new Version.

## Evidence limits and validation

The read-only retained replay qualification
`MDF-P5-GO-STOCK-RETAINED-REPLAY-QUALIFICATION-R1` reported three-snapshot
`RESEARCH_COMPATIBILITY=PASS` and `SOURCE_ADMISSION=NOT_ADMITTED`. That report
is context only; this implementation does not read its retained bytes.
Historical four-clock provenance, independently demonstrated economic units,
source event time, source truth, trading-count units, rights, refresh SLA and
gap completeness remain UNKNOWN. Prior three-SDK matrix deviations remain
disclosed and unresolved by this synthetic bridge.

Run focused bridge and existing candidate regressions, then repository checks:

```bash
UV_CACHE_DIR=/tmp/mdf-bridge-uv-cache uv run --offline --frozen --no-sync pytest -q tests/test_p5_go_stock_observation_bridge.py tests/test_p5_stock_snapshot_candidate.py
UV_CACHE_DIR=/tmp/mdf-bridge-uv-cache uv run --offline --frozen --no-sync ruff check .
UV_CACHE_DIR=/tmp/mdf-bridge-uv-cache uv run --offline --frozen --no-sync ruff format --check .
UV_CACHE_DIR=/tmp/mdf-bridge-uv-cache uv run --offline --frozen --no-sync python scripts/check_repository_hygiene.py
UV_CACHE_DIR=/tmp/mdf-bridge-uv-cache uv run --offline --frozen --no-sync pytest -q
```

Default pytest excludes live-provider and PostgreSQL integration tests. The
bridge tests independently invent input and cover serialization, stable negative
classifications, representation boundaries, atomic errors/recovery, clocks,
temporal equivalence with the old candidate, exact/as-of eviction, correction
age, reconnect and the bounded demonstration. Numeric decoder regressions use
author-created literal tokens in unprojected quote/Kline/level fields and compare
history, state, age and time visibility against an independent unchanged bridge;
they also preserve finite ignored values and existing integer rejection semantics.
Hygiene scans tracked files;
the four untracked paths also need an explicit path/content check before any
separately authorized delivery. Known sandbox ptrace EPERM failures in existing
tests must be recorded separately from external host QA; do not relabel a
sandbox nonpass as host success. No host result is asserted here.

Next gate: a separately authorized fresh, revision-bound independent read-only
professional review. Subsequent AO acceptance and Git delivery are separate gates.
Acceptance, Git delivery and any source-semantic/rights/live-clock qualification
need their own authority; none follows automatically from these tests.
