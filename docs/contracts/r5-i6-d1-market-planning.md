# R5-I6 D1：离线领域与规划合同

业务依据为 `R5_I6_FORMAL_DATASET_DESIGN_R2.md` 第 4、5、9.1 节。
本候选只有不可变值结构和纯函数；没有 Provider、Worker、存储、Publication
执行或 Consumer 入口。所有真实类别全集与 Source 资格保持 UNKNOWN。

## 内容与身份

- `market_universe.py` 提供 `UniverseRevision`、`CalendarRevision`、生命周期、
  历史成员区间和 `SourceMapping`。revision 是规范内容摘要，不能由调用者另填。
  修改成员、日历、映射、已知时间或资格即产生新修订。历史修订不跟随当前目录。
- 稳定身份由显式 namespace 与不随代码改名的 instrument ID 组成。原始展示代码、
  Source 请求代码独立保存。代码复用必须使用不同 instrument ID 和不重叠的映射
  区间；跨市场同码使用不同的显式请求 namespace，禁止猜测转换。
- 所有日期区间均为本地日期半开区间 `[start, end_exclusive)`。终止日期是首个不再
  交易／计算的日期；生命周期缺证据保持 UNKNOWN。`known_at` 是 aware datetime，
  不得早于取得材料的时间。交易所时区保存在 CalendarRevision。
- 快照只能描述一个明确本地日期，不能扩成历史成员证据；历史成员模式保留完整区间。
  目录与日历分别引用 qualification capability、精确 Source／Binding／Mapping 和
  canonical Material key。历史模式下没有证据的成员间隔也不能推成上市或退市。
- `Qualification` 的成员、历史、映射、日历、日线、单位、使用、持有门按四类独立。
  `SourceRoute` 另有精确来源资格，类别资格不会自动赋予任意 Source 资格。
  synthetic 中的 QUALIFIED 仅供确定性反例；`require_execution()` 始终拒绝。
- 新 `DAILY_DEFINITIONS` 区分股票 CNY／shares、ETF CNY／fund_shares 与指数点位。
  指数明确价格／全收益类型，成交量和金额暂不适用；板块指数另有独立 Definition。
  既有 `dense_market.py` 的 `p3-r1` 和有限全集限制保持原语义。

## 冻结与有界展开

`plan_job(spec)` 接收冻结的 `JobSpec`，固定 Universe 内容、Definition／schema／
单位／口径、日历、完成日截止、Source／Binding／Mapping、实现和 endpoint 合同、
执行输入、继承的存储策略、occurrence、算法及预算。输出只表达候选计划。

先检查成员、天数与格点预算，再展开：窗口内历史成员并集 × 全部本地自然日期。
集合外或窗口外的历史成员不纳入该 Job；成员在窗口内的上市前、终止后、闭市、
非成员间隔与未知日期均留在分母。

`PublicationUnit` 是单 instrument × 不跨自然月且最长 31 日的值结构，
`expand()` 得到 `LogicalReadUnit(Definition, instrument, local_date)`。
范围身份同时固定 Universe、日历、时区与窗口。不存在 canonical 顶层发布对象。

请求分片按单成员、显式映射有效期和 Source 的日期上限拆分，与发布单元独立。
初始化／周期候选的日期请求可包含闭市日期；`Shard.request_superset` 明确记录请求范围，
不表示这些日期应有行情。单券排队不代表 Provider 多券 API。页起止 offset、页大小、
页数、顺序、一致性和终止证据均冻结；synthetic 的页终止标签不证明真实历史耗尽。

规划汇总请求、连接／探测、页行容量、材料字节、Version／Publication 容量，
检查预算后才返回完整计划，超限拒绝，不截断。并发固定为 1，自动重试／SDK 重拨为 0；
速率、超时、墙钟、响应字节和恢复上限保存在 spec，供未来 D2 执行时强制计量。
本合同不证明这些 Source 能力或容量已获真实资格，也不提供时延可行性承诺。

规范摘要输入采用字段名排序的紧凑 UTF-8 JSON、保序数组、ISO 本地日期与 UTC instant，
拒绝浮点、可变容器和 naive datetime。具有集合语义的成员、映射、日历和路线先排序。
Job 与 Shard 身份包含冻结内容及 occurrence，不使用列表位置。

## Coverage 与候选恢复

`classify_coverage(plan, evidence)` 为每个预期格点生成且只生成一个分类：
`OBSERVED_VALID`、`MARKET_CLOSED`、`NOT_YET_LISTED`、
`DELISTED_OR_TERMINATED`、`NO_TRADE_CONFIRMED`、`COLLECTION_FAILED`、
`MISSING_EXPECTED`、`QUALITY_REJECTED`、`UNKNOWN`。

确定的生命周期边界优先于日历；其后只有合格日历可以解释闭市。
缺日历、缺生命周期、非成员间隔、类别或 Source 资格不足时保持 UNKNOWN。
开放交易日缺行本身也保持 UNKNOWN，不能自动推成停牌或真实缺失。
观测需显式 Material 与原始行定位，零成交的合法观测仍为 OBSERVED_VALID，规划器不合成 OHLC。
无交易需证据和 Source 缺行合同；失败／真实缺失需确认无未决效果及原 Shard 关联。
这些是上游验证证据引用合同，D1 不解析行情或代替质量 Assessment。

额外成员、额外日期及重复证据另列；重复格点标记质量拒绝并保留全部冲突，
不通过丢弃行平衡分母。计数之和始终等于分母，UNKNOWN 不被移除。
manifest 分开呈现成员枚举、生命周期／日历、行情义务、材料读回与正式发布。
本 D1 的后两轴与 global_status 始终 UNKNOWN；行情解释 COMPLETE 不等于正式完成。
新 manifest 可引用同一冻结计划下的前一修订，旧修订保留。

`plan_recovery(manifest, budget)` 只为确认可重试且无材料可重处理的失败／缺失生成
`GapCandidate`；只合并同来源／映射下的相邻缺口，明确列出请求超集（本版等于缺口），
不会跨成功日期。原 Job／Shard、manifest、缺口、Source 与新预算均固定。
质量拒绝或已有材料先列入重处理评估；UNKNOWN 列入原身份核实清单。
异常 manifest 拒绝补采规划。通用 `plan_job()` 不接受 BACKFILL 模式，防止把完整
矩形重新当成缺口执行；候选的接受、Attempt／request 恢复核实与效果计量留给 D2。

## 离线验证

synthetic fixture 位于 `tests/r5_i6_market_fixture.py`，通过现有 qualification
`Fixture`／`Capability` 标记；所有身份、来源、日历及证据均为虚构。
未创建 live observation、运行材料或依赖项变更。

在当前隔离工作树中使用只读已安装环境执行：

```sh
/srv/market-data-foundation/.venv/bin/pytest -q tests/test_r5_i6_market_contracts.py
/srv/market-data-foundation/.venv/bin/python scripts/check_repository_hygiene.py --include-untracked
/srv/market-data-foundation/.venv/bin/ruff check .
/srv/market-data-foundation/.venv/bin/ruff format --check .
/srv/market-data-foundation/.venv/bin/pytest -m 'not live_provider and not postgres_integration'
```

以上自检不代替 AO 独立评审、Owner 集合边界审核及未来真实资格验证。
