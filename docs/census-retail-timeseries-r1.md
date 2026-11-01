# Census MARTS continuous input foundation R1 candidate

This is an unbound, reviewable backend candidate. The bounded storage/service/API
implementation below adds explicit factories; imports and GET requests do not
create a database, enable an asset, bind the shared WebUI, or start a worker. It
does not qualify an operational continuous service. The fixed two-month T05 candidate and its
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
no partial successful snapshot is returned. The explicit collection service now supplies per-batch persistence and resume
through `completed`/`on_batch`; the default source-only call still returns no
partial snapshot. Successful guarded inputs are retained before each DB write.

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
text. There is no logging or artifact writer in this core. The backend file archive keeps guarded raw and replay metadata under an explicit
`artifacts/` directory, outside Git. Offline `parse_range_response` can additionally accept
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

## Bounded storage/service/HTTP implementation (2026-11-01)

`historical/retail_timeseries_store.py` provides independent in-memory and
PostgreSQL ports, without inheriting CPI storage or reusing a CPI capability.
`schema_sql(schema)` returns reviewable SQL; only explicit collection calls invoke
`ensure_schema()`. PostgreSQL receives an existing autocommit connection and
`FileRetailBatchArchive`; no credentials, connection discovery, grants, migrations
or real database execution are included. The schema defaults to
`retail_timeseries_candidate`; root must select the real schema, privileges and
migration. Isolation schemas accept lowercase SQL identifiers of at most 63
characters and are quoted; data and cutoffs use bound SQL parameters.

Two tables, `batches` and `revisions`, retain six raw cells per month, month-start
DATE, NUMERIC(27,9), original text/status/reason, raw digest/pointer, batch lineage,
observed_at and nullable available_at. `latest` selects each input's latest local
revision; `retail_timeseries_rates` is a convenient latest numeric projection with separate
`official_headline_mpcsm_percent`. SQL views are current projections; historical
service reads select all input versions before calculation. Only content changes
(value, status, reason or original source text) append revisions; changed raw
hash, row pointer or observation time alone do not. An unchanged acquisition
remains a batch, and does not rewrite the previous revision's provenance.

`RetailTimeSeriesService.initialize/update` default to 240 target months ending
at the previous UTC calendar month, plus 12 comparison months. This calendar
anchor makes no release-readiness claim. An explicit end with omitted start
anchors 240 months at that end. Updates accept an explicit revision window or
use the same bounded default. Each successful batch is archived and committed
independently. A returned collection_id is an explicit replay key: resume with
the exact same range and archive. A new collection_id is a new observation.

Archive records publish atomically with file/directory fsync. Resume loads the
same guarded bytes, timestamps and attempt counts, reconciles their batch IDs,
and skips those source requests, including after a lost COMMIT acknowledgement.
PostgreSQL writes serialize with a schema-specific transaction advisory lock;
nested write transactions are rejected. Availability is sampled only after the
data commit returns successfully and confirmed separately. A missing confirmation
stays null in available history; explicit replay confirms it at the new time,
never backdating it. An acknowledged unchanged replay does not append revisions.
An older conflicting observation fails closed instead of reverting newer data.

Collection results report COMPLETED/PARTIAL/ERROR, safe classification,
collection_id, planned/completed/staged batches, new saved points, attempts in
this invocation, attempts attached to retained successful batches, and retry_at.
The caller must respect retry_at and serialize execution of a collection_id.
There is no persistent scheduler, global retry admission or general task system.
Process failure before archive publication, unavailable/corrupt archives, and
archive retention/backup require host handling; resume never substitutes another
provider or treats a local read error as a source gap.

`query_range` reads local raw versions first. Current gaps (no local evidence or
an explicit absent Census row) can be filled by the uniquely bound
`CensusRetailRangeSource`, under both service and HTTP-host permission. The broad
bounded Census query may include existing months; only gaps are complemented.
Queries never archive or persist. `local_only`, history, any explicit cutoff, or
available-basis selection forbid source access. Historical operands and all
1/3/6/12 month comparison bases share one inclusive aware cutoff, normalized to
UTC; default historical basis is post-commit availability. Local absence is
`not_provided`, distinct from a returned `missing` row, null or suppression.
Controls and ratios reuse the verified pure calculation after version selection.
Revision 1 is only the first local observation, never an official first release.

`web/retail_timeseries.py` exposes `timeseries_router` and `create_app` with actual
GET `/api/r5/retail/timeseries/{status,query,api-help}` routes. Factories default to
unbound/denied; the embedding host must provide a service, query permission,
request authorization callback and, separately, source permission. GET routes
perform no DDL. Decimal JSON values are strings, datetimes retain offsets, months
are descending, and levels/derived ratios/official percent explicitly use
`million_current_usd`/`ratio`/`percent`. `api-help` documents every query parameter,
operand provenance, units, nulls, cutoff semantics and URL-encoded +08:00 examples.

Minimal isolated assembly (root supplies connection and authorization):

```python
from market_data_foundation.historical.retail_timeseries_store import (
    FileRetailBatchArchive,
    PostgresRetailStore,
)
from market_data_foundation.historical.retail_timeseries import RetailTimeSeriesService
from market_data_foundation.web.retail_timeseries import (
    RetailTimeSeriesHost,
    create_app,
)

store = PostgresRetailStore(
    connection,
    FileRetailBatchArchive(ignored_artifacts_directory),
    schema="root_retail_isolated",
)
service = RetailTimeSeriesService(store)  # local reads; no source admission
app = create_app(
    RetailTimeSeriesHost(service, query_allowed=True),
    authorize=authorize_request,
)
```

The local CLI is `scripts/retail_timeseries.py` (or the historical CLI module),
with initialize/init, update, query and status. Inject `service_factory` in Python,
or explicitly select a trusted local `--service-factory module:callable` binder.
There is no default credential/config lookup. Example after root provides a binder:

```text
.venv/bin/python scripts/retail_timeseries.py query --service-factory retail_binding:build --start 2026-08 --end 2026-09 --local-only
```

Shared running UI, retail asset activation, calendar/worker and periodic strategy
remain unbound. Root still owns independent code review, isolated PostgreSQL
execution/rollback evidence, permission decisions and any future runtime binding.
No employment migration or existing four-family behavior is changed.

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

## Independent database review corrections

The derived view is named `retail_timeseries_rates`, alongside `latest`,
following the family naming convention. SQL division promotes its numerator to
NUMERIC(64,32), avoiding PostgreSQL's shorter default quotient scale; API
calculations remain Decimal precision 28.

Available-time reads exclude an older confirmed input while a newer observed
revision has an unknown commit confirmation. A cutoff before that newer
observation can still read the past confirmed input. Reconciliation confirms
the retained batch at the present confirmation time without backdating.

Current queries preflight the total source budget and request only the span
containing gaps in each admitted batch window. Query responses explicitly report
their range, request/attempt counts and `persisted=false`. Local failures and
historical queries cannot trigger upstream access.

Independent Host verification used a temporary PostgreSQL schema and retained raw,
with zero live Census requests. All 1,512 input cells and 240 target months were
read back; 1,920 derived ratios matched an independent raw-data calculation.
SQL/API ratio differences were below 1e-26. Three changed inputs created exactly
three revisions; unchanged recollection created none. Read-only ASGI requests,
historical cutoffs, unknown-confirmation masking and reconciliation passed.
The temporary schema was removed; all four existing family schemas and grants
were unchanged. A long-lived retail schema and shared runtime remain unbound.

## Shared DEV Web candidate (2026-11-01, offline implementation)

The shared `create_timeseries_preview` / `TimeSeriesRuntime` now accepts explicit
retail bindings. This code has not switched the running DEV service, created its
long-lived schema, granted privileges or made a real Census request. T05 remains
`source_qualified=False`; its fixed snapshot is navigation metadata, never proof
of continuous qualification. Retail periodic policy/calendar worker is not enabled.
The existing four families retain their own factories, schemas and strategy state.

Root extends its existing `scripts/mdf_cpi_timeseries_web.py` invocation (including
all existing four-family arguments) with:

```text
--enable-retail-timeseries --allow-retail-local-read
--retail-job-directory /ABS/retail-jobs
--retail-artifacts-path /ABS/artifacts/retail-timeseries
```

`--allow-retail-collect` separately enables manual collection;
`--allow-retail-source-query` separately permits nonpersisting current-gap reads.
Both also require the stored `macro/retail` capability and effective **Census**
credential. Enable `retail` through the existing package UI/API while preserving
the current enabled capability set and CAS revision. Do not replace the entire
set with only retail. Existing `[census].api_key` is the legacy trusted reference;
a package-scoped private Census reference takes precedence, without fallback on
failure. No browser may select a config directory, schema or secret reference.
Construction loads no credentials, connects nowhere and creates no directories.

The trusted in-process factory is `create_timeseries_preview(settings,
enabled=True, ..., retail_enabled=True, retail_access=explicit_access,
retail_job_directory=absolute_jobs, retail_artifacts_path=absolute_artifacts,
retail_allow_collect=True)`. Its `app.state.timeseries_runtime.retail_timeseries`
is the bound Host; `runtime.retail_read_service()` yields local-only storage with
no archive/source, while `runtime.retail_write_service(credential_loader=...)`
constructs `PostgresRetailStore` plus `FileRetailBatchArchive` and the exact Census
range source. Injected retail read/write/query factories are also supported for
trusted Host tests. Readers use the existing DEV config and enforced PostgreSQL
session read-only connection. Only collection constructs a write connection.

Root first reviews/applies `schema_sql("mdf_retail_timeseries_dev_r1")`, supplies
DEV ownership and `odin_dba` USAGE/SELECT/default SELECT grants, and verifies no
write/schema CREATE grants. This task executed none of those actions. Root then
starts the shared app with the flags above, verifies `/status`, and submits:

```text
POST /api/r5/retail/timeseries/jobs
Origin: http://192.168.10.20:18761
Content-Type: application/json
{"request_id":"ROOT-GENERATED-CANONICAL-UUID","action":"initialize","start":"2006-10","end":"2026-09"}
```

Replace the UUID placeholder; ranges are editable, and status supplies the current
240-month suggestion. The response returns 202 once `running` is durable, while
the existing single-host journal lock serializes work. Repeating the same command
reads its existing outcome; conflicting reuse returns 409. Refresh GET `/jobs`
and `/executions`; each job's id is exactly its service `collection_id`. A failed
or interrupted job can be explicitly POSTed to `/jobs/resume` with the **same**
command. Retained successful inputs reconcile locally; persisted `retry_at` is
honored. Unknown submission is not automatically reissued. Archives and jobs must
be retained across restarts. There is no automatic retry worker.

Read back via GET `/query?start=2026-08&end=2026-09&local_only=true` and optionally
history with URL-encoded `as_of=...%2B08%3A00`. GET never performs DDL or writes.
Six-input coverage includes the comparison buffer; absent Census rows fail the
job's completeness check, explicit null/suppression remains visible, never zero.
The shared asset page reuses query/help/raw response/copy/calendar/table controls;
shared new-collection and execution list/detail are the only retail workbench.

### Execution accounting

Manual execution coverage counts the six source inputs in the requested range; the preceding 12 months are reported separately as calculation buffers. Confirmed source row absence completes with a source-coverage gap, while local readback or collection failures remain unsuccessful. New observations and changes to existing inputs are separate counters. The service retains `saved_points` as total appended versions and adds `saved_observations` / `saved_revisions`; shared execution details display the latter two. Logical provider requests are counted separately from wire attempts and retained batches.
