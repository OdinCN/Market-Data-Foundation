# 美国 GDP 与增长包 R1

## 交付状态与范围

2026-11-02，Owner 在 CI #131 通过后授权分段推进。PR #21 已合并，
本包基于 main 4f9204d。第一段完成季度合同、当前 NIPA 来源适配与定向测试；
第二段已实现季度数据库、严格截止查询、采集回读与独立 CLI，并通过隔离 PostgreSQL 对照。
已完成正式 DEV 独立 schema、只读授权、80 季度初始化与回读；GDP 官方摘要发布证据已新增独立导入与截止读取；季度日历与持久任务核心也已完成；HTTP API、worker 和 WebUI 尚未接入，不能作为整包验收完成。

本包限于下表八项。GDI、GDP 平减指数、GDPNow、初请/续请、JOLTS、
工业生产和中国包不随本笔启用。依据 MBO 宏观研究 PRD R1 与
2026-11-01 语义确认回执，当前序列、首次本地捕获和官方事件实际值分别表达。

| 指标 | 原生系列 | BEA NIPA 表 / 行 | 值单位与语义 |
| --- | --- | --- | --- |
| 实际 GDP | A191RX | T10106 / 1 | 百万 2017 链价美元，SAAR |
| 实际私人国内最终需求 | LB000003 | T10406 / 8 | 百万 2017 链价美元，SAAR |
| 实际 GDP 官方年化季环比 | A191RL | T10101 / 1 | percent，3.4 表示 3.4% |
| 消费贡献 | DPCERY | T10102 / 2 | 对年化 GDP 增长的百分点贡献 |
| 固定投资贡献 | A007RY | T10102 / 8 | 同上 |
| 库存贡献 | A014RY | T10102 / 14 | 同上，可为负 |
| 政府贡献 | A822RY | T10102 / 22 | 同上 |
| 净出口贡献 | A019RY | T10102 / 15 | 同上，可为负 |

四表均于 2026-11-02 在 MDF-DEV 通过有界真实读取：仅请求 2026 年，
4 次逻辑请求 / 4 次尝试，全部 HTTP 200，覆盖 2026Q1..Q3。
解析器离线读回脱敏留存投影，得到八项共 24 条观测。原响应与执行证据在
control/transactions/MDF-GDP-GROWTH-20261102-R1/artifacts/source-qualification，
不进入 Git；CI 仅用明确标识的合成数据，不访问真实来源或私网数据库。

注意表头虽写 billions，API 本次 UNIT_MULT=6，数值实际按百万返回。
不按表头再次缩放；如果基年、乘数或口径变化，拒绝错误解释并要求适配。
原生历史最早季度尚未核验，不把本次样本覆盖推定为历史起点。

## 已落实的来源与季度边界

- API 合同使用 YYYY-Qn；来源 YYYYQn 显式转换，不把季度伪装为月份。
  内部日期键如需使用季度首日，API 仍返回季度，并拒绝月度参数。
- 官方年化季环比直接读 T10101；五项贡献直接读 T10102。
  不用四舍五入后的金额重新计算并冒充官方值，不强行抹平贡献加总的舍入差。
- 私人国内最终需求直接读 T10406，不通过链价分项相减。
  链价水平通常不具可加性。
- 季度值用 Decimal 保留来源精度；percent、percentage_points、SAAR 金额分别标识。
  缺行、官方 null、零和负贡献保持不同含义。
- 复用 BEA 受控 HTTPS 传输、跨进程间隔、429 持久退避和有界重试。
  不调用既有月度 fetch/planner，不切换到其他供应商。
- 每五个年份分窗，每张表分别计入请求预算，最多 48 次逻辑请求。
  每张表独立保留实际 observed_at，不声称多表读取是同一时刻的原子快照。
- BEA Request 可能回显凭据，整块丢弃；仅保存 credential_free_bea_results_projection。
  Results 再做明文与编码凭据检查；错误只带分类、状态和次数。
- 当前 NIPA 数据没有可据以判定 advance/second/third 的版本字段。
  estimate_kind=unknown、event_binding_status=unconfirmed；API 生产时间和
  LastRevised 注记都不替代 MDF 取得时间或官方事件归属证据。

## 后续两段实施

第二段：季度持久化、查询与采集闭环。

- 沿用 batches / revisions / latest 的职责和来源材料引用，使用独立 GDP schema。
  official percent / contribution 是原生数值；不沿用 bls_pct 列名。
- 查询支持最新与严格本地截止读取；所有派生输入使用同一截止。
  原始序列缺口不阻断其他已有值；事件未绑定不阻断连续序列。
- 初始化默认 80 个季度并允许修改；查询比较缓冲、回读覆盖与采集范围分开记录。
  不通过整段重复初始化修补单个故障。
- 新增季度初始化/指定范围更新与真实 DEV 迁移、幂等和回读验证，
  同步核验 odin_dba 当前及未来表的只读权限。
- 复用发布材料、事实和修订记录机制，增加季度参考期与估计批次。
  advance、second、third、后续更新和 MDF 本地 revision 是不同维度。

第三段：官方发布、周期执行、工作台与整包验证。

- 使用 BEA 官方 GDP 日历和具名备用入口，按季度与估计批次建立事件，
  继承缓存时效、取消/改期、冲突、退避和不可执行提示。
  不能直接复用 PCE/消费收入的月度事件筛选。
- 在有界官方档案样本中核实本次实际值、前值及修订值与事件归属。
  缺证据返回原因；无具名同口径发布前共识则不计算 surprise。
  历史材料后补不回填 MDF 取得时间，也不声称消费者当时已可见。
- GDP 查询、API 说明、发布记录、新建采集、周期策略和执行详情使用
  已验收公共组件；新增季度输入器，金额/百分比/百分点列正确格式化。
- 完成真实 DEV HTTP、采集回读、策略后台进程、执行追踪、数据库、
  API 说明与实际响应一致、Playwright 正常/窄窗/展开/响应切换检查后再准备 PR。
  CI 通过不代替浏览器和业务链路验收。
- 白天不持续轮询 CI。每段 15–20 分钟、最多 30 分钟，落盘后回报。
  RDC 2026-11-02 北京时间 16:00–17:00 维护已完成；Owner 明确要求继续，原断点恢复后推进。

## 第二段核心进度（2026-11-02）

- 新增 gdp_store / gdp_timeseries 与 scripts/gdp_timeseries.py；季度 DATE 首日键、
  NUMERIC 原生值、batches / revisions / latest / gdp_timeseries_rates。
- 相同数值不增加本地修订、不重写首次取得时间；表级生产日期变化仅保留新批次材料。
  明确官方 null 与缺行；迟到的不同值拒绝覆盖较新的本地观测。
- 初始化只补缺行，重复完整初始化不请求来源；更新保留改变后的新版本。
  查询临时补源不写库，不替换已有本地值；历史模式必须提供有时区的截止时间，禁止补源。
- available 截止按提交后确认时间读取；若新版本确认结果未知，不退回旧值冒充当前可用。
  显式确认恢复只使用当前时间，不补造过去可用时间。
- 金额的 1/4 季变化由同一截止下的水平值派生为 ratio；官方年化增长和贡献不再派生。
  SQL rates 视图与 API 核心返回一致。公开 HTTP 路由尚未接入。
- 抽出公共 verified_garage_raw，原月度 wrapper 保留参数、命名空间、错误类别和读回协议；
  GDP 使用独立原始材料目录。仅通过精确读回的原始材料才能入库。
- 核验：295 项定向回归、4 项 CLI 默认行为/写入确认检查、1 项真实临时 PostgreSQL
  隔离测试通过；临时 schema 已清理。全量回归和浏览器核验留待后续完整交付。

CLI：先在仓库中运行 uv run python scripts/gdp_timeseries.py --help。
prepare / init / update 必须显式传 --confirm-dev-write；query 默认本地读取，
--allow-upstream 只适用于当前查询。参考期必须为 YYYY-Qn。prepare 同时给予
odin_dba 当前及未来表 SELECT、schema USAGE；不会创建或改动该角色本身。

## 正式 DEV 联调（2026-11-02）

- 独立 schema `mdf_gdp_timeseries_dev_r1` 已建立。`odin_dba` 已核验 schema
  USAGE、当前表/视图 SELECT 和未来表 SELECT；无该 schema CREATE / revisions INSERT 权限。
- 初始化 2006-Q4..2026-Q3，共 80 季度 × 8 指标，目标 640/640 条可读、无缺口。
  比较缓冲从 2005-Q4 开始，另存 32 条；数据库共 672 条观测、20 个批次、0 次数值修订。
- 本轮实际 20 次逻辑请求 / 20 次尝试；凭据安全投影已通过 Garage 精确读回后入库。
  本地取得时间为 2026-11-02T09:39:21Z..09:40:19Z，可用确认时间为 09:40:21Z。
  不将这次历史回补的取得时间改写为过去的官方发布日期。
- 640 条当前查询的数值、季度变化率和单位与 SQL rates 视图逐项一致。
  重复同范围初始化为 0 来源请求、0 新增、0 修订，表计数保持不变。
  截止首次捕获之前返回 0 条可用，截止可用确认之后返回全部 640 条；未来季度不请求来源。
- 连续序列仍为 `estimate_kind=unknown`、`event_binding_status=unconfirmed`。
  官方初值/第二次/第三次估计由独立发布材料证明；数值相等不自动绑定连续序列版本。
- 迁移、初始化与读回回执仅在 control 事务目录 `artifacts/dev-integration`。
  仍未接 HTTP、任务/worker、季度 UI 或浏览器验收，未建 GDP PR，未启动或延长 18761。

## 官方发布摘要证据（第二段 2c，2026-11-02）

- 新增 gdp_release_material / gdp_release_records 与显式 CLI。
  季度、advance/second/third、同季度前一次估计、上季度修订前/后值分别存储，
  与 revisions 的本地修订编号分离；不改写连续值或取得时间。
- BEA 官方 HTML 摘要表逐列核验目标季度、SAAR、Real GDP 行和估计列标题。
  无 id 的多张表使用独立定位，避免年度修订表覆盖当季估计表。
  URL/标题/参考期冲突、重复目标表、未知数值不猜测。
- 官方材料日期保留 date 精度，actual_at 为空；禁用把 embargo 提示当实际发布瞬间。
  来源历史只返回截止 MDF 确认可用时间已经取得的材料，迟到资料不倒填可见性。
  无具名合格共识则 surprise 为空；同一事件证据冲突时 actual 不选最新覆盖旧值。
- DEV 三次有界公开 GET 均 HTTP200：2026Q3 advance、2026Q2 second、2026Q2 third；
  原始 HTTP 正文经 Garage 精确读回，三事件共八项声明独立入库。
  同季度第二次/第三次估计差可核对；第三次材料同时有上季度原值与修订值。
- 新增 release_events / release_evidence，odin_dba SELECT 已验证，不能 INSERT。
  重复导入和严格截止验证通过，原 672 条连续观测逐行保持不变。
- 当前摘要适配覆盖 A191RL；其余七项金额/贡献明确返回 not_covered_by_summary_adapter。
  不跟随新闻页面中会更新的当前 NIPA 链接冒充当次版本。完整档案表、独立后续更新格式、
  具名共识和连续版本强绑定尚未资格核验；缺口不阻断已有连续序列。
- CLI scripts/gdp_release_records.py：prepare/import 需 --confirm-dev-write；
  import 接收已留存官方材料与来源 URL，read 默认仅本地。支持 --reference-quarter、
  --source-version、--as-of 和 --read-mode source_history；不会自动联网或在读取时迁移。
- 本段仍未接 HTTP、任务/worker、季度 UI 或浏览器。后续必须完成完整产品链路再提交验收。

## 季度日历与持久任务核心（第三段 3a，2026-11-02）

- 新增 gdp_calendar / gdp_schedule / gdp_jobs。季度与官方估计名称共同标识任务，
  同季 advance、second、third 各自独立；历史 Initial/Updated 保留原称谓，
  不改称第二次/第三次，不将州级 GDP 混入全国 GDP。
- BEA ICS 主源与官方 full HTML 备用源共做两次有界 GET，均 HTTP200。
  主源解析 35 个全国 GDP 事件，备用 13 个，两者共有的 13 个季度/估计/时间一致。
  其中 14 个未来目标延伸至 2027 年；不得只按本年度截断主日历。
  原始正文与取得时间留在事务目录 artifacts/calendar-qualification。
- 沿用有效缓存 24 小时、失败退避与 Retry-After；429、TLS/证书失败或跳转拒绝
  不触发备用抓取。修正公共 BEA 传输的 TLS 错误分类，避免被宽泛网络错误掩盖。
  备用源只能确认同一季度/估计/时间，不恢复取消事件或静默接受改期冲突；
  不覆盖的未来目标暂停，过期缓存不驱动采集。
- 日历首次导入只创建未来目标，不自动重放历史发布或重复初始化。
  任务到时先更新目标季度，再以 16 季度批次低优先级更新既定历史范围。
  含计算缓冲的一次执行最多预留 8 次来源 HTTP，滚动 24 小时预留上限 80，
  单阶段最多尝试 10 次；实际 BEA 共享请求控制仍独立生效。
- 复用原子状态文件、进程锁、JobJournal 和策略心跳；策略参考期验证增加可覆盖入口，
  原六族仍验证月份，GDP 验证季度。策略启用与真实 worker 心跳分别表达。
- 每次执行先持久化 running，再采集并以 available 本地模式回读；UUID 重复提交幂等，
  同 UUID 不同请求拒绝。中断/写入不确定记 unknown，不自动重放。
  来源未返回目标季度时保留已有数据，同时明确 source_missing 和 partial，
  不把本地旧值当作本次刷新成功。
- 日历触发更新成功只表明采集回读完成，不证明数值属于该官方发布；
  回执仍为 event_binding_status=unconfirmed，独立发布材料继续承担事件值证明。
- 323 项定向回归通过、1 项 opt-in 测试跳过；Ruff、全仓格式与卫生检查通过。
  真实日历仅在事务目录生成停用策略的核验状态，未触发业务采集。
- 当前完成的是日历/调度/任务核心；HTTP 主机接入、真实 GDP worker、季度 UI、
  API 说明、全量回归、浏览器完整链路及 PR 尚待下一工作段。18761 未延长，
  未更改原六族运行策略，不把本段视作整包交付。

## 官方依据

- [BEA GDP 发布补充说明](https://www.bea.gov/news/gdp-release-additional-information)
- [BEA API 文档](https://apps.bea.gov/API/docs/)
- [BEA Web Service API 用户指南](https://apps.bea.gov/api/_pdf/bea_web_service_api_user_guide.pdf)
- [BEA 官方发布日历](https://www.bea.gov/news/schedule)

本文件是本包的简要实现与核验清单，不新增独立审批或回执流程。
