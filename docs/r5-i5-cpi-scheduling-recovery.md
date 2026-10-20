# R5-I5 CPI bounded scheduling and recovery candidate

2026-10-21 R5 material correction 仅授权隔离源码、测试和必要说明修正。
下文早期候选的 live 探测与命令是历史记录，不构成本轮运行授权。
本轮结果、限制和文件范围见文末；没有执行 Provider/PG/Garage 请求或交付。

本候选仅修改隔离工作区 `r5-i5-cpi-scheduling-20261020`。分支为
`mdf/r5-i5-cpi-scheduling-20261020`，fresh 核对的起点为
`80acbfc52f9d94fe0335f4d428a3830cb7e89557`。没有 commit、push、merge、
canonical main/旧 T10 修改、部署、Cron/Worker/listener 激活或外部存储操作。
Host QA、独立审查及原生 Git/CI 交付由 AO 后续编排；本文不是这些门的通过证明。

## 实际能力与来源语义

- 新增固定四条 BLS CPI-U 指数的历史契约 `r5-cpi-history-index-v1`。
  任意合法参考月不再受 `2026-09` 上限约束；实际可用性由服务返回值决定。
  每计划最多 360 个月、12 个周期，逐月分批，每批只请求参考年及前一年，
  所以跨越单请求认证跨度的区间也不会变成一个超限请求。253 个月区间
  `1990-01..2011-01` 的拆分与实际有限批次执行已有 synthetic 回归。
- 原始单位为 `index_1982_1984_100`；环比使用 `CUSR0000SA0` /
  `CUSR0000SA0L1E` 的季调指数，同比使用 `CUUR0000SA0` /
  `CUUR0000SA0L1E` 的未季调指数。比较月分别为前一个月及上年同月。
  `(current / comparison - 1) * 100` 使用 Decimal、百分数一位小数 half-up。
  输出明确标为 `mdf_index_derived_change`，不冒充 BLS `pct_changes`；
  原始指数、比较指数及双方脚注保留在内容中。读时重算验证这些描述与算术。
- 缺失、`-`、比较指数为零、部分序列及非空 provider warning 都不能被算作
  完成批次。projection 可表达 incomplete，但该批次不会产生完整 Publication。
  不利用 HTTP 200 或 `REQUEST_SUCCEEDED` 掩盖 warning/truncation。
- Acquisition/内容保留实际 requested_at、obtained_at 和 wire attempt 数。
  provider release time、revision、vintage 未声明，
  `historical_as_of_verified=false`。当前取得的历史序列可能已修订；既不补造
  原发布日期，也不声称历史首次公布值或历史时点已核实。
- R5-I4 的 `2026-09` release slot、provider-calculation parser、原有 journal
  文件与字段保持兼容。新历史契约有独立 Source/Binding revision，继续使用
  原 Definition、canonical Version/Publication、受保护 Raw materialization、
  独立 exact 读回和既有恢复机制。没有 T09 FOMC/FRED handler 伪装成 CPI。
- canonical `as_of` 仍要求唯一 eligible Version；多个版本时保留
  `as_of_ambiguous`，不改成 latest-wins。历史批次用 exact Version 验证完成。
  I4 原有 as-of 歧义行为也保留。04/06 可携带具名 reference/version/as_of
  精确读取；读取不触发采集或补写。04 的既有版本目录仍有 128 Version 预算，
  超预算显示 UNKNOWN；不由本笔默默放宽目录或 as-of 选择规则。

来源依据为 BLS 当前官方 [v2 signature](https://www.bls.gov/developers/api_signature_v2.htm)
及 [FAQ](https://www.bls.gov/developers/api_faqs.htm)。文档区分注册/未注册的请求
额度、年份跨度与可选计算权限，且旧段落存在不同措辞；实现没有假定每个凭据
都能取得全部计算选项。新路径关闭 catalog/calculations/annualaverage，使用
保守两年请求。Host 必须明确给出 `HistoryQualification` 的当前 tier、跨度和
**分配给本 journal 的剩余 wire 预算**。registered 路径只加载显式 BLS key，
失效时失败，不静默改用未注册或其他 provider。

## 持久计划、控制、失败窗口与恢复

`CpiPlanStore(journal, initialize=True)` 由 Host 显式初始化本地 SQLite 新表；
原 `cpi-jobs.sqlite3` 不改名，不改原 Job schema。只读 reader 不建目录、表、锁、
Job 或 Attempt。计划身份不可复用为不同规格；重复相同接受返回同一持久意图。

计划包括名称、历史起止月、IANA 时区、每日半开窗口 `[start,end)`、首个 aware
到期时刻、绝对秒数周期间隔与有限周期数。每周期重新观测指定历史区间；
不是自动发现新发布月份。到期未执行的工作保留断点，每个显式 tick 最多推进
一个月，不无限 catch-up。时区窗口按该时刻的 ZoneInfo/DST 换算；周期间隔
按绝对秒数推进。暂停/恢复/取消在有限批次之间经同一 flock 生效；忙时返回
`job_executor_busy`，不假称已经暂停。取消不能撤销已有 Raw/Publication。

`CpiScheduledWorker.tick(identity, enabled=True)` 是库组合点，没有 product HTTP
run endpoint、daemon 或自动启动。Host 必须提供已有 `CpiLocalJob`、独立 reader、
材料保护、preflight、worker identity、显式 acquirer 和相同 journal。
preflight 在配额预留前及既有执行器中核查目标；不推断 PG/Garage 客户端。

计划先持久保存 child Job 映射和 inflight key，再接受 child，再预留最坏的 3 次
wire attempt，再进入来源/存储效果。配额预留不因结果丢失退还；按 UTC 日持久
记账，跨计划串行限速至少 1 秒。实际 transport retry 复用 MDF 统一的 3-attempt
策略与退避。Host 仍须对共享 key/IP 的其他客户端扣减预算；本地表不能观察
所有外部使用量。

每月每周期有独立 Job；每个 Job 最多一个 Attempt。指定失败窗口回补会建立
新的 child Job，前一次不可变 Attempt 及计划映射保留，每批最多 3 个 child。
仅明确 failed/cancelled 且没有 canonical-possible 效果的批次可显式重新请求。
同月其他已证实成功的周期不会阻挡失败周期回补。成功月份不能被“回补”重放。

重启后已有 pending 映射可继续接受/执行；running/outcome_unknown 只进入既有
observation-only reconcile。即使 confirmed-not-published，也不自动重新请求或
重写存储。UNKNOWN 原 Attempt 仍为 UNKNOWN；只有 exact Raw/lineage/content/
Publication 独立读回形成的 `last_success` 才计进度，扫描 cursor 不代表完成量。
无法核实的 UNKNOWN 保持 inflight，阻挡后续自动推进。保留材料修复仍为单独
既有能力，新历史计划不开放该模式。

## HTTP、页面与具名关联

`create_app(schedules_host=None)` 默认 UNKNOWN，POST fail closed。
`CpiSchedulesHost` 显式绑定已有 `CpiJobsHost` 的 Operator 授权、effect domain、
来源 qualification 及相同 journal。Host 必须完成每请求认证/写入及 cookie
CSRF/origin 保护；浏览器 JSON/header/role 不成为身份权威。

- GET `/api/r5/cpi/plans`：受权只读一致快照；最多 50 计划、合计 4320 已建批次、
  每批最多 3 child；单计划持久文档限制 2 MB，超限/坏记录返回 UNKNOWN。
- POST `/api/r5/cpi/plans`：strict DTO 接受有界计划，202 只表示 DURABLE。
- POST `/api/r5/cpi/plans/{identity}/action`：pause/resume/cancel。
- POST `/api/r5/cpi/plans/{identity}/backfill`：指定明确失败窗口。
- 原 Job GET 可精确投影相同 CPI 家族历史 child 的 Job/Attempt/history；
  原 cancel 可对相同 Host 家族 child 请求取消，不能改变已完成事实。
  原列表 100 Job/256 history 预算保留，规模更大时用 identity 查询与计划快照。

05 保持 `新建采集 / 周期任务 / 执行记录`。新表单可接受多期历史或有限周期；
计划状态、断点、已证实进度、失败窗口和每个 child 的 Raw/Version/Publication
可回查。只有当前 Host 明确提供资格与写入授权、并匹配 04 身份/修订时才开放
写入。I4 单月入口继续显示其精确 release slot。02 合同目录增加历史 Source，
当前 qualification/activation 仍是 UNKNOWN，不把源码存在当运行健康。
01/07 关联同一异常 Job，04/06 关联同一资产，06 具名精确读链路包含参考月、
版本及快照时刻。页面 07 仍是平台诊断，没有改成新的产品页。

## 验证证据、live 与沙箱边界

环境是 uv 选择的 Python 3.14.8，依赖与 lock 无变化。沙箱离线 uv sync 缓存
缺少 pydantic-core wheel，执行环境复用既有 I4 虚拟环境的**本工作区副本**，
修正副本 editable/shebang 指向本工作区，使用 `uv run --offline --no-sync
python -m ...`。没有修改系统 Python 或其他工作区。

已完成：

- 新 scheduling 动态/恢复/端点逻辑回归：17 passed，1 HTTP case deselected。
  另一次独立端点逻辑运行通过，覆盖实际 DTO/auth/接受/暂停/读取/Job 投影，
  它不声称覆盖 ASGI 传输与线程池。
- 原 I4/direct + 初版 I5 定向回归：326 passed、1 skipped、1 HTTP deselected。
  两个旧失败已修正：日期上限扩展对应新分类期望；保留 legacy acquirer 的调用
  形状以兼容原 PG ownership 边界测试。原深层材料/commit/credential 负向保留。
- hygiene（含 untracked）、ruff check、ruff format --check、OpenAPI 导出、
  TypeScript contract/check、Prettier 和 build 最终重复通过；git diff --check
  通过。hygiene 含本文及全部新文件共 309 项。
- 扩展离线确定性 pytest：5576 passed、25 skipped、20 deselected、4 failed。
  已排除 4 个 TestClient 文件与新增 HTTP case；4 个失败全部来自未修改的
  `tests/test_tdx_bounds.py`，在 `provider_lab/tdx_bounds.py` 的
  `preexec_fn=trace_me` / `ptrace(0,0)` 启动失败（SubprocessError），
  没有进入其 child 测试逻辑。完整门不是 PASS，须在 Host 复验。
  日志位于 ignored `artifacts/r5-i5-deterministic-pytest.txt`。
- 最终 scheduling 专项复验为 17 passed、1 HTTP deselected；其后更新的端点
  精确读取/取消投影、OpenAPI 与来源合同 4 项检查全部通过。

实际 BLS 开发请求：unregistered raw-index POST，目标 `2024-12`，实际请求
年份 `2023..2024`，四条固定序列，全部可选开关关闭；单次 transport 探测未取得
响应，分类 `transport_failure`。原始请求/分类观测位于 ignored
`artifacts/r5-i5-bls-development/observation.json`。**没有 live 成功或真实数据
物化的证明。** 独立最小 socket 探测确认 AF_INET 创建为 `PermissionError /
errno 1`；AF_UNIX socketpair 可用。没有关闭/绕过沙箱。

未配置应用的 TestClient 在启动前也超时，新的 AsyncClient ASGI 测试在 AnyIO
worker 等待处超时，Vitest 在启动阶段超时。因此完整 ASGI/TestClient/Vitest 门
未通过，也没有标成业务失败。TDX ptrace 测试也保留原样供 Host 复验。Host 必须复验，不删除这些确定性 CI 用例。
没有触碰 `192.168.10.4`、Garage 或其他真实测试库；当前没有足够的归属/隔离
证据可支持外部存储写入/重建。

Synthetic 覆盖：`2024-12` / `2025-01` 的真实领域链路、跨年比较、1978/2001/
2026-10/2027 动态参考月、缺失与坏算术/vintage、253 月拆分、配额/限速、
两个周期、暂停/窗口/取消、指定失败回补、同月跨周期失败回补、flock、重启
映射缺口、来源中断、UNKNOWN commit/reconcile、重复接受、只读零创建、
实际 endpoint/DTO 与授权。全部为 synthetic，不混同 live。

## 可重复 Host QA

先在相同工作区用正常 Host 网络完成依赖准备并 fresh 核对 Git diff，不推送：

```bash
uv sync --locked --group dev
uv run --locked python scripts/check_repository_hygiene.py --include-untracked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest tests/test_cpi_scheduling.py tests/test_cpi_local_job.py tests/test_p4_bls_cpi_direct.py tests/test_r5_cpi_jobs.py tests/test_r5_cpi_api.py tests/test_r5_source_catalog.py tests/test_r5_observability.py
uv run --locked pytest -m 'not live_provider and not postgres_integration'
```

在 `web/` 执行 `npm ci`、`npm test`、`npm run check`、
`npm run contract:check`、`npm run format:check`、`npm run build`。
新增 `CpiSchedules.test.tsx` 通过 Host-only JSON-lines bridge 跑真实
React→ASGI→SQLite→CPI worker，覆盖两月、暂停、失败窗口回补与精确血缘。
bridge 的 tick 控制只存在于测试协议，不是 product API。

真实 BLS 开发验证已在本笔授权内；在允许网络的 Host 保守执行两个月 fixture：

```bash
MDF_R5_CPI_HISTORY_LIVE=1 uv run --locked pytest -m live_provider tests/test_cpi_history_live.py
```

该命令选择 unregistered raw-index，不加载凭据，最多两个逻辑请求/六个 wire
attempt，只写 ignored artifacts，不接触 canonical/store。失败只输出分类。
若 Host 使用 registered，显式构造当前资格并提供 key 路径；不打印 key、不切换
provider。Host 后续真实存储验收须先核实非生产资源身份、隔离和 writer/readback
会话，再组合已有 engine；不要依据此文默认创建或清空共享 schema/bucket。

## 仍未支持

自动发现新增 reference、无限周期、跨午夜/无时区窗口、分布式调度、多来源、
新历史计划的 retained repair、UNKNOWN 自动重放、历史首次公布 vintage、
多个版本时 latest-wins、超预算无限列表、常驻服务部署激活及生产效果。
外部 live/存储资格与完整 HTTP/UI Host QA 仍待验证，不宣称最终交付或通过。

## 修改文件

新增：

- `src/market_data_foundation/historical/cpi_history.py`
- `src/market_data_foundation/historical/cpi_scheduling.py`
- `src/market_data_foundation/web/cpi_schedules.py`
- `tests/test_cpi_scheduling.py`
- `tests/test_cpi_history_live.py`
- `tests/r5_schedules_ui_exchange.py`
- `web/src/CpiSchedules.tsx`
- `web/src/CpiSchedules.test.tsx`
- 本文。

修改：`historical/bls_cpi_direct.py`、`historical/cpi_local_job.py`、
`historical/cpi_local_read.py`、`web/cpi_api.py`、`web/cpi_jobs.py`、
`web/source_catalog.py`（以上路径前缀为 `src/market_data_foundation/`）；
`tests/test_cpi_local_job.py`、`tests/test_r5_source_catalog.py`；
`web/openapi.json`、`web/src/api.generated.ts`、`web/src/App.tsx`、
`web/src/CpiJobs.tsx`、`web/src/CpiJobs.test.tsx`。

## Git 隔离回报

交接检查时，隔离 HEAD、隔离 main、origin/main 及只读核对的 canonical main
均仍为上述 AO 基线。共 22 个未暂存文件（13 修改、9 新增），没有删除、
冲突、依赖/lock 改动或 Git 写入。`git diff --stat` 只统计 13 个已跟踪文件，
不会包含 9 个新文件；完整清单以上述 inventory 与 `git status --short` 为准。
运行日志、BLS 探测和构建输出均被忽略。

## 2026-10-21 R5 两项 P2 有界修正

fresh-read HEAD 为 `80acbfc52f9d94fe0335f4d428a3830cb7e89557`，分支仍为
`mdf/r5-i5-cpi-scheduling-20261020`。读取了本工作区 AGENTS 和 control 根目录的
`research/mdf-unified-source-20261018/ui-r5-stitch/R5_I5_INDEPENDENT_REVIEW_R5.log`；
其末尾两项 P2 是修正依据，`REVIEW_R5_EXIT_CODE=0` 不是专业 verdict 通过。

本轮实际修改七个文件（其余候选改动均为会话开始前已存在）：

- `src/market_data_foundation/historical/cpi_scheduling.py`：只读 snapshot 接受
  Host effect domain，在既有全局预算检查后、child 状态/证据投影前过滤。
- `src/market_data_foundation/web/cpi_schedules.py`：GET 绑定当前 Host 域；
  action/backfill 先经只读 reader 验证不可变计划规格所属域，再进入原 mutation。
  外域与不存在身份统一返回 `409 / cpi_plan_not_found`，不暴露终态或回补资格。
  原 Host Operator 认证、写入授权及由 Host 承担的 CSRF/Origin 检查未改。
- `web/src/CpiJobs.tsx`、`web/src/CpiSchedules.tsx`：传递页面 generation 并将其
  纳入计划查询依赖，复用 AbortController 清理；局部回查继续有效。
- `tests/test_r5_cpi_schedule_domains.py`：同一个真实持久 SQLite 内建立双域计划，
  用离线有限 worker 产生 failed child。每组均有真实 HTTP 和独立 endpoint/DTO
  两种运行参数；覆盖双向域隔离、只读/可写 Operator、同域创建/回补/控制、
  缺失或伪造身份拒绝。拒绝前后比较所有 SQLite 表、journal 文件字节、来源
  调用、Raw 创建次数和 ledger；外域 active/terminal 与不存在身份响应一致。
- `web/src/CpiSchedules.test.tsx`：真实 App 顶部刷新及父组件 generation 回归，
  覆盖状态/进度/写入资格更新、过期成功/失败、卸载 abort、局部回查与身份链接；
  所有刷新断言仅 GET。原 React→ASGI 测试改为经过 App 顶部刷新观察 Host tick。
- 本文。

修前动态证据：双域列表断言实际返回两域（两个只读域方向均失败）；跨域
pause、cancel、backfill 均返回 202，cancel/backfill 双向四个反例失败；
两项 React generation 回归均观察到刷新后仍只有一次计划 GET，预期应为两次。
修前 HTTP 探测 40 秒超时，不能用其宣称 HTTP 复现成功。

本轮使用已有 uv 管理的 Python 3.14.8 环境，以
`UV_CACHE_DIR=/tmp/mdf-i5-correction-uv uv run --offline --no-sync` 运行；没有
改依赖、lock 或系统 Python。修后验证与环境限制：

- 双域专项 `pytest tests/test_r5_cpi_schedule_domains.py -k endpoint -q`：
  **18 passed、18 deselected**；后者全部是保留待 Host 执行的 HTTP 参数。
- `npm test -- --maxWorkers=1 src/CpiSchedules.test.tsx -t 'page generation|App Refresh'`：
  **3 passed、1 skipped**（筛选排除 ASGI bridge case）。顶部按钮真实触发第二次
  GET，周期计划从 active 0/2 变为 completed 2/2，写入资格撤销。
- 全文件 Vitest：**3 passed、1 failed**。ASGI bridge case 未取得第一份快照，
  `findByText(/OFFLINE_FIXTURE · OK/)` 等待超时；没有删除、跳过或放宽该用例。
  另一次真实 HTTP GET 20 秒超时退出 124，8 秒线程栈显示主线程在 TestClient /
  AnyIO `portal.call` 等待，portal 线程在事件循环 select。HTTP/ASGI 门未通过；
  纯 React PASS 与 endpoint PASS 均不代替该门。
- `ruff check .`、`ruff format --check .`、hygiene（含 untracked）、
  `git diff --check`、TypeScript check、Prettier、OpenAPI contract check 和 build
  通过。
- 扩展确定性定向回归：`pytest tests/test_r5_cpi_schedule_domains.py
  tests/test_cpi_scheduling.py tests/test_cpi_local_job.py
  tests/test_p4_bls_cpi_direct.py -k 'not http' -q`：
  **380 passed、1 skipped、20 deselected**。此结果不是完整 deterministic pytest
  或 full Host QA；`-k 'not http'` 同时筛去带 http 名称的少数 wire 参数，故另跑
  原 scheduling 全部非 ASGI 项复核，结果见下。
- `pytest tests/test_cpi_scheduling.py -k
  'not test_http_acceptance_auth_strict_input_and_no_execution' -q`：
  **54 passed、1 deselected**；唯一排除项为实际 ASGI HTTP 用例。真实 wire
  节流/预算、窗口、恢复/UNKNOWN、具名 >128 exact-read、registered credential
  echo、raw SHA pin 等已有负向门禁通过，未因名称带 http 而被漏选。

本轮原始测试输出留在 ignored `artifacts/r5-i5-material-correction/`；
`candidate-manifest.json` 记录交接时所有改动/新文件的内容 SHA256，供 AO 精确
定位未提交候选。没有 commit、stage、push、merge、PR、部署或外部服务访问。
full Host QA、独立专业 Review R6、Git/CI/postflight 均留给 WebChat/AO 后续决定。

交接 Git 状态为 26 个未暂存路径（16 个已跟踪修改、10 个 untracked）；index
为空，HEAD 未变。上文早期候选的 22 路径数字是历史记录，本段为当前状态。

## 2026-10-21 R6 exact-read 时间锚点修正

本段是 R6 唯一 P2 的有界候选修正记录。fresh-read 本工作区 AGENTS、Git 与
control 根目录的 `research/mdf-unified-source-20261018/ui-r5-stitch/R5_I5_INDEPENDENT_REVIEW_R6.log`。
HEAD/main 仍为 `80acbfc52f9d94fe0335f4d428a3830cb7e89557`，分支仍为
`mdf/r5-i5-cpi-scheduling-20261020`。审查进程 exit code 不作为专业无 finding
结论。本轮没有 commit/stage/push/merge/PR，也没有 Provider、PG、Garage、
生产/共享存储、长期 Worker 或 listener 效果；本文早期 live 命令不是本轮授权。

### 精确反例与修正

新增 `test_exact_read_anchor_after_child_success_before_parent_update` 在真实有限
worker 的 `engine.run` 返回成功之后、父计划后续写入之前抛出 KeyboardInterrupt。
子 Job/Publication/Raw/独立读回通过原领域链路生成，不注入成功状态。synthetic
Host 时间从计划映射的 `2028-03-10T12:00:02+00:00` 推进到子 Job 的
`2028-03-10T12:00:07+00:00`。崩溃后父计划仍有 `inflight_key=0:0`、cursor=0，
snapshot 已能证明 1/2 批次完成；父更新时间早于子 Publication.usable_at。

- 修前：React 链接实际采用父时间 `12:00:00` 而非其测试子证据 `12:00:07`；
  同一个真实 `cpi_read` endpoint 以崩溃父时间 `12:00:02` 请求同一 Version，
  返回 `409 / unpublished_or_unavailable_as_of`。修前专用 Python 测试在已确认
  旧查询拒绝之后，因 DTO 缺失子时间失败；React 专用测试 5 failed、2 passed。
- 修后：计划 child DTO 增加可空 `read_as_of`。它只来自持久
  `last_success.observed_at`；该 success 由 confirmed_published 独立读回产生。
  投影要求证据 Version、Raw 摘要、coverage 和批次 reference 一致，Publication
  occurrence 存在，并验证 aware 的观察时刻不早于 known_at/decision_at/usable_at。
  无 success、旧记录缺少时间或证据不一致时不生成该字段值，没有墙钟或父时间回退。
- UI 仅在快照 OK、资产身份与修订匹配且子 Version/Publication/read_as_of 齐备时
  显示具名精确链接。读取权限不依赖写入资格；已有成功证据的 outcome_unknown
  也可投影，UNKNOWN 本身不成为成功凭据。链接保留相同 asset、revision、reference
  和 Version。现有 `cpi_read` 以子读回时刻成功返回同一 Version/reference。
- Reader 的 as-of、身份、Publication、资格及内容判断均未修改。反例测试同时
  验证错误/过时 reference、未知 Version 的拒绝；专用 endpoint 负向覆盖未发布、
  withdrawn、superseded 和 quality_not_admitted。查询前后 Ledger、Job history、
  父计划均相同，来源请求与 Raw 创建仍各为一次。未扩大目录/计划预算。

本轮实际修改九个文件，其余 I5 候选文件保持原内容：

1. `src/market_data_foundation/historical/cpi_scheduling.py`：只读 child 时间证据投影。
2. `src/market_data_foundation/web/cpi_schedules.py`：ChildDTO 的 nullable read_as_of。
3. `web/src/CpiSchedules.tsx`：链接采用子证据时间及快照身份门。
4. `tests/test_cpi_scheduling.py`：崩溃端点/HTTP双路径、证据负向及 Reader 拒绝回归。
5. `tests/r5_schedules_ui_exchange.py`：测试协议支持成功子 Job 后的崩溃点。
6. `web/src/CpiSchedules.test.tsx`：时间、身份、只读资格、UNKNOWN 与缺证据门；
   原真实 UI→ASGI bridge 亦覆盖崩溃后旧查询拒绝、新链接经 existing read API 成功。
7. `web/openapi.json`：生成契约。
8. `web/src/api.generated.ts`：生成 TypeScript。
9. 本文。

### 本轮验证及 Host 留项

使用已有 uv 管理的 Python 3.14.8，通过
`UV_CACHE_DIR=/tmp/mdf-i5-correction-uv uv run --offline --no-sync` 执行；
依赖、uv.lock、系统 Python 无变化。结果保存在 ignored
`artifacts/r5-i5-r6-exact-read/`：

- 专用后端非 HTTP 反例及负向：14 passed；修前/修后日志分别为
  `before-pytest.log`、`after-pytest.log`。
- scheduling：68 passed、2 deselected；仅精确排除原 ASGI acceptance 与新增
  crash HTTP 参数，未按 http 字符串排除 wire failure 测试。
- 原 CPI Job/direct：313 passed、1 skipped；双域 endpoint：18 passed、18 HTTP
  参数 deselected。以上独立定向运行共 399 passed、1 skipped，不是 full pytest。
- 最终纯 React：11 passed、1 bridge case 筛选排除；其中新增 8 项覆盖本 P2。
- hygiene（含 untracked）、ruff check、ruff format --check、git diff --check、
  TypeScript check、OpenAPI contract check、Prettier 和 build 均通过。
- 新 crash HTTP 参数在 TestClient.__enter__/AnyIO portal 启动等待处 25 秒超时，
  exit 124；8 秒 faulthandler 栈保留在 `http-pytest.log`。既有真实 bridge 用例
  在首份 OFFLINE_FIXTURE 快照前等待失败，未到崩溃/读取断言；完整 schedules
  Vitest 为 11 passed、1 failed（`full-schedules-vitest.log`）。两者不能标 PASS，
  也不能据此判断业务断言失败。
- 扩展 App/CpiJobs/CpiSchedules Vitest 45 秒超时（exit 124），超时前 schedules
  非 bridge 项通过；扩展套件未完成，留给 Host。本轮没有运行完整 deterministic
  pytest；此前记录的环境问题不替代本候选 Host 实测。

Host 在精确候选上执行以下无 live-provider、无外部存储命令，并按原流程运行
完整确定性 pytest、hygiene、ruff 及全部前端门禁。不得把 endpoint 测试当作
ASGI/HTTP 或 React bridge PASS：

```bash
uv run --locked pytest tests/test_cpi_scheduling.py tests/test_r5_cpi_schedule_domains.py -q
cd web
npm test -- --maxWorkers=1 src/CpiSchedules.test.tsx
npm test -- --maxWorkers=1
```

`artifacts/r5-i5-r6-exact-read/candidate-manifest.json` 保存全 26 个未提交候选路径
的内容 SHA256、本轮九路径及 R6 审查日志 SHA256；这是未提交候选指纹，不是新
Git revision。index 为空，工作树仍为 16 个 tracked 修改、10 个 untracked，
没有删除、冲突、依赖/lock 变化。WebChat/AO 后续负责 exact-revision Host QA、
独立 Review R7 及全量门禁；本笔不声明专业审查通过或交付授权。
