# I-4 source-native intraday / ExHq candidate R1

Market-Data-Foundation / Session #15 MDF Product Control / Owner-local / MDF-DEV.
Base `e8faac58b8d97594109ed2dc63ea110afb36cfce`. Only the I-4 owned paths
change. This candidate does not publish, adopt, deploy, trade, alter SDK/pins,
write PG/Garage, or install a timer.

The source is the Host's read-only `injoyai/tdx v1.0.0` checkout at
`2a9000681f5a9896ea98e4de8bc4904f1dce78ba`, not an unversioned online API.
The field manifest copies the relevant entries from the Host AST index
`research/mdf-unified-source-20261018/injoyai__tdx/UPSTREAM_V100_FIELD_STRUCT_INDEX_R1.json`
and records the gap audit identity
`b03cd8bdfd1cf32d88391696ab35876d52767ed6971d86f94d0bc51ec48b3125`.
The R5-I6 R2 design, D1-D4 and A-STD/A-EX-DIR contracts are read-only inputs.

| Method | Wire type | Payload / decoder | Qualification boundary |
| --- | --- | --- | --- |
| GetHistoryMinute | 0x0FB4 | 6-byte header, count, signed variable fields | Price ×10 follows SDK; time labels generated from row index, no source instant |
| GetKline | 0x052D | count, encoded civil date/minute, delta prices, float32 volume/amount, index breadth when requested | Only periods 0/1/2/3/7/8; kind/code/period scoped volume; no SDK host-clock FixKlineTime mutation |
| GetTrade | 0x0FC5 | count, minute, price delta, volume, Number, Status, opaque | Fixed UTC+8 combines wire minute with explicit request date; no independent source date/seconds |
| GetHistoryTrade | 0x0FB5 | 6-byte header; minute, delta, volume, Status, opaque | Number absent becomes None, not SDK zero; history is not a live API |
| ExQuote | 0x23FA | exact 150 bytes, market + 9-byte code, native float32 prices and uint32 quantities/depth | Exact returned market/code required; no source clock, lot or currency inferred |
| ExBars | 0x23FF | 20-byte header, count, 32-byte rows | Period is separate from directory category; Amount and Position share bytes; no adjustment claim |
| ExMinute / ExHistMinute | 0x240B / 0x240C | 12/20-byte header, 18-byte rows | Clock is time-of-day only; OpenInterest economics UNKNOWN |
| ExTrade / ExHistTrade | 0x23FC / 0x2406 | 16-byte header, 16-byte rows | Price remains uint32; Nature retained; SDK second may be a fallback and observed_second remains None |

Upstream `client.go:711-715` routes **GetMinute to GetHistoryMinute using the host's
today date** and calls the original real-time decoder suspect. GetMinute is
therefore excluded as a separately supported live operation. Get*All, automatic
pagination, and default/rotating ExHq dialers are also excluded.

`field_facts(request)` exposes AST names/types/tags/source lines plus exact
method/market/category/code/period/kind, transform reference, unit status and
UNKNOWN rules. Standard SDK Price is documented integer milli-yuan; that does
not qualify a Source or extend to ExHq. Minute K volume is normalized in its
own code/period/kind context, never by a global Kline.Volume rule. Index breadth
requires an explicit index request. Values outside finite/int64 projection
bounds fail intake while the original receipt remains retained.

Raw payloads are decompressed response data, not SDK JSON and not complete wire
frames. `Material` separately carries raw payload SHA, original wire SHA,
request-wire SHA, locator, producer declaration, endpoint/peer, source/binding,
capture ID, schema, directory evidence, and obtained/received/known clocks.
The clocks are local observation clocks; None remains UNKNOWN. Comparisons use
UTC instants, including repeated-hour folds. No decoder derives a date from now.

`Page.identity` hashes schema plus the full receipt. `Row.identity` includes page,
physical index, byte range/digest, native projection and explicit clock status.
Changes in reserved/header bytes still change page/row lineage. Native JSON bytes
are immutable; the convenience dictionary is a fresh copy. Content comparisons
are separate from observation identity. Active refresh compares named live
fields, excluding opaque bytes and synthetic labels.

Every declared row must fit; undeclared trailing bytes, partial rows, bool/float
integer coercion, malformed GBK directory fields, oversize code, page overflow,
nonfinite economic float, digest mismatch and mismatched quote identity fail
closed. Empty pages remain EMPTY_PAGE_NOT_PROOF_OF_NO_DATA. One payload is at
most 65,535 bytes, one page at most 2,000 rows (bars 800, current standard trades
1,800). Unknown wire variants are retained and need a new reviewed decoder
revision; no auto-repair or alternate Source is attempted.

ExMarkets/ExCount/ExInstruments are available only as bounded discovery requests
in the Go transport. Intake reuses the existing A-EX-DIR strict decoder.
`associate_sample()` consumes exact ExMarkets and instrument-page receipts from
the same capture context and binds only `(market, category, code)` actually
observed. `attach_association()` checks source/binding/mode/producer and digest.
A sampled association is not a full-directory coverage or historical-membership
claim. Categories, 31/48 markets, code case and leading zeros stay distinct;
market names are not converted into venue calendars or economic metadata.

`observe_material()` uses the existing Capability/Fixture/Observation contract.
Structural success leaves completeness/exhaustion unknown. Its binding is offline
because it consumes retained bytes, and errors contain classification/type only.
Real execution has a separate explicit opt-in Host boundary; tests contacting a
Provider must be marked `live_provider` and stay outside deterministic CI.

The I-1 handoff is a retained immutable payload/wire plus receipt and source-native
projection. It is not a D2 Attempt, D3 Version/Publication or D4 canonical reader
response. This slice intentionally does not invent daily D1 contracts for
intraday/ExHq records. A future central adapter must preserve original identities
and UNKNOWN before any canonical or I-3 storage decision.
