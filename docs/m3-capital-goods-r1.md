# 美国耐用品与核心资本品 R1：来源适配基础

本段在 PR25 基线内增加独立 Census M3 适配和确定性解析，不注册第十族、不改变既有九族、来源配置、策略或运行服务。消费者语义按 `MBO_MDF_MACRO_PRD_R1_SEMANTIC_CONFIRMATION_20261101.md`；Owner 后续授权已允许本段实现。

| 产品 | Census 候选映射 | 原始口径 | 本段 DEV 证据 |
|---|---|---|---|
| 美国耐用品新订单 | `MDM / NO` | 新订单净额，扣除取消订单 | `advm3` 与 `m3` 各返回 2025-01、2025-02 两行，严格解析通过 |
| 非国防且剔除飞机资本品新订单 | `NXA / NO` | 同范围新订单净额 | 两个数据集的 NXA/NO,VS 小窗组合均返回 204；映射仍是候选，未取得真实数值资格 |
| 同口径资本品出货 | `NXA / VS` | 出货金额，与订单独立 | 同上；未证明 NXA 分类和实际可用组合，不从其他来源拼接 |

三项目标均为美国、月度、名义金额、百万当期美元、季调、非年化水平。订单不是出货，金额不是实际数量指数。净订单可为有符号有限数；真实 0 保留为 0，缺失保持空值，NaN、Infinity、非法数值和未知结构拒绝。环比由同一快照、同一指标的相邻月水平计算百分比；缺输入或分母为 0 时返回具体未计算原因，不填 0。

单位依据为 [Census 历史序列页](https://www.census.gov/manufacturing/m3/historical/timeseries.html)，本段留存该页，明确金额单位为 millions of dollars（IS/US 比率例外，本段不请求）。净订单定义依据为主控本次提供的 [Census Definitions](https://www.census.gov/manufacturing/m3/definitions/index.html)；本 Session 未再次请求此页。MDM/NXA 与 NO/VS 的静态代码线索不能替代逐项实际资格，尤其 NXA 两项仍有真实缺口。FRED `DGORDER / NEWORDER / ANXAVS` 只保留为需求中的具名参考，代码没有 FRED 连接、回退或拼接。

## 来源合同与版本

两个独立端点为 `https://api.census.gov/data/timeseries/eits/advm3`（预报）与 `https://api.census.gov/data/timeseries/eits/m3`（完整报告）。请求固定 `get=data_type_code,time_slot_id,seasonally_adj,category_code,cell_value,error_data`，筛选美国、SA=yes、error_data=no 和明确的分类/类型。每个请求最多两个相邻月份，MDM 只取 NO，NXA 候选取 NO,VS。不发送 `time_slot_id=0` 筛选。

请求依据为主控已读取并提供的官方 [advm3 示例](https://api.census.gov/data/timeseries/eits/advm3/examples.html) 与 [m3 示例](https://api.census.gov/data/timeseries/eits/m3/examples.html)，结合本段实际取得的两个 variables.json。`time_slot_id` 是必需的 get 维度，不是 predicate-only。返回 MDM 表头含四组重复筛选列，逐行值一致；解析允许这些列各一至两次，其他列唯一。冲突、重复单元格、越界月份、NSA、误差数据、错分类/类型/地理、未知列或槽位均拒绝。允许列重排。

已观察的行中 `time` 分别为所请求的 `2025-01 / 2025-02`，作为观测参考月保存；`time_slot_id` 为字符串 `0`，原样保留。目前只接受这一已观察槽位；这是响应识别范围，不是请求过滤或发布版本号。没有返回 `program_code`，不凭空补造。报告阶段来自明确的数据集端点；两份快照分别保留，即使值相等也不合并为同一官方版本。

[官方日历](https://www.census.gov/manufacturing/m3/release_schedule.html) 明确预报 8:30、完整报告 10:00，且日期不同；本段已留存此页，未实现日历调度。历史页明确当前月份值可修订。此时读取的旧月份仍是**当前来源快照**，不是该月原始发布档案，也不是指定事件 actual。

快照保留请求、参考月、槽位、来源单元格指针、原始材料摘要与 MDF 取得时间；来源版本日期、官方发布事件均为空。`qualification=parsed_contract_only_not_event_qualified` 不被 HTTP200、解析成功或本地序号提升。`binding_evidence` 明确区分有界 MDM 真实行证据与 NXA 未核实绑定。事件 actual、原官方前值、MDF 前可消费值和本次修订前值后续分别处理；无事件/具名事前共识证据不计算 surprise。日期级证据不补成盘中秒级时间。

## 实现、复用与核验

`historical/m3_capital_goods.py` 提供有界请求、严格解析、缺值投影、快照与同快照环比。`historical/m3_capital_goods_source.py` 提供默认关闭的候选来源、单次 HTTPS 交换与宏观资格观察。有效私有凭据通过调用方注入的既有 loader 加载，不增设凭据发现或配置回退。请求控制注入既有 Census 使用的 `CpiRequestControl`；本次只读核验使用 segment1 下独立记账状态，不改共享配置。

复用现有 `HttpResult`、严格 HTTP 头与报文边界校验、来源异常分类，以及 `MacroCapability.TIMESERIES / Fixture / Binding / Evidence / observe` 合同。新传输只允许 advm3/m3 两个精确端点，不放宽锁定 MARTS 的既有传输。每次至多一次交换、20 秒超时、正文上限 1 MiB，无自动重试、重定向或切源。真实 raw/观察只落忽略的 artifacts，Git fixture 使用明确标注的合成数值；NXA 合成测试不冒充真实资格。

本段累计 **16 次真实 HTTP，其中 9 次数值请求**。初始 12 次与纠正后 4 次均保留：

- 初始：6 次元数据 200；1 次猜测文档路径 404；4 次缺少 time_slot_id 的数值请求 400；1 次带未经核实 slot0 的有限组合 204。最后的 204 不证明数据集无值。最初四次数值失败使用既有 legacy loader；修正后的请求使用当前 mdf-dev/macro 保存的 Census 凭据；不将不同凭据的尝试合并为认证结论。
- 纠正后：advm3/MDM 与 m3/MDM 各 200，两行；advm3/NXA 与 m3/NXA 各 204。每次请求均限定 2025-01 至 2025-02，无重试。初始日志留存副本，累计日志追加，不删除失败证据。

审查/测试与来源材料在工作树 `artifacts/segment1/`，包括 `source-observations.json`、初始日志副本、raw、`review.md`、`tests.json` 和逐用例终态。确定性回归覆盖净订单符号、零/缺值、歧义维度、错口径、异常结构、无重试/切源、凭据回显防护、资格 schema、旧 MARTS 边界与九族既有合同。

## 剩余工作段

1. 在另行有界的来源核验中解决 NXA 分类/NO/VS 的实际可用请求，取得两项真实行和代码定义证据。保留本段 204 的局部缺口；不得先宣告三项完整就绪。
2. 按已核实能力接入现有存储与查询，验证采集后回读；补真实发布材料和事件/修订证据，独立保留预报与完整报告版本。
3. 实现整包页面、API 说明及实际响应一致性、策略进程和执行追踪，使用 MDF-DEV Playwright + Chromium 验收宽窄窗。运行联调、真实业务库写入、能力/策略启停、服务启动和部署不在本段。
4. 耐用品包后续收口后按既有授权推进工业产出与产能利用率；本段没有新增 G.17 或共享日历实现。
