# P4 Raw Daily Bars Replay R1

`MDF-P4-RAW-DAILY-BARS-REPLAY-R1` is the first bounded post-P3 historical/batch slice.

It proves the MDF writer/materialization path for the accepted
`mdf.market.bars.daily.raw@p3-r1` dataset without performing a live provider
request or activating a source binding.

## Why replay first

The current evidence is asymmetric:

- TDX R1 already qualifies `bars.daily` with limitations and recommends
  `tdx-python 0.1.6` only as a later source candidate with an explicit endpoint
  and `redial=False`;
- P3 already fixes raw daily-bar semantics and canonical ledger/publication rules;
- complete A-share universe remains NOT_ADMITTED;
- Macro and KD still require later runtime/binding evidence.

Therefore this slice exercises the historical data plane using an explicit
bounded offline fixture instead of fabricating a live acquisition.

## Source/binding candidate

The product materializes:

- source candidate `mdf.source-candidate.tdx-r1.bars.daily@tdx-r1`;
- binding candidate `mdf.binding-candidate.tdx-r1.bars.daily.raw@p4-r1`.

These are capability-specific canonical contract records derived from existing TDX
R1 evidence. They are not ACTIVE bindings and do not claim a current endpoint,
registry revision, provider app revision, or provider-native operation name.

Preserved TDX R1 limitations include:

- explicit endpoint required for future live use;
- recommended `tdx-python` path uses `redial=False`;
- complete A-share universe is not admitted;
- native multi-page global ordering is not asserted;
- lot precision already lost by the source library cannot be recovered;
- local daily dates are not promoted to absolute UTC event timestamps;
- no silent provider/source fallback.

## Offline lineage

A replay does not create an `AcquisitionOccurrence`.

The canonical path is:

`TDX R1 qualification semantics`
→ explicit offline fixture
→ `DataRun.REPROCESS`
→ MDF-normalized `EvidenceMaterial`
→ finite `DatasetVersion`
→ `Publication`

The replay `DatasetVersion` therefore has:

- `binding = None`;
- `mapping = None`;
- no acquisitions;
- an explicit replay `Transform`;
- material produced by the replay run.

The separate TDX source/binding candidate remains available for later source
admission without being misrepresented as the provenance of a live acquisition.

## Replay content

The first slice is stock-only and requires:

- one explicit provider-suffix security identity (SH / SZ / BJ);
- no provider-suffix to MIC crosswalk is performed by this slice;
- `bounded_explicit_membership`;
- explicit requested local-date bounds;
- normalized units `CNY` price / `shares` volume / `CNY` amount;
- raw/unadjusted semantics;
- observed rows only.

The TDX R1 stock normalization keeps the previously qualified unit mapping:
milli-CNY price/amount are converted to CNY and stock lot volume to shares.

A canonical row contains:

- security identity;
- identity namespace;
- local trading date;
- open/high/low/close;
- volume;
- amount;
- `adjustment = none`.

The content also records the dataset definition, source candidate, binding
candidate, requested local-date range and observed local dates.

## Fail-closed boundaries

The replay rejects:

- complete-universe claims;
- identity namespace mixing or guessed crosswalks;
- units other than the admitted CNY/shares/CNY normalized contract;
- qfq/hfq or other adjusted semantics;
- observations outside the requested date range;
- duplicate local dates;
- nonascending normalized replay rows;
- nonfinite/negative quantities;
- inconsistent OHLC.

Missing dates remain missing. The slice does not synthesize sessions or infer a
suspension.

## PostgreSQL proof

The accepted P3 ledger now explicitly permits the one additional development test
namespace:

`mdf_p4_raw_daily_bars_replay_r1`

No general namespace widening is introduced.

After a fresh read-only development-PG gate, the opt-in integration test may use
only that schema with synthetic data. It verifies:

- canonical records and content are durable;
- exact DatasetVersion content survives reconnect;
- Publication is durable and historically queryable;
- source/binding candidates remain readable;
- the replay remains acquisition-free;
- final schema cleanup is explicit.

Hosted CI excludes `postgres_integration` and never needs the private LAN
PostgreSQL service.

## Deferred

This slice does not authorize or implement:

- a live TDX/provider adapter;
- endpoint discovery/failover;
- ACTIVE source binding;
- universe discovery;
- corporate-action/factor/adjusted-bar ingestion;
- Macro collection;
- KD/P5 realtime/current-state work;
- S3/Garage/QuestDB/Parquet;
- scheduler/backfill/repair orchestration;
- MBO access;
- release/deployment/activation/adoption.
