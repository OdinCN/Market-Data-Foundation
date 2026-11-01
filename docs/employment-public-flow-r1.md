# 美国就业公共 DEV 流程 R1


当前研究字段扩展及迁移/补史入口见[五族研究字段扩展 R1](macro-research-fields-r1.md)；下文保留此前合同与验收记录。
开发与审查先读 [五族基线指南](macro-family-development-review.md)；本文保留就业公共流程及迁移证据。

连续就业能力通过显式 `EmploymentTimeSeriesHost` 接入 MACRO 公共流程，前缀
`/api/r5/employment/timeseries`。启用 MACRO 的 `employment` 能力、BLS 凭据、
Host 开关、DEV 读取权限及采集权限分别核实。未装配 Host 时返回
`employment_timeseries_not_enabled`，package 的读取/采集资格为 false。
旧 `t03-r1` 定义仍用于资产身份与目录映射；其固定快照、资格断言和
`source_qualified=false` 均未修改。新 status 明示
`host_binding=employment-continuous-dev-r1`，旧快照不构成连续运行证据。

查询按月范围和五系列多选，不使用通胀 basket/adjustment。CES 非农为千岗位及
月净增，CPS 失业率为百分数及百分点差，CES 平均时薪为 USD/hour 及环同比；CPS 劳动参与率为 percent 及百分点差；
CES 私人非农全体雇员平均每周计薪工时为 hours 及小时差。五项均季调。
官方 `provider_calculations.net_changes/pct_changes` 与 `footnotes` 原样保留，
本地计算单列；缺值不补零。月份仅在表格副本中倒序，原始 HTTP 正文不改写。
当前查询本地优先，缺口透传不保存；history 只读本地，默认 available/入库截止。
页面时间为北京时间，官方日历时间不表示本次数据修订当时已发布。

API 说明复用 CPI/PPI/PCE 的权限、参数、统计序列、字段、缺值、示例和失败诊断
折叠框架；请求预览直接使用当前表单的请求。Web `local_only` 默认值为 CPI=true，
PPI/PCE/就业=false，history 始终禁止上游；表单会显式传入该参数。
`as_of_basis`/`vintage_mode` 仅支持 observed/available，同传必须一致。
current 无截止时实际使用 observed（来源响应观察时间）；history 默认 available
（MDF 提交确认可用时间），显式 observed 不证明当时已入库，更不重建官方历史版本。
`availability` 是返回证据，不是查询参数：available 为本地可用，unpersisted
为临时补源；source_unavailable/withdrawn/unknown 进入缺值证据数组。
就业的公共 HTTP 点不返回 `index_value` 或 `batch_id`；返回原生 `value/value_unit`，
本地对比输入使用 `comparison_value`，材料证据为 `raw_sha256/raw_locator`。
顶层不返回 `measure/unit/measure_complete`。来源空值计入记录覆盖，但不计入数值完整，
不补零；派生计算缺少比较月另列 `calculation_missing`。合成示例不作为官方首发证据。

新建采集、执行简报和执行记录复用共享窗体与覆盖说明。初始化默认240个月可编辑；
优先已有可用数据/已到期官方事件，无证据时截至北京时间上一个完整自然月，
只是预置范围。CLI 省略 end 同样使用上一完整月，包含跨年。
周期任务复用一行启停与右侧状态、心跳、日历、月份计划和结果，没有另建就业执行窗体。

就业采用独立 schema `mdf_employment_timeseries_dev_r1`、plan/jobs、策略文件
`employment-strategy.json`、文件锁及既有独立 PostgreSQL advisory lock。
BLS 请求控制仍与 CPI/PPI 共享；保持既有发布快采、请求上限、退避和低优先级历史核实。
当期完成后回查历史并保存前值修订；不增加更激进频率。

官方参考月页固定为 `https://www.bls.gov/schedule/news_release/empSit.htm`。
ICS 仅筛选 Employment Situation；没有明确 Reference Month 证据时只显示未绑定事件，
不从发布日期推算月份，不借 PPI 计划。改期、取消、历史展示沿用共享事件规则。
月历历史接受明确 `Employment Situation for October 2026` 一类文本。
早轮通用 urllib 探测收到 HTTP 403，不能据此判定正式日历 fetcher 失败。
root 曾通过另一检索通道取得官方页面原始 HTML，验证并显式导入14个未来事件及
2026年度10条历史展示事件；状态保留 operator_import 与来源哈希，不代表直连成功。
就业 Host 默认的 ICS 和 empSit.htm fetcher 均通过共享 `_fetch_calendar` 调用
`bls_web_transport`：验证证书和主机名、保留既有 cipher advertisement，实际握手为
TLS1.3 后才发送 HTTP。离线回归覆盖实际 Host 默认装配和 TLS1.2 发 HTTP 前拒绝。
2026-10-31 07:47–07:48Z，root 使用正式传输取得就业 ICS（80672字节，解析23个事件）、
就业参考月页（55395字节，14条）、CPI/PPI参考月页（各55389字节，各14条），全部通过，
并用真实就业 scheduler 完成一次有锁日历刷新。证据为本轮事务中的
`CALENDAR_PRODUCTION_PROBE.json`、`CALENDAR_REFRESH.json`；不是缓存或检索代理成功。
实现检查本身仅执行离线测试，没有额外联网。

## root 的实际 DEV 操作

以下为 root 已验证的操作方式；实际采集通过公共 jobs API 完成。
预览运行目录、Git HEAD 与到期时间以控制目录当前 `RUNTIME_WINDOW.json` 为准。
以下示例不可与现服务并启。此启动器沿用 `192.168.10.20:18761` 与原有访问边界，
仅在 root 安排部署切换、端口可用后启动，不能与现服务并启。不得把测试 shim 带入服务。
`MDF_CONFIG`、`MDF_PACKAGE_STORE`、`MDF_ENV`、PCE 的 owner/OID 使用 root 已核实的
现有路径/身份；程序不改写 runtime/secret。读身份必须与 runtime 的 PG user 一致。

```sh
cd /srv/market-data-foundation-control/worktrees/mdf-employment-five-20261031-r1
uv run --no-sync python scripts/employment_timeseries.py \
  --runtime "$MDF_CONFIG/runtime.toml" --secret "$MDF_CONFIG/secret.toml" prepare-schema
uv run --no-sync python scripts/employment_timeseries.py \
  --runtime "$MDF_CONFIG/runtime.toml" --secret "$MDF_CONFIG/secret.toml" \
  --live-provider init --end 2026-09 --months 240
uv run --no-sync python scripts/employment_timeseries.py \
  --runtime "$MDF_CONFIG/runtime.toml" --secret "$MDF_CONFIG/secret.toml" \
  query --start 2025-01 --end 2026-09 --local-only
```

由 schema owner/DBA 针对新 schema 核实并执行读取授权，保持既有三族权限不变：

```sql
GRANT USAGE ON SCHEMA mdf_employment_timeseries_dev_r1 TO odin_dba;
GRANT SELECT ON ALL TABLES IN SCHEMA mdf_employment_timeseries_dev_r1 TO odin_dba;
```

`prepare-schema` 需要写身份，不能以只读连接执行。后续对象的默认权限由 root 按
实际 schema owner 设置。DEV 已核实5个关系的 SELECT 与 schema USAGE，
无 INSERT/UPDATE/DELETE，并配置了未来表的默认 SELECT 权限。

```sh
uv run --no-sync python scripts/mdf_cpi_timeseries_web.py \
  --opt-in-timeseries-web \
  --config-directory "$MDF_CONFIG" --package-store-directory "$MDF_PACKAGE_STORE" \
  --environment-id "$MDF_ENV" --pg-role "$MDF_PG_ROLE" \
  --pce-schema-owner "$MDF_PCE_OWNER" --pce-schema-oid "$MDF_PCE_OID" \
  --schedule-path "$MDF_STATE/cpi/plan.json" --job-directory "$MDF_STATE/cpi/jobs" \
  --enable-employment-timeseries --allow-employment-local-read --allow-employment-collect \
  --employment-schedule-path "$MDF_STATE/employment/plan.json" \
  --employment-job-directory "$MDF_STATE/employment/jobs" \
  --employment-history-start 2006-10 --allow-package-config-save
```

`MDF_STATE` 必须是 root 选择的绝对运行目录；就业路径不能与三族计划/jobs 相同或互相包含。
需要同时装配三族时保留 root 原有相应 flags 和独立路径，示例只列就业新增参数。
在原接入包管理路径启用 `employment` 并核实 BLS 凭据；配置开关本身不代表来源资格。

```sh
curl 'http://192.168.10.20:18761/api/r5/employment/timeseries/status'
curl 'http://192.168.10.20:18761/api/r5/employment/timeseries/query?start=2025-01&end=2026-09&series=CES0000000001,LNS14000000,CES0500000003,LNS11300000,CES0500000002&query_mode=current&local_only=false&measure=index'
curl 'http://192.168.10.20:18761/api/r5/employment/timeseries/query?start=2025-01&end=2026-09&series=LNS14000000&query_mode=history&local_only=true&as_of_basis=available&as_of=2026-10-31T00%3A00%3A00Z'
```

独立 worker 的前台启动参数如下。实际 worker 已随有期限的 DEV 预览启动；
root 本轮确认保留用户已启用的就业策略，CPI/PPI/PCE原启用状态保持不变：

```sh
uv run --no-sync python scripts/mdf_cpi_timeseries_worker.py \
  --asset employment --opt-in-employment-worker --allow-collect \
  --config-directory "$MDF_CONFIG" --package-store-directory "$MDF_PACKAGE_STORE" \
  --environment-id "$MDF_ENV" --pg-role "$MDF_PG_ROLE" \
  --pce-schema-owner "$MDF_PCE_OWNER" --pce-schema-oid "$MDF_PCE_OID" \
  --schedule-path "$MDF_STATE/cpi/plan.json" --job-directory "$MDF_STATE/cpi/jobs" \
  --employment-schedule-path "$MDF_STATE/employment/plan.json" \
  --employment-job-directory "$MDF_STATE/employment/jobs" \
  --employment-history-start 2006-10 --poll-seconds 5
```

启动 worker 不自动启动策略，需在周期任务行显式启动。GET/status 不刷新官方日历；
启用后的 worker 才执行既有有界刷新和到期检查。已有官方 HTML 的只展示历史导入可用
`scripts/import_official_calendar_history.py --asset employment`，必须指定就业现有 state、
本地 HTML、官方 source URL、start/end 和带时区 acquired-at，不联网、不生成采集任务。

## 2026-10-31 三项基线 DEV 验证记录

公共 jobs API 的初始化范围为2006-10至2026-09，2次BLS请求、2次实际尝试，
保存751条观测（含计算前置月份），新增修订0；独立入库回读通过。
751条中750条有值，2025-10失业率为官方空值，未补零。时薪在2005-10至2006-02
未由来源提供，属于计算前置范围；所选240个月的记录覆盖720条。
PG视图与公共API的2026-09数据及单位一致：非农159044千岗位、月净增29千岗位；
失业率4.2%、月差0.1个百分点；时薪37.81 USD/hour、环比0.13%、同比3.02%。
历史入库截止在本次采集之前返回0条，之后返回3条，两者上游请求均为0。
原三族行数及汇总未改变，原策略启用状态保持；服务与四个worker均有有效心跳。
服务实际返回的前端构建字节与工作树产物一致。

事务证据保存在控制目录 MDF-EMPLOYMENT-WEB-20261031-R1：SCHEMA_PREPARE、
CALENDAR_IMPORT、LIVE_INIT_JOB、LIVE_QUERY、FINAL_VERIFY、RUNTIME_WINDOW。
此前全套pytest本地限时中止；此后基线78e0225的CI104已完整green。
本轮对齐修正的定向离线验证为后端485项、前端245项通过，详见 MDF-EMPLOYMENT-ALIGN-20261031-R1 的
`IMPLEMENTATION_REPLY.md`；不以基线CI代替本轮验证。用户浏览器视觉验收仍待后续，
预览期限不因本轮代码修改延长。

## 五项兼容扩展（2026-10-31）

本节描述五项兼容扩展；实际部署、数据库迁移及补采结果以控制目录
MDF-EMPLOYMENT-FIVE-20261031-R1 的当前运行回执和验证记录为准。
原 T03 `t03-r1` 三项固定快照契约不变；连续 API 使用显式就业 profile，
默认从三项扩至五项，原三项的 series、字段和语义兼容，旧调用显式指定三项即可保持范围。
新增 LNS11300000（CPS 劳动参与率，percent，FRED 别名 CIVPART，仅标识映射）与
CES0500000002（CES 私人非农全体雇员平均每周计薪工时，hours，FRED 别名 AWHAETP）。
只访问 BLS，不回退 FRED；五项进入同一有界年份批次，沿用 Employment Situation
日历、共享预算和重试。状态、策略与默认采集范围自动采用五项。
查询 `series` 为逗号串；CLI 重复 `--series`；就业 jobs 可选 `series` 逗号串，
省略时全部五项。WebUI 查询支持多选；新建采集统一采集全部五项，复用三族的简洁表单。
HTTP 与 CLI 支持明确指定子集，其他族 jobs 格式不变。

劳动参与率 HTTP 使用 `change_1m_percentage_points`，周工时新增 `change_1m_hours`；
仅 AHE 输出 `mom_pct/yoy_pct` 与 1/12 月 ratio。缺比较月仍为 null 并列出缺口；
官方 null、同截止比较和历史仅本地规则沿用。别名不会触发另一个 provider。

新建采集的真实 CollectionWorkbench 渲染分支纳入就业，两个目标下拉均保留目录外的
就业 key；共享确认框、可编辑240月预置、右侧简报和执行记录跳转/轮询沿用。
只允许无 operation 或精确匹配已登记就业来源、绑定和资产的 operation 进入连续入口；
能力、读取、采集和 HTTP origin 权限仍独立核验，不由旧快照授予。

迁移前应核实实际 pg_constraint、视图列序、owner 与 odin_dba 权限。
现有跨列数值约束名为 `revisions_check`，系列约束名为 `revisions_series_id_check`；
迁移按这两个已核实名称执行，未知布局应先检查。
先运行只读审计 `scripts/sql/employment-five-series-audit.sql`，保存事务前后的约束、列序、权限与计数。
迁移文件为 `scripts/sql/employment-five-series-r1.sql`，由
`five_series_migration_sql()` 生成，只扩展 revisions 的两个具名 CHECK 并替换两个业务视图。
不 replay schema/init，不改 batches/revisions/latest 的结构或数据，不改授权。
`employment_latest` 原列顺序/类型保留，增加两个 series 的单位与 CPS 调查映射。
`employment_timeseries_rates` 原 `change_1m_percentage_points` 仍仅失业率；末尾追加
`labor_force_participation_rate_percent`、`participation_change_1m_percentage_points`、
`average_weekly_hours`、`change_1m_hours`。CREATE OR REPLACE 保留 owner/grants。
root 先 BEGIN，执行 SQL 并核查原有数据、列序和只读权限后 ROLLBACK 演练；
确认后再开事务执行并 COMMIT。SQL 不自带提交，重复执行幂等；未知约束命名会拒绝执行。
旧三项 CHECK 不会被 CREATE TABLE IF NOT EXISTS 自动扩展，已有库必须先完成此迁移。
本节迁移不使用以上历史 `prepare-schema` 示例；不要用旧 init/schema 脚本代替。
