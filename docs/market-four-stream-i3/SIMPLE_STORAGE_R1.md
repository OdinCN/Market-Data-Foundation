# I3 PG + Garage 简化合同与 DEV 运行

入口为 `SimpleGarageMaterials` 与 `SimplePostgresDataset`。复用现有
Material / Version / Publication / projection 表及 D1–D4 验证；不改变 Source、
Driver、I4 或业务时间语义。旧 CAS probe 与旧历史收据保留，不作为本入口门禁。

物理 Key 绑定**整个原始 bytes** 的 SHA-256，原文长度与摘要均校验。
Job、shard、attempt、request、source、mapping/upstream/decode revision 和
requested/obtained/received/known 时间留在 PG receipt 中。同一 Material key
对应同一完整 receipt 与 level，不允许用重抓的新内容或新 provenance 冒充原 attempt。

顺序为：PG 持久化原始 intent 与 raw → Garage GET → 必要时普通 PUT → exact GET
→ PG 原有事务提交 Material 与 commit receipt、pending complete。
Version 与 Publication 继续使用已有独立幂等事务及发布验证。pending 提交未知时
停止对象 IO，重连后按原 identity 查询/恢复。对象成功但 PG 失败留下对象和 pending。
`recover(content_id(material.key))` 只读取已冻结的 PG payload，不调用 provider。
COMMIT ACK 未知用 `verify` 核对原 identity / payload digest；ABSENT 只是本次快照
未见，随后原身份重试仍由 PG 锁与唯一约束仲裁。所有查询连接必须专用，禁止嵌套事务
或在线程间共享连接。

PUT 最多默认 2 次（可设 1–3）；每次重试都是相同 Key、相同固定 bytes，短退避后
重新观察。404 允许同 bytes 重试，不证明旧请求已排空；403、其它非 404 失败和 GET
未知不授权写入。已存在内容不符直接拒绝，绝不覆盖修复。跨进程恢复重新核对冻结原文；
一个失败任务不设置全局停止位，其它任务可继续。读路径仍验证 bytes / 业务事实。

新入口的 `_send` 只允许 GET / HEAD / PUT；cleanup 与 CAS qualification 入口明确
拒绝。无 Garage DELETE、自动 GC 或 TTL；不声明 WORM、恶意管理员防篡改或物理恰好
一次。旧入口属于历史候选，不用于简化运行；尤其不要运行旧 cleanup 脚本处理新前缀。

每个 schema 的冻结原文总量有 64 MiB 硬上限（包括 complete），单条 8 MiB；当前 DEV
Garage adapter 更严格限制每个实例最多 16 个地址、4 MiB 原文及 96 请求/120 秒。
`pending_inventory()` 返回身份、intent 摘要、长度和 pending/complete；pending 表示
仍需对账，可能已有对象，不是已经证实无引用的孤儿。pending complete 与业务提交原子，
confirmed payload 目前保留，容量耗尽需单独规划维护，不能自动删 Garage 原文。

SQL 候选为 `001_market_dataset.sql` 加 `002_simple_pending.sql`，只向新的
`mdf_i3_simple_i3_<32 hex>` schema 执行；合并 DDL 摘要、revision、run 绑定进入
schema_metadata。`migrate_simple` 拒绝已有同名 schema，不升级或清空旧 schema。
复用现有 semantic ledger binding，由隔离 schema 与 owner run 区分物理实例。

受限验收命令使用现有 uv Python 3.14.8 环境：

```bash
PYTHONPATH=src:. \
UV_CACHE_DIR=/tmp/mdf-simple-uv-cache \
UV_PROJECT_ENVIRONMENT=/srv/market-data-foundation/.venv \
uv run --frozen --no-sync python scripts/mdf_i3_storage_simple_dev.py \
  --opt-in-dev-write --receipt artifacts/i3-simple-validation/dev-receipt.json
```

该命令每次会产生新 run。一次授权只执行一次，不作循环或 timer；读取已有 owner-local
runtime/secret 配置并验证固定 PG host/db/role/TLS、Garage endpoint/bucket，不打印
凭据或原始异常。先检查 fsync/synchronous_commit/full_page_writes 与可观察归档统计；
没有备份恢复证据则明确记录 UNKNOWN，不伪称生产 PITR 已验证。

脚本只生成一份 2024 年虚构日线材料，最多新建一个 schema；父/恢复子进程的请求预算
分别 80/16（合计低于 128），同 bytes 的专门重复 PUT 计入请求，整个进程有 295 秒
闹钟。新前缀内最多 16 个对象、4 MiB，实际 fixture 仅一份对象。结果、每次 HTTP
意图/响应分类、确切身份、大小、PG 行数与未清理原因保存在 ignored artifacts；
进程子收据单独保存。所有新合成对象和测试表保留，不将其自动纳入产品存储。

离线 SQLite/fake 测试仅验证算法与约束路径，不证明 PG/TLS/锁/持久性。完整 deterministic
结果、独立专业审查和真实 DEV 收据必须分别报告。真实 PG 的提交后注入丢 ACK 验证
客户端恢复，不等于服务器崩溃、网络分区或 PITR 演练。有限 DEV 通过也不是生产上线、
Driver pin、adoption 或 phase 放行。

错误响应保持 HTTP 状态，丢弃 S3 错误 XML；材料容量上限只约束成功返回的 bytes。
这避免把小材料的正常 404 误记为传输未知，同时仍保留 403 的拒绝语义。
`--resume-receipt` 仅用于初次单 GET 未知、无 PUT/child/前次恢复的失败形状，复用
原 run/schema，扣除已用请求；不会创建第二 schema。父子进程共享最初 origin 和
295 秒截止时间，使用精确计时器；父进程被超时中断时先终止并回收子进程，不能让
子进程越过原预算继续 IO。该恢复选项仍要求显式写入 opt-in，不能拿成功收据重复跑。

恢复预检还核对本次 schema/表 owner、schema/表/列 ACL 无外授，以及 DEV 角色
createdb/replication/bypassrls 均为 false。漂移时直接拒绝，不自动修复既有 ACL。
新 schema 尚未创建时允许进入一次创建事务；恢复仍必须通过 owner-run / DDL metadata
核对。权限守卫与截止时间回收逻辑在本次真实效果结束后做了离线补强，未额外执行实仓。

原始业务请求身份另由固定 domain、job/shard/attempt/request/level 组成，其摘要
`attempt_identity` 在 pending 中具有 NOT NULL UNIQUE 约束。Material key 的原 PK
同时保留。冻结事务同时查原业务请求摘要和 Material key，必须完整 intent/raw 相同；
换 Material key 也不能用不同 bytes 重抓冒充原请求。独立的新 request 可继续运行。

本笔收尾曾发现上述原请求换 key 绕过 P1，修复前的合成 DEV 收据不构成最终候选验收。
修后增加唯一约束及离线回归，未追加真实效果；原合成 schema 保留，合并 DDL 摘要
已不同，不能用新入口接纳该旧实例。本轮不报告最终候选“限定 DEV 能力通过”，需在
另行明确的受限真实复验授权下验证修后候选，不能用 fakes/SQLite 补足这份 PG 证据。
