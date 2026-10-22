# I-3 市场数据物理端口 R1

绑定 Session #15 MDF Product Control / Owner-local / MDF-DEV，候选基线
`e8faac58b8d97594109ed2dc63ea110afb36cfce`。依据本批 BATCH_DIRECTIVE、
SHARED_PORT_R1、正式设计 R2 §6/§7.4–7.7 和现有 D1–D4 合同。
本目录采用本工单最后指定的 `docs/market-four-stream-i3/`。

## 目标与物理命名

| 层次 | 推荐与本候选实现 |
| --- | --- |
| PostgreSQL | 原有 `market_data_foundation_dev` database / 同名独立 role；固定 `192.168.10.4:5432`、TLS `require`、UTC session |
| schema | `mdf_market_dataset_dev_r1`；仅在不存在时新建，任何既有同名 schema 均 STOP，不接管、不自动升级 |
| binding / DDL | `mdf.market.dataset.dev.r1` / `market.dataset.physical.r1`；DDL 文件 SHA256 与 owner run 入库，后续每事务精确检查 |
| Garage | 原有 `mdf-artifacts`，`http://192.168.10.8:3900`、region `garage`、path addressing；不新增 bucket |
| 资产目录 | `market-datasets/v1/`；本候选真实操作只允许其 `probes/i3-<32位小写hex>/` 子树 |
| 普通 key | `<prefix>/<level>/<Source-Key-SHA>/<Binding-Key-SHA>/<semantic-SHA>/sha256/<exact-byte-SHA>` |
| level | `raw`、`source-projection`、`status`、`metadata`；SDK 解码投影必须显式选 `source-projection`，不得因其也是 bytes 而冒充 wire raw |
| semantic-SHA | `content_id((mapping_revision, upstream_revision, decode_revision))`；完整语义和 request/Definition/Capability 仍保存在 receipt 中 |
| 专用探针 | `<prefix>/conditional-sentinel`；唯一允许以不同 bytes 尝试同 key 的一次负向条件创建测试对象 |

所有 SQL 对象为小写 snake_case，不接受调用方 schema 名或 DSN。完整 Key 仍为
`(Kind, identity, revision)`；主键使用 D1 `content_id(Key)`，封闭类型 payload
保留完整值并在读取时重算摘要、检查身份。这个 SHA 是索引地址，不允许跨 Kind
或 revision 合并。仪器保持 namespace + stable identity，不使用裸代码。
local_date 为交易日 `date`，窗口保持半开范围；requested/obtained/received/known、
frozen/decision/usable/effective 使用 `timestamptz`。None 原样保留 UNKNOWN。
序列化将已知 instant 规范化为 UTC，保留 D1 canonical digest。

## 实际建表和索引

DDL 在 `db/market_dataset/001_market_dataset.sql`；15 张表均由现有值合同驱动。

| 表 | 主键 / 约束与用途 |
| --- | --- |
| schema_metadata | singleton=1；schema revision、DDL digest、binding、owner run、创建时间 |
| materials | Material 完整 Key 摘要；原 receipt、Job/Shard/Attempt/request、Source/Binding/Mapping、level、URI、byte digest/length、四时钟；attempt 复合索引及 content 索引 |
| versions | Version Key 摘要；canonical payload/digest、frozen、内容摘要和长度≤64KiB |
| version_materials | `(version_id, material_id)` 外键；反向材料引用索引，供持有审计 |
| publication_events | Publication Key 摘要；occurrence 全局唯一；Version FK；每 Version decision_at 唯一；`(version_id, decision_at, id)` 完整历史索引 |
| publication_heads | 每 Version 独立 canonical count/head/history_digest；head FK，零计数/空 head 成对约束；不依赖投影表作为权威 |
| commit_receipts | `(operation, identity)`，冻结 payload_digest/result_digest；原身份幂等、UNKNOWN 核实 |
| admission_guards | 精确 subject PK；revision/state/change 值；对应 commit receipt 校验。这里只是 DEV 显式事实，无真实准入授权效力 |
| projection_generations | generation PK；唯一 active；binding/schema/algorithm/READY；历史名称不删除、不复用 |
| projection_versions | Version FK/PK；D3 VersionMetadata 投影 |
| projection_months | `(definition_id, instrument_namespace, instrument_id, year, month)`；完整 MonthKey；明确覆盖，缺行不当零 |
| projection_slots | `(definition_id, instrument_namespace, instrument_id, local_date)`；完整候选 count/digest/overflow，>64 保留 count、digest=NULL |
| projection_rows | 上述格点 + Version FK 的复合 PK；反向 `(version_id, local_date)`；每自然日一条，含非交易日 |
| projection_publications | event FK/PK；Version/decision 索引，完整发布投影 |
| projection_guards | Version FK/PK；完整历史 count/digest/change，与独立 canonical head 对账 |

不额外创建目前没有独立完整输入合同的 data_runs/acquisition_attempts 空表：D2 的
原始执行身份已在 materials 显式索引，不能制造 AcquisitionOccurrence。
coverage assessment 作为现有 `MarketVersion.assessment` 不可变 payload 的组成部分
保存，不新增可漂移的 assessment 真相。D1 Job 全局 coverage manifest 的单独持久化
尚未实现；需由集中集成补齐明确接口，不能把此处 Version assessment 当全 Job COMPLETE。

## 写入顺序与原子性

1. Host 冻结 run、目标、预算及 namespace/ACL intake。迁移使用普通 PG 事务。
2. Garage 对 sentinel 执行 GET=404、条件 PUT 成功、不同 bytes 条件 PUT=412、
   GET 原字节。失败只可能影响该新 sentinel；不降级成先读后写。
3. 材料按内容地址条件 PUT，再独立 GET 校验长度/SHA；PG 原 receipt 后提交。
4. Version 使用原 D3 `MemoryMarketLedger` 作为纯验证器重放完整材料解码、
   framing、行摘要、coverage、required subjects 和 acquisition/freeze 时序。
   验证器的 `put` 只做既有 Garage 字节读回，不新增写。随后 Version、材料 FK、
   canonical 空 publication head、完整日期投影和 receipt 同事务提交。
5. Publication 独立事务：当前 material/准入校验，唯一 occurrence、完整 predecessor
   与转换校验；decision_at 来自 PG `clock_timestamp()`。事件、head、投影、guard、
   receipt 同事务提交。撤回不要求已损坏材料重新可读。SUPERSEDE 继续不支持。

所有写事务使用同一个 transaction advisory lock 串行化 DEV schema，避免并发更新
目录/guard 和重建竞争。这个候选优先简单正确，不声称已完成高并发优化。
Garage 与 PG 无分布式事务：Garage 成功后 PG 失败允许留下未引用对象，不能成为
已发布事实。提交阶段异常及测试 AFTER_COMMIT 都返回 UNKNOWN，调用者在新连接按
原 operation/identity/payload_digest 只读核实；一次 ABSENT 不是自动换身份重试授权。
相同身份不同 payload 拒绝；幂等重放不改时间、count、guard、generation。

物理 material 的提交 payload 为 `(MaterialReceipt, level)`，与内存 D3 的纯 receipt
不同，因为须把物理语义等级纳入身份核实。Version、Publication 的 payload 则继续
分别为原 Version / 原 PublicationRequest。物理 Material 调用者必须在发起 retain
前保存不可变原意图，不依赖成功返回的 CommitReceipt：

```python
intent = writer.material_intent(receipt, level="source-projection")
# 在 commit 前由调用者保存 intent；level 必须来自明确的原提交意图。
try:
    ack = writer.retain(intent[0], raw, level=intent[1])
except CommitUnknown:
    outcome = verify_original(writer, intent)
```

`material_intent` 是无 I/O 的纯冻结接口，返回现有提交 payload 的 tuple，不产生新
物理合同。I-1 `verify_original(writer, intent)` 使用原 receipt Key 摘要及整个 tuple
摘要，只调用一次只读 verify；正常提交与 AFTER_COMMIT 丢 ACK 均按同一原意图核实。
同 Key 的错误 level/receipt 返回 CONFLICT，异 Key 无记录返回 ABSENT，无法读取返回
UNKNOWN。不查询材料记录猜 level，不从 material_kind、locator 或 ACK 推断，不尝试
其他摘要。纯 `MaterialReceipt` 仍按原 D3 payload 核实；直接拿它核实 I-3 会 CONFLICT。
现有 I-1 `retain_page` 仍是纯 receipt 的便捷提交路径，未提供物理 level/locator 适配；
I-3 调用者须采用上述显式原意图接口。完整 D2→物理存储接线仍待独立工单处理。

**I3_PHYSICAL_STORAGE: STOP**：已核实 Garage 不执行所需同 key 条件不可覆盖。
本接口修正及内存 SQLite 回归不改变该失败，不证明真实 PG/Garage、恢复或运行资格；
原集中审查 CORRECTION_REQUIRED 保留，候选须经新 Host QA 和独立只读 Reviewer。

## 读取与 I-1 交接

`PostgresMarketDataset.read_snapshot(expected_generation=None)` 是显式上下文管理器；
内部使用 `REPEATABLE READ READ ONLY`，提供原 D3 view 方法：month_directory、
candidates、versions、histories、version_guards、admission_guards、lineage、content、select。
每页必须重新打开和关闭。离开 context 的 view 拒绝后续使用。

SQL 按精确月份/日期复合键或 Version PK 点查；exact 通过反向日期索引核实完整
范围，不枚举 sibling。目录读取只取 count/digest/overflow，候选读取前预检完整累计
预算，再取≤65条作越界检测。历史按 canonical head 和 guard 先预检整个请求预算，
再读完整历史并对账，不能截取前64条当成功。数据恢复与内容仍复用 D3 的零隐式
效果校验；不得从 Reader 触发 rebuild、Provider 或 PUT。

沿用 D3 的累计 slots 8192、references 32768、distinct Versions 256、exact 128、
months 448、history/version 64、events 2048、metadata 16MiB、content+raw 8MiB 等门。
canonical Version 加载另有累计16MiB物理编码预算。>64候选/事件继续持有完整事实，
拒绝超限读取，不删除第65条。目录计数不能检测 canonical/投影被同时恶意篡改；
可信边界仍是受控写入口、数据库权限、同一事务及明确重建。

这是显式 PG context API，尚非 D4 `reader.open()` 的无生命周期替换。集中集成须让
每页 D4 view 使用发生在该 context 内，不能将关闭的 SQL view 交出继续使用；此线
未改 D3/D4 kernel、Consumer HTTP、旧 PostgreSQL allowlist、Driver pin。
当前逐键 SQL 的代表性 EXPLAIN/I/O/延迟尚 UNKNOWN，不能据离线字典或 SQLite
测试声称已满足 R2 代表规模性能门。模块运行依赖 checkout 内的 DDL；wheel 分发需
集中集成另行打包，不能从未包含迁移文件的 wheel 运行迁移。

## 权限、持有与恢复

连接入口固定 MDF 独立账户，拒绝 wrong server/db/user/session user、非 TLS、
superuser/createrole 和不隔离的 default ACL。创建前检查同名 namespace；新 schema
和表 REVOKE PUBLIC。不创建 role/database，不改已有宏观/P4 ACL。DB owner 本身
可以直接改表，因此此权限边界仍依赖可信 Host writer；本候选不虚称拥有不同只读
DB role。Reader 使用 SQL READ ONLY 事务及只读 facade。正式角色分离需独立设计。
Garage 依 Host intake 证明独立凭据、bucket 权限及全新 prefix；没有复用旧 CPI
“先 GET 后无条件 PUT”的写路径，也没有凭据发现或 endpoint fallback。

新投影先 NOT_READY。`rebuild(new_generation, covered_months)` 在同一写锁内读取
有界 canonical 图（≤256 Versions/4096 Publications/448 months），逐项重放材料、
时钟及 canonical↔receipt 双向检查，再重建所有派生表与独立 heads，原子切 READY。
缺 canonical 尾部但 receipt 仍在会拒绝。恢复不生成新 Publication，不改原 occurrence
或时间。历史 generation/receipt 永久保留，本候选不做在线分段重建。

单表投影 DELETE/损坏：Reader 拒绝；Host 显式 suspend 后 rebuild 成新 generation。
rebuild 事务失败保持旧状态；旧 cursor 对新 generation 失败。canonical 表损坏不能
靠投影补真相：先从已验证备份恢复 exact canonical/receipt，再显式重建。
目前未实证 PG WAL/PITR、磁盘崩溃恢复、Garage 副本/备份、WORM/Object Lock、
跨存储备份一致性；这些均 UNKNOWN，正式持有/生产发布 STOP。

建议未来备份：停写并记录数据库 snapshot/LSN、DDL SHA、binding/generation 和全部
material manifest；仅本 schema 的 `pg_dump` 与 manifest 指定 Garage keys 做一致保留，
隔离恢复后逐对象 GET/SHA、canonical↔receipt、全索引/heads 检查，最后新 generation。
本候选不安装备份任务、不宣称 PITR 可用，也不调用外部 PG 管理命令。

DEV probe 默认成功后回滚：核对 owner_run/DDL/binding，按 FK 逆序 DROP 本候选已知
15表，最后 `DROP SCHEMA`，**不使用 CASCADE**；未知对象/依赖导致整体回滚。
独立确认 PG schema 不存在后，才删除本次记录的 exact Garage keys，并 GET=404。
失败或 `--hold` 不自动 GC：收据保留所有 PUT intent（含 UNKNOWN），先同身份核实，
再使用精确 cleanup 脚本。建议 Host 在48小时内复核 hold，但无 TTL 自动删除。
未决 Version/Publication/material 不自动清理；正式资产 retention/GC 规则仍须裁定。
