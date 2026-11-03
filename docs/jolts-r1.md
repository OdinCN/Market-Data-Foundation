# JOLTS R1 第1段：来源合同与基础适配

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
