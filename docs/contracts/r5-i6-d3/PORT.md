# D3 离线材料、发布与投影合同 R1

绑定 `e7b64b382cbd8492b3e98785014270f2326ee2d3`、六线 batch/shared-interface R1、R2 §9.3/§7.4–7.7。四个新增 Python 模块独立可导入；没有修改共享 canonical kernel、D1、Go、依赖或服务配置。

## 材料与验证入口

`canonical.market_material.MaterialReceipt` 是 D2→D3 的具体接缝。必须显式提供 Job/Shard/Attempt/request、精确 Source/Binding/Definition/Mapping revision、instrument namespace/identity、qualification `Capability` 和 `Fixture.fixture_id` 引用、producer operation、upstream/decode revision、请求字段、mode、raw/derived、digest/length/locator、页顺与逐行 byte offset/length/digest。requested/obtained/received/known 四种时钟独立；None 表示 UNKNOWN，不相互填充。新增 `requested_at: datetime | None = None` 放在 receipt 尾部，旧调用保留 UNKNOWN；D2 必须从精确请求事实映射它，不能从 obtained/received/known 反推。专属测试使用既有 Fixture 类型；本线不产生 live observations。

`MemoryMarketLedger.retain(receipt, raw)` 经 `MaterialBytes.put/read` 独立读回才建立 canonical 材料。URI 只是替身键，产品没有文件/HTTP/Provider fallback。相同材料身份异内容或 locator 异字节拒绝。原始材料不因 UNKNOWN 经济含义而丢弃；UNKNOWN 效果、未知 known_at、冲突单位不可形成可发布 assessment。

`assess_daily(unit, receipts, evidence, decoder, store, evaluated_at)` 消费冻结 D1 `PublicationUnit` 和每个自然日 `PointEvidence`，校验逐行摘要、全量行中的重复日期/未解释行、OHLC、有限非负 decimal 文本、零成交、stock/ETF/index 字段边界，以及逐行精确 request lineage。闭市、未上市、退市和无成交只能来自显式 D1 evidence，不从缺行推断。这里不授予 Source/Universe/Calendar 权利；其事实生成与资格仍属 D1/对应 adapter 的边界。

所有显式 `PointEvidence.material`（包括非 OBSERVED_VALID 分类）必须解析到 assessment 的 receipts 集合，每份 receipt 均独立校验合同、时钟、效果与原始字节读回，并进入 `lineage()`。悬空引用返回 `lineage_material_missing`；可读但未入 canonical 的材料仍被 Version 提交拒绝。提交、PUBLISH/RESTORE 和全局审计均重放此验证。非观测证据不因此被要求伪造行情行或 request；没有显式 Material 引用时仍沿用 D1 的证据合同。

`DailyDecoder` 是受信、显式注册的纯解码 port，其 `NormalizationContract` 精确绑定 API、请求类别/period、Source/Binding/Mapping、upstream、decoder、Definition 和字段/转换证据。新增必需方法 `enumerate_rows(raw: bytes, receipt: MaterialReceipt) -> tuple[DailyRowFrame, ...]`：必须验证完整材料及逐页 framing，不读取 `receipt.rows`，按物理顺序独立枚举所有日线行；每个 frame 给出 `page_ordinal/offset/length`，非行情证据也必须有明确且验证过的 framing。assessment 将这些 frame 的页身份、offset、length 和独立计算的摘要逐项与 receipt locators 全量对账；漏行、重复 locator、页错配、乱序或未解析字节均拒绝。Version 提交与 PUBLISH/RESTORE 继续重放此验证，不能直接伪造 assessment/bars，也没有缺失枚举方法时的旧路径 fallback。

内置 `SyntheticDailyDecoder` 的本线 fixture revision 为 `synthetic-json-r2`，只接受 synthetic JSON：LF 分隔的每页恰为一个完整 daily JSON object，或显式 `{"evidence":"非空合成证据文本"}`；页数必须等于 receipt 的有序 pages，禁止空页、尾部 LF 和非法 JSON。零行情行也必须独立解析成明确 evidence 页，不能靠空 rows 接受任意字节。这是合成测试格式，不是 TDX wire/page 合同；拒绝 retained/live，没有实现 TDX 解码器。解码器不得自行联网。

TDX 对接依据固定 v1.0.0 commit `2a9000681f5a9896ea98e4de8bc4904f1dce78ba` 的 AST field index 和 field-gap audit：`protocol/model_kline.go:42–53` 的 Price/Volume 等 native 字段，以及 `KlineCache.Type/Kind/Code` 必须一起绑定。Price 的 SDK 表现不自动证明货币；Volume 的注释不能跨 producing API/Type/Kind 推成手或股；Quote.Kline、ExKline、gbbq category6 不继承普通日线换算。当前实现不做这些经济换算；应由后续能力专属 decoder 保留 raw/UNKNOWN，给出可复核证据后再注册。

## 原子持久边界

`MemoryMarketLedger` 是离线事务替身：一个锁下复制草稿，canonical 与所有投影同步更新后整体切换；读者持有上一份或新一份完整状态。raw blob 在失败时可能成为未引用材料，不能因此发布。它不提供断电、跨进程或真实数据库 durability。

- `commit_version(MarketVersion)`：不可变内容、assessment、材料、D1 universe/calendar 引用、半开范围、冻结时间、admission subjects；同步写 version、canonical 空历史 head/count/digest、每自然日候选、精确反向索引、月目录与空历史 guard。未发布 Version 同样进入候选。
- Version 提交和 PUBLISH/RESTORE 重放验证都要求每份材料有明确 `requested_at ≤ obtained_at ≤ frozen_at`；缺值、逆序或取得晚于冻结一律返回 `material_acquisition_timing_unverified`，不产生该提交的 canonical/投影效果。raw receipt 及 assessment 可保留这些原始时间和 UNKNOWN，但不能因此成为可发布 Version。received_at 和 known_at 保持各自含义；known_at 原有 assessment 门继续生效。
- `publish(MarketPublicationRequest)`：独立提交 Publication、唯一 occurrence、严格 predecessor/状态转换、canonical head/count/digest、完整投影历史和单调 guard。decision_at 由注入的受控 clock 生成；不接受 caller decision_at、不倒填。PUBLISH/RESTORE 必须再次验证材料与全部当前 admission。撤回不要求损坏材料恢复可读。
- `verify(operation, content_id(key), content_id(payload))`：返回 COMMITTED/ABSENT/CONFLICT/UNKNOWN，无写入。Version payload 是 Version，Publication payload 是 request；AFTER_COMMIT 只丢 ACK。UNKNOWN 不能推成 ABSENT，不能创造新身份重试。
- 相同身份重放直接返回原 receipt，不递增候选数、事件数、guard 或时间；不同内容拒绝；相同 occurrence 不得改 Publication identity。
- `required_admissions(version)` 给出精确 Version、Definition、Source/Binding/Mapping、Universe/Calendar 与 retention policy 主体。缺失一律拒绝。`set_admission` 只注入离线受控事实；synthetic True 不能用作真实准入凭据。
- `rebuild(new_generation, covered_months)` 停写锁内从完整 canonical 图与 receipts 校验重建并原子切换 READY。空月份必须显式声明；Reader 不创建目录。`suspend()` 显式停止新视图；修复只由写边进行。
- `checkpoint/restore` 是内存备份值合同，恢复要求空 ledger、同 binding、全图/原 receipts/字节读回正确、新 generation；时间、identity、occurrence 不改写。失败无半恢复 READY。它不是持久备份文件格式或真实灾备资格。
- restore/rebuild 共用 generation 名称历史：当前 generation 或任一操作 receipt 已使用的名称（包括恢复导入的 receipts）不能用于新切换。原 operation/identity/payload 的幂等重放只返回原 receipt，不重新激活该 generation；拒绝复用不改变状态，旧 generation 继续返回 `INDEX_GENERATION_CHANGED`。真实写适配器必须在同一原子边界保留并检查这两类恢复身份，不能分开去重。
- `coverage_manifest(D1_manifest)` 保持 D1 三轴和材料/Publication 两轴独立；缺单元、歧义、不完整、无 READY 覆盖不提升全局 COMPLETE。每个计划单元逐自然日核对全部 canonical 重叠候选（包含严格子区间及未发布版本）与投影候选、元数据/反向索引、完整 canonical Publication 历史及 count/digest/change guard；有效候选必须独立读回并通过当前 admission，每格点必须恰好一个有效候选。候选缺失、历史截断、任何验证或容量失败都会阻断全局 COMPLETE；不修复投影、不自动淘汰候选。此全局离线审计扫描内存 canonical 图，每单元使用读 port 的预算，不代表全 Job 成本有界或真实数据库查询计划合格。`freeze_manifest/read_manifest` 将该时点的完整 D1 manifest 和结果作为独立不可变 assessment Material 保存、独立读回，保留部分状态，不自动产生 Publication；新冻结材料使用相同完成门，旧材料不被覆写。

替代组明确不支持：SUPERSEDE 请求拒绝。两个有效重叠版本 exact 各可读，as-of AMBIGUOUS；不能按最新 freeze time 或 identity 选胜者。

每 Version 的 `PublicationHead(version, count, head, history_digest)` 存放在 `_State.publication_heads`，独立于可重建的 `ProjectionIndex.histories/guards`。空 Version 同事务建立 count=0/head=None/空历史摘要；每次 PUBLISH/WITHDRAW/RESTORE 在写 canonical Publication 与 occurrence 时同步更新锚点，再写投影，整体提交或回滚。writer 在追加前把已有投影历史和 guard 与 canonical 锚点核对，旧 history 与旧 guard 一起回滚也不能成为新决定的依据；不匹配返回 `publication_head_mismatch`，不补写或修复。

`checkpoint` 保留完整 canonical Publications 与原 receipts，不信任或导出投影及锚点快照。`restore/rebuild` 验证 canonical 图、receipt 双向对应、完整 predecessor/转换/时间后，为每个 Version（包括无发布和超过读预算的历史）重建锚点与投影，在同一恢复事务中切换 READY。丢失 canonical 尾部但仍有对应 receipt 的恢复会拒绝。只有显式写侧恢复可重建；读口永不修复。这里的可信边界是受控 canonical 状态与事务，不是 Python 私有字段本身的权限保证。

全局覆盖不限制一个计划单元只能保留一个 Version：完成条件按每个日期格点当前唯一有效候选核定，允许未发布 sibling、撤回 predecessor 和不同日期范围共同覆盖。所选有效 Version 的逐日 evidence 必须与传入 D1 manifest 精确一致；历史 evidence 不能为当前候选补证。`MarketCoverageManifest.versions` 在发布覆盖验证成功时记录这些有效候选的去重、稳定排序 Key；发布覆盖失败时保留材料轴独立核实的候选引用。所有历史候选和事件仍在原投影保留，同时有效的重叠候选继续阻断 COMPLETE，不自动替代或清理。

用于计划完成的 Version 必须精确匹配计划单元的 Definition、instrument、Universe revision、Calendar revision 和 timezone；日期范围仍允许拆分或重叠。当前有效候选 scope 不匹配会阻断 Publication 完成轴；发布验证失败后的独立材料核实也只使用匹配 scope 的 assessment。不兼容版本不会进入该 manifest 的证明引用，也不会借用相同日期和 evidence 提升全局状态。已撤回的兼容历史仍可独立证明材料读回，但不能证明当前发布完成；读侧继续保留所有候选，不按 scope 静默删除候选或消除歧义。

## D3 → D4 只读 port

导入 `canonical.market_projection.MarketProjectionRead/MarketProjectionView`。传给 D4 的对象是 `ledger.reader`，不传 ledger writer。每个请求/页面重新 `open(ledger_binding, expected_generation)`；返回固定 committed snapshot 和服务端 `now`。旧已打开视图是短期 repeatable read；下一页必须新建视图并重核 cursor guards，不能跨页复用。

| 方法 | 完整性与用途 |
| --- | --- |
| `view.generation` | 精确 binding、schema/algorithm revisions、generation、READY；open 拒绝不匹配/不可用 |
| `month_directory(months)` | 指定 instrument/Definition revision/月的每自然日计数、digest、OVERFLOW；未覆盖不是零候选 |
| `candidates(slots)` | 指定格点全部候选，与目录、canonical 摘要和复合行索引核对；绝不截断 |
| `versions(keys, exact=True)` | 精确 Key 点查，校验完整反向日期范围与 composite row；不枚举同格点其它候选；返回范围、字段/单位/转换合同和 assessment 摘要，不携带 bars 内容 |
| `histories(keys)` / `version_guards(keys)` | 投影 guard 必须绑定独立 canonical head/count/digest；histories 再核对全历史和 canonical payload、唯一时间、predecessor、转换 |
| `admission_guards(key)` | 所需主体的当前 revision/state/change，缺失是 UNKNOWN/denied |
| `lineage(key)` | 精确 receipt/page/row/request 及时间、字段来源引用；有累计元数据/引用预算 |
| `content(key)` | 有界规范化 bytes；当前 Publication 与 admission 均通过后才返回，再读回原材料；不存在自动改选 |

`MemoryProjectionView.select(slot, as_of)` 是离线参考选择，验证完整历史的历史可用性，再执行当前服务门；D4 自己实现 Consumer response、批量映射和 cursor。元数据和历史可以解释未发布/撤回版本，不能据此绕过 `content()` 的当前门。

错误是稳定的 `ProjectionError.code: ReadCode`：INDEX_NOT_COVERED/NOT_READY/INCONSISTENT/UNAVAILABLE、LEDGER_BINDING_MISMATCH、INDEX_GENERATION_CHANGED、READ_CAPACITY_EXCEEDED，以及 VERSION_NOT_FOUND/NOT_PUBLISHED、CURRENT_ADMISSION_DENIED/CURRENT_PUBLICATION_DENIED。未知效果核实独立使用 Verification，不能混成读取零结果。

每个 view 累计：格点≤8192、目录不同月份≤448、格点全部候选≤64、候选引用≤32768、不同 Version≤256、exact≤128、每 Version 全历史≤64、累计事件≤2048、每 Version 内容≤64KiB、累计内容和原材料验证≤8MiB、元数据≤16MiB、每 Version material+row 引用≤128、不同 lineage 引用≤4096。第65候选/事件仍存储，读取失败；exact 不因 sibling overflow 自动失败。限额不是截断/清理权限。Material 入库单份≤8MiB，单 assessment 原材料总量另限8MiB。

`histories(keys)` 先对整批 keys 按 Version 主键点查 canonical anchor 与 projected guard，检查身份、非负整数 count/change、canonical count/digest 相等、head 空值语义及 head 对应 canonical event 的身份；已知 Version 缺少 anchor/guard 一律 `INDEX_INCONSISTENT`。再检查每 Version 的 64 事件门，并预留本 view 累计 2048 事件预算；完成所有预检后才访问任何 history。随后先核对实际长度与末事件 head，再计算历史摘要、逐项点查 canonical event 与验证转换。旧 history 与旧 guard 同时替换仍在预检失败；guard 超限、批量累计超限或跨调用预算耗尽时，不访问或序列化任何历史。写入保留完整历史，不以读预算限制事实留存。

`version_guards(keys)` 使用同一 canonical anchor 绑定检查，再返回既有 guard 类型；为 cursor 提供超过64事件时的准确 count，但不读取历史。anchor 检查每个 key 仅需固定数量的点查；`histories` 的事件点查/摘要/转换验证受上述64/2048上限约束，不枚举 canonical Publication 全表。`select/content` 必经 histories，因此该故障统一 fail-closed，无静默修复、canonical 业务写或原材料读回。正常完整历史仍可解释撤回事件，content 继续返回 `CURRENT_PUBLICATION_DENIED`。

`version_guards()` 和 `admission_guards()` 在访问 guard 前同样计入此 view 的 distinct Version 集合，与 candidates/versions/histories/content/lineage 共享累计 256 上限；重复 key 不重复计数。UNKNOWN/denied admission 也计数，第 257 个不同 key 返回 `READ_CAPACITY_EXCEEDED`，不能靠单独或交替调用 guard 入口绕过预算。两种 guard 方法的签名和返回类型保持既有 R1 port，D4 无需新增接口。

## 精确集成缺口与限制

1. **D2**：将本线具名 receipt 字段逐项映射，尤其页/逐行 byte locator、request fields、raw/derived 与 requested/obtained/received/known 四时钟；缺少精确 requested_at/obtained_at 时只保留材料，不从其它时钟补齐，不形成可发布 Version。D2 文件路径不是 canonical Material。D2 的真实 adapter 尚未在此树接线。
2. **D1/能力 adapter**：提供精确 Universe/Calendar/Mapping 的事实与资格，以及给定 producer scope 的受信 decoder/field evidence。decoder 必须实现新增 `enumerate_rows` 全材料/逐页 framing 与独立行枚举方法，再由 `decode` 解释单行；不能用 D2 的 locators 充当枚举结果。这里只复用冻结内容类型与 synthetic classifier，不制造资格。
3. **D4**：以 `MarketProjectionRead.open` 为唯一入口；按页重开、累积本页预算，签名并核对 generation/完整候选与历史摘要/selection/全部 guards/content 摘要。item≤32、日期跨度、响应/page/cursor 容量及签名由 D4 实施，本线不声称已实现 HTTP 或 cursor。
4. **共享 kernel**：单一集成人把本线 MaterialReceipt/DailyAssessment/MarketVersion/MarketPublication/ManifestMaterial 映射到既有 EvidenceMaterial/DatasetVersion/Publication；Source/Binding/Acquisition/Run 图由受控 adapter 补齐。当前独立 Market 值图没有冒充已注册进旧 kernel。
5. **PG/Garage/Go**：未来具名授权后实现 `MaterialBytes`、统一事务写适配器和同接口 read adapter，落地复合索引/约束/权限、namespace、retention、备份和恢复；canonical publication head 必须具有独立于投影的受保护主键存储，与 Publication/withdraw 同事务更新和恢复，读口按 Version 点查；不得只信任 projected guard 或 canonical commit 后异步补投影。Go→receipt 的 native field/byte locator 接缝也由单点集成处理。此处没有 DDL、真实连接、业务数据或上线资格；内存点查不能证明 PG 查询计划、I/O、并发隔离或断电恢复性能。
