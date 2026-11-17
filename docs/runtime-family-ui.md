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
Registered reads publish each family's verified rows independently; a slow or
failed peer must not hide the others. A selected family's optional execution
facts retain the exact asset revision and request identity while its detail
read is pending. Errors remain visible and pending facts never authorize writes.
TDX details use `GET jobs/{id}?view=summary`; shards use `view=steps` with
explicit search and server pagination. The default GET stays compatible with
full historical readers. Execution lists discover Macro protocols once per
scope/refresh; live status polling is limited to the selected asset. Check these
read paths with `TdxExecutionRead.test.tsx` and `check-tdx-execution-read.mjs`.
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


### 采集生命周期与详情外壳

`CollectionBriefs`、`CollectionFormLock`、`ExecutionBriefCard` 与 `ExecutionDetailShell` 是实际产品共用组件。受理后锁定原命令并按原号 GET 跟踪；运行、未知、未核实与在途状态不能重开。终态还须通过领域限制（金融未决步骤、TDX 提交效果未知等）。仅当前卡片出现“开始另一项采集”；点击清除旧意图与确认，保留已核实记录，新的明确确认才能 POST 新号。`useCollectionSubmit.reset()` 使旧轮异步结果失效；改变资产/命令作用域的旧回执不能更新当前状态。

详情共同顺序为标题、编号/资产/来源/范围/状态/时间、操作区、领域扩展。发布材料、名册证据、分片、失败与覆盖保留各自真实口径。恢复只在注册了实际 `resumePath` 且该记录状态支持时显示；完成记录不渲染无意义的灰色续跑。所有直接恢复写操作须在共同操作区确认原请求；金融、TDX 保留各自已有协议与回执检查。来源准入不足会禁用恢复并说明原因。

稳定验收元素：`aside[aria-label="执行简报"]`；当前卡片 `.execution-brief[data-current="true"][data-execution-id]`；公共详情 `section[aria-label="执行详情主体"][data-execution-asset][data-execution-id]`；操作区 `[aria-label="执行操作"][data-execution-id]`；选中列表行 `tr[aria-selected="true"][data-execution-asset][data-execution-id]`（同时保留原 `data-record-id`）。按钮为“开始另一项采集”“查看资产数据”及真实周期入口，恢复按钮保留领域名称。已核实简报通过卡片进入原资产/原编号详情，不另设重复回查；仅尚无可查看记录时保留“核实原请求”，只 GET 原号。详情不设自指回查，沿既有可见页轮询自动跟踪在途状态。结果入口保留资产/版本与 `job`，目录显示“关联执行”及“返回此执行记录”；手动切换资产清除旧关联。

`collectionRegistry` 的 `intent=server-offer` 标记兼容入口：CPI/PCE 单次、指定回补、旧计划批次使用服务实际提供的编号/唯一后继资格，不能改成浏览器随意造新 UUID。没有新的服务准入编号时不显示“开始另一项采集”；原编号仍可跟踪、查看或按资格回补。旧计划回补回执指向周期计划/原批次，不冒充新的执行编号。这些为真实协议例外，矩阵必须验证，不能删除登记以绕过。

`CollectionSubmitMatrix.test.tsx` 由产品登记及实际目录/能力自动枚举，要求每条路径完成 accepted/lost/denied/lifecycle 证据；领域协议 fixture 均禁止外网与 PG，走真实 ASGI 路由和隔离 journal。生命周期用例检查未知防重、锁定、新确认新号、实际能力恢复、简报/列表/深链/结果返回身份；跨族用例故意复用请求号并迟返旧详情。新增族使用共同组件、登记真实 `resumePath`/编号能力，新增协议适配器后必须通过整张矩阵。PR 的前端 CI 使用该矩阵驱动 Chromium 实际点击构建产物。每个可采集资产验证正常与丢回执，沿正常路径验证锁定、重开、新编号、列表切换、刷新、查看结果与返回。名册和消费记录还故意共用同一编号，并延迟返回旧请求。写入限定在隔离的实际作业路由和临时日志。DEV 交付时应在最终预览地址复验，并点击真实已有任务比对详情与操作。


简报、列表及直接深链必须无需预备点击即可选中同一资产/编号并显示详情；直接链接到后续页的任务应定位到该行。生命周期矩阵和 `check-collection-submit.mjs` 观察自动 GET 后的可见状态，分别验证卡片导航、重开、结果返回与重新加载；不增加操作按钮供测试驱动。全部资产列表按实际服务能力选用 PCE 新协议或已绑定旧协议，能力探测不受所选执行记录的 operation 限缩。不能同时探读未启用旧协议并把其失败聚合为全页陈旧；真正被选用协议的读取失败仍须显示。

### 执行事实、动作与周期绑定

`ExecutionFacts` 将数据来源和触发方式分别呈现；缺失或未知来源保留待核实。`ExecutionNavigation` 在全局详情边界提供默认导航，`ExecutionDetailShell` 按资产、版本和原编号匹配，族组件不能用私有回调越过核实结果。`collectionRegistry` 的 `strategy` 声明周期行身份及实际只读端点；Macro 经 profile、注册族经 `RuntimeFamily.collection` 继承。无策略须显式 null。声明本身不证明可用：策略读失败、无权限或响应未核实不产生周期按钮；已暂停的策略仍可查看。Macro 的 `exists=false` 表示启用意图尚未持久创建；若真实 GET 已核实预置策略，它在既有周期工作台仍有可选中的预置行，允许查看，不冒充原记录的自动计划。手动作业也可查看已核实的本资产当前策略，页面明确这不证明原作业由该策略触发。历史批次仅定位唯一的实际关联计划，多个候选不选最后一个。周期页深链不存在时不展示其他策略冒充。

`PeriodicNavigation` 统一注册策略详情的读导航，保留资产/版本；从执行进入时保留 `job`，可直接返回并选中原执行。人工选择另一资产策略清除旧执行关联。公共验收元素为 `[aria-label="周期操作"][data-periodic-id][data-periodic-asset]`、周期表选中行 `tr[aria-selected="true"][data-record-id]`，以及既有执行选中行/详情身份。兼容历史计划仍使用真实 plan identity 与原协议操作引用。

新增资产/API 的验收从登记能力、独立协议响应和作业状态推导必需/禁止动作，不能先查找可选按钮再决定测试哪些行为。`ExecutionNavigation.test.tsx` 检查每条现代登记的绑定、权限/未持久创建的预置策略/未知来源/迟返隔离及历史歧义；`CollectionSubmitMatrix.test.tsx` 从隔离 ASGI 读取策略证据，实际点击每族的周期入口，断言选中行、资产、版本、原编号及原执行返回；恢复能力从隔离实际路由的 OpenAPI POST 契约推导，不能用缺失的前端 `resumePath` 同时省掉按钮和断言。继续验证结果往返、恢复的真实 POST 与同号状态。旧计划的实际点击往返在 `App.test.tsx` 的 F2 用例。现有领域发布材料、分片和 server-offer 恢复约束不变。新增协议须为这些行为提供独立 fixture；缺声明、错目标或私有回调不得当作可选按钮跳过。

名册的周期目标为自身的 `tdx-roster-auto` 行，策略证据读取既有 `/api/r5/tdx/daily/strategy`，复用同一执行器。隔离 fixture 只额外准入这个精确 GET，不开放日线作业写入；没有另一个名册 worker。返回原执行保留进入前的全局执行筛选，并重新定位列表页码；手动选中第二页以后记录的策略往返回归见 `CpiCollectionWorkflow.test.tsx`。新建采集继续使用原目标选择和表单页签，不产生隐式提交。

具体策略的“查看执行记录”同时限定资产/版本和策略，仅收录协议明确关联的自动记录；同资产手动及来源未知记录不混入。无自动执行显示真实空态；“返回此执行记录”清除策略筛选并定位原手动编号。异步读取其他族造成重排时，`WorkflowTable` 跟随明确定位/选中的编号；用户搜索或翻页即停止跟随，新的明确定位重新生效。公共简报允许长结果和编号完整换行。

构建 Chromium 矩阵的周期预期由 `scripts/collection-protocol-evidence.ts` 读取隔离协议证据产生，与产品声明及可选按钮独立；缺按钮、错目的地和混入手动记录均失败。新协议在这一处增加适配，确无策略返回 null；注册族的真实周期行由 `RuntimeFamily.readPeriodic/periodic` 核实，不默认解释为 TDX。回归含无策略可采集 API、非 TDX 新族及 1440/1180 宽度长简报。名册从“新建采集”页签进入原资产表单，实页脚本不再要求重复按钮。

执行索引直接读取登记协议，不以整库 `/status` 汇总作为前置发现；只有所选资产核实读写能力。Macro 策略 GET 支持 `Prefer: return=minimal`，同源鉴权和真实策略不变，省略可选数据统计；旧客户端默认完整响应。策略导航、分期计划、原号详情不得等待统计。缺少统计或正在读取时标为尚未核实，不充作零值或无成功。可选统计随所选资产的发布计数、最新期与策略回执变化重新核实；状态读取失败时不沿用旧统计确认，页面刷新强制重读。`MacroExecutionRead.test.tsx` 覆盖所有登记 Macro 的延迟隔离；CI 的 `check-tdx-execution-read.mjs` 在合成的两个 5582 分片作业上验证原号、策略往返、分页和搜索，其中一个仅有实际分片、没有 `base.json`。离线 fixture 禁止外部网络和 PostgreSQL。

周期详情使用 `PeriodicDetailShell`，统一顺序为标题、已核实导航、领域正文。注册族可提供 `PeriodicEntry.title`，缺省继承族标题；正文保留真实领域内容，不能在公共导航之前另放资产主标题。历史 CPI 计划保留自己的批次/operation 导航。构建矩阵从登记与实际策略协议取样，在 1440/1180 两种宽度检查标题位于详情内边距起点、导航在标题之后和正文之前。

`collectionReadScope` 是 App、Macro/金融索引和 RuntimeFamily 的资产/版本/筛选判定入口：显式 all 随原号保留；没有筛选的资产或来源深链仅读取那个版本。普通工作台可声明 `CpiAvailability.discover`，用只读执行索引核实协议（含周期页），不以整库 status 为前置条件；独立接入方未声明现代能力时不探读其族。仅明确的 CPI/PCE 现代协议缺失可转原兼容入口，其他族不借用 CPI。200 的索引空态、未装配、真实读取失败分别呈现；聚合失败不否定已成功核实的策略，策略失败也不能冒充聚合在等待。旧夹具须在未实现的族路径返回真实未提供响应，不用 CPI 成功兜底。
