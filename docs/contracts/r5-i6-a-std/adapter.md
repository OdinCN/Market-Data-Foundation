# A-STD 离线目录输入与 D1 端口

绑定：Market-Data-Foundation / #15 MDF Product Control / Owner-local；基线
`e7b64b382cbd8492b3e98785014270f2326ee2d3`。本模块不初始化 Go SDK、
`NewCodes`、SQLite 或 cron，不提供网络、文件读取、持久化、Publication 接口。

## 固定字段依据

上游 `injoyai/tdx v1.0.0`，commit
`2a9000681f5a9896ea98e4de8bc4904f1dce78ba`。只读合同输入：

- `UPSTREAM_V100_FIELD_STRUCT_INDEX_R1.json` 与
  `UPSTREAM_V100_FIELD_SEMANTICS_GAP_AUDIT_R1.md`，位于控制根
  `research/mdf-unified-source-20261018/injoyai__tdx/`。
- 精确源码 `protocol/model_code.go:8–18,36–65`、`model_count.go:5–7`、
  `model_block.go:103–128`、`protocol/unit.go:333–479`、
  `client.go:264–371`、`codes.go:174–221,283–292,373–434`。

模块内 `FIELD_FACTS` 固化 AST 字段名、Go type、source_ref、单位/时钟状态、
变换说明、UNKNOWN 规则和上游 commit；运行与 CI 不依赖外部 checkout。
实际 producer operation、request market、route、implementation SHA、fixture、
raw digest 位于每个 `OfflineMaterial`，与字段事实联合使用。

| 响应 | 原生形状 | 特别限制 |
| --- | --- | --- |
| GetCount | `{"Count": uint16}` | 仅 SH/SZ 标准路径 |
| GetCode | `{"Count": uint16, "List": [Code]}` | 显式 uint16 start；原生固定页长 1000 |
| GetCodeAll | 同 CodeResp | SH/SZ 聚合页证据 UNKNOWN；BJ 必须绑定报告路径 |
| GetStocks / GetETFs / GetIndexes | `[CodeModel]` | 显式 retained CodesBase；保留 limit |
| GetStockCodes / GetETFCodes / GetIndexCodes | `["sh600001", ...]` | metadata 缺失保持 UNKNOWN |
| GetStockCodeAll / GetETFCodeAll / GetIndexCodeAll | 同前缀字符串列表 | 精确 client market scope；分页终止 UNKNOWN |
| ParseTdxBjMore | 显式 GBK `tdxbjmore.cfg` bytes | 不读取 zip/文件，不隐式下载 |

`Code` 精确字段为 `Name:string, Code:string, Multiple:uint16, Decimal:int8,
LastPrice:float64`。`CodeModel` 使用上游小写 JSON tags，含 `id/editDate/inDate:int64`、
`exchange:string`；这些本地数据库时间不被提升为来源发布时间或 known_at。
原生 required 字段缺失、额外字段、重复 JSON key、越界、bool 冒充数字、非有限价格均拒绝。
Go 的 nil slice 可为 null。float64 的整数 JSON token 还原为 Python float；原字节完整保留。

`LastPrice` 只提供 raw 和条件状态：上游明确个股无效，指数以元表达昨收，其他 UNKNOWN。
指数状态仍为 unqualified；无通用 `preclose`、价格缩放、货币换算、手/股换算输出。
`Multiple/Decimal` 保留原生范围，包括 0、负 int8，不推导货币、交易单位或 tick。
BJ 原报告无上述三个字段；Go Code 投影中的默认零值标 `SDK_DEFAULT_NOT_OBSERVED`，
非零则记录 decoder 冲突。报告原 market/category 保存在 row raw 字段与原 bytes。
`Codes.update()` 会将 BJ 报告投影的默认零值复制到 `CodeModel`；缓存 BJ 行的
`multiple/decimal/lastPrice` 保留原生值，metadata_status 为
`UNKNOWN_CACHE_FIELD_PROVENANCE`，last_price_status 为 `UNKNOWN`（包括 BJ 指数）。
缓存字段存在、本地时间或非零值本身均不构成独立字段证据；当前接口没有字段证据升格端口。
此 UNKNOWN 不阻断有独立身份/类别证据的目录成员选择。

## 调用与异常

入口包：`market_data_foundation.candidates.tdx_security_directory`。

1. 构造 `DirectoryRequest(operation, exchanges, route, snapshot_id, start?, limit?)`。
   Exchange 为显式 `SH/SZ/BJ`（native market 分别 1/0/2），绝不猜裸代码归属。
   BJ 标准 GetCount/GetCode 被拒绝；GetIndexCodeAll 的 BJ 仅含上游种子，不能宣称请求过 BJ 标准页。
2. 构造 `OfflineMaterial`：传入原 bytes、匹配 digest、Source/Binding Key、fixture_id、
   synthetic/retained 模式、三种本地时间、显式 implementation SHA、固定上游 commit、
   capability（security.universe 或 security.metadata）。本地时钟须 obtained≤received≤known。
   上游身份与 implementation SHA 是调用方声明，离线模块不认证二进制身份。
3. `decode(material)` 返回不可变 records、全部异常行定位和 termination。
   每行可读 `symbol/exchange/market/code/raw_fields/hints/metadata_status/origin`。
   raw payload 不出现在对象 repr；仅明确保留在内存中。
4. `observe(material)` 返回 capability/fixture/material_id、结果分类、异常类名；
   不含异常消息、认证信息、traceback，也不持久化。调用方保留 raw/observation 时必须使用 ignored artifacts/。

`ContractError` 仅含模块定义的分类；坏 schema 拒绝整体 decode。
合法形状中的重复、同码异值、错误 category、count 不一致、报告坏行等保留原数据并产生 issues。
相同 JSON key 顺序差异不构成数据冲突。多市场同码互不覆盖。
上游 IsStock/IsETF/IsIndex 仅为范围 hints，涵盖 SH 50/51/52/53/56/58、SZ 15/16；
不以范围证明 ETF 经济类别。SH 880/881 同时保留 index/sector_index 歧义。
`GetIndexCodeAll` 的 `bj899050` 始终标为 hardcoded seed，缺失/换序有 issue，不能作为成员证据。
该 producer 只枚举 SH/SZ；额外 BJ 行原样保留，产生带 row locator 的
`index_producer_scope_conflict`，阻断该响应的 D1 转换。
`RangeIndexes` 的上游 GetETFs 实现错误不被继承；该 operation 不在 allowlist。

## 分页与容量

`assemble_pages(before_count, tuple(pages), after_count)` 要求同一显式快照标识、Source、
Binding、capability、fixture、模式、implementation、market 以及 Shanghai 本地取得日。
它重新 decode 核对每个输入，检查 count 前后漂移、0/1000/... 页顺、实际行数、
同码重复/冲突、取得时间顺序、短页后继续、缺失终止。整 1000 的倍数需要额外空页证据。
uint16 offset 不会绕回；最多 66 页。异常 assembly 为 UNKNOWN，不截断冒充完整。

`PAGE_TERMINATED_COUNT_MATCH` 只表示传入页的局部一致性，不证明上游原子快照；
同 count 的成员替换无法仅靠 Count 发现。GetCodeAll、cache、filtered list 没有逐页证据时
仍可用于明确有限成员的 D1 输入，其 termination 保持 `UPSTREAM_AGGREGATE_UNVERIFIED`。
不允许据此宣称完整市场或完整历史。

单材料≤16 MiB、单响应≤65535行。D1 转换≤144个原始响应、累计≤64 MiB、
记录及选择≤65535行。超限整体拒绝。它们是离线防御边界，不是生产性能承诺。
BJ 报告逐行迭代，不预先拆分全部行；所有非空、非注释输入行（含坏行）均占用
行数预算，第65536行在解析及分配 record/issue 前以 `row_budget` 拒绝。

## 精确共享集成缺口

已直接调用冻结 `domain_contracts.market_universe`，未修改 D1 或其它共享文件：

- `snapshot_universe(inputs, selections, material_links, universe_id=..., mapping_revision=...,
  known_at=...) -> UniverseRevision`。`inputs` 为已 decode response / assembly；
  `MemberSelection` 必须提供精确 row_locator、外部稳定 InstrumentIdentity、AssetClass、
  身份与类别证据。模块不从代码生成稳定 instrument；证据真实性由调用方承担，
  文本存在不等于 qualification。
- `MaterialLink(candidate_id, material=Key(Kind.MATERIAL,...))` 必须精确覆盖输入材料，
  且不得把不同候选绑定到同一个 canonical key。当前只接受调用方给出的 Key；
  **D3/kernel 必须另行实现原 byte digest/length 独立读回与 canonical Material receipt，
  再回传这些 Key**。本模块不生成或认证 canonical Material。
- 结果仅为 obtained_at 的 Shanghai 本地日单日 SNAPSHOT；history_window=effective，
  lifecycle UNKNOWN，所有资格 UNKNOWN，calendars 为空，不推 Source/使用/持有权。
- `source_mappings(tuple[MappingAssertion], source=..., binding=..., revision=...)`
  可接受外部具证的半开有效区间并直接生成 D1 SourceMapping。同市场同码不同身份的区间
  可相接但不可重叠；同 instrument 并发多代码也拒绝。该端口不赋予历史 Universe 成员资格。
- `tdx.standard.SH/SZ/BJ` 是本 lane 的显式 request namespace 字符串，不注册全局 Source。
  `snapshot_universe` 中的 Source/Binding 是目录能力输入身份；**接 D2 daily 时须另外提供
  daily Source/Binding 和相应 MappingAssertion，经共享集成核对 namespace/market/类别**，
  不能把目录 Source 资格继承为行情 Source 资格。
- **Go/IPC 集成缺口**：具名目录 operation 的纯响应 envelope（上述原生 JSON / BJ bytes）、
  完整请求身份与逐页材料、实现 SHA 和独立时钟尚未接入 driver。
  应由单一集成人改 `drivers/tdx/main.go` / Source registry 等共享区；不得从此 Python 模块
  调用 `NewCodes`、`RangeIndexes`，不得更改 binary trust pin 或静默启用 BJ 报告网络。
- Calendar/历史成员、Source live、usage/retention admission、真实 PG/Garage/Publication 均无实现或授权；
  不自动补造，也不依赖这些服务运行专属测试。

## 可复现验证

现有 uv 管理环境 `/srv/market-data-foundation/.venv/bin/python` 为 Python 3.14.8；
工作树不新建环境或修改依赖。

```bash
PYTHONDONTWRITEBYTECODE=1 /srv/market-data-foundation/.venv/bin/python -m pytest -q tests/test_r5_i6_a_std.py -p no:cacheprovider
/srv/market-data-foundation/.venv/bin/ruff check --no-cache src/market_data_foundation/candidates/tdx_security_directory tests/test_r5_i6_a_std.py
/srv/market-data-foundation/.venv/bin/ruff format --check --no-cache src/market_data_foundation/candidates/tdx_security_directory tests/test_r5_i6_a_std.py
PYTHONDONTWRITEBYTECODE=1 /srv/market-data-foundation/.venv/bin/python scripts/check_repository_hygiene.py --include-untracked
```

所有专属样本均为测试代码内 synthetic；没有新生产数据、retained 实盘样本、
credential 或 observation 文件进入 Git。以上命令和代码不构成独立 reviewer verdict。
