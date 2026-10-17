# T05 Census Retail R1 candidate

This closes the existing candidate for reference `2026-09`, with comparison
period `2026-08`. It does not activate a binding or establish complete source
qualification. The deterministic fixtures are synthetic. The parser correction
also checks retained R2 raw bytes locally; it performs no new Census request or
PostgreSQL/Garage IO.

## Evidence boundary

The accepted current official dataset is Census
`https://api.census.gov/data/timeseries/eits/marts`. Supplied current official
examples use a broad `get`/`time` query, optionally with `for=us:*`; they do not
demonstrate comma-separated category or data-type predicates. Supplied current
variable metadata confirms `category_code`, `cell_value`, `data_type_code`,
`seasonally_adj`, `error_data`, `time` and geography predicates. No web lookup or
fresh official response was obtained during this parser correction.

The retained `HOST_SOURCE_CAPTURE_R2_RAW.json` and sanitized
`HOST_SOURCE_CAPTURE_R2_SUMMARY.json` provide R2 HTTP200 real-response evidence.
The real header is `category_code,data_type_code,seasonally_adj,cell_value,`
`error_data,seasonally_adj,error_data,time,us`: predicate fields are repeated at
zero-based positions `[2,5]` and `[4,6]`. Across all 84 data rows the respective
duplicates agree (`yes`/`yes` and `no`/`no`), and all 11 target cells are present.
Read-only local qualification parses those retained bytes, constructs all four
measurements and their lineage, and validates the reconstructed plan. Its output
contains only the raw hash, byte size and measurement names; raw bytes and business
values are not copied into Git or printed.

This is local parser/lineage evidence, not source qualification COMPLETE or proof
of storage, publication or readback. `source_qualified=False` and the candidate
binding's limitations remain unchanged. Any later live qualification requires
explicit opt-in and retained raw bytes and safe observations under ignored
`artifacts/`.

Historical MDD descriptions are historical implementation references only. They
are not current official dataset evidence or proof of current provider shape.
The supplied legacy MDD real proof also records unsuitable comma category filters
and a broad current-plus-previous response.
This candidate does not read MDD source/runtime, migrate MDD data, or inherit a
historical MDD endpoint as an alternative binding. There is no MARTS-to-MRTS
fallback; request and Source identity stay bound to `timeseries/eits/marts`.

## Adopted request and accepted response shape

One logical GET covers both periods using the broad query shape demonstrated by
the supplied current official examples. Category and data-type predicates are
absent; the candidate's existing `seasonally_adj=yes` and `error_data=no`
predicates are retained. Its persisted nonsecret parameters are:

```json
{
  "get": "category_code,data_type_code,seasonally_adj,cell_value,error_data",
  "seasonally_adj": "yes",
  "error_data": "no",
  "for": "us:*",
  "time": "from 2026-08 to 2026-09"
}
```

Only `key` is added on the wire. Persisted request, request identity and
acquisition parameters exclude it. Loading credentials requires explicit live
opt-in. Errors expose classifications and attempt counts, without raw exception
messages. A response containing the credential bytes is rejected before storage.
Redirects are terminal; there is no alternate endpoint or provider.

The response is a JSON header/row array. Its required columns are
`category_code`, `data_type_code`, `seasonally_adj`, `cell_value`, `error_data`,
`time`, `us`; column and row order may vary. Only `seasonally_adj` and `error_data`
may occur once or twice; every other column must occur exactly once. Missing,
extra or unauthorized duplicate headers fail closed. Repeated predicate columns
must be exactly equal in every row, including non-target rows, or parsing fails
with `census_retail_duplicate_dimension_mismatch`. The parser explicitly indexes
column positions and uses the first predicate value after verifying equality.
Every row must have the expected structure, one of the two requested periods,
US `1`, SA `yes` and error-data `no`. The parser then projects the target matrix
before checking duplicate cells or numeric values. Each `cell_value` source
pointer still identifies the unique value column in the original response.

Only 11 cells are strictly required: five category `SM` cells for each of the two
periods, plus current `44X72` `MPCSM`. Only those 11 target cells enter the target
index and `source_cells`. Other categories/types and all other `MPCSM` cells are
ignored before numeric/suppression and duplicate validation; an unrelated `(S)`
cell cannot reject the projection. Duplicate, missing, suppressed or malformed
target cells fail closed. Each target cell retains the full response's raw digest
and its original JSON cell pointer. `SM` levels and resulting control levels must
be positive.

## Measurement mapping

| MDF measurement | Input | Origin | Unit |
| --- | --- | --- | --- |
| `headline_level_sa` | Current `44X72` `SM` | `provider_returned` | Million current USD |
| `headline_mom_sa` | Current `44X72` `MPCSM`, preserved verbatim | `provider_returned_calculation` | Percent |
| `control_level_sa` | Current five `SM` operands | `product_derived` | Million current USD |
| `control_mom_sa` | Current and previous derived controls | `product_derived` | Percent |

MDF control group v1 is defined as:

```text
control[t] = SM[44X72,t] - SM[441,t] - SM[444,t] - SM[447,t] - SM[722,t]
control_mom = (control[2026-09] / control[2026-08] - 1) * 100
```

`445` remains included through the total. Broad responses may return it, but it
is not subtracted or selected as a separate operand. The control is an MDF product
definition, not a Census named field. Both control calculations use only `SM`;
category `MPCSM` values are not control operands. Headline MoM is not recomputed
from headline levels. The fixture
intentionally supplies `9.7` headline MPCSM beside levels implying `10%`.

The formula revision is
`mdf-retail-control-44x72-less-441-444-447-722-v1`. Derived calculations use Decimal
precision 28, half-even rounding, with control MoM quantized to six decimal places.
The synthetic levels produce previous control `500`, current control `570`, and
control MoM `14.000000`.

## Snapshot, revision and local read limits

Both periods come from the same current retrieval snapshot. Monthly revisions and
annual benchmark/seasonal revisions can change earlier values. This response does
not establish a historical as-published vintage, original release values, finality
or an official revision state. Provider vintage is null and provider revision
state is `not_declared`. No independent previous-month request is joined to the
current response.

Reference period, configured scheduled release, actual release, request time,
obtained time, freeze time, publication decision and usable time remain distinct.
The configured September release slot/schedule is metadata, not proof of an
observed official release. Actual release time stays unknown. Local as-of chooses
among locally published, usable snapshots by acquisition time; it cannot recover
an earlier official vintage absent retained evidence. Ambiguous equally recent
snapshots are rejected.

The plan retains raw response bytes and exact parser/formula/request lineage.
Materialization verifies the raw content before canonical persistence and
publication. Local read reconstructs that graph from retained raw bytes, requires
complete measurement scope and rejects payload or lineage mismatch. It performs
no Source request. Unknown local outcomes require reconciliation; they do not
trigger another acquisition.

## Deterministic verification

`tests/test_census_retail_vertical.py` covers the required matrix, broad-response
target projection, matching duplicate predicates, mismatched/unauthorized
duplicates, missing/extra headers, ignored unrelated/suppressed cells, target
failures, mapping, plan roundtrip, in-memory materialization and exact/as-of local
reads.
`tests/test_census_retail_live.py` simulates default-off behavior, HTTPS/query
shape, credential safety, shared Source retry and terminal/local failures. These
are deterministic tests, not `live_provider` qualification tests. Network and
PostgreSQL connections are forbidden by test guards; material storage is in
memory.

Shared Source policy permits at most three wire attempts per logical read, only
for classified transient failures, with delays of 1 and 2 seconds. Parsing,
validation, artifacts and local storage/publication/readback stay outside retry.
No success-followed-by-local-failure path reissues the provider query.
