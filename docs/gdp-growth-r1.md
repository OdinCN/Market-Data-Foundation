# 美国 GDP 与增长包 R1

## 当前交付范围

本包在既有六族工作台上增加美国 GDP 与增长，完成季度来源、独立存储、查询、
采集回读、发布材料、日历策略、实际 worker、执行记录及共享 WebUI。
已完成有界 DEV 联调与真实 Playwright/Chromium 核验，进入整包审查及 PR 准备。
开发预览和测试结果不等于生产发布、长期激活或用户最终验收。

| 指标 | 原生系列 | BEA NIPA 表 / 行 | 单位与语义 |
| --- | --- | --- | --- |
| 实际 GDP | A191RX | T10106 / 1 | 百万 2017 链价美元，SAAR |
| 实际私人国内最终需求 | LB000003 | T10406 / 8 | 百万 2017 链价美元，SAAR |
| 实际 GDP 官方年化季环比 | A191RL | T10101 / 1 | percent，3.4 表示 3.4% |
| 消费贡献 | DPCERY | T10102 / 2 | 对年化 GDP 增长的百分点贡献 |
| 固定投资贡献 | A007RY | T10102 / 8 | 同上 |
| 库存贡献 | A014RY | T10102 / 14 | 同上，可为负 |
| 政府贡献 | A822RY | T10102 / 22 | 同上 |
| 净出口贡献 | A019RY | T10102 / 15 | 同上，可为负 |

本笔不包含 GDI、GDP 平减指数、GDPNow、初请/续请、JOLTS、工业生产或中国包。
复用现有 BEA 传输、请求控制、Garage 精确读回、权限与工作台组件；季度合同、
八项指标、GDP 估计材料、按季度/估计的调度和存储独立实现。

## 数据、单位与时间合同

- API 使用 YYYY-Qn，拒绝月份参数；数据库季度键为季度首日 DATE。
- 数值以 NUMERIC/Decimal 保存，API 返回十进制字符串；金额、percent、
  percentage_points 与派生 ratio 分开。当前 BEA API UNIT_MULT=6，金额按百万解释，
  不因网页表头写 billions 再缩放；基年、乘数或口径改变时拒绝错误解释。
- 年化季环比和五项增长贡献读取官方原生系列；不从舍入后的金额重算并冒充官方值。
  私人国内最终需求直接读取 T10406，不以不可加的链价分项相减。
- 只有两个水平系列派生非年化 1 季/4 季变化，使用同一截止的两个端点。
  缺行、官方 null、零、负贡献、未确认可用各自保留原因。
- 当前连续 NIPA 值没有足以判定初值/第二次/第三次估计的版本证据，保持
  estimate_kind=unknown、event_binding_status=unconfirmed；不影响正常连续序列读取。
- observed_at 是 MDF 取得时间，available_at 是提交后确认时间。日期级来源证据
  保持日期级；不把入库时间、API 生产日期或日历计划时间当成官方实际发布时间。
- 同值重复采集不新增数值修订、不改首次取得时间；有效改值保留新版本。
  未确认可用的新版本不允许退回旧值冒充当前可用，显式恢复也不回填过去时间。

## 查询、采集与数据库

独立 schema 为 mdf_gdp_timeseries_dev_r1。batches 保存来源批次，revisions 保存
数值版本，latest 选取当前版本，gdp_timeseries_rates 派生水平变化率；release_events /
release_evidence 独立保存官方材料，不覆盖连续序列。odin_dba 具有 schema USAGE、
当前及未来表 SELECT，无该 schema CREATE 或 revisions INSERT 权限。

HTTP 前缀 `/api/r5/gdp/timeseries`，提供 status、query、api-help、releases、jobs、
executions、strategy、schedule。查询、采集和临时补源分别授权，注册资产不自动启用。

- API 与 GDP WebUI 当前查询默认 local_only=true；只有明确获准的当前查询才能选择
  false 临时补缺，返回值不入库、不替换已有本地值。旧六族默认行为保持不变。
- 历史查询必须提供带时区 as_of，强制本地；as_of_basis 支持 observed/available，
  历史默认 available，不声称这是官方历史版本。所有派生输入使用同一截止。
- 初始化预置 80 个完整季度且可编辑；init 只补缺行，update 刷新指定范围。
  八项输入一起采集，比较缓冲最多前置 4 季，与用户目标范围分开计数。
- 来源按每表五年分窗，最多 48 次逻辑请求。四表分别记录取得时间，不宣称来源原子快照。
  请求控制共享 BEA 间隔、限流与有界重试，不切换供应商。
- 查询的短只读数据库快照与写入互斥分开，临时补源前结束读事务；保留写入互斥和修订保护。
- POST jobs 在持久 running 回执后返回 202；UUID 幂等，同 UUID 不同命令拒绝。
  完成必须通过 available 本地回读；中断或写入结果不确定标 unknown，不自动重放。

CLI：`uv run python scripts/gdp_timeseries.py --help`。prepare/init/update 需显式
`--confirm-dev-write`；query 默认只读，`--allow-upstream` 仅用于当前查询。
配置使用既有 runtime/secret 绑定，不在 Git 中保存凭据或 DEV 数据。

## 官方发布记录与日历

官方发布摘要分别保留当季 advance/second/third、同季度前一次估计、上季度原值及
本次修订值。本地 revision 与官方估计批次不是同一维度。当前适配已资格核验 A191RL，
同事件证据冲突时不挑最新值覆盖；没有具名、同口径、发布前可得共识时 surprise 为空。
历史材料后补可增加事件关联，但不倒填取得时间或消费者当时的可见性。

`gdp_release_records.py` 的 prepare/import 需显式确认，read 不联网或迁移；
支持 reference_quarter、event_id、source_version、as_of 和 event/source_history。
查询中的相关发布入口仅表示参考季度与口径相关，不证明当前连续值属于该次发布。

- GDP 日历用 BEA ICS 主源与官方 HTML 备用源，按季度与估计批次建立独立目标。
  初值、第二次、第三次不能合并成一个季度任务；不混入州级 GDP。
- 缓存 24 小时，保留取得时间、刷新失败、备用来源、取消/改期/冲突和退避信息。
  429、TLS/证书错误及拒绝跳转不绕过；无有效日历时不执行采集。
- 首次导入只生成未来目标，不重放历史发布。发布目标成功后，按 16 季度分批更新
  已设置的历史范围；每次阶段预留最多 8 次请求，滚动 24 小时预留上限 80，单阶段最多 10 次尝试。
- 启动/暂停策略与真实 worker 状态分开。worker 必须显式 `--asset gdp --opt-in-gdp-worker`；
  日历触发采集成功只证明采集回读完成，不自动证明官方事件数值归属。

## 完整链路核验

| 环节 | 已有真实 DEV 证据 | 边界 |
| --- | --- | --- |
| 来源与入库 | 四表有界读取；2006-Q4..2026-Q3 初始化 640/640，含缓冲共 672 条、20 批次 | 未资格核验原生最早历史季度 |
| 查询展示 | 全范围 API/SQL rates 640 条一致；当前、历史截止、原始响应、单位和季度倒序核对 | 本地截止不等于完整官方 vintage |
| 采集回读 | 浏览器提交持久任务、执行详情和 available 回读通过；完整范围重复 init 不请求来源、不新增修订 | 本次浏览器成功任务为幂等采集，不冒充新发布演练 |
| 发布证据 | 三份真实官方材料、三事件、八项声明，原始正文经过 Garage 精确读回 | 摘要仅覆盖 A191RL，其余七项明确缺口 |
| 日历与进程 | ICS/HTML 共有 13 项一致，14 个未来目标；真实 worker 心跳与策略启停核对 | 自然发布到点的实跑尚未发生 |
| API 与 UI | 查询/API说明/发布记录/新建采集/周期/执行使用共享页面；真实 HTTP 响应与说明核对 | CI 不替代浏览器核验 |
| 布局与名称 | Playwright/Chromium 核验 1440、1280、960、480；可换行日历链接、七族美国名称和单击启停 | 不扩展运行总览或平台管理设计 |

确定性测试、明确 opt-in 的临时 PostgreSQL 测试和真实浏览器分别留证。历史整包后端
10,597 项已按原收集顺序分段覆盖，失败修正后定向复验；该证据不是一次连续全量命令退出 0。
后续修正的实际测试结果以本轮日志与 PR 验证说明为准，不以旧数量代替新验证。

证据根目录为 control/transactions/MDF-GDP-GROWTH-20261102-R1/artifacts，包含
source-qualification、dev-integration、release-qualification、calendar-qualification、
http-runtime、ui-query-cli、ui-collection-cli、integration-cli、ui-consistency-fix 和
read-contention-fix。原始材料、真实库响应、截图和运行状态不进入 Git；CI 使用标明的合成数据。
临时服务参数、到期和当前策略状态以 RUNTIME_WINDOW.json 与实际进程为准。

## 尚未交付的来源能力

1. BEA 原生历史最早季度的完整资格核验。
2. 其余七项指标可绑定官方事件的完整历史发布档案表。
3. 独立后续更新格式、完整来源 vintage 及连续序列版本的官方估计强绑定。
4. 具名合格共识及事件 surprise。

以上缺口不阻断八项连续序列和已有 A191RL 发布材料，但不能宣称完整官方 vintage 包。

## 官方依据

- [BEA GDP 发布补充说明](https://www.bea.gov/news/gdp-release-additional-information)
- [BEA API 文档](https://apps.bea.gov/API/docs/)
- [BEA Web Service API 用户指南](https://apps.bea.gov/api/_pdf/bea_web_service_api_user_guide.pdf)
- [BEA 官方发布日历](https://www.bea.gov/news/schedule)
