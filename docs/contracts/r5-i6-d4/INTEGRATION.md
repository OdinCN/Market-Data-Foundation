# D4 市场读取接缝 R1

本实现绑定 `mdf.consumer.market.read.v1`、R2 §7/§9、六线共享交接 R1。实现位于独占新增 package，旧 Consumer、canonical kernel、D1 合同和 `web/app.py` 均未修改。所有合成 QUALIFIED、市场闭市、身份及证据均仅为 synthetic fixture，不建立真实 Source/Calendar/Universe 权利。

## 可调用入口

- `canonical.market_consumer_read.MarketReader(projection, binding, cursor, clock, limits)`：`read(Query, cursor=None)` 返回统一字典；失败只抛带稳定分类的 `ReadError`。单项是一个 item，未另设选择逻辑。
- `canonical.market_consumer_wire.parse_body/read_body`：严格 JSON 请求和序列化响应。请求形状遵循 R2 示例；instrument 使用 `namespace/identity`；Definition 使用 `identity/revision`；Version 清单在各 item 的 `exact_versions: [{"identity": "..."}]`。selection.mode 是 `as_of` 或 `exact_versions`。可选 `semantics` 是字符串键值对象，必须与投影精确匹配；不推断单位/复权。可选 `universe_revision` 必须由 admission 明确核验。
- `web.market_api.create_app(reader, dispatch=None)`：独立 HTTP ASGI callable，路径 `/mdf/consumer/market/read/v1`、JSON POST。cursor 仅放 body；URL query 和 cursor header 拒绝。body 流累计限额 72 KiB。默认同步执行，适合离线/单并发宿主；并发宿主应注入有界 worker dispatcher 和传输 deadline。
- `web.market_api.create_router(reader)`：延迟导入既有 FastAPI/Starlette，使用其 worker dispatcher；集中集成人可在**另行允许的共享接线事务**执行 `app.include_router(create_router(reader))`。当前未挂载、未启动服务。

独立 Reader/wire/ASGI 可在未安装 FastAPI/Starlette 的测试环境导入和执行。显式请求 `create_router` 时仍要求宿主提供锁定依赖；缺包抛出 `ModuleNotFoundError`，不会安装依赖或自动切换入口。专属测试在缺包环境跳过这一项可选 route 验证，并始终执行主动阻断 Web 依赖的 ASGI 首读/续页负例；完整依赖环境执行全部路由验证。

## D3 → D4 精确端口

完整可导入类型在 `market_consumer_read/contracts.py` 的 `MarketProjectionRead`、`ReadView`。这是 D4 已实现的规范化端口，不声称并行 D3 已提供同名接口。集成人应编写 D3 adapter，不能用 `versions_for_definition()`、旧全量 Publication 枚举或 payload 扫描代替。

1. `read_view(budget)` 必须是每请求独立的短 `REPEATABLE READ READ ONLY` 事务，退出即释放连接；`now` 来自同视图可信 UTC 服务时间。提供受控 `Binding(ledger, schema_revision, algorithm_revision, generation, ready)`，不由请求选择存储。
2. `directories(month_keys, budget)`：精确 Definition/instrument/月点查，READY 明确覆盖每个自然日，含零计数、候选集合摘要和 overflow；缺目录不可当零数据。
3. `candidates(slots, budget)`：每批最多 512 个精确格点，返回每格点全部 Version Key；空格点也必须有空 tuple。无 LIMIT 截断。Reader 核对数量、唯一性、集合摘要、范围，跨批累计预算。
4. `versions(keys)`、`version_slots(keys)`：主键/反向索引点查；exact 仅指定清单，核验 Version 完整月内范围。`Version.range_key` 必须继承 canonical/D1 发布单元身份，不能由 D4 猜日历、时区或映射；fixture 中的合成 range key 不可当真实 D1 身份使用。
5. `canonical_versions(keys)`、`canonical_publications(keys)`：按完整 Key 读 canonical 原记录，**由 adapter 验证 canonical digest、scope、Assessment/材料读回、Publication rule/evidence 以及跨记录关系**，再返回与投影相同的规范化 descriptor。D4 比较 descriptor，并额外验证 predecessor、唯一 occurrence/decision time、合法 PUBLISH/WITHDRAW/RESTORE/SUPERSEDE 转移和三时间可用性。不能只原样返回投影副本。
6. `version_guards(keys)`、`histories(keys)`：准确全历史数、摘要、单调变化号和完整事件；未发布 Version 也进入候选，历史可为空。超过读取容量拒绝，不清理/截断历史。幂等重放不得增加 guard 号；恢复/撤回再恢复必须增加。
7. `admission(query, versions)`：Definition、units、可选 Universe 精确成员以及各 Version 的 version/source/binding/mapping/calendar/各 lineage retention guard。subject 编码使用本模块 `query_subjects/version_subjects`，revision/change_no/status 不能由 Reader 虚构。UNKNOWN 和 UNQUALIFIED 均拒绝消费；Source capability 及 policy/scope 的全验证由 D3/受控资格宿主完成。
8. `contents(versions)`、`lineages(versions)`：有界批量精确读取。先依 descriptor 长度/引用数拒绝超限，再返回实际 bytes；Reader 校验实际累计限额、长度和 SHA-256。lineage 为公开 JSON 证据列表，每条仅 `key/record_digest/references`，不能泄漏私有 locator；全部引用闭合到给定 lineage 或精确 Definition。canonical 私有原材料和真实经济验证留在 adapter 内。
9. 每个端口方法和所有内部 SQL 必须共享传入的 `Budget`，在 IO 前调用 `budget.statement()`，不能每批重置。底层单语句 timeout≤1s，事务≤5s，由 adapter/DB 强制；Reader 另做返回后的耗时和请求≤8s检查。这些 Python 检查不能中断一个阻塞的任意第三方 adapter。

应用侧 statement/transaction/request 耗时超限统一抛 `ReadError("READ_TIMEOUT", 503, dimension)`，dimension 为 `statement_time/transaction_time/request_time`；adapter 的底层查询超时也应映射到此稳定分类，禁止携带原始异常消息。数量、字节及语句数等容量超限仍为 HTTP 422 `READ_CAPACITY_EXCEEDED`。超时是请求级失败，不返回行情或续页 cursor。

descriptor/content/lineage 的规范化表示是 D4 端口合同，**不替代 D3 canonical 存储格式**。D3 若使用别的类型/摘要算法，应在 adapter 转换并验证，不能悄悄改共享 kernel 或假装两者已经联调。当前未实现真实 PG/Garage adapter、namespace/DDL、索引维护、Source admission、D2 job 闭环或七入口 UI 接线。

## 内容与结果

内容是规范 JSON 自然日列表，每个 Version 的全部自然日恰一条 `day/classification/raw/evidence`。classification 继承 D1 coverage 名称；合法无行情日不从分页流删除。raw 保留字段名与 native 标量，无汇率/股手/价格单位转换。非 UNKNOWN 分类需要公开证据引用；原始经济/日历事实真实性由 canonical adapter 验证。

`day` 和 `classification` 必须先验证为字符串，再进行字典/集合查找。即使内容摘要、长度及 canonical descriptor 均一致，非法字段类型仍归该 item 的 `INTEGRITY_FAILURE`，不能中断同批健康 item。

item 错误（歧义、范围/语义不符、不合格、非法历史、内容损坏）抑制整个 item 的行情，其他 item 可返回，整体标 `PARTIAL_FAILURE`。索引、覆盖、全局预算、存储、协议/cursor 错误拒绝整页，仅错误信封，无 units 和 cursor。缺口/未发布按自然日明确表示 UNKNOWN，不能凭空解释为闭市。

签名 cursor 只含 A/T0/query/binding/generation/C/S/G/R/位置/last/总量/固定到期时间，不含 Version 清单或服务端会话 ID。规范 JSON + SHA-256 + HMAC-SHA-256，无压缩。每页完整重建；相关晚于 A 的新增、历史变化、当前 guard、未来决定成熟及 generation 改变均终止旧分页；无关 instrument 改变可继续。相同 cursor 重放有相同 page_id、范围和数据；续签不延长 expiry。

密钥由 Host 注入，至少 32 bytes，有限 key allowlist，一个签发 kid、旧 kid 可仅验签；Reader 不生成或落盘密钥。示例测试值只用于合成测试。缺配置/时钟不可用返回 503。

容量默认实现 R2 §7.13 的应用侧上限；允许配置降低，构造时拒绝升高。单请求最大值组合仍可能因其他累计门失败。候选/Publication 完整性、内容验证和响应分页是独立预算，page_size 不能绕过候选超限。真实数据库扫描、buffer/physical reads、事务中断和代表性性能须由 Host 对 D3 查询计划另行验证。

TDX 字段依据固定 `v1.0.0` AST 索引和字段语义差异审查 R1。本读取层不调用 TDX、不实现 decoder/经济字段解释；synthetic Volume 保持 raw，semantics 明确 UNKNOWN。其通过不能授予真实 Source、Calendar、Mapping 或经济单位资格。
