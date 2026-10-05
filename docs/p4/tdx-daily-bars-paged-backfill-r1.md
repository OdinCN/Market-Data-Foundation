# P4 TDX Daily Bars Paged Backfill R1

`MDF-P4-TDX-DAILY-BARS-PAGED-BACKFILL-R1` is an offline retained-evidence slice.
It does not contact TDX and does not activate a source binding.

## Bound retained evidence

The slice is bound to the existing TDX R1 `tdx-python 0.1.6` SH-main artifacts:

- `daily-SH-main-history.raw.json` — 4800 rows;
- `daily-SH-main-page0.raw.json` — newest two-row witness;
- `daily-SH-main-page2.raw.json` — preceding two-row witness.

Exact SHA-256 identities are part of the product contract. A byte change is rejected
before semantic merge.

## Source pagination evidence

The retained R1 generator requested six bounded pages at offsets
`0, 800, 1600, 2400, 3200, 4000`, each with requested page size 800.

For the bound SH-main artifact:

- every 800-row page is strictly ascending by local trading date;
- every page is internally unique;
- the six page date ranges do not overlap;
- source page sequence is newest page first;
- concatenating source pages therefore creates five page-boundary inversions;
- all 4800 observed dates are unique;
- page0 equals the final two rows of the newest 800-row page;
- page2 equals rows `-4:-2` of the newest 800-row page.

The R1 generator had a stop-if-empty condition, but this bounded six-page capture
does not prove that the provider was exhausted at offset 4800. It therefore does
not prove complete history.

## Canonical reorder

MDF validates every source page before reordering it.

The only admitted reorder for this slice is:

`reverse(validated_page_sequence)` while preserving row order inside every page.

Each page metadata record binds row locators explicitly:

- `source_row_start = 0`;
- `source_row_end = 799`;
- `canonical_row_start`;
- `canonical_row_end`.

For the six-page R1 layout, source page 5 maps to canonical rows 0..799 and
source page 0 maps to canonical rows 4000..4799. Within a page:

`source_row_index = canonical_row_index - canonical_row_start`.

This makes the source-row-to-canonical-row mapping explicit instead of requiring
consumers to reconstruct it from an implicit fixed-page positional assumption.

The implementation does not call a global sort to repair malformed source pages.
A nonascending page, duplicate date, overlapping page range or wrong newest-first
sequence fails closed before canonical content is created.

Canonical content is globally ascending by local trading date after this explicit
page-sequence transform.

## Completeness

The DatasetVersion completeness is exactly:

`bounded_six_page_observed_not_exhaustive`

This means:

- exactly the validated 4800 observed rows are represented;
- provider exhaustion is unknown;
- complete history is not claimed;
- missing trading sessions remain missing;
- suspension is not inferred;
- page order observed from retained evidence is not re-described as provider
  chronological pagination.

## Identity and units

This first paged-backfill slice stays within the already admitted raw-stock contract:

- security `600000.SH`;
- namespace `provider_suffix`;
- stock only;
- raw/unadjusted only;
- CNY price;
- shares volume;
- CNY amount;
- no provider-suffix to MIC crosswalk.

## Canonical lineage

No provider request occurs in this transaction.

The canonical path is:

retained TDX R1 artifacts
→ exact SHA + page-boundary validation
→ `DataRun.REPROCESS`
→ MDF-normalized merge-validation `EvidenceMaterial`
→ finite raw-daily-bars `DatasetVersion`
→ `Publication`

The replay Version has no binding, mapping or AcquisitionOccurrence. Historical P2
artifacts are not rewritten as current-run acquisitions. Their refs and SHA identities
are pinned in transform/content metadata.

## PostgreSQL proof

After a fresh read-only gate, only schema
`mdf_p4_tdx_daily_bars_paged_backfill_r1` may be used.

The opt-in integration reads the existing retained artifacts, persists the current
REPROCESS/material/version/publication records, reconnects for exact readback and
then removes the transaction schema.

Hosted CI does not need the retained artifact directory or private PostgreSQL:
deterministic tests use synthetic six-page fixtures and the integration test remains
`postgres_integration` opt-in.

## Non-scope

- any provider/network request;
- ACTIVE source binding;
- automatic provider pagination;
- scheduler/backfill/repair service;
- complete-history assertion;
- complete universe;
- multi-symbol coverage;
- adjusted bars, factors or corporate actions;
- Macro/KD/P5;
- S3/Garage/QuestDB/Parquet adoption;
- MBO/API/WebUI;
- release/deployment/activation/adoption.
