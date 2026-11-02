# 美国初请与续请：R1 第 1 段

本段提供周频领域、FRED 正式 API 解析/有界适配、纯查询派生和注入式只读查询服务。
这是完整产品包的基础，尚未接入业务数据库、HTTP/UI、事件材料、日历或 worker。
依据 `docs/macro-family-development-review.md`、GDP 季频/就业实现，以及事务引用的
`MBO_MACRO_RESEARCH_INPUT_PRD_R1_20261101.md` §10 目标 B、附录 A01、§12 和
`MBO_MDF_MACRO_PRD_R1_SEMANTIC_CONFIRMATION_20261101.md`。
后者对首次捕获/官方首发、两类前值、日期级 vintage 的确认沿用。

## 数据合同

| FRED ID | 全国常规州失业保险口径 | 季调 | 统计量 | 原始单位 |
| --- | --- | --- | --- | --- |
| ICSA | 初请 | SA | 周水平 | Number |
| ICNSA | 初请 | NSA | 周水平 | Number |
| CCSA | 续请 / insured unemployment | SA | 周水平 | Number |
| CCNSA | 续请 / insured unemployment | NSA | 周水平 | Number |
| IC4WSA | 初请 | SA | 官方四周均值 | Number |
| CC4WSA | 续请 | SA | 官方四周均值 | Number |

原始机构为 **DOL-ETA**，传输提供者为 **FRED**。ID 是 FRED 原生 ID；不冒称 DOL 表编号。
每条记录独立保留周六 `week_ending`、SA/NSA、单位、原始数字文本和来源日期字段。
计数不表示新增失业人数或跨周去重个人；不包含所有福利项目合计或州级扩展。

范围使用包含两端的 ISO 日历日期，输出其中所有周六，每序列倒序。
`twenty_year_range` 按 20 日历年计算，`buffer_start` 额外提前 28 天；本次目标
2006-11-02 至 2026-11-02 共 1044 个周六，来源读取从 2006-10-05 开始。
没有月初归一、240 周替代或发布日期减固定天数的参考周推算。

## 实现入口和查询语义

- [claims_contract.py](../src/market_data_foundation/historical/claims_contract.py)：六系列身份、日期/周序列和四周缓冲。
- [claims_source.py](../src/market_data_foundation/historical/claims_source.py)：`ClaimsFredSource.fetch` / `metadata`；严格单位/频率映射、分页完整性、周六/排序/重复检查、Decimal 和 `.` 官方 null。
- [claims_timeseries.py](../src/market_data_foundation/historical/claims_timeseries.py)：`ClaimsObservation`、`project_range` 和 `ClaimsTimeSeriesService.query_range`。

只读 store 必须提供 `read_snapshot()` 上下文和
`read_range(series_ids, buffer_start, end, cutoff, basis)`，返回 `ClaimsObservation` 历史记录。
要展示首次捕获值，须包含每个键截止时可见的首次及后续版本，不能只传 latest。
本段没有持久化实现；下一段复用 batches/revisions/available 机制接入。

查询默认 `allow_upstream=False`、`as_of_basis=available`。显式 `as_of` 为包含边界，
先按同一 observed/available 截止选出全部操作数，再计算：

| 字段 | 公式 | 单位 / 缺项 |
| --- | --- | --- |
| weekly_change | `value(t) − value(t−7d)` | 原始 Number；不是百分点或新增失业人数 |
| weekly_change_ratio | `value(t) / value(t−7d) − 1` | ratio；分母零返回 zero_denominator |
| local_four_week_average | `mean(t,t−7d,t−14d,t−21d)` | Number、local_derived，仅四个周水平系列提供 |

使用 Decimal 精度 28、half-even，不按展示精度提前舍入。每个派生保留操作数的值、
参考周、版本、批次、来源和取得/可用时间。IC4WSA/CC4WSA 是独立官方序列，
不能被本地均值覆盖；不为均值序列再计算“均值的四周均值”。
`coverage` 统计目标水平覆盖，不证明全部比较缓冲或所有派生均可用。
缺行 `local_not_provided`、官方 null `official_null` 和数值零分别保留，不插值、不补零。

历史查询始终零上游请求，不支持的 official 截止明确报错。
当前查询显式允许补源时，只补不存在的键，不覆盖本地值或本地官方 null；
补充记录不写库、没有 available_at 或本地 revision，也不伪造首次本地捕获。
尚未结束/未来的参考周不触发补源。来源失败保留本地结果及安全分类；
未绑定 FRED 能力明确报告，不改用其他来源。普通本地存储错误不伪装为来源缺口。

`first_captured_value/at` 是 MDF 截止时可见的首次捕获，独立于当前修订值；
`official_first_release` 保持未知。FRED `realtime_start/end` 保留为日期，
依据为 `fred_not_disambiguated`，不转成午夜或盘中可用时间。
FRED `last_updated` 不是官方发布时间。observed_at 不代替提交后 available_at。
未绑定事件不阻断连续读取；没有具名共识，surprise 为 null 并给出原因。

## 来源请求和真实 DEV 取样

正式入口为 `https://api.stlouisfed.org/fred/series` 及其 `/observations`。
`load_api_key` 复用现有 `config/local/secret.toml` 的只读 `[fred]` 机制。
默认真实执行需要 `live_opt_in=True`；确定性测试注入 transport。
每序列/操作一个逻辑请求，实例累计最多 20，读取六项元数据和六项历史共 12。
响应最多 2 MB，单次 HTTPS 超时 12 秒，不跟随重定向。
使用项目共享三次 transport retry 规则和跨进程请求控制；状态独立命名在
ignored `artifacts/claims/fred-request-control.json`，不消耗 BLS 控制状态。
可注入同一固定路径供所有 Claims 调用方共享；常驻 worker 后续须明确绑定该路径。
短 spacing 最多等一次且不超过 5 秒；429 持久化 Retry-After 后立即退出，不轮询冷却。
原始响应通过 raw_sink 在解析之前留存，解析/留存失败都不重试来源。
凭据不进入请求描述/日志；响应若回显凭据则拒绝留存。

2026-11-02 DEV 六项元数据和六项历史 API 均 200、各 1 次 HTTP。
窗口首条均为 2006-10-07；有值范围内无缺周/null。边界之外的末端缺口不算覆盖完整。

| 系列 | 含缓冲行数 | 最新所属周 | 最新值 |
| --- | --- | --- | --- |
| ICSA | 1047 | 2026-10-24 | 194000 |
| ICNSA | 1047 | 2026-10-24 | 178851 |
| CCSA | 1046 | 2026-10-17 | 1701000 |
| CCNSA | 1046 | 2026-10-17 | 1490071 |
| IC4WSA | 1047 | 2026-10-24 | 196750 |
| CC4WSA | 1046 | 2026-10-17 | 1710000 |

取样证据在当前事务 `artifacts/segment1/sources/`，不进 Git。
`index.json` 记录 capability、有限 fixture、响应观察、参数、时间、SHA256、覆盖和错误分类；
最终请求预算另见 `request-budget.json`。总计保守计入 20 个逻辑请求，包含一次发现搜索、
一次官方 PDF 浏览工具读取、12 次 FRED 和 6 次 DOL HTTP。达到上限后未继续联网。
DOL 直连三次未成功；后续明确使用环境 HTTP transport 保存当前 PDF 和具名档案 PDF，
各 200。历史 10-22 HTML 为 403，原始失败正文已保存。首轮三个失败正文未留存，
其日志只有安全失败分类，HTTP 状态没有记录，不能追认为已取得原文。
没有再次访问已知故障的 `claims.asp`；ETA539 `ar539.csv` 未请求、未接入、无 fallback。

## DOL 周报给下一段的可实现映射

已留存 [2026-10-29 具名官方周报](https://www.dol.gov/sites/dolgov/files/OPA/newsreleases/ui-claims/20261624.pdf)
和 [当前 PDF](https://www.dol.gov/ui/data.pdf?os=v0) 的原始字节。两者属于同一发布周，
不能声称取得两期独立档案。离线 PDF literal-string 部分提取和浏览工具文本互相核对；
前者仅是样本检查工具，尚未交付生产 PDF parser。

报告第 1 页 SA 正文、第 2 页 NSA 正文、第 4 页 regular state programs 表可共同映射：

| ID | 本期 week-ending / actual | 上期 week-ending / 本次报告中的上期值 | 官方此前公布的上期值 |
| --- | --- | --- | --- |
| ICSA | 10-24 / 194000 | 10-17 / 195000 | 194000（正文修订前） |
| ICNSA | 10-24 / 178851 | 10-17 / 173203 | 未证明 |
| CCSA | 10-17 / 1701000 | 10-10 / 1722000 | 1728000（正文修订前） |
| CCNSA | 10-17 / 1490071 | 10-10 / 1476064 | 未证明 |
| IC4WSA | 10-24 / 196750 | 10-17 / 198000 | 197750（正文修订前） |
| CC4WSA | 10-17 / 1710000 | 10-10 / 1709500 | 1711000（正文修订前） |

日期均为 2026 年。第 4 页分别有两组 WEEK ENDING 表头；不能给续请套用初请表头。
SA 正文明确给出的“from ... to ...”支持四项官方原前值；NSA 的 prior 列仅证明
本次报告所载前期值，不能无证据称其发生修订或等同此前首报。
另保留 MDF 事前最后 available 的上期值，不能用上述官方原前值代填。
第 6 页注明 in thousands，若以后解析，必须单独乘 1000 后核对正文，不能直接拼入 Number。
all-programs continued weeks、UCFE/UCX、IUR percent 与当前六项不等价，明确排除。

页首 8:30 A.M. Eastern / 2026-10-29 是 embargo/计划依据，换算 UTC 为 12:30；
本段未证明实际发布时刻，不由 PDF CreationDate、HTTP 时间或 FRED 更新时间推导。
具名材料获取晚于事件，不能倒填 MDF 历史可用性。当前 PDF 是可变入口，须靠原始留存和
具名档案定位后再建立事件材料绑定；数值相同不单独构成 FRED 批次到官方事件的绑定证据。

## 验证与完整包后续

针对测试：`uv run --locked --no-sync --offline pytest -q tests/test_claims_source.py tests/test_claims_timeseries.py`。
运行 hygiene、ruff check/format 和确定性针对/相邻回归；具体观察见事务简报。
真实取样是显式 opt-in 的独立步骤，不并入 deterministic CI；未核验历史全部 vintage。

下一段最小工作：实现周键 batches/revisions 存储与无写查询适配，显式 prepare-schema；
让采集 raw_sink 关联持久化批次，提交后确认 available_at，按值变更落 revision；
接入本段具名样本的事件/actual/两类前值与报告修订角色，保留 unknown 与材料失败。
然后完成显式授权 DEV 迁移/回读、周频 HTTP/共享 UI/API 帮助、发布日历及节假日、
独立任务/worker 控制和真实宽窄窗浏览器验证、完整回归及 PR 准备。
本段未改业务库、迁移、worker、Web 服务或其他族的默认 API；不代表完整包上线验收。

本段最后观察：Claims 针对测试 51 项通过；相邻 GDP/就业组合回归通过；
全仓 ruff/hygiene 通过。扩展全仓 pytest 约 25% 处被中断，未取得全通过结果，
完整包后续必须补做。真实 DOL PDF 两份原始字节均已独立检查到同一事件日期和六项数字。
后续重复真实 FRED 资格核验需同时显式设置 `MDF_CLAIMS_LIVE_OPT_IN=1` 并选择
`pytest -m live_provider tests/test_claims_live.py`；本段预算已用尽，未再执行该命令。
