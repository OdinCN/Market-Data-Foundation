# Two-stock offline snapshot candidate R1

This is an inactive implementation candidate for
`MDF-P5-STOCK-SNAPSHOT-BOUNDED-CANDIDATE-R1`, not professional PASS, AO acceptance,
Git delivery, P5 phase authority, SourceBinding/Source/Revision Admission, or an
accepted extension of `mdf.consumer.read.v1`. The standalone module reuses
canonical `DatasetDefinition`, restricted `Scope`, `ContractError`, `require`,
and aware-instant validation without altering canonical contracts or registries.
Its named Definition is `mdf.candidate.market.stock.snapshot.go@inactive-r1`,
`source_qualified=False`. Rights and source freshness remain UNKNOWN and excluded
from product effect. Only author-created synthetic decoded-shape fixtures are
supported; the code neither fetches nor publishes data.

## Evidence and units

The local read-only reference is `injoyai/tdx v0.0.87`, release
`51c1ed51020fa43727300339dd92cbecf06a7d2e`, inspected through local Git source
objects, without reading retained provider responses. SDK `protocol/types.go`
defines Shenzhen=0 and Shanghai=1; `model_quote.go` carries Exchange/Code,
Kline, BuyLevel/SellLevel. Only `(1,600519)` and `(0,000001)` are accepted;
no identity guessing or universe inference occurs. Full response membership is
atomic: exactly one of each stock and five levels per side, otherwise no update.
The tests invent all numeric values; they do not copy provider quotations.

`protocol/types_price.go` documents `Price` as integer li (厘), divides by 1000
for yuan, and initializes decoded Kline.Time with client `time.Now()`.
`model_quote.go` sets Amount through `Yuan(...)`; price fields and Amount retain
integer li and have exact Decimal yuan projections, constructed with the integer
coefficient and exponent -3, independent of caller Decimal precision and traps.
Volume and level Number retain nonnegative SDK counts with trading units UNKNOWN:
no shares/lots mapping.
Decoded `Price` (all five Kline prices, Amount and both sides' level prices) and
Kline.Volume are signed Go `int64`: accepted integers are 0..2^63-1. Active1/2
are `uint16`: 0..65535. Level Number is platform-sized Go `int` (32 or 64 bits);
this candidate explicitly accepts only 0..2^31-1, the common nonnegative subset
and the range of the SDK's `int32`-backed `CutInt` decoder. It never derives Go
width from the Python host. These are decoded representation bounds, not proof
that every accepted value is reachable from the wire. Every numeric input must
be a native Python `int`; any invalid or out-of-range field rejects the whole
two-stock response with `invalid_integer_field`, retaining both old values.
The input must explicitly declare the fixed unit tuple; floats, booleans,
negative counts, incorrect sides, truncated levels and malformed shapes fail
closed. This checks the declaration and decoded representation; numerical values
alone cannot detect a caller falsely labelling a different scale. It does not
prove actual SDK parser accuracy or source qualification. Other quote fields are
outside this projection. Opaque ServerTime and Active1/2 are retained in bounded
form, never interpreted as event time, exchange sequence, or freshness.

## Time, quality and candidate reads

Each offline acquisition supplies aware requested <= received <= frozen <= known
clocks. Received is the MDF observation clock; known is candidate visibility,
not canonical publication. Known must strictly advance. Kline.Time is labelled
client sampling only; source event time remains absent. Request time orders
local acquisitions only and cannot establish source event order. Older requests
are `late`; equal request times are `order_uncertain`, preserving the last state.
Both outcomes retain acquisition evidence but cannot overwrite it. Newer failures
(including malformed responses) mark availability false and retain any old value.
Only timeout/disconnected classifications or parser gate classifications are
stored, never exception text from an external service.

Before any observation a read returns unknown. After failure it returns
unavailable, even if no value exists. A valid synthetic observation is `observed`,
which asserts local observation only. Age > TTL is stale (equality remains
observed); unavailable can simultaneously expose a stale old value and age.
Default TTL is 30 seconds; explicit values must be positive and <= 5 minutes.
Content duplicates retain new acquisition lineage and update observation age;
they create no trade event or source-freshness claim. Source freshness is always
UNKNOWN. A caller-declared reconnect records disconnection and begins a new
segment; requests predating that segment cannot recover it. A new baseline is
required, and the intervening gap stays unknown.

The instance holds at most 32 acquisition records by default (configurable
2..256), including failures, duplicates and ignored observations, with immutable
two-stock state snapshots. There is no unbounded ID index or durable storage.
`exact` addresses a retained candidate acquisition; `as_of` selects the last
record known by the supplied time. Reads before the retained horizon fail closed;
evicted exact identities fail closed. An empty instance supports unknown current
reads, but has no exact/as-of acquisition. These are sampled local visibility
semantics, not tick replay or a consumer wire contract.

A correction requires a retained current acquisition in the same segment, the
same request time, and a different parser revision. The revision must be a native
immutable Python `str` of 1..64 characters; subtypes and other types are rejected
with `parser_revision` before any history, state, ordinal or ordering mutation,
including failure and correction calls. External mutable objects cannot enter
retained revision identity. A valid correction creates a new known-time record
with `supersedes`, preserving the original observation age; earlier reads
still see the earlier parser output while retained. It cannot repair arbitrary
older samples, backdate knowledge, or imply canonical version admission.

## Scope and validation limits

No provider/SDK execution, network, ETF, cursor, native push, MBO, full-market
collection, tick recorder, database/Storage, listener, deployment or activation
is included. No dependency or Python baseline change is required. Source/data
rights, production units beyond Price/Amount, independent source truth,
source event timing, availability SLA and reconnect completeness are UNKNOWN.
Professional review, AO acceptance and Git delivery remain separate gates.

Validation uses the existing uv-managed Python 3.14.8 environment, offline and
without dependency synchronization. Focused tests and the default full suite
exclude `live_provider` and `postgres_integration`. Existing Fuyao deterministic
fixtures remap frozen interpreter paths through `tests/conftest.py`; that test
accommodation provides no provider proof. Repository hygiene scans tracked
files; the new candidate paths also require an explicit unstaged content/path
check until separately authorized Git delivery. No real raw bytes are tracked.

Bounded correction R1 adds synthetic regressions for every projected integer
field's zero/maximum and rejected negative, maximum+1, very large, boolean,
float and string inputs, including atomic second-member rejection and old-value
retention. Yuan regressions cover zero, sub-yuan values and the int64 maximum
under normal/low precision and Inexact/Rounded traps. Revision regressions cover
invalid types (including a str subtype), length bounds, mutation of an external
revision container, and absence of history/state effects on invalid sample,
failure and correction calls. This correction is not independent review PASS;
fresh revision-bound review requires separate authorization.
