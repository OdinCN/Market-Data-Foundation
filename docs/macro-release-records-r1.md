# 五族发布事件、材料和修订读取

发布记录复用五族连续资产工作台、任务、策略和 worker。材料导入不改写连续数值表，
不以最新 API 值、本地 revision=1、数值相等或日历计划证明某次官方 actual。
真实未来自然发布验证仍未完成；历史官方留存、离线合同和 DEV 自动抓取是分别验收的事实。

## 官方材料覆盖

| 族 | 已实现且由留存官方材料检查的统计量 | 明确缺项 |
| --- | --- | --- |
| Employment | NFP 当期月净增、前两月明说的旧值/修订值；U3、参与率、EPOP；私营工时、时薪；材料列出的主要行业月净变动 | Summary B 是月变化表，不能冒称行业岗位水平；未列贸易/运输/公用事业聚合项；不相加子行业制造聚合证据 |
| CPI | 总体/核心/居住/食品/能源/核心商品/服务（不含能源服务）：Table 1 NSA 指数及同比、SA 环比，独立系列/统计量 | 所选 Table 1 未列 SA 指数水平；官方舍入百分数不等于 MDF 用最新指数计算的变化 |
| PPI | Table A 最终需求总体、剔除食品能源及贸易服务的 SA 环比与 NSA 同比；真实修订月份脚注留存 | 最终需求“仅剔除食品能源”核心篮子未在所选材料列出；Table A 近似名称列是 goods，不是该核心篮子；不推断 P/final 状态 |
| PCE价格 | HTML 官方当前 MoM/YoY、当次材料报告的上月当前 MoM；独立上次 HTML 的原官方 actual | HTML 未列指数水平；hist.xlsx 是历史比较，不是 old/new 修订表，未解析它 |
| Retail | 明确发布日期/参考月的销售额（十亿美元换算百万美元）、官方 MPCSM、明确旧/新上月变化 | HTML 未给控制组/除汽车全套原始输入，不能制造事件派生值 |

测试 fixture 是带来源摘要和父材料摘要的聚焦政府材料投影，不是完整 HTTP body，
不能替代 Root `artifacts/ROOT_SOURCE_NOTES.md` 和资格 manifest。
BLS 四个已核验 archive URL 的 DEV 直取仍为 403；本轮不重新探测。
Root 的 Firecrawl 留存投影支持离线/人工导入证据，不表示自动抓取成功。

## 实际读合同

`GET /api/r5/{cpi,ppi,pce,employment,retail}/timeseries/releases`

| 参数 | 含义 |
| --- | --- |
| `event_id` | 精确 canonical 官方事件；BEA PIO 用 `bea:personal-income-and-outlays:YYYY-MM-DD`，不按指标/资产另造事件 |
| `reference_month` | `YYYY-MM`，按事件 COVERAGE 筛选，不能只检查 primary reference_month；BEA 同次可覆盖多月 |
| `as_of` | 含时区的 inclusive MDF 可消费截止；证据、共识、版本关联均受约束，默认当前 |
| `read_mode` | `event`（默认）/`source_history`；后者是具名官方留存档案，不回退 latest |
| `source_version` | 精确 `YYYY-MM-DD` 来源版本日期；仍受 MDF 截止约束 |

GET 不建 schema、不刷新日历、不请求来源、不写任何证据。
返回 `provider_requests=provider_attempts=0`、`persisted=false`、history basis/coverage；
ALFRED 未在本轮接入，不能把当前值伪装成它的 vintage。

事件含全部截止内不可变 `evidence`：原文摘要/位置、留存 SHA256/locator/类型、解析版本、
取得及提交后确认可用时间、source date/precision/basis。`actual_at=null` 仍可表示日期级官方事件，
embargo header 不证明实际传输秒；来源日期保持日期，不补 00:00。
同材料/解析/留存声明幂等，晚导入不倒填。

`claims` 按系列、参考月、统计量、单位、季调、角色合并，后续 partial 不抹掉先前证明范围。
合并后从全部选定claims重新计算`missing_scopes`和`material_state`；每份材料自身的缺项与证据ID仍在`material_deficiencies`/`evidence`保留。
`actual`、`official_previous`、`prior_revised`（上期值〔本次修订后〕）分别保留。
普通冲突保留先前证明值、记录 alternative evidence；显式 `supersedes_evidence` 的解析修正可选择新值，
完整旧材料和选择轨迹仍保留。未解决冲突 actual 不计算 surprise。
`value_identity` 是内容身份，不是经济 revision；相同数值的新事件不新增连续修订。

每个 claim 的 `mdf_first_captured` 和 `mdf_pre_event_last_consumable_previous` 给出本地统计量及
revision/batch/raw/available 引用；派生所有操作数使用同一 cutoff。
日期事件以前一整天的保守界限判断：若发布日期内另有版本而先后未知，“最后前值”为空，
另列 guaranteed previous 及原因，不冒称盘中最后已知值。
无 actual 也不阻断连续数据；连续响应仍可提供本地首次捕获和 related scope 链接。

`continuous_version` 只在明确证明后填写。`bind-version` 要求已存版本、原始 batch digest、
原始数值响应 JSON pointer 指向的官方 release URL 与材料来源完全一致，再核对统计量/值。
一般 BLS API 响应缺该官方版本标识，保持 null；不是通过 equality 绑定。
原始数值响应不再写进 annotation，只保留 digest、定位和版本证明。

## 可选具名共识

`import-consensus` 持久化 event/scope/statistic/unit/adjustment、value、具名来源/方法、
材料 locator/digest、source_available_at 和 frozen_at；MDF obtained/available 是实际导入时间。
read 返回截止内 snapshots，实际 surprise 在每个 actual claim 中。
instant、date、interval 仅在证据界限证明事前可得/冻结时比较；日期不被伪造为发布秒。
共识date证据必须明确`timezone`，缺失返回`release_date_timezone_missing`；不默认纽约时区。
已知官方美国材料使用其明确时区；旧就业档案的具名官方日期依据可在读取时保守归一化，不改原证据或as_of。
actual 确认且同口径共识合格即可计算 actual−consensus，不要求官方原前值。
actual 未确认、范围不符、先后未知等返回 null+原因，不补零。
actual必须明确`confirmation_state=confirmed`；actual/consensus为0及无官方前值均不阻断合格计算。
无已核实的稳定实时共识源；测试 `SYNTHETIC_TEST_SURVEY` 明确仅为合成合同。

## 迁移、人工导入和自动执行

```sh
uv run --locked --no-sync --offline python scripts/macro_release_schema.py --asset employment
uv run --locked --no-sync --offline python scripts/macro_release_records.py \
  --asset cpi --service-factory trusted_local_binding:cpi prepare
uv run --locked --no-sync --offline python scripts/macro_release_records.py \
  --asset cpi --service-factory trusted_local_binding:cpi import-material \
  --file /absolute/artifacts/cpi_10142026_selected_projection.html \
  --source-url https://www.bls.gov/news.release/archives/cpi_10142026.htm \
  --material-format official_selected_projection
```

统一 CLI 支持五族 `schema-sql/prepare/import-material/import-consensus/bind-version/acquire/read`。
来源、读写身份由本地显式 service factory 绑定；无隐式 Provider 替换。
原就业 CLI 的 prepare/import/read 保留，并接受 selected projection。
`acquire` 额外要求 `--live-provider`、selection JSON 和独立 durable state path；普通导入不联网。

迁移只在已有实际 DEV family schema 加 `release_events/release_evidence/release_annotations`，
升级旧事件的 JSON reference_months 并回填原 primary month；不 drop/reset 原就业两个记录，
不改连续 revisions/batches/views。Root 先备份、事务演练及回读原记录时间/digest，再授权只读 SELECT。
回退首选回退应用且保留增量 schema；需要物理回退时先备份 annotation/coverage，
仅撤新增 annotation 表和 reference_months 列，**不删除已有 release_events/release_evidence**。

manual job 与实际 WorkerService 都调用同一个材料适配器；零售自动调度通过真实 host.submit。
一次执行只选范围内最近已到期且参考月有官方映射的事件，不抓 240 个档案。
选择同时读取现有`calendar.display_history`及`calendar.events`（BEA显式reference_months/uid/summary）；不推算month-minus-one。
当前target和calendar取消/移除/改期优先于旧历史；同次BEA多月标题保留同一个官方URL/uid。
官方 allowlist、redirect/size/time 限制、持久化 reservation、成功去重、最多三次尝试和 cooldown 独立生效。
403 至少冷却一天；材料失败不能覆盖成功数值提交或冒称完整事件。
执行 `release_material` 给出 material_outcomes、event/evidence/numeric batch IDs、原因和 transport calls；
numeric provider attempts 与 material transport calls 分列，后者不伪装为含 redirect 的精确物理 HTTP 总数。

资产内发布记录提供北京时间 cutoff、来源日期/覆盖月/事件筛选、请求 URL/cURL、图标复制及实际 HTTP 正文。
表格定位使用表名/行键/列口径，文本定位使用段落跨度；未知source-version basis仍明确未知。
Retail十亿美元换算百万美元时保留`reported_value/reported_unit/normalization`及原显示小数位，不能把745.0十亿美元当成API的745021百万美元或据此绑定版本。
解析版本v3新增这些语义检查/元数据；旧证据不可变。需替换旧解析选择时由操作者明确`supersedes_evidence`，包括同值的元数据修正，不回填旧可用时间。
连续结果仅“口径相关”与已有明确版本证明分开；执行详情可打开事件/证据。
Root 负责真实 PG/权限、DEV 原材料导入、运行进程、实际浏览器及未来自然发布验收；离线测试不代替这些事实。

本次后续修复不增加DDL或依赖；仍沿用上一轮增量迁移与Root显式装配，不修改既有失败任务日志。
如需复核PPI短历史init，Root使用已留存两份SHA匹配响应与已有PG只读证据，不再发20年来源请求。
Root正常环境的重点回归：`tests/test_macro_release_followup.py`（含标准ASGI批次/历史/列投影），
以及`web/src/MacroResearchFields.test.tsx`、`MacroReleaseRecords.test.tsx`、`MacroReleaseLocators.test.tsx`。


查询点的 related_release_events 另带 source_version（value / precision / basis / source）
及 roles（匹配该参考月与口径的 claim 角色，可含 actual、prior_revised、official_previous），
用于区分原公布和后续修订。它们来自已留存事件，不由事件编号或月份偏移推断。
旧 event_id、evidence_ids、association、reason 字段与版本绑定规则不变；新增展示信息不构成版本证明。
零售派生口径的关联仍是月份级材料相关性，不代表网页提供了该派生值。
