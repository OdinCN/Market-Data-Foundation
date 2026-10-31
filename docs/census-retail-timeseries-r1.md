# Census MARTS continuous input foundation R1 candidate

This is an unbound, reviewable data core. It does not enable retail collection,
create a database, expose HTTP/WebUI routes, start a calendar worker, or qualify
an operational continuous service. The fixed two-month T05 candidate and its
limited qualification in `census-retail-r1.md` remain unchanged.

## Source contract and request budget

The only data endpoint is
`https://api.census.gov/data/timeseries/eits/marts`. Every request uses US `1`
(`for=us:*`), `seasonally_adj=yes`, `error_data=no`, and the broad query:

```text
get=category_code,data_type_code,seasonally_adj,cell_value,error_data
time=from START to END
```

There are no category comma predicates or alternate providers. A
`RetailRangeRequest.persisted()` and its identity omit the credential; only the
wire parameters contain `key`. `CensusRetailRangeSource` requires both
`enabled=True` and an explicitly supplied credential. It never reads secret
files, writes artifacts, or changes provider bindings. Planning and parsing work
offline. Injected transport, UTC-aware clock and sleep support deterministic use.

`plan_range(start, end)` treats the arguments as inclusive **target** months and
adds a 12-month comparison buffer by default. It budgets nonoverlapping windows
of at most **252 months**. A 240-month initialization, e.g. 2006-10 through
2026-09, therefore uses one request for 2005-10 through 2026-09. Longer ranges
use at most ten requests; `max_requests` can reduce this ceiling. The full plan
is rejected before IO if it exceeds the logical request budget.
`transport_attempt_budget` exposes the worst case of three attempts per request.
`buffer_months=0` explicitly requests input without automatic comparison history;
shortfalls then remain visible in derived results.

The existing precise Census HTTPS transport supplies bounded reads (8 MiB per
range batch), a 20-second timeout and no redirects. Shared Source exception
classifications, retryable HTTP statuses and 1/2-second backoff are reused.
There are at most three wire attempts per logical batch. Retry-After delta
seconds and HTTP dates are honored. A wait exceeding five seconds returns a
safe error with aware UTC `retry_at`; HTTP 429 has a conservative minimum
300-second deferral. No polling or worker is provided. The caller must retain
and respect a returned retry time before another invocation. There is no durable
cross-process admission state in this candidate.

Only transport failures and transient HTTP responses are retried. Successful
responses are parsed, checked, timestamped and assembled outside retry. A parse,
clock, or other local failure never reissues that successful request. A later
batch failure does not repeat earlier successful batches within the invocation;
no partial successful snapshot is returned. Durable partial acquisition/resume
belongs to the later collection integration, which must retain successful inputs
rather than blindly restart a failed initialization.

## Raw input, availability and lineage

`parse_range_response(raw, start, end)` parses an inclusive input range, without
adding a buffer. It returns six explicit `RetailCell` records per requested
month: five raw `SM` categories (`44X72`, `441`, `444`, `447`, `722`) and official
`44X72 MPCSM`. Every cell carries its month, category, data type, Decimal or null,
status, reason, original source string/null, raw SHA-256 and JSON cell pointer.
An absent row has status `missing`, reason `source_row_absent`, and no pointer.
A returned null or nonnumeric marker has status `unavailable`; `(S)` and `(D)`
retain status `suppressed`. Original markers remain distinguishable via
`source_value`; their detailed provider meaning is not inferred. Unknown values
never become zero. An explicit numeric zero remains zero; division by it produces
an unavailable rate with `comparison_level_zero`.

Only SA/error headers may appear once or twice. Duplicate dimensions must agree
in every row, including non-target rows. Other header duplicates, extra/missing
headers, wrong range/geography/SA/error dimensions and duplicate target cells
are rejected. Non-target values and their suppression do not enter the product.
Future/unknown months omitted by Census remain explicit missing matrix entries;
a returned batch alone does not imply complete source coverage.

The network Source checks exact credential bytes and decoded JSON strings for
credential echoes before raw bytes can enter `RetailBatch`. JSON unicode escapes
cannot bypass the guard. Raw is excluded from object repr. Failures contain only
classification, attempt count, status and retry time, without provider exception
text. There is no logging or artifact writer in this core. A future persistence
adapter must keep guarded raw and safe observations under ignored `artifacts/`,
not in Git. Offline `parse_range_response` can additionally accept
`forbidden_secret`; without a known credential it cannot certify echo absence.

## Calculation and snapshot semantics

`CensusRetailRangeSource.fetch(...)` returns a `RetailInputSnapshot` with the plan,
request start time, per-batch observation time, guarded raw and parsed input
cells. Its identity binds the cohort to request identities, raw hashes and
request time. `calculate_snapshot(snapshot)` merges the entire input matrix
before computing target-month results. It rejects duplicate, incomplete or
mismatched batch matrices; comparison bases can span batch boundaries.

SM is **million current USD**, seasonally adjusted, neither a price index nor
constant dollars nor an annualized level. The MDF control definition remains:

```text
control[t] = SM[44X72,t] - SM[441,t] - SM[444,t] - SM[447,t] - SM[722,t]
```

Category 445 remains included. This is an MDF calculation using five operands,
not an official source field. Any unavailable operand makes that control level
unavailable. The inputs on each `RetailCalculation` retain the specific source
statuses and reasons. Headline and control 1/3/6/12-month changes are
`level[t]/level[t-h]-1`, using Decimal precision 28 and half-even rounding,
without quantization or annualization. They have unit `ratio`: `0.0312` is 3.12%.

`RetailMonth.official_headline_mpcsm` separately preserves Census's **percent**
value and source status. It is never replaced by an SM-derived ratio. Official
MPCSM and inferred SM changes can disagree due to rounding or other source
calculation details. This candidate does not infer the cause of a discrepancy.

One invocation is a current retrieval cohort. Multiple batches are sequential
requests, **not an atomic official vintage**. Monthly/benchmark/seasonal revisions
may alter history. `requested_at` and `observed_at` are acquisition metadata, not
source release timestamps or historical-as-published cutoffs. There is no
provider-vintage or finality assertion.

## Official calendar

`census_retail_release_schedule.parse_official_calendar(raw)` performs no IO.
It selects only the table immediately following the exact heading **Advance
Monthly Retail Trade Report** at
`https://www.census.gov/retail/release_schedule.html`. The next **Monthly Retail
Trade Report** table is excluded. The selected table must explicitly name Data
Month and Release Date at 8:30 am. Missing or ambiguous tables and conflicting
same-month entries fail closed. Identical duplicate entries coalesce.

Records follow existing calendar names (`reference_month`, `release_at`,
`release_at_local`, `timezone`), with aware datetime values rather than serialized
strings. Dates are interpreted at 08:30 in `America/New_York`, then converted to
aware UTC for later shared UI timezone conversion. No fixed UTC offset is used.
TBA stays null and visible. Every event retains source URL and raw SHA-256;
`actual_release_at` remains null. A scheduled time does not establish actual API
publication or readiness. No release time, calendar worker or UI entry is
hardcoded or enabled by this candidate.

## Next integration boundary

The next task must connect this verified core to the existing unified collection,
DB/HTTP and shared UI/strategy contracts, with explicit provider admission and
review. It must:

1. Persist versioned **raw source cells**, availability, acquisition metadata and
   raw lineage. Persist control outputs only as derived values with formula/input
   lineage, never as provider-returned categories.
2. Implement revisions/latest/rates and history cutoff selection on explicit
   source snapshots first, using one common input cutoff for all five operands
   and each comparison base. Recompute derived controls/rates after selecting
   inputs; do not combine latest operands with historical bases or imply Census
   as-published history without evidence. Define durable multi-batch completion
   and recovery before exposing initialization.
3. Adapt Decimal/aware datetime values to the shared storage/HTTP serialization
   contract, preserving the difference between percent, ratio and million USD,
   and exposing range gaps and unavailable calculations.
4. Bind the official calendar to the shared worker with persistent retry budgets
   and safe release probing. Scheduled time alone cannot mark data available.
5. Complete independent Host review, source qualification and end-to-end
   storage/readback/UI/strategy validation before enabling a retail product.

No existing half-connected retail page is exposed by this work.

## Verification evidence

Synthetic tests cover the 252-month initialization/budget, longer ranges,
preflight rejection, absent months, suppression/nulls, official-vs-derived
values, all four horizons across a batch boundary, repeated headers/dimensions,
duplicate targets, raw credential echoes (including escaped JSON), retry policy,
Retry-After, redirects and success-followed-by-local-failure behavior. Calendar
tests cover DST, year rollover, TBA, two-table isolation and duplicate conflicts.

Host-retained 2-month and 252-month JSON plus the saved official calendar are
replayed offline from the transaction directory; no real raw is copied into Git.
The long replay projects 1,260 SM and 252 official MPCSM cells, then 240 target
months and 1,920 available derived ratios. The two-month levels and official
MPCSM match unchanged T05 parsing. The calendar yields 16 months (15 scheduled,
1 TBA) and verifies summer/winter UTC times and year rollover. This is parser and
calculation evidence, not proof of a newly connected live service or database.

## Official references

- [MARTS API variables](https://api.census.gov/data/timeseries/eits/marts/variables.html) and [official query examples](https://api.census.gov/data/timeseries/eits/marts/examples.html) define the dimensions and key requirement.
- [Retail definitions](https://www.census.gov/retail/definitions.html) explain current-dollar sales and identify estimates derived by users as their own calculations.
- The [official advance report](https://www.census.gov/retail/marts/www/marts_current.pdf), checked 2026-10-31, names 08:30 EDT for the October release and 08:30 EST for the November schedule. This supports America/New_York rather than a fixed UTC offset.
