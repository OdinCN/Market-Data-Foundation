# 美国初请与续请：R1 第 1–2 段

已提供周频领域、FRED 正式 API 有界采集、PostgreSQL 持久化、同截止查询、
DEV CLI 与 DOL 周报材料及事件回读。HTTP/UI、日历、worker 和完整包验收仍待后续。
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

范围使用包含两端的 ISO 日历日期，输出其中所有周六，按 week-ending 整体倒序，同周按请求系列顺序稳定排列。
`twenty_year_range` 按 20 日历年计算，`buffer_start` 额外提前 28 天；本次目标
2006-11-02 至 2026-11-02 共 1044 个周六，来源读取从 2006-10-05 开始。
没有月初归一、240 周替代或发布日期减固定天数的参考周推算。

## 实现入口和查询语义

- [claims_contract.py](../src/market_data_foundation/historical/claims_contract.py)：六系列身份、日期/周序列和四周缓冲。
- [claims_source.py](../src/market_data_foundation/historical/claims_source.py)：`ClaimsFredSource.fetch` / `metadata`；严格单位/频率映射、分页完整性、周六/排序/重复检查、Decimal 和 `.` 提供方缺值。
- [claims_timeseries.py](../src/market_data_foundation/historical/claims_timeseries.py)：`ClaimsObservation`、`project_range` 和 `ClaimsTimeSeriesService.query_range`。

只读 store 必须提供 `read_snapshot()` 上下文和
`read_range(series_ids, buffer_start, end, cutoff, basis)`，返回 `ClaimsObservation` 历史记录。
要展示首次捕获值，须包含每个键截止时可见的首次及后续版本，不能只传 latest。
`claims_store.py` 已实现该合同：返回可见的全部历史修订，以保留首次捕获。
每次查询仅开启短 REPEATABLE READ READ ONLY 快照，不取得写锁或执行 DDL。

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
缺行 `local_not_provided`、FRED 提供方缺值 `provider_missing` 和数值零分别保留，不插值、不补零。

历史查询始终零上游请求，不支持的 official 截止明确报错。
当前查询显式允许补源时，只补不存在的键，不覆盖本地值或本地提供方缺值；
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

## DOL 周报解析与事件角色

已留存 [2026-10-29 具名官方周报](https://www.dol.gov/sites/dolgov/files/OPA/newsreleases/ui-claims/20261624.pdf)
和 [当前 PDF](https://www.dol.gov/ui/data.pdf?os=v0) 的原始字节。两者属于同一发布周，
不能声称取得两期独立档案。离线 PDF literal-string 部分提取和浏览工具文本互相核对；
前者仅是样本检查工具，第 2 段已改为 pypdf 正式解析和独立文本适配；literal-string 工具不参与产品链路。

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

## 显式存储、采集与 CLI

新增 `claims_store.py`、`claims_collection.py`、`claims_release.py` 和
`scripts/claims_timeseries.py`。Python 仍为 uv 管理的 3.14.8；
pypdf 6.21.4 已用真实 PDF 离线解析和确定性文本合同核验，依赖及 `uv.lock` 一起提交。
pypdf [文本解析接口](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)
支持逐页保留证据；当前适配器限定 regular state programs 的 Number 表和 SA 正文，
不扫描或拼接 thousands、所有福利项目、州或联邦项目表；未知版式明确失败。

固定 DEV schema 为 `mdf_claims_timeseries_dev_r1`，owner 为既有
`market_data_foundation_dev`。只创建/修改 Claims 自己的：
`batches`、`revisions`、`latest`、`claims_weekly_changes`、`release_events`、
`release_evidence`。DDL 只在显式 prepare-schema 中发生，初始化、更新和普通读取不隐式迁移。
prepare 核验 schema/object owner 与 revision 列形状，并给 odin_dba schema USAGE、
现有表/视图和 owner 未来表/视图 SELECT；撤销本 schema 的 PUBLIC/odin_dba 对象写权限。
不调整角色本身、数据库级授权或其他宏观族对象。

数值及缺值状态是经济修订的比较依据。FRED realtime 区间、原数字格式、抓取时间、
请求窗口或原始包变化只形成来源批次，值相等不新增 revision；缺行不会删除已有键。
`.` 只能证明 FRED 返回缺值，不证明 DOL 官方 null。
每批按原始字节重新解析并验证参数后才准入；整个响应组先验证再保存，原始材料复用
VerifiedGarageRawStore 的 GET-before-PUT 和字节回读，前缀为
`macro/claims-timeseries/r1/`。真实来源 raw_sink 在解析前留存。
第一次数据库提交返回后另做 available_at 确认；未确认版本不可消费。
当前存在较新未确认版本时不把旧值冒充当前值；显式重采/重放可在真实重放时刻确认，
即便本次原始包和捕获时刻改变，也不倒填原捕获时间为可消费时间。

```bash
uv run python scripts/claims_timeseries.py prepare-schema --confirm-dev-write
uv run python scripts/claims_timeseries.py init --confirm-dev-write --live-provider
uv run python scripts/claims_timeseries.py update --start 2026-10-24 --end 2026-10-31 --confirm-dev-write --live-provider
uv run python scripts/claims_timeseries.py status
uv run python scripts/claims_timeseries.py query --start 2026-10-10 --end 2026-10-31 --include-releases
uv run python scripts/claims_timeseries.py query --start 2026-10-10 --end 2026-10-31 --as-of 2026-11-02T18:00:00+00:00
uv run python scripts/claims_timeseries.py releases
```

初始化默认截至执行当天、20 个日历年，可用 start/end 编辑；更新默认最近八周并额外读取
28 天比较缓冲。查询、初始化、更新均使用 ISO 自然日边界。初始化仅请求仍缺键的系列，已有本地覆盖时将请求范围缩到剩余缺口；
尾端确无来源行时再次初始化可重新检查缺口，但不会因包或时间变化生成修订。
采集的生产绑定每实例预算 16 个逻辑请求，复用共享节流路径
`/srv/market-data-foundation/config/local/claims-fred-request-control.json`，
每逻辑请求至多三次 transport 尝试；429 留冷却后返回，不换提供者。
普通 CLI query 不绑定真实来源，零网络补源、零业务写入。

材料导入使用 `import-release --confirm-dev-write --material <已留存文件>
--source-url <具名官方URL> --obtained-at <原始获取时刻>`。原 PDF 获取时刻来自第一段
安全请求观察，不被本次导入覆盖；available_at 在本次提交后记录。相同材料/parser 重放幂等，
解析合同升级形成独立 evidence，不制造连续序列经济修订。
`releases` 通过与其他族一致的 event/claims/evidence 角色读取六项实际值；
Python `ClaimsReleaseRecords.read(read_mode="source_history")` 可逐证据读取。
多份具名官方材料冲突时保留 conflict，不任取一个 actual；前值未知与 actual 可用分开。
页码、行片段、所属周表头、具名来源及日期/时刻精度一起保存。
`--include-releases` 只补 scope_only 关联，`continuous_version_bound=false`；
数值相等不强绑定 FRED 历史版本。实际发布时间未知，因此 MDF 事前前值缺少已证实的
事件截止，保持未知；没有具名共识，surprise 也未知。

## 第 2 段实际验证与边界

本段没有重复六项元数据资格核验、DOL 下载或历史档案扫描。
FRED 正式 API 全窗口初始化 6 请求，最小更新 6 请求，共 12 个逻辑请求/12 次 HTTP，
均 200；原始包与安全观察位于 ignored `artifacts/segment2/`。
目标窗口 2006-11-02 至 2026-11-02：6,264 个目标值中有 6,255 个，末端缺 9 个；
含缓冲实际入库 6,279 行。初请三项最新为 10-24、续请三项最新为 10-17。
缺周是各系列来源覆盖状态，不当成整个初始化失败或补零。
相同初始化包离线重放 0 新值/0 新修订；最小真实更新 0 新值/0 新修订。
available_at 全部非空且不早于 observed_at；业务 SQL 视图和 CLI 均真实回读。

DOL 2026-10-29 原始 PDF 已通过实际生产解析器入库；六项 actual、六项本次前期值、
四项明确 SA 原前值均通过 CLI 回读；NSA 两项原前值未知。具名档案原始获取时刻为
2026-11-02T17:56:18.668551+00:00，实际可消费时间保留为本段导入提交之后。
`odin_dba` 的 schema USAGE/无 CREATE、现有六个表/视图 SELECT/无表写权限和
事务回滚探针的未来表 SELECT/无写权限均经真实目录核验。
DEV writer 无 odin_dba 的 SET ROLE 权限；本段没有该账号独立登录凭据，
直接只读登录会话及会话默认只读状态尚未验证，不以目录核验冒称已验证。

确定性测试覆盖来源缺值、全局周倒序、同截止、变更准入、材料角色和冲突；
隔离临时 schema 的 PostgreSQL 测试另需显式 `MDF_CLAIMS_PG_TEST=1`，不进 CI：

```bash
uv run pytest -q tests/test_claims_source.py tests/test_claims_timeseries.py tests/test_claims_storage.py tests/test_claims_release.py
MDF_CLAIMS_PG_TEST=1 uv run pytest -q -m postgres_integration tests/test_claims_postgres_integration.py
```

完整包下一段入口为周频 HTTP/共享 UI/API 帮助、官方日历及节假日例外、worker/任务链路，
并补齐独立只读登录验证、真实宽窄浏览器、全量 deterministic 回归与 PR 准备。
第一段全仓 pytest 的中断结果仍不是通过，本段按授权只跑针对及相邻能力，不提前再跑全仓。
没有生产部署、合并、push、服务重启或其他七族写入。
