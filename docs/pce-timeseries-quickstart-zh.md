# PCE 连续时间序列（DEV）

新接口前缀 `/api/r5/pce/timeseries`，操作与 CPI/PPI 共用：
`GET status/query/api-help/jobs/strategy/schedule/executions`、`POST jobs`、
`PUT strategy`。运行时属性 `pce_timeseries` 独立于旧 PCE exact/history reader；
新库的资产持有证据直接来自连续库，无须先写旧 PCE 数据；连续库为空时保留旧库持有证据。
新接口未启用时明确返回 `503/pce_timeseries_not_enabled`，允许 UI 使用旧 reader。
其他 503、资格或权限失败不能降级；新指数响应与旧百分比响应分别解析。

来源固定为 BEA，包资格为 `macro/pce`，仅加载该包的 `BEA` 绑定。替换凭据失败不回退
BLS 或旧凭据；响应不含 key、原始异常或带 key 请求地址。新查询可按缺口临时透传，
不会持久化；history/as-of 始终只读本地。手动采集与自动采集均需要 Host 开关、
包能力启用和有效 BEA 凭据；自动采集还必须显式启用持久策略。

索引为 NIPA `T20804`、`Frequency=M`：`DPCERG` 总体 line 1、`DPCCRG` 核心
line 25，均为 SA。`basket=headline/core/all`；`adjustment=sa/all`，拒绝 NSA。
`T20807` 官方环比序列不作为指数。`change_1m_ratio/change_3m_ratio/change_6m_ratio/
change_12m_ratio` 使用同 cutoff 的指数端点，结果为累计 ratio，非年化；
`0.0312` 表示 `3.12%`。手工和历史采集包含前 12 月 buffer；发布目标快采只请求当月。
手工来源缺席可完成并明确报告 `source_coverage_partial`；实际返回行未落库或回读丢失失败。
请求范围覆盖与计算 buffer 缺口分别报告，不能用已有缓存冒充本次刷新返回。

初始化默认请求 240 个月，截至北京时间前一个日历月，起止范围可改。
已核实的官方参考月或本地可用数据可提供覆盖锚点；两者均无时，`anchor_basis` 为
`request_calendar_month_no_release_claim`。这只是请求范围，不代表当月已发布或已有
240 月覆盖。来源实际覆盖以返回缺口为准。

Web 启动参数新增 `--enable-pce-timeseries --pce-schedule-path <绝对路径>
--pce-job-directory <绝对路径> --allow-pce-local-read`，可选
`--pce-history-start YYYY-MM`、`--bea-request-control-path <受信绝对路径>` 和
`--allow-pce-collect`。CLI/Web/worker 须共用同一 BEA request control；
`Retry-After` 截止时间持久化，跨实例及重启生效，不占用 BLS 配额。
控制路径由运维统一配置；Web/worker 默认基于受信 config-directory，不受工作目录影响；
CLI 默认位于 `--runtime` 所在目录。PCE schedule、job 路径
必须彼此独立，且不能重叠 CPI/PPI 路径。现有 CPI 参数仍按原脚本提供。
runtime 服务由运维显式开放并设置有界运行窗口；真实开发库初始化与只读 API 验收结果见文末。

Worker 使用 `scripts/mdf_cpi_timeseries_worker.py --asset pce --opt-in-pce-worker`，
提供独立 `--pce-schedule-path/--pce-job-directory` 和原有公共运行参数。
`--allow-collect` 只授权所选 PCE worker，不 tick CPI/PPI。策略初始为关闭；
`PUT strategy {"enabled":true}` 才允许自动采集。没有 worker 时状态为
`not_started`，未收到心跳不会伪装运行；过期心跳显示 `stale`。

官方日历链接已通过公开无 key 页面核对：
[订阅说明](https://www.bea.gov/news/schedule/icalendar)、
[ICS](https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics)、
[完整日历](https://www.bea.gov/news/schedule/full)。默认获取 ICS；也可向
`PceReleaseScheduler(calendar_fetcher=...)` 注入经过审阅的获取器，或显式
`import_calendar(raw, acquired_at)`。默认公共获取器拒绝重定向，单次最多 2 MB，
15 秒超时。网络获取失败进入可见的 unavailable 状态，并有界退避。

只有标题明确的参考月才可能形成目标；同次明确发布两个月时可形成两个目标。
首次启用保留完整日历目录，但自动目标只有未来发布，以及七天内最近一次、经本地回读确认缺口
的发布。已有尝试、运行中或未知的真实任务保留；旧版本未执行的历史队列会迁移收敛。
手工初始化可指定范围；自动目标优先快采当前参考月，不带计算 buffer。
随后历史阶段从配置 `history_start` 起分批核实，附带前 12 月计算 buffer，
不会回放全部旧发布事件。不把发布月减一，
不把 LastRevised、DTSTAMP、response production 或入库时刻当作官方发布时间。
ICS 的 UTC 时间直接保留，Eastern 时间按 America/New_York/DST 转 UTC；
浮动、夏令时歧义或不存在的时间拒绝。北京时间显示由前端处理。
未知月份事件保留在日历，但不采集；取消/移除事件阻止采集；过期或失败的日历不触发采集。

调度独立于 BLS parser、Errata 和配额：发布前 30 秒最多一次小预探，预探成功仍须发布后刷新。
发布后按 15/30/60/120/300/900/1800/3600 秒退避，单阶段最多十次；每事件预留最多 30 HTTP，
滚动 24 小时预留最多 100 HTTP。来源共享的 BEA cooldown/retry_at 优先。
历史阶段每批最多 60 月（额外 12 月 buffer）；尚未到重试时间的任务不会挡住其他到期工作。
自动执行明细保存实际结果、范围、来源计数、回读和错误分类；目标快采需要两条序列真实返回且回读一致。
BEA 官方限制为 100 requests/min、100 MB/min、30 errors/min；来源适配另有独立 BEA 限速。
中断的写入标记 unknown，不自动假定回滚并重放。

真实开发库已初始化 `2006-10..2026-09`：5 次 BEA 请求写入 504 条，
其中双序列各 240 月、共 480 条可见数据，另有 24 条前 12 月计算缓冲。
双序列 1/3/6/12 月 ratio 核验、Garage 去凭据响应投影回读，以及受保护的
CPI/PPI/旧 PCE 数据与原配置前后不变检查均通过。本地查询无来源请求、无数据库写入。
独立来源 probe 另用 1 次 BEA 请求取得 120 points、未写数据库；合计 6 次真实
BEA 数据请求，其中初始化仍为 5 次。

授权的 ASGI 只读 smoke 中，status/query/jobs/strategy/schedule 均返回 200，
来源请求与业务数据库写入均为 0；这是 API 层验收。真实官方日历的 35 events、
36 refs 已导入为 14 targets；官方日历导入时策略为 `enabled=false`，不会自动启用。
最新运行窗口与完整测试状态以当前交接记录为准；
自然发布捕获和浏览器人工验收仍需另行确认。
