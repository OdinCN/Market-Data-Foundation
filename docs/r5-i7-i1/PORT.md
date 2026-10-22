# I-1 离线纵向接缝 R1

绑定 Session #15 MDF Product Control / Owner-local / MDF-DEV，基线
`e8faac58b8d97594109ed2dc63ea110afb36cfce`。遵守当前 AGENTS、四线批次
`BATCH_DIRECTIVE.md`、`SHARED_PORT_R1.md` 和正式设计 R2 §5–7/9。
这是离线工程候选；没有真实 Provider、PG/Garage、部署、激活或 adoption 效果。

## 可调用顺序与独立提交边界

1. 调用既有 `DailyRunner.run()`，冻结 D1 Plan 和 D2 Journal。
   读取 `report()` 的原始 `MaterialReceipt.request`，不另造请求身份。
2. `retain_page(worker, request, IntakeLayout, writer)` 调用
   `Journal.check_intent/receipt/read_material/attempt`，核对完整 request 与
   response identity，向注入的 `MarketWriter.retain` 交付相同 bytes。
   `MemoryMarketLedger.retain` 负责独立 bytes readback 和 canonical receipt。
3. `prepare_version(key, unit, receipts, evidence, decoder, store, evaluated_at,
   frozen_at, *, plan)` 必须传入原冻结 D1 Plan；先用 `validate_plan_evidence`
   核对 unit/CalendarRevision/UniverseRevision、原 Job/Shard 和逐 date 完整
   evidence，再调用 D1 `classify_coverage(plan, evidence)`，通过后才调用既有
   `assess_daily` 和 `required_admissions` 构造 `MarketVersion`。不提交、不授予
   资格、不从缺行推断闭市或 NO_TRADE。不能用最新或另一个 Job 的 Plan 替代。
4. Host 显式调用 `writer.commit_version(version)`。所需 admission 由受控宿主
   独立提供；I-1 不自动调用 `set_admission`。测试中只有具名 synthetic facts。
5. Host 显式构造 `MarketPublicationRequest` 并调用 `writer.publish(request)`。
   `verify_original(writer, value)` 按原 Key 和 payload digest 只读核实 material、
   version 或 publication。UNKNOWN 不转换为 ABSENT，不触发重试或新身份。
6. 写侧显式完成 `MemoryMarketLedger.rebuild(generation, covered_months)` 后，
   使用 `MemoryConsumerProjection(ledger.reader, binding, plans, codecs)` 注入
   现有 `MarketReader`，调用 `read(Query, cursor)` 完成 single/batch exact/as-of。
   Reader 不创建覆盖月份，不修复索引，不访问 Worker。
7. 全局覆盖另用 `worker.coverage(assessment_evidence)` →
   `ledger.coverage_manifest(manifest)` 验证。ACK、已 retain 或部分单元读回
   均不能代替完整 coverage 与 publication 结果。

具体可运行示例是 `tests/test_i1_market_vertical.py` 的 `Vertical` fixture；
其所有日期、证券、价格、资格和签名测试值均为虚构，不能用于真实准入。

## D2 → D3 字段映射

| D3 / I-1 字段 | 原始事实或显式输入 |
|---|---|
| material Key | `content_id(("i1.d2.material.r1", request.identity))`，同请求不能更换内容 |
| job / shard / attempt / request | `request.shard.job_identity / shard.identity / attempt_identity / identity` |
| Source / Binding / Mapping / Definition / instrument | 冻结 request.shard.route 和 shard.instrument |
| producer / upstream / decode / mode / capability | D2 Page 与冻结 OfflineBinding；不使用最新目录 |
| digest / byte_length / bytes | Journal 已校验的原始 material；不重序列化原材料 |
| locator | `mdf-i1://sha256/<raw-digest>`，仅是传给 MaterialBytes 的逻辑键，无 URI resolver |
| pages | 单个 D2 response identity：SHA-256(raw + 原始 page_metadata UTF-8) |
| rows | 明确 `IntakeLayout.rows`，顺序与 D2 NativeRow.locator 一致，offset/length/digest 指向原 bytes |
| request_fields / fixture_id | 能力 adapter 明确提供，与 NormalizationContract 精确匹配 |
| requested_at | 原采集请求时间及 `request_clock_evidence`；不能用离线重放 started_ns 补齐 |
| obtained / received / known | 原 D2 Page 原样继承，None 不填值 |
| source_time / retained_reference / request schema / page metadata digest | `IntakeReceipt` sidecar，原样保存，不与服务时钟合并 |
| effect / exception_classification | SUCCEEDED→CONFIRMED，REJECTED→FAILED，其余→UNKNOWN；只有分类，无异常原文 |
| material_kind | 原 Page.raw/derived 标记；source_projection 不能谎称 transport wire |

`IntakeReceipt` 是返回给调用方的不可变值，没有隐式落盘。真实交接必须将其与
原 D2 Journal/原 Page metadata 一起显式保留在 ignored artifacts 或经批准存储。
仅保留 raw digest 不能证明原请求、source clock 或语义。D3 MaterialReceipt 没有
source_time 独立字段；本轮通过 response identity 和 sidecar 保留该事实。

布局不证明经济语义。D3 decoder 必须独立枚举完整 bytes 的全部 rows，再核对
locators、OHLC、coverage、请求 lineage 和语义。requested_at/obtained_at UNKNOWN
仍可 retain，但不得提交可发布 Version；known_at UNKNOWN 不能通过 assessment。

## UNKNOWN、late FOUND 和重启

`DailyRunner.restore(job_identity, journal, source)` 恢复原 Job。
`worker.reconcile(request)` 只读查原身份；只有显式调用
`accept_original(worker, request, lookup)` 才接受核实结果并写 D2 Journal。
该 I-1 包装在任何写入前检查外层 request、内层 page、producer/upstream/decoder、
retained allowlist；已存在 receipt 还要求 raw 和完整 UTC page metadata 一致。
核对通过后复用 Journal 的原 Page，以免 D2 的 dataclass equality 将同一 UTC
时刻的不同 timezone 表示误判为冲突；真正不同的 DST fold 时刻仍拒绝且无写入。
重复相同 FOUND 不重采、不增加请求预算。乱序/漂移页保留为 FAILED，不能发布。

发现的共享接口问题：`historical/market_daily_worker.py:DailyRunner.accept_reconciliation`
只在入口检查 Lookup 的外层 request identity；外层正确但内层 page 属于另一请求时，
原实现会先 reopen UNKNOWN、observe response，再报身份错误。I-1 已用前置包装避免；
集中集成人应考虑将同样的前置校验移到共享 D2，测试必须证明失败前 Journal 不变。
本候选没有修改 D2 文件，其他调用方仍需单独审查。

## D3 → D4 适配边界

`MemoryConsumerProjection` 是明确的离线 Memory 适配器。每页通过现有
`ledger.reader.open(binding)` 获得一份一致快照，然后只按精确键读取。
不使用 checkpoint、全表枚举、旧 Consumer、跨页服务端缓存或后台补索引。

适配器有意只读访问 `MemoryProjectionView._state/_store`：D3 公共协议目前未暴露
D4 需要的 canonical point-read。它校验 commit receipts、材料、独立 assessment
重放、D1 unit/Job/Shard scope、canonical publication heads/occurrences 和全部
admission guards。不能将此 private Memory 接缝推广为数据库通用 API。

P2 修正后，每个 Version 在 raw read、assessment 重放和 Consumer 内容规范化前，
从材料原 Job 查已注册冻结 Plan，执行与 `prepare_version` 相同的
`validate_plan_evidence`。逐日 evidence 必须完整，且重新验证 PointEvidence 合同
（包括 references 和 NO_TRADE contract）；D1 的分类规则是唯一 Calendar/lifecycle
裁定来源。不会把 D1 对缺失 structural point 的解释填回 evidence。已存在的错误
classification、缺失日期或 references 均拒绝为 `INDEX_INCONSISTENT`；batch
exact/as-of 同样拒绝，不拼接旧的健康 Version 或相邻月份以报告 COMPLETE，且不修复。
共享 D3 不持有冻结 Plan，单独调用其接口仍不提供这项 I1 gate；本候选只收紧 I1
两端接缝，不改变共享 D1/D2/D3/D4。详见 `P2_RETURN.md`。

规范化差异显式处理：

- D3 的 `Decision.value` 是小写；D4 Publication 使用 `Decision.name` 大写。
- D3 的 content 是 `(PublicationUnit, bars, evidence)`；D4 要求逐自然日的
  `day/classification/raw/evidence`。`ConsumerCodec` 同时注入受信 DailyDecoder
  和纯 `native_fields(raw_row, receipt)`；raw 字段来自原始 row bytes，不能将
  DailyBar 的换算值冒充 native 值。I-1 不提供 TDX 单位换算或默认 decoder。
- D4 内容、lineage、candidate、history digest 使用 D4 的 encode/digest；
  canonical_digest 仍保留 D3 的原摘要，不能直接混用两种序列化。
- 公开 lineage 是经验证 receipt graph 的引用记录，仅含 key/record_digest/references。
  Acquisition 引用使用原 D2 attempt identity；不宣称已写入旧 kernel 的
  AcquisitionOccurrence/Source 图。私有 locator 不进入 Consumer。
- D4 的每个 Version guard 绑定该版本全部 D3 admission dependencies；共享
  Source/Binding/Calendar/retention guard 只绑定对应主体，防止单个版本失去资格
  污染健康 item 或未发布 sibling。D4 mapping 主体仅含 revision，故将对应的
  精确 Source/Binding/Mapping 依赖合并，不取第一条。revision/change 参与
  cursor 摘要，缺资格仍为 UNKNOWN。
- 每个请求有独立预算；在读材料前预留 assessment replay/native projection/
  content readback 的累计字节，保留 D3 自身的计数门，D4 再执行应用容量与签名门。
  这些内存证明不代表数据库 SQL 中断或物理 I/O 性能资格。

已知容量限制：D3 月目录摘要绑定 CandidateRecord，D4 月目录摘要绑定完整规范化
Version descriptor。因此本 Memory bridge 会在有界请求内转换所请求月份的
非 overflow 候选；额外同月候选也消耗预算，可能保守拒绝本来较窄的 exact/as-of
请求。overflow 条目只保留 count/flag，不展开候选；没有截断或“最新版本”替代。
物理实现应在写事务中同步维护规范化 descriptor/digest，避免读路径重复转换；
代表性规模、exact 选择成本和查询计划是集中 Host/I-3 的下一道门。

## I-2 / I-3 / I-4 交接

**I-2**：提供具名 immutable retained raw、原 Page metadata、SHA/长度、完整
PageRequest/OfflineBinding、明确 Source/Universe/Calendar、请求与全部来源时钟、
原始/decoded/source_projection 层级、页耗尽与无交易证据、fixture/capability 引用。
`RetainedPageSource(binding, tuple[RetainedPage, ...])` 只回放调用方已加载的
allowlisted bytes；校验原请求/端点/版本和 raw/metadata 摘要，最多 1000 页并受
冻结请求/材料/metadata 总预算限制，不打开 URI，不搜索文件，不访问 Provider。
一份 manifest 只允许同一个冻结 Job/budget；未列入 manifest 的原请求保持 UNKNOWN，
不能用“未找到 retained 输入”证明原采集 ABSENT。
未知原请求不能套用最新计划重新命名。现有 SyntheticDailyDecoder 明确拒绝 retained。
另一个共享门仍原样生效：`DailyRunner.__init__` 对非 SYNTHETIC Universe 调用
`JobSpec.require_execution()` 并拒绝 `d1_offline_only_execution_not_admitted`，即使
Source 为 RETAINED 也不例外。真实 I-2 Universe 的全链重放需要集中集成人建立明确的
离线 REPROCESS/资格接缝；不能把真实 Universe 标成 synthetic 绕过此门。本线提供
材料输入合同，并证明具名 retained 输入能保留，未声称真实 Universe 已可执行。

**I-3**：写侧实现 `MarketWriter` 和 `MaterialBytes` 即可接入 retain/prepare/commit/
publish/verify，逻辑 locator 可由具名实现映射到内容地址 key，禁止 fallback。
读侧应直接实现 D4 `MarketProjectionRead.read_view` 全协议，或提供同一只读事务内
canonical Version/Publication/material/receipt/occurrence point-read、reverse index、
精确 admission 及一致预算。需要保证 canonical 与投影原子性、恢复 generation、
原材料独立读回，不能复用此 Memory private map 访问方式冒充 PG 适配器。

**I-4**：分时/逐笔/ExHq 仍为独立能力；日线 BARS_DAILY decoder 和本线
DailyDefinition 不给这些能力授予字段、市场时钟或单位语义。需要各自明确合同后
再接入相应纵向；本线没有把非日线响应强行装入 DailyBar。

本轮未接收真实 I-2 retained 下载、I-3 物理 adapter 或 I-4 语义 decoder。
这些接口的真实资格与效果仍为 UNKNOWN，未影响本线 synthetic 离线验证。
