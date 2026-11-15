# Registering a family in the shared workbench

`web/src/runtimeFamilies.ts` is the default entry for a non-Macro data family.
A `RuntimeFamily` supplies its exact asset identity/revision, title, local query,
API help, collection parameters, execution detail, and read adapters for execution
and periodic facts. Register the adapter in `RUNTIME_FAMILIES`; do not add an
asset-specific page branch in `App` or create another task table.

The normal source directory must expose the asset's binding, and the package
catalog remains responsible for runtime visibility and permissions. Source deep
links must match the exact asset/revision; neither registration nor an empty
local read authorizes a provider fallback.

`RuntimeAssets` owns catalog search, package filtering, selection and expansion.
`FamilyAssetReader` owns the normal query/API tabs. `CollectionWorkbench` owns
creation target selection, task tabs, independent asset filters, execution
ordering, pagination, search and the two-pane execution layout.
`CpiStrategyWorkbench` accepts an array of `PeriodicEntry` supplements alongside
its existing Macro and financial entries, retaining the existing strategy
switch, desktop detail pane and narrow-screen drawer interaction.

`useFamilyRecords` reads only registrations matching the active global filter.
Execution row identities include the asset revision and request identity, so a
shared request ID across families cannot select another family's detail. The
route owns the selection; reloading or returning restores the named object.
Ordinary row selection keeps the search and filters. Initial deep links and
submission receipts can locate their row across table pages.

Adapters must describe their own domain facts. TDX uses native price basis,
market membership, calendar/suspension evidence, durable collection shards and
provider request counts. It must not use Macro month/series coverage metrics or
present scheduler heartbeat as a latest data period. TDX query, row pagination
and independent missing-item pagination use local GET reads. Time instants are
validated with a zone and displayed in Beijing time. Mutations are explicit;
a lost submission receipt is recalled by its original request identity without
automatic resubmission.

`RegisteredFamilyPaths.test.tsx` runs the same user path for TDX and a synthetic
future registration through `App`: package to catalog, API help, standard target
and form, submission, global execution filters/search/details, reload, and global
periodic details. It also checks exact source deep links and conflicting revisions.
The synthetic registration uses the same shared renderers without an App branch.
A restoration of the old whole-page bypass fails these path assertions. Keep
provider execution in separate opt-in tests; these fixtures never use a network
listener, LAN database or live collection.

Daily query and collection forms use `DailyFormControls`: `DailyDateRange`
(implemented with the product `DateField`), `HistoryCutoffFields`,
`QueryResultTabs`, `CollectionColumns`, `ExecutionBriefCard` and range validation.
Financial daily and TDX render these actual controls. The shared workbench always
owns the collection target, including for registrations with a minimal leaf.
A family must retain its domain defaults and limits: TDX's fourteen-day lookback
is independent from the financial family's fourteen inclusive calendar days.
Initialization and update drafts remain independent; the submitted range is the
range shown in the form. Recent execution cards come from persisted jobs and
navigate to the global named execution detail.

Historical cutoffs accept fractional Beijing wall-clock seconds without passing
through a millisecond-only `Date` conversion. The preview and cURL reflect the
current validated request. Edits cancel in-flight queries and clear their results;
controls never trigger an implicit query. Row and missing-item pages remain
separate. Pending summaries do not gate the form or appear as empty holdings.

`tdxJobOrigin` presents the origin returned by the jobs API. The API records new
manual/periodic intents explicitly and qualifies old automatic slots by the
resident strategy's exact UUIDv5 namespace/name contract. Other legacy origins
remain unverified rather than being labeled manual from an action or UUID
version; the frontend does not duplicate that provenance derivation.
`DailyFamilyForms.test.tsx` exercises the same actual query/collection interactions
for financial daily and TDX, including precision, previews, cancellation, result
views, independent drafts, exact submitted ranges and recent-job navigation.

DEV 18766 may compose the existing Macro capabilities using the explicit
`--allow-unified-dev-macro-operations` flag together with `--tdx-state-directory`
and `--tdx-dev-port 18766`. Other listeners reject this opt-in before constructing
runtimes. The socket adapter and ASGI routes use the same server-owned policy;
they validate the actual private peer, Host, Origin, framing and existing exact
path/method allowlists. No request authority is rewritten or forwarded. Existing
per-capability collection, strategy, source and package flags remain mandatory.
Use the existing Macro job/schedule directories and worker window configuration;
this flag starts no Macro worker and changes no strategy intent.

`QueryPagination` is the actual shared financial/TDX result pager. Each displayed
page corresponds to one API cursor; there is no additional client pager inside
the TDX result table. Previous/next navigation remains available at terminal pages.
`LocalQuerySummary` retains holding details without pushing the query form below
a long metric list. Both components live in `DailyFormControls`.

For read-only deployed acceptance, run `web/scripts/check-live-workbench.mjs`
with the origin and an evidence directory. Set `MDF_PLAYWRIGHT_MODULE` to an
installed Playwright module and `MDF_BROWSER_EXECUTABLE` to Chromium. The default
cases exercise financial daily, CPI and (on 18766) TDX through the actual catalog,
query/results/raw response, API help, collection target, real recent jobs,
global execution deep links and periodic details. Every mutation is blocked.
Query 4xx, absent result rows and missing-only results fail. The checker respects
CPI's `points` schema and daily families' `rows` schema. A JSON case array supplied
by `MDF_WORKBENCH_CASES` can register another family's row selector, exact asset,
API prefix, row/value properties and form parameters without another page script.
Cases can also provide `selects` / `fields` by actual control label,
`expectedRows` with exact returned field values, and `periodicContains` for
required schedule facts. Register the same asset at several `history` / `cutoff`
values to check a correction immediately before and at its actual publication;
query counts are independent per case. Keep live sample values and receipts in
the transaction evidence directory.

`rangeWithDefaults` merges daily collection defaults per untouched field. An
operator may edit a start date while status is loading, switch collection modes,
and receive the remaining end-date default without losing either draft. Pending
financial receipts pin both submitted fields. TDX's immediate fallback follows
the same Beijing 15:05 boundary as its backend; server defaults still qualify the
untouched fields. Daily row reads and coverage reads have independent loading
and cancellation states: returned rows remain pageable during reconciliation,
and paging rows does not duplicate an in-flight coverage request.

The stock roster is a separate base registration:
`mdf.market.tdx.stock.roster@r1`, `/api/r5/tdx/roster`. Its query, refresh form,
execution records, deep links and shared 09:00 roster / 15:35 daily Asia/Shanghai trading-day schedule use the existing
family renderers. The API and daily collector consume `tdx_roster.published`;
refresh uses the same `DailyJobs` implementation, journal and lock with the
legacy `action=universe`. The old daily universe/job routes remain compatible.
Daily reads/receipts retain the exact publication identity. Missing or incomplete
rosters fail before provider requests. Stale exchange evidence remains explicitly
unqualified for full-market coverage; a published complete same-day provider
directory can still support acquisition under the existing daily policy. Its
directory-only candidates remain separately identified and traceable.
Resume retains the original publication for already-created shards. Provider
refresh never extends exchange lifecycle validity or upgrades unverified code
lineage. The UI reports current-directory-only, missing and stale evidence.

18766 package administration additionally requires the existing
`--allow-package-config-save`; unified DEV admission does not grant it.
`NativePackageStore` preserves shared `packages.json` byte-for-byte when editing
native TDX capabilities. An atomic `tdx-capabilities.json` in the same private
configuration directory holds only `daily_native` and `stock_roster`, under the
same configuration flock. The public CAS revision binds the legacy and native
snapshots, so a legacy TDX edit invalidates a pending native save. Other packages
and credential references remain in their original store. The old `daily_raw`
capability remains managed by the reference host and is visibly read-only in
this composition; it is never silently enabled as a native alias. No runtime
configuration or secret belongs in Git.

Financial periodic status uses each source group's latest maintenance slot;
older exhausted failures remain in execution history and do not permanently
poison a later successful slot. A current exhausted failure remains actionable,
with its actual reason, attempts and times. Claims separates a completed local
readback from an unverified publication report, rolling HTTP budget deferral,
and history waiting for target verification. Read-model annotations do not
rewrite durable failures, release a reservation or change retry policies.

The deployed checker now includes Claims and the roster. It still requires real
nonempty local values, HTTP 200 and visible rows, then visits help, collection,
real job details and periodic details from the same catalog. A pre-roster host
fails rather than passing a form-only check. Use `MDF_WORKBENCH_CASES` to select
reference-host families or another registered case.

Roster pagination pins the returned `snapshot_id` for forward/back navigation.
A new explicit query chooses a new publication; edits clear the prior result.
The actual request preview and cURL include the page cursor and pinned identity.
The native configuration store accepts the product form's single TDX `keep`
intent, while rejecting replacements, unrelated sources and duplicates.

### 新增 API 的按钮流程验收

涉及页面的新增或变更 API，在同一 PR 的共享流程用例中登记资产、入口按钮、请求方法及次数、提交后的页面与回执、显式详情入口和失败恢复。由开发者先执行。新建采集的共同预期为确认后一次 POST、停留原表单、右侧简报显示同一请求编号、主动点击简报才进入该作业详情。断响应后按原编号 GET，禁止重复 POST。查询/翻页/刷新只读；启停和续跑在隔离状态中检查真实变化。

实页只读浏览脚本只证明已走过的读路径。写按钮应在交付构建上实际点击，用隔离 DEV 作业服务执行 POST 并回读；记录哪些写入被隔离、点击前后页面、请求编号与结果。共享用例见 `web/src/RegisteredFamilyPaths.test.tsx`，两个 TDX 表单的部署构建复验见 `web/scripts/check-tdx-submit.mjs`。出现新按钮或预期不同，在同一变更中补对应断言，不将逐钮回归交给 Owner。

### 共用采集提交契约

`useCollectionSubmit` 负责单个确认意图的一次 POST、同号回查和回执匹配；留在新建页，只有点击简报才进入详情。族适配器保留命令、持久意图、明确拒绝及续跑规则。Macro/legacy PCE 用真实列表 GET 匹配原号；支持单号读取的族用原号 GET，回执未知不重发。TDX 未核实编号在当前标签页刷新后仍保留。

新增采集能力须登记实际用于目标选择/提交准入的 `collectionRegistry`（Macro 通过 `MACRO_PROFILES`，注册族通过 `RuntimeFamily.collection`）；读写准入仍以真实状态能力为准，缺失权限按未核实处理。无执行器的目录资产列在只读说明中，不借用其他族提交。

TDX 日线查询沿用金融日频的本地优先语义。`DailyQuery` 只读本地发布；缺口通过同一绑定源的无留存读端临时补齐，不调用作业或入库端。18766 的 `--allow-tdx-source-query` 独立于采集权限；`local_only=true` 或显式 `as_of` 始终不补源。单日截面的 `total/offset/next_offset` 以已发布名册候选计数，可能出现空页，必须沿返回游标并固定 `snapshot_id`。临时值、未完成日 K、真实观测时间和缺口由结果表及原始响应共同展示，不计入本地 coverage。确定性回归见 `test_tdx_daily_query.py` / `TdxEphemeralQuery.test.tsx`；`web/scripts/check-tdx-query.mjs` 从交付目录进入，分别记录真实只读和显式模拟盘中，两者不可互相代替。

`CollectionSubmitMatrix.test.tsx` 从上述产品注册和后端目录/包能力自动发现资产，以正常 App 入口、独立 ASGI 协议 fixture 检查成功、丢回执、关闭能力三种场景，要求每条登记路径都有完成证据。新协议只需增加隔离适配器，无需维护第二份资产名单。增加族的恢复操作时继续保留相应领域测试；矩阵不代替初始化范围、续跑或调度语义验收。
