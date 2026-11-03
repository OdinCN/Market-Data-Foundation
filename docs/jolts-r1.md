# JOLTS R1：分段交付记录

## 第1段：来源合同与基础适配

本段是全国非农合计、SA 的八项连续序列基础，**不是整族验收通过**。
基线为已合并主线；沿用 `docs/macro-family-development-review.md` 的开发与审查约束，
需求依据是 MBO PRD R1 §10 目标B、附录A01及其语义确认回执。
尚未注册 Web/API、采集任务、策略、worker 或发布记录入口；现有八族默认行为保持原样。

## 原生身份与口径

原生前缀为 `JTS000000000000000`，下表后缀与前缀拼接构成完整 BLS ID。
`L` 与 `R` 是独立来源序列，不从数量或外部 CES 分母重建官方比率。

| 指标 | 数量后缀 / 比率后缀 | 数量合同 | 参考期合同 | 官方比率分母合同 |
| --- | --- | --- | --- | --- |
| 职位空缺 | JOL / JOR | 千岗位 | 当月最后一个营业日存量 | employment + job openings |
| 招聘 | HIL / HIR | 千人次流量 | 月内流量 | employment |
| 主动离职 | QUL / QUR | 千人次流量 | 月内流量 | employment |
| 裁员与解雇 | LDL / LDR | 千人次流量 | 月内流量 | employment |

取得的 [BLS 原生报表](https://data.bls.gov/pdq/SurveyOutputServlet) 已逐项验证八个 ID、
Seasonally adjusted、Total nonfarm、Total US、All areas、All size classes、
Job openings/Hires/Quits/Layoffs and discharges，以及数量 `Level - In Thousands`、比率 `Rate`。
报表通过 POST 合并八个 `series_id`，具体请求及原始正文保留在事务证据中。
[单序列报表](https://data.bls.gov/timeseries/JTS000000000000000JOL) 也取得并核对。

**证明边界**：原生报表只明确千单位与 Rate 标签，未独立说明比率分母和存量/流量定义。
上表的分母、千岗位/千人次细分、百分数及参考期语义是本段明确保留的合同，仍需补齐
[官方定义](https://www.bls.gov/jlt/jltdef.htm)、[FAQ](https://www.bls.gov/jlt/jltfaq.htm) 和
[手册计算章节](https://www.bls.gov/opub/hom/jlt/calculation.htm) 的可读正文核验。
千人次流量不声称去重人数。代码以 `thousand_persons` 保留该流量单位，以 `percent` 表示比率；
4.3 表示 4.3%，不是 0.043 或 4.3 倍。
`native_metadata_status=native_report_verified_definitions_pending`、`source_qualified=false`
显式保留上述缺口，不能由 catalog 核验或数值回读单独解除。

## 真实覆盖与发布材料边界

三次 [BLS v2 API](https://api.bls.gov/publicAPI/v2/timeseries/data/) 请求均合并八项、
启用 catalog/calculations；成功响应中的原生 `seriesID` 与以下数值覆盖已核验：

| 请求窗口 | 每序列实际返回 | 八项月值数 | 解释 |
| --- | --- | --- | --- |
| 2000–2001 | 2000-12 至 2001-12，13个月 | 104 | 起点小窗证据；不外推更早年份或宣称原生目录起点已证实 |
| 2006–2025 | 2006-01 至 2025-12，240个月 | 1920 | 20日历年来源窗口完整；不是 DEV 业务库存量或完整 vintage |
| 2026 | 2026-01 至 2026-08，8个月 | 64 | 本次响应的最新参考月；不推断9月以后尚未发布或已取消 |

API 同时明确报告八项 `Unable to get Catalog Data`，因此用独立原生报表核验身份，
没有用 FRED 别名或搜索摘要代替；本段没有 FRED 请求或跨提供者拼接。
`start_month` 与 profile 的 `earliest_month` 保持 null；`evidence_start_month=2000-12`
只表示样本边界。所有原始正文留事务 `artifacts/segment1`，不进入 Git。

近期八项8月值都有原始 `P/preliminary` 脚注。原脚注和 `net_changes/pct_changes` 原样保留；
`provider_status` 只从 P/R 脚注判断 preliminary/revised，缺脚注返回 unknown，不标作 final。
年度五年修订窗口作为刷新设计约束保留，final 不表示永不修订；具体官方规则正文和
覆盖五年的自动回修策略尚未交付。共享 `update_range` 目前只刷新调用区间及12月比较缓冲。

FAQ、定义、手册、[具名当前发布材料](https://www.bls.gov/news.release/jolts.htm)、
download.bls.gov 和 downloadt.bls.gov 的目录请求均为403；data.bls.gov 的发布路径为301，
本段未跟随该重定向。预算用尽后停止联网，没有取得可绑定日期/参考月的发布正文。
因此没有实际官方发布事件、官方原前值/当次修订前值或来源历史版本证明。

## 已落地的复用与确定性行为

- [合同](../src/market_data_foundation/historical/jolts_contract.py) 定义八个身份、单位、
  参考期、分母与尚未补齐的资格；[共享 profile](../src/market_data_foundation/historical/bls_timeseries_profile.py)
  分配独立 schema/raw namespace/lock，只生成配置，不运行 DDL。
- [基础适配](../src/market_data_foundation/historical/jolts_timeseries.py) 复用 `BlsRangeSource`、
  来源解析、`CpiTimeSeriesService`、共享 store 的 changed-only 版本和 available 截止。
  原生 catalog 校验复用已有 `Material` 表格解析器；缺系列、重复身份、错误单位/地理/季调均拒绝。
- `BlsJoltsRangeSource` 必须显式 `allow_live=True` 才能 fetch；默认沿用既有 BLS 凭据加载和
  跨进程请求控制，默认单次 HTTP 尝试、20秒超时。注册来源按20日历年合并八序列，未注册按10年；
  只有构造不会读取凭据或联网。没有新增账号、依赖或凭据路径。
- `JoltsTimeSeriesService.query_range` 默认本地；显式当前 `allow_upstream=True` 只临时补缺，
  不保存数据，不赋持久 raw locator 或可消费时间。history/as_of 在共享核心内禁止补源，
  所有派生操作数用同一 cutoff；`official` vintage 明确不支持。
- 数量 `value` 保留长 Decimal 字符串；`change_1m_level` 沿用该数量单位，
  `change_{1,12}m_ratio` 为未年化累计比例、四位小数。官方 Rate 的 `value` 独立保存，
  只派生 `change_{1,12}m_percentage_points`，不把 API 的 pct_changes 当成官方 Rate。
  零数量/零比率有效；零增长分母返回 null 和 `zero_denominator`，缺行与原始 `-` 不补零。
- `observed_at` 是实际取得时间；`available_at` 是提交后确认。日历计划、实际发布事件和
  来源历史版本未取得，均不据报表 `Data extracted on` 或本地 revision=1 推断。
  `publication_event/source_version/surprise` 为 null，并提供 surprise 未计算原因。
- [回归](../tests/test_jolts_timeseries.py) 使用合成值验证隔离、精度、脚注、零/null、
  临时补源、changed-only 回读、同截止比较及跨20年窗口；不接触私网数据库。
  既有 qualification capability/fixture/observation v2 合同增加 `macro.timeseries` 和具名八项 fixture，
  未新增资格框架或治理流程。

事务中的离线留存回放以原始2006–2025正文存入内存 store，1920项数值逐项回读相等；
2007–2025目标加2006比较缓冲共1920项，目标1824项的派生无缺口。
该回放的提交确认时间为明确标注的合成时间，只验证 cutoff 机制；不是 DEV 业务库采集验收。

本段相关确定性 pytest 为277项通过，包含JOLTS、qualification、共享来源/查询/关系存储、
就业/PPI/PCE/消费收入及公共API帮助/请求控制回归；有一条既有AnyIO别名弃用警告。
仓库卫生、`ruff check .`、`ruff format --check .`、`git diff --check` 全部通过。
命令与退出记录留事务 `test-exits.json`；未运行浏览器、私网PG或完整全量测试。

## 下一段入口与完整链路未交付项

1. 先复用本段原生报表/API证据，补定义/FAQ/方法及一份具名发布正文；明确月末存量、
   月内流量、官方比率分母和年度五年修订规则。发布材料未取得时保留独立缺口，不伪造事件。
2. 沿八族现有 `macroTimeSeriesProfile`、共享查询组件、`macroQueryHelp`/`CpiQueryApiHelp`
   和 backend Host/router 接独立 JOLTS；API 默认本地与 Web 当前显式临时补源分别说明，
   历史查询本地只读。以真实 HTTP 正文验证 Decimal、列投影、表格和 API 帮助一致。
3. 复用共享采集/任务 journal、策略/周期、实际 worker、执行简报及按系列覆盖/回读，
   实现并核验五年回修窗口；策略启用、进程在线、执行完成、数据可消费分别报告。
4. 在后续明确授权段准备并执行独立 schema/授权、DEV采集与只读回读；接发布记录、
   日历、来源历史及当次修订证明，随后执行浏览器和完整包确定性回归。
   无具名事前共识就保持 surprise 未计算；真实未来自然发布需要后续实际捕获。

本段没有写DEV业务库、执行DDL、切换18761、改变八族策略、重启/延长服务、修改主控handoff或CHECKPOINT，
也没有 push、创建PR、合并或生产部署。完成返回与测试/来源分类见事务 `artifacts/segment1`。


## 第2段：持久化、查询 API 与受控手工采集

第2段基于第1段本地提交 `c14cc3b0db0f80a488b1091da33fd497baabccfd`，
交付独立 DEV 存储和实际 HTTP/采集闭环；仍不表示整族验收或生产部署。
第1段上文的“尚未注册/未入库”描述保留为当段状态，以本节增量为准。

### 实现与操作入口

- [JOLTS 读写工厂](../src/market_data_foundation/historical/jolts_timeseries.py)
  复用共享 PostgreSQL/Garage store；写工厂不自动 bootstrap，来源必须显式
  `allow_live=True`。读工厂使用短连接并设置 `default_transaction_read_only=on`，
  无来源凭据加载、raw 写入或 DDL。共享 BLS 请求合同仍为注册20年/未注册10年窗口，
  本族每范围最多8逻辑请求、默认单次传输、20秒 HTTP 超时。
  默认读补源与采集均复用 `cpi_timeseries.DEFAULT_REQUEST_CONTROL_PATH`，与
  CPI/PPI/Employment 共用同一 BLS 凭据的滚动配额、单飞锁和 cooldown；
  仅任务 journal、策略等族状态独立。测试/受控调用可显式注入 `request_control_path`，
  `state_directory` 不再隐式隔离 BLS 配额，构造工厂本身不读取控制文件。
- [手工 Host](../src/market_data_foundation/web/jolts_timeseries.py) 复用
  `TimeSeriesHost` 的权限、任务 journal、UUID 重放、异步 dispatch 和同截止回读；
  [独立 app 工厂](../src/market_data_foundation/web/jolts_runtime.py) 只装配 JOLTS。
  调用方提供现有 `PackageHost` 以及显式 read/collect/source 开关；默认全部关闭。
  声明 [候选 capability](../src/market_data_foundation/historical/jolts_binding.py)
  不启用它，且 `source_qualified=false`。
- 路径为 `/api/r5/jolts/timeseries/{query,status,api-help,jobs,executions}`。
  GET `/jobs` 读取任务，POST `/jobs` 手工执行；POST 继续使用共享 operator/origin/JSON
  准入合同。API 帮助和 OpenAPI 说明相同默认值；Decimal 值以字符串序列化。
  独立 app 不挂到18761，不启动周期 worker。
- query 必填 `start/end=YYYY-MM`，可选 `series=原生ID,...`、`columns=...`。
  默认全部八序列、`local_only=true`；显式当前 `local_only=false` 只临时补缺。
  history/as_of 始终本地，available 截止包含等号，所有比较操作数用相同 cutoff。
  `measure=index` 沿就业原生值接口命名，不表示价格指数；不支持篮子或季调过滤。
  查询、帮助、状态、任务列表 GET 均不 DDL、不刷新日历、不创建任务或族策略状态文件；
  显式当前补源的 BLS 请求仍须更新共享请求控制。
- POST body 为 `request_id`（规范UUID）、`action`（init/initialize/update）、
  `start`、`end` 及可选 `series`。`init` 仅补缺，`initialize/update` 显式刷新指定范围
  和12月比较缓冲。回执含实际来源计数、saved_points/saved_revisions、按 available
  的 readback 和最终 complete/failed/unknown；相同请求重放不调用来源。
  不推断官方事件，材料回执单独为 `jolts_release_material_pending`。
- 显式 bootstrap 入口为 `uv run python scripts/jolts_timeseries.py --runtime <绝对路径>`
  `--secret <现有秘密文件绝对路径> --state-directory <独立事务目录> prepare-schema`。
  同脚本提供 `status` 和默认本地 `query --start ... --end ...`。
  [只读授权 SQL](../scripts/sql/jolts-readonly-r1.sql) 在 owner 事务内单独执行；
  不修改密码或全局角色属性。为当前 owner 后续创建的本 schema 表/视图默认授予 SELECT；
  其它 owner 创建对象仍须另行核验。

### 已观察的 DEV 最大效果

仅在192.168.10.4既有 DEV 数据库新建 `mdf_jolts_timeseries_dev_r1`。
对象为 `batches`、`revisions`、内部 `latest`、原生值投影 `jolts_latest` 和
`jolts_timeseries_rates`。raw 使用既有配置 Garage 的独立
`jolts-timeseries/dev-r1/sha256/` namespace。业务数值和来源原文不进入 Git。

本段实际新抓取5个 BLS 逻辑请求/5次 HTTP 尝试（上限8/16，超时20秒），均为200。
这5次 DEV 请求使用了临时隔离控制文件；主控复核修复仅使后续默认请求接入共享控制，
没有补填此前计数、清空或访问既有共享配额文件，修复本身新增真实来源请求为0。
未重试第1段403入口，也没有 FRED 请求或提供者替换。先采集2025-07至08历史小窗及
12月缓冲，112个值入库；指定范围重复更新新增点/修订均为0；同 request_id HTTP重放
零来源调用。随后显式临时查询2026-08返回8项，业务库数量仍为112，临时点的
available_at/raw_locator均为空。

在剩余预算内，初始化2006-09至2026-08的240个月目标；加2005-09起比较缓冲，
最终共4个持久批次、2016个版本/最新点（每序列252个月），无合成业务写入。
该窗口按已验证历史终点指定；不声称当前日历月推导的默认240月建议已全部有值。
八序列分别 HTTP 回读各240项；1920个目标原值逐项与本次新取得 BLS 原文一致，
目标区间派生无缺口。latest 的单位、原计算与脚注，以及 rates 的数量差/ratio与
独立官方比率百分点差，各1920项与 API 同截止结果一致。

真实 available 边界：小窗首批提交确认前1微秒返回0项，恰好截止及后1微秒均返回16项；
这些历史读取零来源调用。读连接实际拒绝 CREATE TABLE（ReadOnlySqlTransaction）。
`odin_dba` 的5对象 SELECT、schema USAGE 均通过目录 ACL 检查，CREATE 与全部表写权限
均为 false；默认权限也已核对。未使用该身份独立登录，因此只报告 ACL 检查。

### 验证、证据与后续

[HTTP 回归](../tests/test_jolts_web.py) 使用隔离合成来源验证默认本地/显式临时补缺、
Decimal长小数和列投影、历史同截止、任务重放/读回及 changed-only、权限拒绝与发布/周期
禁用；数量0/null/缺行和合成修订继续由第1段回归覆盖，不写真实业务数据。
相关共享查询、来源、关系存储、就业/消费收入/Claims、catalog/package/API帮助及
qualification 回归与 live DEV 验证分开执行。命令及退出码留事务，不在文档预写测试通过数。

事务 `artifacts/segment2` 保存 bootstrap/ACL、来源计数及原文、真实 HTTP 字节正文、
任务执行和按系列回读、视图校验、qualification v2 observations、测试退出记录与简报。
来源真实取得时间与 MDF 提交后 available 时间不倒填；第1段留存只作合同/证据复核，
本次实际业务入库使用本段新取得响应，不冒称已重建官方首发或历史 vintage。

定义/FAQ/方法正文资格仍缺；具名发布材料和事件归属另缺，source_qualified继续为false。
这两类缺口不会禁用已可追溯的连续序列。下一段从独立全国JOLTS日历、年度五年有界回修、
发布修订证据和周期/真实worker继续；之后接共享UI/API说明、真实浏览器和整包回归。
现有八族运行配置、数据与开关、18761和既有worker、主控handoff/CHECKPOINT均未修改；
本段无push、PR、合并或生产动作。
