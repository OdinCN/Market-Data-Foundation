# 宏观连续数据开发与审查基线

本文是宏观连续数据的共同开发与审查入口；下列五族表保留 CPI、PPI、PCE、美国就业、
美国零售销售的原合同，消费收入、GDP 和 Claims 的差异另见各自说明。
各族来源合同和旧固定快照仍各自有效，不据历史验收推断当前部署状态。
新增一族时先复用以下路径，只增加来源、单位、计算和日历的真实差异。
真实未来发布的自然验证尚未完成，离线测试、策略开启和历史采样均不能替代该验收。

## 连续产品入口与复用位置

以下链接均指实际实现，文件名中的 CPI 不表示仅供 CPI 使用。

| 层次 | 应复用或核对的实际文件 |
| --- | --- |
| 资产路由、五族 profile | [App.tsx](../web/src/App.tsx)、[macroTimeSeriesProfile.ts](../web/src/macroTimeSeriesProfile.ts)、[workflowModel.ts](../web/src/workflowModel.ts)；核对资产导航、刷新、直达链接、历史截止和返回导航 |
| 查询、表格、API 帮助、原始正文 | [CpiTimeSeries.tsx](../web/src/CpiTimeSeries.tsx)、[cpiTimeSeriesApi.ts](../web/src/cpiTimeSeriesApi.ts)、[decimalValue.ts](../web/src/decimalValue.ts)、[retailTimeSeriesAdapter.ts](../web/src/retailTimeSeriesAdapter.ts)；原始正文保留实际 HTTP 字节文本，不能重新 stringify 解析对象冒充 |
| 采集、预置策略、执行简报与详情 | [CollectionWorkbench.tsx](../web/src/CollectionWorkbench.tsx)、[CpiTimeSeriesCollection.tsx](../web/src/CpiTimeSeriesCollection.tsx)、[CpiStrategyWorkbench.tsx](../web/src/CpiStrategyWorkbench.tsx)、[MacroSourceCoverage.tsx](../web/src/MacroSourceCoverage.tsx)、[WorkflowRuntime.tsx](../web/src/WorkflowRuntime.tsx) |
| 后端 profile、Host、读写工厂 | [bls_timeseries_profile.py](../src/market_data_foundation/historical/bls_timeseries_profile.py)、[cpi_timeseries_host.py](../src/market_data_foundation/web/cpi_timeseries_host.py)、[cpi_timeseries_api.py](../src/market_data_foundation/web/cpi_timeseries_api.py)；[PCE](../src/market_data_foundation/web/pce_timeseries.py)、[就业](../src/market_data_foundation/web/employment_timeseries.py)、[零售 Host](../src/market_data_foundation/web/retail_timeseries_host.py) 保留各自适配 |
| 实际查询/帮助 HTTP 合同 | `/api/r5/{cpi,ppi,pce,employment,retail}/timeseries/{query,api-help}`；[零售 router/serializer](../src/market_data_foundation/web/retail_timeseries.py)、[零售 OpenAPI](../web/src/retail.openapi.json)、[公共帮助测试](../tests/test_macro_query_help_contract.py)；以真实序列化响应核对帮助，不能只对 TS 手写 fixture |
| 任务、worker | [cpi_timeseries_jobs.py](../src/market_data_foundation/web/cpi_timeseries_jobs.py)、[dispatch](../src/market_data_foundation/web/cpi_timeseries_dispatch.py)、[Web 启动器](../scripts/mdf_cpi_timeseries_web.py)、[worker 启动器](../scripts/mdf_cpi_timeseries_worker.py)、[服务模板](../scripts/mdf_cpi_timeseries_worker.service.in)；所选 asset 的 opt-in、独立任务/日历路径和能力绑定分别核对 |
| 存储与 bootstrap | [共享 store.ensure_schema](../src/market_data_foundation/historical/cpi_timeseries_store.py)、[PCE store](../src/market_data_foundation/historical/pce_timeseries_store.py)、[零售 schema_sql/store](../src/market_data_foundation/historical/retail_timeseries_store.py)、[就业 prepare-schema](../src/market_data_foundation/historical/employment_timeseries_cli.py)、[就业五项迁移](../scripts/sql/employment-five-series-r1.sql)、[只读审计 SQL](../scripts/sql/employment-five-series-audit.sql)；建库、迁移、权限和应用启动分开，GET 不 bootstrap |

连续入口必须落在上述范围查询和共享工作台。旧 `/api/r5/cpi/query`、旧 PCE exact/history
及其单月页面保留兼容用途，不能成为新族默认入口；PCE 的旧版读取与明确版本链接有专门路由规则，
参见 [PCE 工作流测试](../web/src/PceTimeSeriesWorkflow.test.tsx)。未启用、权限失败、存储失败不允许静默换提供方。

## 五族真实差异表

数量按每参考月计；批次是来源请求边界，不是原子官方 vintage。表内合同依据为代码和仓库既有来源说明，
本轮未由实现CLI联网取样；Root留存新增系列资格见[五族研究字段扩展](macro-research-fields-r1.md)。各族精确历史起点以来源证据为准，预置 240 月不承诺 240 月覆盖。

| 族 / 来源、系列、季调 | 单位与官方 / 派生指标 | 数量、批次与比较缓冲 | 修订与官方日历依据 |
| --- | --- | --- | --- |
| CPI / BLS v2；总体 `CUSR0000SA0` SA、`CUUR0000SA0` NSA；核心 `CUSR0000SA0L1E` SA、`CUUR0000SA0L1E` NSA | 指数；BLS `bls_pct_*` 保留官方百分数。派生 mom/yoy 为 percent，1/3/6/12 月变化为累计 ratio，ROUND_HALF_UP 至 ratio 四位 | 14 序列（总体/核心及五项研究分篮子各SA/NSA）；[BLS source](../src/market_data_foundation/historical/cpi_timeseries_source.py) 注册请求每窗最多20日历年，未注册10年；计算向前12月，跨窗合并后计算 | 季调历史修订、Errata 信号和本地 changed-only 版本分开；BLS ICS 加明确参考月证据，不能按发布月减一。[CPI 合同](macro-publication-history-design.md) |
| PPI / BLS v2；`WPSFD4/WPUFD4` 总量 SA/NSA，`WPSFD49104/WPUFD49104` 核心 SA/NSA | 最终需求指数；核心剔除食品、能源，仍含贸易服务。官方百分数及派生 percent/ratio 同共享数值合同；不拼接旧 SOP | 6 序列（新增剔除食品、能源及贸易服务第三篮子）；同 BLS 有界批次和12月缓冲，不能用 CPI 的历史起点承诺 PPI 覆盖 | 月度初步值与年度季调回修；不靠本地 revision 或时间流逝认定最终值；BLS PPI ICS/参考月证据；未实现 CPI Errata 通道。[PPI 合同及官方引用](ppi-timeseries-quickstart-zh.md) |
| PCE / BEA NIPA `T20804` 月度；`DPCERG` line1 总体、`DPCCRG` line25 核心，均 SA，无 NSA | Fisher price index；官方 `T20807` 的 `DPCERGM/DPCCRGM` 映射为 `bea_pct_1m`，percent。指数派生 mom/yoy percent 与1/3/6/12月 ratio 分别保留，不替代官方值；另有3/6月年化ratio（指数比值4/2次方减1） | 2 指数系列，加2官方环比；[source](../src/market_data_foundation/historical/pce_timeseries_source.py) 每5日历年窗分别取两表，逻辑配对批次含2次 HTTP，重试另计；常规采集前12月，发布目标快采仅当月 | 当前返回可含历史修订，不重建未观察 vintage；BEA Personal Income and Outlays ICS 的明确参考月；不把 LastRevised/DTSTAMP 当发布时间。[PCE 双表合同](pce-timeseries-quickstart-zh.md) |
| 美国就业 / BLS v2，均 SA：`CES0000000001` 非农；`LNS14000000` 失业率；`CES0500000003` 时薪；`LNS11300000` 参与率；`CES0500000002` 周工时 | 依次为千岗位、percent、USD/hour、percent、hours；岗位不是人数。派生分别为月净增千岗位、百分点差、时薪 mom/yoy percent及1/12月 ratio、百分点差、小时差。官方 `net_changes/pct_changes` 与脚注独立保留 | 18 序列（原五项+私营/EPOP/十一主要行业）；共享 BLS 20/10年批次及前12月缓冲；不套 headline/core 或四期限通胀列 | CES 月度/年度基准修订与 CPS 季调规则各自处理；Employment Situation 明确日历绑定，首次本地观察不等于首发。[五项合同](employment-continuous-core-r1.md)、[公共流程](employment-public-flow-r1.md)、[既有官方修订引用](local-first-bls-employment-r1.md) |
| 零售 / Census MARTS；US、`seasonally_adj=yes`、`error_data=no` | SM 为百万当期美元、SA、非年化水平；headline=`44X72`，MDF control=`44X72−441−444−447−722`，含445。1/3/6/12月为 ratio、Decimal精度28 half-even；官方 `44X72 MPCSM` 已是 percent，不能再乘100或以派生值覆盖 | 6原始输入（5个SM加1个MPCSM）→3可选结果（新增除汽车=总额−汽车）；[source](../src/market_data_foundation/historical/census_retail_timeseries_source.py) 每窗≤252月、最多10请求；240目标月加12月正好一窗；先合并全输入再跨窗比较 | 月度及年度基准/季调修订；Advance Monthly Retail Trade 的 Data Month / 年度 Period Covered，不混入 Monthly 表。TBA保持未知。[连续合同](census-retail-timeseries-r1.md)、[计算](../src/market_data_foundation/historical/census_retail_timeseries.py) |

新增篮子、单位、列选择、同截止派生、显式迁移与新增系列补史命令见[研究字段扩展说明](macro-research-fields-r1.md)。

## 一次审查走通的最小路径

1. 先取得授权范围内的真实来源样本及既有官方合同，核对系列、季调、单位、脚注、缺行/null和原始输入数量；
   原始材料、观察和运行证据放 ignored `artifacts/`，只记录安全分类，不记录凭据或原始异常消息。
   合成样本通过不等于来源合格；现有历史采样也不自动覆盖新增系列。
2. 用实际后端 HTTP 正文验证前端解析、表格与“原始响应”，连 API 帮助、请求预览和真实请求一起检查。
   Decimal 字符串、非零科学计数、`0E-9`、长小数、负变化、零与 null 分开；不要先转 JS Number 再宣称精确。
   金额分组/去无意义尾零与 MPCSM 百分数展示是零售规则；就业差值带各自单位，不统一套指数/百分比格式。
   当前共享表格把 ratio 换为百分比并显示两位小数；这只是展示精度，完整 Decimal 值仍查实际 HTTP 正文。
3. 点击资产→连续查询→新建采集：默认240月可编辑，核对锚点理由及实际提交区间；
   在执行简报和详情分别看目标覆盖、前12月缓冲、来源缺失/空值、写入/修订、逻辑请求/HTTP尝试、回读和错误。
   来源覆盖部分缺失可完成且明确带缺口；HTTP 200、任务 complete 都不能单独证明覆盖完整。
4. 预置策略、worker 进程、心跳新鲜度、官方日历、已计划任务和真实执行结果逐项核对。
   策略开关不证明 worker 常驻，心跳不证明采集成功；发布目标与历史核对阶段分别验收。
5. 写入后由授权操作者数据库回读，再用 `odin_dba` 检查 schema USAGE、现有/未来对象 SELECT、会话只读和无写权限。
   bootstrap/迁移须核实实际 schema、owner、既有视图和授权；不能在 GET、前端点击查询或只读身份中顺带建库。
   以上是 DEV 人工核对清单，本轮未执行来源采集、初始化、策略切换、服务重启或数据库访问。

存储 `latest`/rates 视图只表达当前投影；历史查询须先在 `revisions` 为所有分子、分母及零售操作数
选择同一 inclusive `as_of` 的版本再计算，不能只过滤 latest 的分子。
`observed_at` 是获取时间，`available_at` 是提交后确认可消费时间；首次本地 revision 不是官方首发，
晚导入材料不能倒填历史可用性。当前缺口可临时补上游，仅合并本次响应且不写业务数据；
历史/显式截止禁止补上游。零售当前查询还限制未结束月份，不因未来目标而补取过去比较缓冲。
源行缺席、官方 null、抑制值、本地未提供须保留区别，不能补零或插值。

## 日历审查边界

六族同机构官方入口切换、24 小时缓存边界、取消/改期保护和可见刷新诊断见
[六族官方日历容错](macro-calendar-resilience-r1.md)。新增族按相同故障矩阵验收。

历史月份和未来月份都要对照官方证据，不能只核对下一事件。
官方计划时间、当前修订发布时间、获取时间、入库可用时间是不同事实；`scheduled` 不随时钟自动变为已发布。
日历覆盖与数值覆盖分别展示：过去有日历未必有历史数值，未来可有日历而水平为空。
共享 [calendar_publications.py](../src/market_data_foundation/historical/calendar_publications.py)
按证据获取截止过滤；证据晚于 as_of 就不借给早期查询，当前缓存不能还原未保留的旧日历版本。

BLS、BEA、Census parser 各自核对参考月和来源身份，按 America/New_York/DST 或明确UTC解析，
前端统一北京时间。改期、取消、冲突、TBA/未知及缓存过期分别核对，不外推日期、不将短覆盖等同取消。
普通 GET 只读本地日历，不刷新、补历史、建任务或写 state/数据库。
历史维护入口是 [BLS 离线证据导入](../scripts/import_official_calendar_history.py) 与
[零售年度日历导入](../scripts/import_retail_calendar_history.py)，其显式写入/联网操作与查询分离；
本轮不运行这些维护操作。

## 现有测试与可运行检查

共性优先参数化，新增一族只补真实差异；以下是复用位置，不要求再建一套框架。

| 关注点 | 已有测试 / 本轮最小补强 |
| --- | --- |
| 五族日历与截止 | [test_retail_calendar_history.py](../tests/test_retail_calendar_history.py) 已有五族投影 as_of、较晚映射证据、只读边界；[test_calendar_publications.py](../tests/test_calendar_publications.py) 原有四族实际 Host 测试，本轮扩为五族×observed/available，核对获取前1微秒与包含边界、零来源请求和文件不变，均为合成来源 |
| Decimal HTTP→前端 | [test_retail_decimal_fixture.py](../tests/test_retail_decimal_fixture.py) 与 [retail_decimal_response.json](../tests/fixtures/retail_decimal_response.json) 已绑定生产 serializer；本轮在原测试加实际 router/JSONResponse 正文逐字比对，服务输入仍合成。[RetailWorkflow.test.tsx](../web/src/RetailWorkflow.test.tsx) 已用该正文覆盖 parser、共享App、金额/MPCSM/ratio、科学计数、零/null、原始正文，无须重造前端测试 |
| 各族独立 HTTP / 工作流 | [CPI interface integration](../tests/test_cpi_timeseries_interface_integration.py)、[CPI Web](../tests/test_cpi_timeseries_web.py)、[PPI Web](../tests/test_ppi_timeseries_web.py)、[PCE Web](../tests/test_pce_timeseries_web.py)、[就业 Web](../tests/test_employment_web.py)、[零售 backend](../tests/test_retail_timeseries_backend.py)；前端保留 [CPI](../web/src/CpiTimeSeries.test.tsx)、[PPI](../web/src/PpiWorkflow.test.tsx)、[PCE](../web/src/PceTimeSeriesWorkflow.test.tsx)、[就业](../web/src/EmploymentWorkflow.test.tsx)、[零售](../web/src/RetailWorkflow.test.tsx) |
| 同 cutoff 分母 / 跨批次 buffer | [CPI review](../tests/test_cpi_timeseries_review.py)、[CPI events](../tests/test_cpi_timeseries_events.py)、[PCE core](../tests/test_pce_timeseries_core.py)、[PCE percent](../tests/test_pce_percent_persistence.py)、[就业时序](../tests/test_employment_timeseries.py)、[零售计算](../tests/test_census_retail_timeseries.py)、[零售历史存储](../tests/test_retail_timeseries_backend.py) |
| 未来、204/PARTIAL/429、权限与本地故障 | [test_retail_query_partial.py](../tests/test_retail_query_partial.py) 已覆盖未来月、204空正文/异常正文、保留本地数据、权限/本地异常不伪装来源失败；[零售 scheduler](../tests/test_retail_release_scheduler.py) 覆盖429及实际worker入口；[BLS请求控制](../tests/test_cpi_timeseries_request_control.py)、[PCE来源](../tests/test_pce_timeseries_source.py)、[PPI worker](../tests/test_ppi_worker_history.py) 保留各自预算/失败合同 |
| 旧 fixture / 缺 range | [queryTableOrder.integration.test.tsx](../web/src/queryTableOrder.integration.test.tsx) 的 legacy ordinary 响应本来就无 range，复用其真实渲染；[MacroSourceCoverage.test.tsx](../web/src/MacroSourceCoverage.test.tsx) 已覆盖执行缺范围时 unknown，不能据布尔状态捏造覆盖数量。连续查询仍要求返回 start_month/end_month 与请求一致，不为旧 fixture 放宽合同 |

在已准备依赖的 uv/Python **3.14.8**、Node **24** 环境运行；不换系统 Python，`--offline` 限制 uv 下载，
pytest 默认排除 `live_provider` 与 `postgres_integration`。从仓库根执行：

```sh
uv run --locked --no-sync --offline python scripts/check_repository_hygiene.py
uv run --locked --no-sync --offline ruff check .
uv run --locked --no-sync --offline ruff format --check .
uv run --locked --no-sync --offline pytest -q tests/test_calendar_publications.py tests/test_retail_calendar_history.py tests/test_retail_decimal_fixture.py
uv run --locked --no-sync --offline pytest -q
```

在 `web/` 执行：

```sh
node --version
npm run check
npm run contract:check
npm test -- --configLoader runner src/RetailWorkflow.test.tsx src/CpiTimeSeries.test.tsx src/PpiWorkflow.test.tsx src/PceTimeSeriesWorkflow.test.tsx src/EmploymentWorkflow.test.tsx src/queryTableOrder.integration.test.tsx src/MacroSourceCoverage.test.tsx
npm test -- --configLoader runner
```

Vite 使用 `--configLoader runner`，避免配置 bundle 的缓存写入限制。测试若受沙箱、依赖或运行时阻断，
在本轮交接证据记录准确命令、退出结果和未覆盖部分，不更换版本、不绕开失败；
人工 DEV 点击、实际数据库权限与未来自然发布仍按上文分别核对。

## 五族发布事件与修订产品链（round03）

共同入口和完整语义见[发布事件/材料合同](macro-release-records-r1.md)。
五族实际 `/timeseries/releases` 已统一接入资产内列表/详情、来源历史、北京时间 cutoff、
请求预览/原始 HTTP；不是四族 unsupported stub。读取严格本地，无 DDL/upstream。
材料/claim、MDF首次捕获、官方原前值、MDF事前最后可消费值、当次报告修订前值和连续版本绑定分别呈现。
partial 材料按 scope 保留旧证明；冲突和解析修正保留选择轨迹。相同数值的新事件不造经济修订。
BEA PIO 使用 canonical 日期事件和多参考月 coverage，供独立消费收入包复用。

manual/实际 WorkerService/零售自动 host.submit 都有有界材料调用；execution 中单列材料失败/原因及
证据、事件和数值批次。初始化不产生 240 个 archive 请求。BLS 现存 archive 403 不据日历修复推断解决，
人工投影导入不代表自动抓取成功。
共识是可选具名冻结 snapshot persistence/read；无稳定源，不声称真实交付。
允许 date/interval 本身足够证明事前关系；无官方前值不阻断合格 surprise。

留存检查覆盖七 CPI 篮子的 NSA index/YoY 与 SA MoM、PPI 总体/第三篮子的官方率、
PCE 两次 HTML 当前/当次上月率、就业 household/工时/时薪/行业变化及 NFP 两月修订、零售销售额/MPCSM/明确修订。
PPI仅剔食品能源核心、就业 Summary B 行业水平/贸易运输公用事业聚合、PCE HTML 指数水平及零售派生输入
仍分别缺材料，不用近似列或最新数值补证。PPI初步/修订状态只据真实脚注，未知保持未知。
CPI所选NSA行基期有 Table1 明确1982-84依据；不外推原生历史起点或逐项PPI基期。

Root 持有的就业 round01 两个原始 release record 必须 additive upgrade，保留原 evidence/time/digest；
GET 不迁移，旧回退说明不得用于删除这两张已持有材料的表。
ASGI HTTP→UI正常环境、PG集成、实际浏览器、worker自然发布分别验收；本轮沙箱不加入 socket/loop 绕过。

## 独立消费收入增量（round04）

[美国消费与收入完整链与Root运行步骤](bea-consumption-income-r1.md) 新增独立BEA资产，
保留五族现有身份/存储/初始化。PCE明确称价格指数。新增七个原生金额/储蓄率序列，
`UNIT_MULT=6`金额为百万美元，储蓄率0/负值有效；SAAR水平不冒充年化增长，链式成分不相加。
当前临时补缺与历史同cutoff只读、首次捕获、事件/来源历史、手工任务、独立策略/实际worker、
共同采集/执行/查询UI均接共享合同。未把2026留存样本外推成二十年已交付覆盖。
BEA日历/公共原始材料可复用，但consumption独立解析revision/evidence，canonical事件和rawSHA共享，
不重写PCE旧证据和可用时间。新增schema准备/只读授权及Root只读PG验收，真实DEV效果仍由Root执行。


## 真实浏览器复核补充（2026-11-02）

- 查询回归要覆盖正常右侧窗体、展开窗体，以及 800 / 960 / 1440 像素视口。
  不能只检查 document.scrollWidth：月份必须能整行阅读，长表在自己的容器内横向滚动，
  不得将日期、数值和发布记录挤成逐字竖排。检查真实截图及展开发布记录后的布局。
- 状态 GET 遇到明确的 timeseries_busy / HTTP 503 可有界重试三次（200/400/800ms），
  离开页面或切换资产要取消等待；其他错误保持原处理。不得把该重试用于采集 POST 或策略 PUT。
- 零售 latest_months 分别以总体、控制组、除汽车所需的非空原始输入判断；总体存在不代表派生口径完整。
- 消费金额变化率显示百分号；储蓄率水平为百分数，其差值为百分点。原始响应保持原精度与原单位。
- 月份下的发布记录按事件展开，使用后端提供的 source_version 日期/时刻与 claims 的 roles。
  日期级证据不补午夜时间；当期公布与后续修订分别标注。只有 explicit_version_proof 显示版本已证明，
  相关月份/口径不自动证明当前表格数值属于该事件。未知字段兼容旧返回。
- 浏览器回归只读时拦截非 GET/HEAD，分别检查查询表格、原始响应逐字一致、API说明、发布详情、
  初始化表单、策略与执行详情；记录真实的失败及恢复，不把短暂重试的 503 说成从未发生。

## 六族页面一致性补充（2026-11-02）

- 六族 API 说明复用 `CpiQueryApiHelp` 的章节与表格，零售、消费收入通过
  `macroQueryHelp.ts` 提供原生参数、字段类型/空值、单位和示例；不再旁路成简版页面。
  API 默认值和 WebUI 调用策略分别说明，保留真实差异。零售水平/变化是含 value/reason 的对象，
  官方 MPCSM 是 percent；消费金额、储蓄率和百分点变化不套价格指数语义。
- 显示列修改会发送 `columns` 并影响响应投影；核对请求预览、真实 URL、
  `selected_columns` 与表格，不能写成“只改显示、不发送参数”。
- 新建采集与策略把长口径说明折叠；CPI 目标名称不固定为“四项”。初始化显示后端
  `anchor_basis`，区分官方日历计划、本地已确认月份及自然月预置，不暗示所有月份已发布。
  就业、消费收入清空系列时明确提示须选择指标；无显示指标时单独说明。
- 发布记录使用与查询相同的“表格 / 原始响应”、URL 复制及 HTTP 状态/耗时/完成时间；
  包括错误正文逐字保留，不以重新序列化对象代替。取消旧资产请求，避免迟到响应覆盖当前资产。
- 本轮产品修正范围为数据源管理、数据资产及采集任务；运行总览和平台管理按用户要求排除。


## Claims 周频增量

[Claims R1 能力与验证边界](claims-r1.md) 说明全国六项 FRED 连续序列与 DOL 材料的独立语义、
周六参考周、20日历年预置/28天缓冲、官方与本地四周均值、周差/ratio及每系列最新周。
它复用共享查询/采集/策略/执行/发布记录/API帮助，以 /api/r5/claims/timeseries 独立接入；
不把月初、季度、前12月缓冲或240月预设套到周频。本地/历史GET不迁移、不写、不刷新日历；
显式当前补源只临时读源，包能力与控制路径独立。

审查须验证 available 同截止、提交后pending恢复、材料每份身份/claim归属、六精确周本地回读、
精简任务回执，以及日历跨日重试/反复fallback保护/稳定rule_slot；不能只看最新周大于目标周。
Claims 的真实 DEV 采集、暂时进程和 Chromium 1440/960/480 证据与旧七族自然历史查询复核见专属说明。
共享覆盖列表还须检查相邻文字与原生marker、完整日期和表内滚动，不能以document无横向溢出替代。
未来自然发布、旧档案、完整vintage、具名共识及odin_dba独立登录的边界继续明确保留。


## JOLTS 全国非农 SA 增量

[JOLTS 当前说明](jolts-r1.md) 覆盖八项独立数量/官方 Rate、同截止数量差/ratio和比率百分点差、
零/null，以及共享查询/采集/策略/执行/API帮助。历史分段状态另存专属阶段记录。
全国日历须排除 State JOLTS 和 Employment Situation，参考月不按日期猜测。
发布材料在逐项数值的全国/SA/参考期映射尚未合格前只保留事件 scope，不从叙述中生成 actual；
首次本地捕获、计划发布、官方首发和数值事件绑定分别核对。
年度初次和每个已确认发布后的五年维护是 MDF 保守规则，去重、三次预算、UUID恢复、
共享 BLS single-flight/配额/cooldown均须保留；策略 enabled 不代替实际有界进程证据。
5A/5B1真实DEV和浏览器证据可在不受相关改动影响时复用，未完成的全量回归保持 pending。
