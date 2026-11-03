# 美国耐用品与核心资本品 R1

## 当前产品范围

同一资产联合管理耐用品先行报告（Advance，`advm3`）和制造业完整报告（`m3`）。三项均为美国、月频、季调、百万当期美元、名义金额、非年化；订单和出货独立，净订单允许有符号有限数值。零是有效值，缺值不填零。

| 指标 | Census 分类/类型 |
|---|---|
| 耐用品新订单 | MDM / NO |
| 非国防除飞机资本品新订单 | NXA / NO |
| 同口径资本品出货 | NXA / VS |

两个端点独立获取和保存；NO、VS分别请求。`get`包含`data_type_code,time_slot_id,seasonally_adj,category_code,cell_value,error_data`，筛选US、SA=yes、error_data=no。保留已观察slot维度，不将它当发布版本。不回退FRED，不混用两个阶段的值。

## 查询与比较

页面默认`dataset=both`，按同月同指标并排显示两金额、各自1月环比和完整报告减先行报告的差额。比较组数（月×指标）、阶段观测数（两阶段×组数）及缺值数分别统计。可筛选单报告，顶部数量与最新月对应各报告。宽窗可展开查询窗体，窄窗只在表格内横向滚动。

当前查询本地优先，缺口按已验证的最多240个月窗口逐指标临时补源，**不落库**。两报告共享150秒来源预算，单次HTTP最多20秒，无自动重试；页面180秒超时为本地读取、响应序列化留余量。共享来源限流/冷却仍生效。真实上游请求超时与共同截止分别标记；共同截止导致的截断设置`budget_exhausted=true`。一侧失败保留另一侧已有值及完整缺口，未请求不能叫官方缺值。

历史查询严格本地，按同一`observed`或`available`截止选择各阶段版本，页面采用`available`。截止前尚未捕获的阶段保持为空，不从另一阶段借值。GET永不写入。

1月环比为`(本月/上月－1)×100`，同阶段、同截止，Decimal计算、half-even保留6位小数。`include_mom=true`时读取窗口向前扩展一月；缺本月、缺上月或零分母分别返回空值和原因。金额本身不因环比不可算而消失。

当前差额仅为当前来源快照差额；历史差额使用所选截止下已持有快照。当前历史月值、本地首次捕获、revision=1或日历计划时间均不是官方首发/actual证明。没有事件版本证据就不称官方当次修订或surprise；这不阻断连续序列。发布记录页只读取真实已持有材料，缺失时显示空态。

## API

基础路径`/api/r5/m3/timeseries`。旧API省略`dataset`仍默认`m3`，省略`local_only`仍默认`true`；旧族默认不变。

| 参数 | 行为 |
|---|---|
| start/end | 含首尾YYYY-MM，目标窗口最多2400月 |
| dataset | both / m3 / advm3 |
| series | 三个具名指标ID的CSV，默认全部 |
| query_mode | current / history |
| local_only | 当前可显式false允许临时补源；历史始终本地 |
| as_of/as_of_basis | 历史需有时区截止；observed / available |
| include_mom | API默认false；页面true，增加同阶段上月输入 |

```text
GET /api/r5/m3/timeseries/query?start=2025-10&end=2026-09&dataset=both&include_mom=true&query_mode=current&local_only=false
GET /api/r5/m3/timeseries/query?start=2025-10&end=2026-09&dataset=both&include_mom=true&query_mode=history&local_only=true&as_of_basis=available&as_of=2026-11-03T18:28:37Z
```

联合响应`reports.m3/advm3`独立保留来源、单位、参考月、取得和可用时间；`comparisons`包含两金额、各自MoM、差额及版本语义。`coverage`统计阶段观测；`provider_requests`为逻辑请求数、`provider_attempts`为真实HTTP次数（未知则null），失败分类不含Key或原始异常。`budget_exhausted`与阶段失败并列报告。

## 采集、恢复和存储

`POST jobs`使用`request_id/action/start/end`，新初始化/更新覆盖两个阶段各三项；回执早于后台完成。阶段身份持久保存，初始化仅复用范围一致、完整确认且可回读的旧来源分片；更新重新核验。旧单阶段任务、原文和取得/可用时间不改写。`POST jobs/resume`使用原身份和范围继续，完成后同请求重读不另造任务。

正常后台推进完整大窗计划，不把自动核验额度作为产品总限额。逐片完成Garage原文验证、PG提交、实际可用确认和回读后推进。调度分批不是失败；任务完成与数值有值覆盖分开。完整请求后真实缺行可完成但明确缺口；来源失败、准入等待、未请求、存储未知分别表达。确认失败恢复采用实际恢复时间，不倒填、不造经济修订。缺失的已确认存储行不能伪称来源缺值。

仅本包`mdf_m3_capital_goods_dev_r1`；月份date月首，外部YYYY-MM。相同值重放不造经济修订，真实变化按取得顺序保存。原文位于既有Garage，测试/观察及私有运行材料只放ignored artifacts。

## 周期策略

复用共同策略页和持久worker模型，保留Owner现有enabled选择。显式worker逐tick读取启停状态并记录真实心跳。官方来源为Census M3 release_schedule页面；纽约8:30的先行报告与10:00的完整报告分别路由，各自幂等任务、覆盖和执行结果。

自动日历刷新间隔6小时，缓存有效期24小时；失败保留原取得时间和有效缓存，显示降级状态。缓存过期则停止依赖日历的自动来源动作，继续保留连续序列查询；按有界退避重试刷新。HTTP、解析均不持有日历文件锁；拒绝未知结构，不编造日期或actual。

到期任务每tick最多一个阶段，最多三次尝试，最少5分钟冷却并尊重来源更晚retry_at。实际来源缺行终态与失败重试分开；已完成的2026-09两个阶段不重跑。目标之后执行历史维护：如配置范围内每个指标月已有可用、且在计划时间之后捕获的来源快照，则记录真实本地历史核验回执并引用原批次；否则以同阶段独立任务重验配置历史。历史维护也有身份、有限尝试和冷却，不造经济修订或过去自然发布证据。

`GET/PUT strategy`、`GET schedule/jobs/executions`读取真实策略/日历/任务。历史本地核验明确为只读核验，零来源、零观测新增，不冒充新采集。

## DEV验收与边界

正常18761保持配置保存、四来源凭据操作、原九族查询/采集/策略装配；凭据不在Git或响应中。Web与M3 worker均由独立systemd生命周期保证最晚2026-11-04T04:00:00Z（北京时间12:00）停止。

真实两阶段各720条、各717有效，2026-10各三项缺行；原m3工作和时间保留。实际未初始化窗口的六请求临时补源、PG零新增、同截止与MoM、官方日历刷新、心跳和启停分别保留证据。确定性失败/缓存过期/冷却测试与真实DEV浏览器分开；不声称未来自然发布或整包独立验收已完成。

证据目录：`artifacts/m3-segment9-completion/`；先前各段原始观察仍在ignored目录。第8段在收口时被SIGINT，执行退出1；已提交代码及真实结果保留，不改写成正常退出0。主控后续执行全量门禁、PR及CI，本段不push、不合并。
