# Census MARTS continuous input foundation R1

当前代码包含连续来源、独立存储、共享 WebUI、手动任务和官方日历周期 worker 的显式绑定。
开发与审查共同入口见 [五族基线指南](macro-family-development-review.md)。
导入模块或 GET 不建库、不启用资产、不启动 worker；代码能力不证明当前运行状态或未来自然发布已验收。

本文依次保留来源核心、独立后端和共享 DEV 接线的阶段合同。下文早期的 unbound / no worker
描述仅适用于相应独立组件与当时验证，不描述后续共享 Host 的现有能力。
固定两月 T05 的原合同与有限资格见 [历史 T05](census-retail-r1.md)，保持不变。

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
300-second deferral. The source component itself provides no polling or worker. The caller must retain
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

The bound DEV retail query adds `publication_events` and each month's nullable
`publication_event` using the retained Census `calendar.events`, including past
events absent from the execution queue and future months without numeric inputs.
The shared calendar projection retains official source/digest evidence and labels
times as scheduled plans, never actual publication or revision availability.
No matching month remains null; TBA/unknown retains an `unknown` event with null
time. The table shows both headline and MDF control rows in Beijing time, with
explicit no-record and pending-time labels. No date is extrapolated. When an
`as_of` cutoff applies (observed or available), a calendar acquired after that
inclusive cutoff is withheld in full; the current cache cannot reconstruct an
earlier calendar version. GET only reads the cache, without refresh, collection,
schedule writes or database writes.

Retail table amounts use exact string digit grouping and trim only insignificant
fractional zeros (`745021.000000000` → `745,021`). Official MPCSM is already
percent (`0.800000000` → `0.8%`, `-0.400000000` → `-0.4%`, `0E-9` → `0%`),
without multiplying by 100. Nonzero scientific notation remains exact. These
display rules leave the raw HTTP body, stored values, ratio formatting and other
product families unchanged.

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
This standalone collection service has no scheduler or global retry admission;
the shared Host's periodic scheduler is described below.
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

At this standalone-backend stage, shared UI, activation and calendar/worker were
unbound; subsequent explicit bindings are documented below. Root owns independent code review, isolated PostgreSQL
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
were unchanged. At that verification stage, a long-lived retail schema and shared runtime were unbound.

## Shared DEV Web binding (2026-11-01, implementation scope)

The shared `create_timeseries_preview` / `TimeSeriesRuntime` now accepts explicit
retail bindings. The implementation task did not switch the running DEV service,
create its long-lived schema, grant privileges or make a real Census request. T05 remains
`source_qualified=False`; its fixed snapshot is navigation metadata, never proof
of continuous qualification. Policy/calendar worker code is described below;
runtime enablement and natural publication verification require separate evidence.
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
be retained across restarts. Manual unknown outcomes require explicit resume;
the official-calendar worker below handles its separately planned periodic work.

Read back via GET `/query?start=2026-08&end=2026-09&local_only=true` and optionally
history with URL-encoded `as_of=...%2B08%3A00`. GET never performs DDL or writes.
Six-input coverage includes the comparison buffer; absent Census rows fail the
job's completeness check, explicit null/suppression remains visible, never zero.
The shared asset page reuses query/help/raw response/copy/calendar/table controls;
shared new-collection and execution list/detail are the only retail workbench.

### Execution accounting

Manual execution coverage counts the six source inputs in the requested range; the preceding 12 months are reported separately as calculation buffers. Confirmed source row absence completes with a source-coverage gap, while local readback or collection failures remain unsuccessful. New observations and changes to existing inputs are separate counters. The service retains `saved_points` as total appended versions and adds `saved_observations` / `saved_revisions`; shared execution details display the latter two. Logical provider requests are counted separately from wire attempts and retained batches.

## 官方日历周期采集（DEV 显式绑定）

共享周期任务页预置零售单行，只有启动/暂停。策略、真实 worker 心跳、
官方下次事件、月份计划和真实执行分别显示；启动策略不等于 worker 已运行。
GET strategy/schedule/executions/status 不启动、不请求 Census、不建表、不补历史。
`PUT` 或 `POST /api/r5/retail/timeseries/strategy` 只接受 `{"enabled":true/false}`；
沿用共享的 Origin、权限与凭据准入。零售 OpenAPI/生成 TS 位于
`web/src/retail.openapi.json` / `retail.generated.ts`。

官方表只解析 Advance Monthly Retail Trade Report 的 Data Month；08:30
America/New_York 随 DST 转 UTC 持久化，页面显示北京时间。TBA、移除、失效日历
阻止执行，不推算日期。日历六小时刷新，缓存最多有效24小时；失败记录保留，
成功获取时间不前移。刷新失败按5/15/30/60分钟退避，429遵守共享 Retry-After。
过去且未跟踪的发布不排入旧历史队列，已跟踪同月改期保留执行身份与完成状态。

当期一次采集六原始输入和12个月比较缓冲。请求成功但目标月任一输入缺席，
继续等待，不能以旧月份或已有本地值判当期成功。每阶段最多10次、每事件最多
预留30次 HTTP；15/30/60/120/300/900/1800/3600秒退避加0–2秒抖动。
日历、手动、周期和当前补缺口请求共用独立 Census 控制文件：滚动24小时400次
实际请求、串行准入、最小间隔及429冷却。失败和超时也计预算，不回退 provider。
当期完成后只做一次最近12个月历史核实（另加12月缓冲），不重跑240月初始化；
更早修订需手动指定范围。原控制组/比率/官方 MPCSM 和版本时间语义保持。
计划时间不是实际发布时间，响应观察与本地入库确认分别记录。

Runtime 新可选参数 `retail_schedule_path`、`retail_request_control_path`；省略时
在既有 retail_job_directory 的父目录分别使用 `retail-schedule.json`、
`retail-census-request-control.json`，策略为 schedule 同目录 `retail-strategy.json`。
保留原零售 jobs/artifacts 和四族路径，拒绝复用其他族状态或请求控制文件。
Web CLI 对应 `--retail-schedule-path`、`--retail-request-control-path`。
独立共享 worker 使用 `scripts/mdf_cpi_timeseries_worker.py --asset retail
--opt-in-retail-worker --allow-collect`，并显式传入 `--retail-schedule-path`、
`--retail-job-directory`、`--retail-artifacts-path`（必须含 artifacts 目录）、
`--retail-request-control-path`；其他 config/package/environment/pg 及原 CPI
占位路径参数沿用已有 worker CLI。Web 与 worker 必须指向同一套零售路径。
暂停 worker 仍写真实心跳，但不刷新日历或采集；UI 暂停返回后没有新的来源调用。

手动与周期共用零售 journal 串行锁，每次尝试记录 request_id=collection_id。
已知来源未就绪使用新尝试身份，保留旧结果；中断/本地提交未知停止自动重写。
先查 executions 的真实身份和分类，通过既有 `POST jobs/resume` 显式用原身份、
原范围核实 retained batch，成功后 worker 只读取已完成回执并解除该月份阻塞，
不重发已保留响应。预算耗尽需人工检查，停启不重置预算或执行进度。
T05 `source_qualified=false` 保留；以上不声明完成真实未来发布或 DEV 部署验收。


### Current query partial results

A current read retains trusted local target inputs when Census gap filling fails
with a timeout, transport failure, or retryable HTTP status (408/429/500/502/503/504).
HTTP 200 then carries `status: PARTIAL` and `source_failure` with safe
`classification`, nullable `http_status`, and nullable UTC `retry_at` derived from
Retry-After. Requests/attempts include the failed request. Without trusted local
target data the source error still fails. Local, permission, binding, parameter
and source-contract errors are not downgraded.

`missing_input_count` counts absent input cells including comparison buffers.
Unrequested inputs remain `not_provided` (no evidence), never official missing.
`last_completed_month` is the previous calendar month in UTC;
`source_skipped_months` lists absent input months later than this bound.
These months are never requested from Census, but remain in the full descending
response frame. This bound does not assert publication: older missing months
remain eligible regardless of the latest local month or release calendar.
`OK`/null `source_failure` does not guarantee complete input coverage.
History and local-only queries never request Census; queries never persist.


HTTP 204 with an empty body is handled only by the read-only query projection:
`status: PARTIAL`, `source_failure: null`, and `source_no_content` entries with
`classification: census_retail_no_content`, `http_status: 204`, `response_bytes: 0`,
and the actual requested `start_month`/`end_month`. This means no records were
returned by this exchange, including when there are no local target inputs. It
is not a service failure, official null, publication or release-time evidence.
Absent inputs stay `not_provided`; no synthetic HTTP 200/JSON batch, raw hash,
revision or persistence is created. Collection retains its previous failure/wait
behavior. A nonempty 204 body remains a source-contract error.

## Retained official calendar display

The continuous DEV host reads the cached Census release schedule and annual
`https://www.census.gov/economic-indicators/calendar-listview-YEAR.html` tables.
Only the exact **Advance Monthly Sales for Retail and Food Services** rows and
explicit **Period Covered** months are accepted. Release dates never imply the
data month. America/New_York timezone rules preserve DST; the shared macro table
renders Beijing time, planned, pending, cancelled and unmatched states for
CPI/PPI/PCE/employment/retail alike.

An enabled periodic worker maintains the current and previous **release years**
(seven-day annual cache; independent refresh errors). Annual calendar HTTP shares
the existing Census admission file with current-calendar and data requests.
Explicit named-year imports support older evidence without a twenty-year
coverage claim. The supplied 2025/2026 annual calendars contain 24 records,
covering reference months 2024-12 through 2026-11. December 2026 requires explicit
evidence from the current schedule or a subsequent annual calendar. October 2025
maps to December 16, 2025 at 08:30 EST; November 2025 maps to January 14, 2026 at
08:30 EST. No lag arithmetic or XLSX inference is used.

Annual imports only add calendar evidence, never execution targets or data jobs.
Current-calendar evidence wins for the same month; conflicting annual evidence
uses the shared unknown/cancellation rules. Rolling current calendars retain
past months before the new coverage begins. Explicit cancellations, TBA,
retimings and removals within current coverage (or of future releases) supersede
retained plans. The existing scheduler lock and atomic replace are reused.

GET never fetches calendars or writes schedule/DB state. Each imported record
retains source URL, SHA-256, acquisition time and the original Period Covered,
release date/time cells. `as_of` withholds evidence acquired after cutoff, also
for the other four macro families. Unknown acquisition times are withheld for
historical cutoffs. This cache is not a complete archive of successive calendar
revisions: an overwritten plan may be unavailable for earlier cutoffs. Planned
calendar times never establish publication, revision availability or ingestion.

Calendar-only maintenance (root supplies the **existing DEV** paths):

```sh
PYTHONPATH=src uv run --no-sync python scripts/import_retail_calendar_history.py \
  --state "$RETAIL_SCHEDULE_PATH" --request-control "$CENSUS_REQUEST_CONTROL_PATH" \
  --fetch --year 2025 --year 2026
```

This explicit fetch respects the shared budget and durable cooldowns. Alternatively
use one `--year YEAR --html FILE --acquired-at ACTUAL_OFFSET_TIMESTAMP` without
`--fetch`/`--request-control` for offline official HTML. Keep original files and
runtime caches outside Git. The entrypoint validates an existing retail schedule
and preserves its history start, targets, strategy and task identities.

历史日历维护优先级低于数据采集：距离计划尝试不足两分钟、重试已到期或存在运行阶段时，worker 暂缓年度日历抓取，继续现有采集流程。
