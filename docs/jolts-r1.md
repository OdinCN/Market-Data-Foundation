# JOLTS R1：全国非农 SA 连续数据与共享工作台

本族提供 BLS 全国非农合计、季调（SA）的四项数量及四项独立官方 Rate，复用宏观查询、
采集、周期、执行追踪和 API 说明。资产为 `mdf.macro.bls.jolts@r1-segment1`，来源为
`mdf.source.bls.jolts@r1`，binding 为 `mdf.binding.bls.jolts@r1`。
开发与审查沿用 [共同基线](macro-family-development-review.md)；各段当时的来源、DEV 和
浏览器事实移至 [阶段历史](jolts-delivery-history-r1.md)，不作为当前功能或在线状态说明。

## 原生身份与数值合同

下列后缀与前缀 `JTS000000000000000` 拼接构成 BLS 原生 ID；地域为 Total US / All areas，
行业为 Total nonfarm，规模为 All size classes，八项均为 SA。

| 指标 | 数量 / 官方 Rate 后缀 | 数量单位与参考期 | 官方 Rate 分母合同 |
| --- | --- | --- | --- |
| 职位空缺 | JOL / JOR | 千岗位；当月最后一个营业日存量 | employment + job openings |
| 招聘 | HIL / HIR | 千人次流量；月内 | employment |
| 主动离职 | QUL / QUR | 千人次流量；月内 | employment |
| 裁员与解雇 | LDL / LDR | 千人次流量；月内 | employment |

Rate 是独立来源序列，4.3 表示 4.3%，不从数量或 CES 分母重建，不以 BLS calculations 的
`pct_changes` 替代。流量不表示跨岗位去重个人，职位空缺存量不可跨月相加作为流量。
数量派生月差保留原单位，月/年累计 ratio 未年化、四位小数；官方 Rate 仅派生月/年端点
百分点差。所有操作数先选择同一 inclusive cutoff 的版本再计算。零数量及零 Rate 有效；
零分母 ratio 返回 null 和 `zero_denominator`，官方 null、本地缺行、撤回分别保留，不补零。
HTTP 使用 Decimal 字符串，表格 ratio 乘100显示百分比；原始响应保留实际 HTTP 字节文本。

## 查询、采集与 API

入口为 `/api/r5/jolts/timeseries/{query,status,api-help,jobs,executions,strategy,schedule,releases}`。
共享页面支持资产直达、可选八项原生序列、月份倒序、列投影、历史截止、原始正文、发布
记录、初始化、周期及真实执行详情；数量、Rate、ratio、百分点分别展示。

query 必填 `start/end=YYYY-MM`，包含首尾，最多2400月；`series` 可为原生 ID CSV，默认
全部八项，不接受篮子/季调过滤。`measure=index` 是接口兼容名称，实际仍为原生数量/Rate。
`columns` 投影同步请求预览、实际 URL、响应和表格。API 默认 `local_only=true`，Web 当前
查询显式 false；history/as_of 一律只读本地，默认 available 截止。当前显式临时补源只合并
本次返回，不写业务库，不赋持久 raw locator 或 available 时间，不换提供者。

`observed_at` 是真实取得时间，`available_at` 是提交后确认可消费时间；迟到材料不倒填两者。
changed-only `revision=1` 是首次本地捕获，不是官方首发。官方 vintage 不支持。
普通 GET 不迁移、不刷新日历、不建任务、不改族策略状态；显式临时补源仍更新共享 BLS 准入。

POST jobs 使用规范 UUID `request_id`、`action=init/initialize/update`、`start/end` 及可选
`series`。init 只补缺；initialize/update 刷新指定区间及前12月比较缓冲；UUID 重放不重复
调用来源。真实 Host 复用权限、journal、dispatch 和 available 回读；执行简报/详情列出
来源缺失/空值、写入/修订、逻辑请求/HTTP尝试、覆盖与回读。complete 可以带来源覆盖缺口，
不能据 HTTP200 或 complete 宣称覆盖完整。自动任务另有发布目标八项数值准入。

初始化默认240月，可编辑，锚点为北京时间上一完整自然月；这只是范围建议，不证明该月
已发布。表单另列本地实际观测/批次和各序列最新月份，不以最新月份推断中间完整。

## 日历、事件与来源资格

全国 JOLTS ICS 过滤 State JOLTS / Employment Situation；参考月只取官方明确映射，不按
发布日期减月猜测。日历时间使用 America/New_York/DST，页面展示北京时间。缓存24小时，
同机构全国年度表为 fallback；改期、取消、冲突、过期及请求失败保留诊断。日历证据按
取得/available cutoff 过滤，较晚获得证据不能借给更早查询。计划时间不自动变成实际发布。

材料解析验证全国发布标题、明确参考月、归档文件日期和正文 embargo 日期一致。
目前只保留事件 scope 和八项 missing_scopes：单个叙述数字的 SA、参考期及全国总量归属
尚无合格表头/单元格映射，不能从文档内任意 SA 短语或 rate 句生成 actual。
NSA、旧月/旧年、行业/地区、修订段或统计表中的数字都不会被叙述适配器升格为当前全国值。
因此尚不提供官方原前值、当次修订前值、精确实际发布时间或连续版本与事件的数值绑定。
没有具名事前共识，surprise 为 null；相关月份只证明 scope，不证明当前数值属于该事件。

已留存的 BLS 原生报表证明八项身份、全国/非农/SA 和 Level in Thousands / Rate 标签；
API 样本证明返回数值和采样覆盖。定义/方法正文及具名发布材料的可读性与语义资格须分别
核实，下载成功本身不解除资格缺口。`source_qualified=false`、
`native_metadata_status=native_report_verified_definitions_pending` 继续有效；不以这些局部
缺口禁用可追溯连续数据。官方起点未证明，`earliest_month/start_month=null`；
`evidence_start_month=2000-12` 只表示已观察样本边界。

官方待核材料：[定义](https://www.bls.gov/jlt/jltdef.htm)、[FAQ](https://www.bls.gov/jlt/jltfaq.htm)、
[方法](https://www.bls.gov/opub/hom/jlt/calculation.htm)、[具名发布入口](https://www.bls.gov/news.release/jolts.htm)。
第5B2本次定义、FAQ、计算方法和2026-09-29具名归档的4逻辑/4HTTP请求均为403，
沿共享准入留存原文；5B1已取得的日历200证据不受此影响。实际失败分类与原文见事务 `artifacts/segment5b2/source-observations.json`；
不据早期403推断今天仍不可读，也不以今天取得材料重写历史取得时间。

## 周期、五年回修与进程

策略文件默认暂停；显式启用与真实 worker 进程、心跳、执行完成和目标可用分别报告。
每个明确映射、已到期的发布任务要求本次八项目标均返回，且 available 回读均为非 null；
已有旧值或比较缓冲不能完成发布，零值可以。参考月＋计划发布时刻去重。

MDF 保守维护规则为每个日历年初次核对一次前五个完整日历年，并在每个数值确认发布后
再安排一次相同五年核对；这不是官方年度基准日期或修订信号。2026年目标2021-01至
2025-12共60月，另读2020全年12月比较缓冲。每 tick 最多一任务，每项最多三次尝试，
等待300/900秒；预留 UUID 在重启后复用，由 journal 判断实际完成/未知，不重置预算。

数值、临时查询、日历和材料复用既有 BLS 跨进程 single-flight、滚动配额及 cooldown。
注册按20日历年、未注册10年分窗；JOLTS 每范围最多8逻辑请求，默认单次传输、HTTP超时20秒。
不清空配额、不通过独立状态目录隔离默认来源准入、不回退另一 provider。

[worker](../scripts/mdf_jolts_timeseries_worker.py) 要求显式 worker/collect opt-in，支持
once、maintenance-only 和默认1–600秒上限，或最长四小时的绝对 deadline；不自行启用
策略、安装服务或监听。`--no-upstream` 在到期工作前停止，并在 fetch/submit 边界拒绝
来源；共享锁避免重复 worker，停止记录和实际 PID/心跳单独呈现。

## 存储、DEV 证据与验收边界

独立 schema `mdf_jolts_timeseries_dev_r1`、raw namespace `jolts-timeseries/dev-r1/sha256/`、
任务/日历/策略路径不混用八族。读工厂设置连接只读；GET 不 bootstrap。
显式 prepare-schema 入口为 [CLI](../scripts/jolts_timeseries.py)，只读授权另见
[SQL](../scripts/sql/jolts-readonly-r1.sql)；凭据、原文及运行状态均不进入 Git。

第2段真实 DEV 已有2016版本（8×252月），2006-09至2026-08共1920目标值逐一匹配来源。
第3段真实五年维护576项匹配（480目标＋96缓冲），第5B1补充修复后有界维护与回读证据。
这些不证明完整官方 vintage、未来自然发布或完整来源资格；odin_dba 为目录 ACL 核验，
未独立登录。

第5A及5B1在实际18761以 Chromium 核验1440/800/480窗口、查询/历史/投影/原始响应、
API说明、采集表单、周期与执行详情；用户已确认页面。复用证据位于事务
`artifacts/segment5a`、`artifacts/segment5b1/parent-fix`。本段材料解析变化不改变页面布局。

18761及 JOLTS 临时 worker 的既定停止截止为2026-11-03T11:24:04Z；该时间只是运行
上限，实时在线仍须检查进程。本段不因CLI退出停止或延期，保留 enabled 和旧八族配置。
此有界零新增上游预算 worker 不等于晚间自然发布已自动验收。

本段完整离线验证的实际命令、状态及独立退出回执见事务 `artifacts/segment5b2/tests.json`。
仍运行或待续检查不记作通过。开发自审、独立专业PASS、CI、用户页面验收、合并、部署
分别成立；本段只提交本地代码和 PR 材料，push/PR/CI判断/条件合并由主控接续。
