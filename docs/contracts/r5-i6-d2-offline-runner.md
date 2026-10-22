# D2 日线离线执行接口

绑定冻结 D1 `market_planning` / `market_coverage` / `market_universe`，及六线 `SHARED_INTERFACE_R1.md`。模块不修改 D1、canonical kernel、Go bridge 或全局入口。

## 可调用入口

- `historical.market_daily_worker.DailyRunner(plan, journal, source, policy=...)`：核对完整 D1 计划；只接受 synthetic Universe。retained/live Universe 仍调用冻结 `JobSpec.require_execution()` 拒绝。Source 可以是 synthetic，或具名 retained 字节回放。
- `Journal(Path("artifacts/d2-local"))`：独立本地 SQLite journal，`synchronous=FULL`，durable intent 包含完整规范 Plan、Source 描述和 RuntimePolicy。目录由调用者明确配置，必须在忽略的 artifacts 下；测试使用临时目录。
- `accept()`：幂等接受；同 Job 身份不同执行合同拒绝。`run()`：独立 POSIX 主线程 Worker；`run(resume=True)`：显式解除取消并继续未发起的页，保留预算、接受时间、成功页和原 Attempt 身份。
- `DailyRunner.restore(job_identity, journal, source, policy=...)`：从 durable intent 严格重建 D1 Plan；Source 与 policy 必须匹配原接受合同。无须调用者保留原 Plan 对象。
- `cancel()`：幂等持久取消；在效果边界检查。已返回的原始字节仍可完成回执提交；不再发起下一页。
- `report()`：Job / Shard 状态、完整已持久回执及累计效果计数。`COMPLETE` 仅表示原始采集分页完成，不代表 D3 材料、经济口径或正式发布已合格。

SQLite 原始字节与页回执在同一事务提交。请求意图、响应实际量计数各自先持久化；因此回执事务回滚不会抹掉已观察到的请求/响应效果。SQLite 本地 durable 语义依赖正常文件系统与 fsync，不提供 PG/Garage 或断电硬件资格证明。

## 精确身份与 Source port

`market_daily_source.OfflineSource` 要求：

```python
binding: OfflineBinding
fetch(request: PageRequest, charge) -> Page
lookup_original(request: PageRequest) -> Lookup
```

`OfflineBinding` 固定 source/binding/mapping revision、capability、实现、endpoint 合同、producer operation、upstream/decode/schema revision 和 retained 材料 allowlist。每个 route 必须逐一匹配；无 fallback。一个 runner 只使用一个精确 Source 描述；不同描述的 route 当前明确拒绝。

`PageRequest` 规范身份覆盖 Job、Shard、Source/Binding/Mapping/Definition、instrument namespace、源 market/code、日期、页序/offset/count、冻结执行参数与预算。Attempt 是原 Request 身份的确定性派生，每页仅一个 Attempt，无自动重试。分页严格检查单调日期、重复、范围、页容量、跨页 snapshot anchor 和明确终止证明。row locator 与同一 Shard 全部已成功前页逐页比对，包含非相邻页；恢复时从 journal 重新验证。只保留当前 locator 集合并逐页读取历史材料，内存受页容量约束；累计扫描成本随页数二次增长，受冻结分页和材料预算限制。短页不能暗示耗尽；最后允许页仍无终止证据时阻断。

`Page` 保留原始 bytes、row locator、native fields、producer/upstream/decode、raw/derived 标记、retained 引用，以及相互独立的 obtained_at / received_at / known_at / source_time（`None` 即 UNKNOWN）。不会用宿主时间补填源时钟。native 浮点可由 adapter 用无损字符串编码；本模块不是 wire/native 类型或经济单位资格器。

TDX 未来 adapter 必须绑定 upstream v1.0.0 commit `2a9000681f5a9896ea98e4de8bc4904f1dce78ba`，沿用只读 AST index 和 field-gap audit。特别保留 producer × KlineCache.Type × Kind × Code，`Volume` 不统一转换股/手；`Close` 当日含义、源时钟及冲突字段保持 raw/UNKNOWN。目标 D1 Definition 的规范单位不证明回执原字段已有该经济口径。

## claim、预算和未知效果

同一配置 journal 目录的所有 Job 共用永不替换 inode 的 `flock`；每次 claim 增加 generation，随机 owner 与 lease deadline 共同 fence 每次写入。租约过期不允许偷走仍存活且持锁的 Worker；进程退出释放内核锁，下一 Worker 只能继续原 journal。取消可以由另一进程提交，不需要 Worker 锁。所有 Worker 必须共享同一权威 journal 目录；独立目录不互相排他，多 Host/NFS 不在此实现范围。

Source 在每个模拟 probe/connect/handshake/heartbeat/redial/retry **之前**调用 `charge`；被拒绝就不得执行效果。requests/pages/返回 bytes/rows 与材料写入量由 runner 分开记录。D1 retry/redial 上限为零；handshake/heartbeat 默认零，可在首次接受时通过 `RuntimePolicy` 明确冻结。该 policy 另规定累计 decoded metadata bytes 上限，不能在恢复时放大。超限返回记实际返回量，拒绝超限材料持久化，永不自动重采。

接受时间持久化，停机时间计入 wall budget；限速等待不重置它。Source 调用使用请求 timeout 与剩余 wall budget 的较小值，通过 POSIX `ITIMER_REAL` 中断可响应 Python signal 的阻塞；已有 alarm owner 和非主线程不允许使用。Source adapter 仍须遵守给定 bytes/timeout 预算，禁止吞掉限额异常。无法响应 signal 的 native 调用需要未来 Go bridge 的独立进程超时/kill 边界；此实现不声称可强制中止任意注入代码。

`STARTED` 崩溃或异常后的 `UNKNOWN` 均禁止重新 fetch。流程是：

1. `reconcile(original_request)` 只读 journal；已有回执独立读回，否则调用离线 `lookup_original`。它不申请 claim、不改 journal、不存材料、不发布，也不允许 Provider 调用。结果只有 FOUND / ABSENT / UNKNOWN，必须精确匹配原身份。
2. `accept_reconciliation(original_request, lookup)` 是独立显式写操作，重新持有 host claim。FOUND 验证原始页后存入原 Attempt；ABSENT 保留原 Attempt 为失败，供独立缺口候选使用。已观察到响应的 Attempt 不接受 ABSENT。UNKNOWN 保持只读结果、不自动执行。

正常提交与 FOUND 接纳统一以 journal 原 `observed_at_ns - started_ns` 判定请求超时（负值或超过冻结 timeout 即 TIMEOUT）。已持久观察到的响应，即使回执事务回滚并延迟恢复，也不能丢失原超时分类；及时响应不会因恢复等待而被误判超时。若崩溃前没有持久响应观察，首次 FOUND 接纳才记录 observed_at，不推测实际响应时间。

注入 Source 属于受信离线程序边界；Protocol 本身不能阻止恶意实现擅自联网或伪造 ABSENT。现有模块没有 live adapter、真实 endpoint 连接或任何 Provider admission。

## 回执、coverage 和精确集成缺口

`MaterialReceipt` 的 `request` 包含全部冻结身份；`digest/byte_length/locator/metadata/state/classification` 供 D3 接入。`metadata` 是规范 JSON 页元数据（含原生字段、行定位和各时间），`Journal.read_material(receipt)` 校验 bytes、长度和 metadata 联合身份后读回 `Page`。`journal:<attempt_id>` 是 D2 本地 locator，**不是 canonical Material Key**。

`coverage(assessments=...)` 使用冻结 D1 classifier；原始采集成功仍 UNKNOWN。D3 必须提供独立验证的 `PointEvidence`（包括 canonical Material、row locator 和资格证据），本模块不伪造该材料或 OBSERVED_VALID。只有整 Shard 第一次请求被可靠证实未执行，才自动生成 COLLECTION_FAILED / retryable evidence；部分成功的 Shard 继续保留 UNKNOWN，等待 D3 评估已有 raw。

`recovery_candidates(manifest, separate_budget)` 调用冻结 D1 `plan_recovery`，返回独立 `GapCandidate` / reprocess_first / investigate_original；保留原 Job/Shard、manifest revision、新 occurrence 规则。成功、已有材料、UNKNOWN 不自动重采。候选不提交成新 Job，也不发布。D1 `plan_job` 明确拒绝 BACKFILL；未来集成需提供经独立 acceptance 的 GapCandidate→RecoveryOrigin/JobSpec/Shard 规划入口，不能在 D2 伪造一个绕过 D1 的 BACKFILL Plan。

尚需集中集成的具名 port：

| 接缝 | 必需输入/行为 |
| --- | --- |
| D2 → D3 | `MaterialReceipt` + `Journal.read_material` 的原 bytes；由 D3 独立创建 Material/Assessment/Version/Publication，再返回 PointEvidence；D2 不写 canonical kernel |
| adapter → D2 | 按 `OfflineSource` 精确 descriptor/request/page/row envelope 转换；live 需要新的具名 Source/Universe/Calendar/单位/使用权及 endpoint/窗口预算准入，当前代码不开放 |
| Go/IPC transport | 每次网络尝试预计量，包括内部连接/握手/心跳/重拨；单独的可强制超时进程边界、readonly original-request lookup；不能把现在的 Python 离线 Protocol 当作真实 SDK 证明 |
| D1 recovery acceptance | confirmed GapCandidate 独立接受并构造冻结 Backfill Plan 的正式接口；原成功材料先复用，禁止把 UNKNOWN 或当前目录变成历史可补采集 |
| Host launcher | 独立主线程进程、唯一 journal 目录和保留/清理策略；未接全局 registry/UI/配置，也未创建或部署服务 |

## 稳定异常与故障注入

`DailyError.classification` 是 `Failure` 枚举，消息仅含分类。Source 原始异常消息从不进入 journal/events/report；raw 内容属于材料，不能当 observation 输出。D1 输入门保留其 `ContractError` 分类。

| 分类 | 处理 |
| --- | --- |
| EFFECT_UNKNOWN / TIMEOUT / TRANSPORT_FAILURE | 原身份核实，禁止重采；超时后有返回 raw 则可保留为 rejected receipt |
| CONFIRMED_NOT_PERFORMED | 原 Attempt 保留失败，只能生成独立候选 |
| PAGE_INVALID / PAGE_DRIFT / EXHAUSTION_UNKNOWN / IDENTITY_MISMATCH | 保留限额内的 rejected raw，阻断该 Shard；不同身份/同身份异字节拒绝 |
| BUDGET_EXCEEDED / CANCELLED | 不扩预算；已经可能发生的效果保持 UNKNOWN，取消不抹去成功页 |
| HOST_BUSY / LEASE_LOST | 禁止并发执行或过期 claim 写入 |
| JOURNAL_UNAVAILABLE / JOURNAL_INCONSISTENT | 停止相关写/读；恢复后核实原身份，不用 Provider 修补 |

故障注入点覆盖 acceptance 前/后、Attempt commit 前/后、Source 前/后、material insert 后及 receipt commit 前/后。专属测试还覆盖真实子进程退出和并发、限速、隐式尝试拒绝、字节/metadata 超限、时钟退后、阻塞超时、页漂移、丢回执、取消恢复、部分成功、独立补采与拒绝真实 Universe。

执行验证使用现有 `/srv/market-data-foundation/.venv/bin/python`（3.14.8）、同环境 Ruff；没有新增依赖，不修改系统 Python/uv.lock。

## 初版验证结果与环境限制（修正前）

- `pytest -q -p no:cacheprovider tests/test_r5_i6_d2_runner.py tests/test_r5_i6_market_contracts.py`：178 passed，其中 D2 80 项、冻结 D1 98 项。
- 全树 `ruff check .`、`ruff format --check .` 和 `scripts/check_repository_hygiene.py --include-untracked` 通过。Python 禁止写入 bytecode；Ruff cache 指向 `/tmp`。
- 无排除项的 deterministic pytest 在收集时遇到缺失 `fastapi` / `httpx`：`test_cpi_scheduling.py`、`test_r5_cpi_api.py`、`test_r5_cpi_jobs.py`、`test_r5_cpi_schedule_domains.py`、`test_r5_observability.py`、`test_r5_source_catalog.py` 共六个既有模块无法收集。未安装依赖或修改环境。
- 显式排除上述六个模块后的回归：5802 passed、25 skipped、19 deselected、4 failed。失败均为既有 `tests/test_tdx_bounds.py`：`test_child_cannot_execute_denied_connection_or_overrun[boundary/attempt/wall]` 三项及 `test_trace_follows_threads_without_provider_access`。它们在 `provider_lab/tdx_bounds.py` 的 `preexec_fn=trace_me` 中失败，`trace_me` 仅调用 `ptrace(0, 0)`，子进程命令尚未执行。未调整 ptrace/sandbox 控制，也未修改这些共享文件。

因此不声明全树 deterministic pytest 全绿；上述环境与启动门失败需由集中 Host QA 处理，不能用 D2 专属结果代替。

## 独立审查两项修正验证

本轮仅修改 `market_daily_worker.py`、`tests/test_r5_i6_d2_runner.py` 和本说明，未改变 D1、journal schema 或任何共享接口。

- 新增 7 个确定性用例：响应延迟 1/5/6 秒、回执提交前回滚及延迟恢复；三页非相邻 locator 冲突的升序/降序和正常/恢复路径。修正前新增用例 5 failed、2 passed，复现两个 blocker。
- 修正后使用现有 uv 环境 Python 3.14.8，执行 `PYTHONDONTWRITEBYTECODE=1 /srv/market-data-foundation/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_r5_i6_d2_runner.py tests/test_r5_i6_market_contracts.py`：185 passed（D2 87、D1 98）。使用可写 `/tmp`，文件系统和进程锁未替换，包含原有真实子进程退出及并发排他测试；不代表断电硬件资格。
- 全树 Ruff lint、format check、包含未跟踪候选的 repository hygiene 检查通过。未重跑初版所述全树 pytest，不据此消除其环境限制。
- 超时恢复保留原时间戳、TIMEOUT rejected receipt 和原始字节；重复接纳/恢复不重采、不重复计量。及时响应的恢复等待不计入原请求延迟。第三页冲突保持前两页成功并将第三页标为 PAGE_DRIFT。

集成缺口仍为上述 D2→D3 receipt/独立评估、adapter/Go transport、D1 独立补采 acceptance 和 Host launcher 接线。本轮没有 Provider、PG/Garage、网络或 Git 交付效果；独立复核与集中 Host QA 仍由集成事务执行。

## 后续独立审查修正：终止凭证与核实观察

本次修改仅涉及 D2 worker、journal、source、专属测试和本说明。终止页的 `termination_evidence` 必须是非空白字符串；布尔值、整数、空字符串、空白和缺失值均标记 `EXHAUSTION_UNKNOWN`，限额内 raw 保留为 rejected receipt。正常执行与 FOUND 核实接纳使用相同校验，空终止页也不例外。

`Lookup.evidence` 明确为证据引用，要求非空白字符串且 UTF-8 编码最多 4096 bytes。新增本地 `reconciliations` 表，按原 Attempt 唯一保存 Job／Shard／Attempt／request 身份、原 Lookup status、完整 evidence 引用、响应联合摘要及接纳时间。`Journal.reconciliation(original_request)` 提供只读返回；原 STARTED／UNKNOWN 事件不删除。状态／材料接纳与独立观察在同一 SQLite 事务提交；响应效果计数仍按原合同单独提前持久化。

`reconcile()` 优先返回已接纳的原证据，不再用 `CONFIRMED_NOT_PERFORMED` 或材料 locator 覆盖它。每个 Attempt 只保存一份不可变的已接纳观察，限制累计空间；相同结论、证据和页可幂等重放，改换证据或响应则拒绝 `IDENTITY_MISMATCH`。UNKNOWN 查询及其接纳仍不写观察。FOUND 响应因预算被拒绝且未保留材料时，原 FOUND 观察可从 `Journal.reconciliation()` 读取；`reconcile()` 因无法重建 Page 返回带原证据的 UNKNOWN，不生成补采资格。新表可在打开既有 D2 journal 时添加，但无法追溯恢复旧实现已经丢弃的证明。

新增回归在修正前为 13 failed、12 passed，复现了正常／恢复路径和空／非空页的无效终止凭证，以及 ABSENT 证明丢失。进一步测试覆盖 FOUND／ABSENT、STARTED／UNKNOWN、事务回滚／提交后响应丢失、证据字节边界及冲突重放、超预算材料拒绝；真实子进程在观察插入后／提交后退出，验证 SQLite 恢复与内核 claim 释放。所有材料均为合成字节，使用真实临时文件、SQLite 和 flock，不替换文件系统或锁。

本次仍不接线共享 D1/kernel/Go：D2→D3 需要独立 Material/Assessment 和 PointEvidence；D1 需要 GapCandidate→独立接受的 Backfill Plan port；adapter／Go 需要精确 Source envelope、尝试计量及进程超时；Host launcher 需要统一 journal 目录。真实 Source/Universe/Calendar 权利、经济单位及 PG/Garage 资格仍未补足。

最终验证使用现有 uv 管理的 Python 3.14.8：`PYTHONDONTWRITEBYTECODE=1 /srv/market-data-foundation/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_r5_i6_d2_runner.py tests/test_r5_i6_market_contracts.py` 得到 230 passed（D2 132、D1 98）。全树 `ruff check .`、`ruff format --check .`、repository hygiene（含 untracked）及 `git diff --check` 通过。本次未重跑全树 pytest，前述全树环境限制未据此消除；没有 Git 提交、推送、网络或外部存储操作。

## 第三轮审查修正：持久预算 STOP 与响应墙钟期限

本次只修改 D2 `market_daily_worker.py`、`market_daily_journal.py`、`tests/test_r5_i6_d2_runner.py` 和本说明。外部历史 STOP、`review1..3.return.md` 保持原字节；本修正不撤销审查结论，不产生专业 PASS 或集中 Git/CI 批准。

响应的实际 bytes／rows、原 `observed_at_ns` 和已知预算超限的 Job `stop_reason=BUDGET_EXCEEDED` 在同一 SQLite 事务持久化。检查单页 bytes、累计返回 bytes／rows、预计材料／metadata／页写入和 Job wall budget；超限材料仍不保留。新增 `after_response_commit` 故障注入点，验证响应计量提交后、回执提交前崩溃仍不能启动其他 Shard。transport 的预算异常和材料提交时的预算拒绝同样持久阻断 Job。`run()` 在新请求前检查 STOP，journal 的 `begin()`／`charge()` 也在效果边界拒绝；`restore()`、重复运行及取消后 `resume=True` 均保留 STOP、原接受时间和累计计数。

正常执行与 FOUND 接纳统一比较原持久 `observed_at_ns` 和 `accepted_ns + wall_seconds × 10^9`，到达期限即拒绝为 `BUDGET_EXCEEDED`（与请求启动的墙钟边界一致），即使请求本身尚未超过 timeout。限额内 raw 及其原 metadata 保留为 rejected receipt；原 `started_ns`／`observed_at_ns` 不覆盖。及时响应若已持久观察，即使回执回滚、恢复时已超过 Job deadline，仍按原时间接纳；崩溃前没有持久观察的 FOUND 则以首次观察时间判断，不凭 Source 时间戳推测及时返回。

STOP 仅阻止新增采集和 transport 效果；原 Request 的只读核实、FOUND／ABSENT 显式观察接纳、既有材料读回仍可调用。已知响应不得改成 ABSENT。被预算阻断的 UNKNOWN 保持原身份核实要求，不转为独立补采资格；D1 对未合格真实 Universe 的拒绝继续保留。

先加入的 10 项回归在修正前为 8 failed、2 passed，复现两项 blocker。最终新增 12 项回归，包含两 Shard 返回 2001 bytes／总预算 2000 bytes 后无第二次 fetch、metadata／transport 超限、重启／取消恢复、journal 效果边界拒绝、响应事务提交后崩溃、3 秒 wall／5 秒 timeout 下正常和 FOUND 路径、期限边界、及时响应延迟恢复、未观察响应的延迟 FOUND，以及接受时间早于请求时间的反例。测试使用真实临时文件、SQLite 和 flock；原有真实子进程退出／排他测试继续执行，不替换文件系统或锁，不代表硬件断电或多 Host 资格。

本次全树 deterministic pytest 未排除任何模块，在收集时再次因现有环境缺 `httpx`／`fastapi` 产生上述六个既有模块的 import error（19 deselected、6 errors）。没有安装依赖、绕过测试或调整 sandbox；集中 Host QA 仍须处理该环境缺口。精确集成缺口仍为 D2 `MaterialReceipt`／`Journal.read_material` → D3 独立材料与 `PointEvidence`、D1 `GapCandidate` → 独立接受的 Backfill Plan、adapter／Go transport 精确身份与尝试计量及强制超时、Host 唯一 journal 目录和 launcher。未修改共享文件，也未调用真实 Provider、网络、PG/Garage 或 Git 交付。

最终候选使用现有 uv 管理的 Python 3.14.8 执行 `PYTHONDONTWRITEBYTECODE=1 /srv/market-data-foundation/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_r5_i6_d2_runner.py tests/test_r5_i6_market_contracts.py`：242 passed（D2 144、冻结 D1 98）。全树 `ruff check .`、`ruff format --check .`、repository hygiene（含 untracked）及 `git diff --check` 通过；不据此声称全树 pytest、独立复核或专业验收通过。

## 本轮审查修正：限速等待前的时钟与预算检查

本轮仅修改 D2 worker、专属测试和本说明。`DailyRunner.run()` 在 sleep 前取一次 journal 时钟，分别比较持久 `accepted_ns` 和 `last_request_ns`；任一时钟回退或墙钟期限已到即通过 `Journal.block()` 持久记录 `BUDGET_EXCEEDED` 并返回，不 sleep、不创建新 Attempt、不再次 fetch。合法限速等待取限速间隔与剩余 Job wall budget 的较小值；等待后仍经过原 `Journal.begin()` 的取消、STOP、速率和预算效果门，耗尽预算不能启动采集。未改变接受时间、累计计数、journal schema 或 claim/lease 合同。

新增 7 项真实临时文件／SQLite／flock 回归：成功非终止页的 receipt 提交后故障退出，再重建 Journal 和 runner；覆盖回拨 3600 秒（旧实现会请求 3601 秒 sleep）、仅早于最后请求但晚于接受时间、恰好到期及超过期限。断言无 sleep、无额外 fetch、原成功 receipt／计数／时间戳保留、STOP 仅记录一次，校正时钟后再 restore、cancel、重复 resume 仍保持 STOP，返回后 claim 可再次取得。另外覆盖剩余预算 0.5 秒截断 1 秒限速等待、正好到期拒绝，以及预算充足的正常等待与完成。注入时钟和 sleep，不进行真实长等待；文件系统、SQLite 和 flock 未替换，不代表硬件断电资格。

加入修复前，新增测试为 3 failed、4 passed，复现时钟回拨及等待超预算；随后将接受时间回拨用例细化为审查给出的精确 3601 秒等待场景。全树 deterministic pytest 未排除模块，本轮再次在收集阶段出现现有环境缺少 `httpx`／`fastapi` 的六个模块 import error（19 deselected、6 errors），未安装依赖或调整 sandbox。

本轮最终代码在现有 uv 管理的 Python 3.14.8 环境执行 `PYTHONDONTWRITEBYTECODE=1 /srv/market-data-foundation/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_r5_i6_d2_runner.py tests/test_r5_i6_market_contracts.py`：249 passed（D2 151、冻结 D1 98），耗时 75.72 秒。全树 Ruff lint、format check、repository hygiene（含 untracked）、`git diff --check` 及六份未跟踪候选的额外 whitespace 检查通过。全树 pytest 的上述环境缺口仍保留。

集成端口仍为 D2 `MaterialReceipt`／`Journal.read_material` → D3 独立 Material/Assessment 与 `PointEvidence`；D1 `GapCandidate` → 独立接受的 Backfill Plan；adapter／Go transport 的精确身份、尝试计量和强制超时；Host 唯一 journal 目录及 launcher。本轮未改共享文件、执行网络／真实 Provider／PG／Garage，或改变 Git HEAD／提交／推送。本轮修正仍待独立 revision-bound 复核及集中 Host QA。

## 本轮审查修正：元数据失败不得抹去返回量

仅修改 D2 journal、worker、专属测试及本说明，不变更 schema。`Journal.observe_response()` 先以独立事务记录实际 response bytes、rows、returned pages、原观察时间与 raw digest，并原子记录由响应／累计 bytes、rows、材料容量、页容量或墙钟触发的预算 STOP；事务中不序列化行内容或其他 decoded metadata。`after_response_commit` 故障点现在位于此事务之后、元数据处理之前。之后才计算响应联合身份并检查 metadata 容量；后续失败不能回滚已观察效果或解除 STOP。

重放依据 `observed_at_ns` 判断是否已计量，而不是依据可能尚未生成的 `response_identity`。已有观察必须匹配原 raw digest、bytes 和 rows，保留原时间且不重复增计。worker 的 ABSENT 接纳门也以已有观察为准，避免元数据失败留下空联合身份时将响应误判成未执行。

孤立 Unicode surrogate 只在 rejected 响应的联合身份摘要输入中用 `surrogatepass` 无损表示，供超限 FOUND 的原身份校验；不将这些编码字节写入材料或 metadata，不替换、忽略或规范化原字段。有效 UTF-8 的联合摘要保持原算法。材料存储仍要求严格 UTF-8，超限响应仍不保留材料。元数据序列化器本身抛错时，已提交的实际量及 STOP 同样保留；恢复后可按原 raw 摘要核实 FOUND，不重新 fetch。

新增 13 项回归：snapshot／native value 孤立 surrogate × 1001／2001 bytes × 正常执行／Source 后崩溃／响应观察提交后崩溃，另注入序列化器直接抛错，检查 STOP、重启、FOUND 接纳与重复重放、ABSENT／异字节反例、无超限材料和第二 Shard 零 fetch。使用真实可写 `/tmp`、SQLite 和 flock，不替换文件系统或锁；不代表硬件断电或多 Host 资格。新增用例在修复前 13 failed，修复后 13 passed。

本轮全树 pytest 未排除模块，收集阶段仍因现有环境缺 `httpx`／`fastapi` 出现六个既有模块 import error（19 deselected、6 errors）。未安装依赖或修改 sandbox。精确集成端口与真实 Source／Universe／Calendar／单位／使用权、Go transport、PG／Garage 和 Host launcher 限制均沿用上文；D1 对未合格真实 Universe 的拒绝未改变。本轮仍需独立 revision-bound 复核及集中 Host QA。

最终代码在现有 uv 管理的 Python 3.14.8 环境执行 `PYTHONDONTWRITEBYTECODE=1 /srv/market-data-foundation/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_r5_i6_d2_runner.py tests/test_r5_i6_market_contracts.py`：262 passed（D2 164、冻结 D1 98），耗时 88.12 秒，包含既有真实子进程退出及锁测试。全树 Ruff lint、format check、repository hygiene（含 untracked）和 whitespace 检查通过；未修改 D1／共享路径、依赖、Git HEAD 或执行 Git 交付，未调用网络、Provider、PG／Garage。

## D2 post-STOP Directed Correction R1

本次是针对 `ao_recovery_review3.return.md` 两处 P2 的一次具名有限纠正，保留历史 STOP。仅修改本线 journal、worker、专属测试及本说明；未改变 schema、共享合同、Provider 或部署权限。

累计材料／metadata 预算拒绝与单页超限现在采用相同的拒绝路径：`Journal.finish()` 拒绝材料事务后，worker 调用 `Journal.mark()`，在同一拒绝事务中记录 `REJECTED:BUDGET_EXCEEDED`、Job STOP 和显式接纳的原 FOUND 观察。不提交超限材料，不增加 material／metadata／pages 计数，也不回滚已经观察的 response bytes／rows／returned pages。原请求、响应身份、观察时间、已有成功材料及计数保留。核实记录插入后提交前故障会整体回滚此次拒绝及观察；恢复后可再次显式接纳，提交后重复接纳保持原证据、事件和计数。

`Journal.charge()` 对传输轴超限、非法计量值、墙钟到期及回退的拒绝，先在拒绝事务内持久记录 STOP，再在事务提交后抛出 `BUDGET_EXCEEDED`。拒绝的量不计入累计计数，先前成功计量不变；因此 runner 的后续 `block()` 尚未执行时进程退出，也不能在重启后继续第二 Shard。

专项覆盖精确 `planned(size=2, pages=3)`／`RuntimePolicy(max_metadata_bytes=2400)`，以及双 Shard、STARTED／UNKNOWN／REJECTED 原状态、核实记录插入后回滚和提交后故障；另含精确 metadata 总预算正常完成、异 raw／异 metadata／ABSENT／更换证据拒绝、原成功材料读回和重复恢复。传输测试覆盖六个允许计量轴、墙钟边界、时钟回退、负值及非整数；在 runner 的 `block()` 入口注入 crash，并额外使用真实子进程 `os._exit()` 验证 SQLite STOP 和原累计量在重启后保留，第二 Shard 零 fetch。使用真实临时文件、SQLite 和 flock，不代表硬件断电或多 Host 资格。

修复前先运行的专项用例为 17 failed、1 passed，分别复现累计 FOUND 无法接纳和 charge 拒绝后缺少 STOP；扩充恢复状态后的专项用例为 30 passed。无排除模块的全树 deterministic pytest 仍在收集阶段因当前环境缺少 `httpx`／`fastapi` 出现六个既有模块 import error（19 deselected、6 errors），未安装依赖或修改环境。不据专项测试声明全树 pytest 或独立验收通过；仍由 Host 计算最终 bytes／SHA，并进行新的 revision-bound 独立复核。既有 D2→D3、D1 独立补采、Go transport 和 Host launcher 集成缺口沿用上文。

最终代码从实际 D2 工作树使用现有 uv 管理的 Python 3.14.8 执行 `PYTHONDONTWRITEBYTECODE=1 /srv/market-data-foundation/.venv/bin/python -m pytest -q -p no:cacheprovider --tb=short tests/test_r5_i6_d2_runner.py tests/test_r5_i6_market_contracts.py`：291 passed（D2 193、冻结 D1 98），耗时 123.49 秒。全树 Ruff lint、format check、含未跟踪候选的 repository hygiene、`git diff --check` 及六份未跟踪候选的额外 whitespace 检查通过。Git HEAD 仍为原起点，未 commit／push；历史 STOP 和独立复核门保持。
