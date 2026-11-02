# 美国初请与续请：R1 第 1–4 段

已提供周频领域、FRED 正式 API 有界采集、PostgreSQL 持久化、同截止查询、
DEV CLI、DOL 周报材料及事件回读，以及独立 HTTP、官方日历和统一 worker 入口。
第4段已接入统一 UI 并核验前端/HTTP合同；真实到期 worker 联调、浏览器及完整包验收仍待后续。
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
具名材料获取晚于事件，不能倒填 MDF 历史可用性。当前 PDF 是可变入口；第 3 段允许原始不可变 bytes 与报告 header 建立
`official_current_report_snapshot` 事件证据，具名档案另标 `official_archive`，不伪造档案链接。
数值相同不单独构成 FRED 批次到官方事件的绑定证据。

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


## 第 3 段：HTTP、官方日历及任务进程

独立 prefix 为 `/api/r5/claims/timeseries`。复用 GDP 的严格路由、operator/Origin
边界、包权限、短只读连接、JobJournal、dispatch 和既有策略/心跳原语；GDP 默认保持。
Catalog 注册资产 `mdf.macro.dol.claims`，provider 为 FRED，原机构为 DOL-ETA。
绑定声明直接描述六条原生 FRED 观测操作与周频 Number/SA/NSA 合同，没有复制 BEA source。
默认不启用 Claims runtime、来源查询或采集，也不改已有包的 enabled 列表。

| HTTP 方法 / 路径后缀 | 合同 |
| --- | --- |
| GET status / api-help | 实际本地状态、20 日历年可编辑预设、权限与字段说明 |
| GET query | 必填 start/end 自然日；可选 series/local_only/query_mode/as_of/as_of_basis |
| GET releases | 可选 as_of、read_mode=event/source_history、event_id、reference_week、source_version；严格相交过滤，六实际与证据角色 |
| GET jobs / executions | 持久化回执；中断显示 unknown，不在 GET 中修复或重放 |
| GET strategy / schedule | 持久策略、日历缓存/失败、计划和独立进程心跳 |
| POST jobs | 精确四键 request_id/action/start/end；UUID；action=init/update；六系列 |
| PUT strategy | enabled 布尔；可选 history_start 自然日；一键启停 |

重复/未知/非法参数和重复 JSON key 均拒绝。query 默认 local_only=true、available 截止；
显式 false 仅对当前查询临时补缺，不写业务库。历史始终零来源，同截止后再派生周差、
比例和本地四周均值。帮助与 OpenAPI 参数一致，不接受月份或季度边界。
GET 不创建 schema、不刷新日历、不推进任务、不取得采集 advisory 写锁、不启动常驻进程。
普通读取使用 runtime 既有默认 READ ONLY 连接及短 REPEATABLE READ READ ONLY 快照。

POST 在 durable running 回执写妥后返回 202，随后回读 jobs/executions。
六系列采集后重新本地读取，校验键集合、值/版本/batch/取得和可用时间指纹及覆盖，
允许明确的各系列末端来源缺周。材料取得失败单独呈现，不抹去已提交的连续值。
不凭相同数值把 DOL actual 绑定到 FRED 版本。阶段成功要求材料事件身份匹配，且
报告独立给出的六个精确 `(series_id, week_ending)` 都已通过本地 available 回读，
具有非 null 的可消费数值（零有效）。内部缺周、provider_missing 或仅有更晚一周都不算就绪；
不要求把续请缺一周补成初请的周，也不比较 DOL/FRED 数值来绑定版本。
同 request_id 幂等，冲突 409；执行中断保持 unknown，不静默重放。

jobs/executions 使用精简回执：saved_points/saved_revisions、status、coverage、
series_coverage、provider_requests/provider_attempts、start_date/end_date、readback_digest、
readback、material、event_binding_status 和 completion。数千行观测及派生溯源通过 query 获取。
各系列 missing_weeks 和 trailing_missing_weeks 分别最多保留 16 个样本，
对应 `*_total` 为真实未截断总数，`*_truncated` 明示截断；coverage 总量不截断。
readback 保留 verified/as_of_basis；actuals_verified、actual_weeks 和最多六个 actual_missing
说明报告精确键的消费验证，material.readback_verified 独立证明报告材料回读。
任务 complete 说明采集与回读结束；周期 target/history 还须满足材料身份与六键就绪。
公开 api-help 的 collection.receipt 描述此 shape，后续 UI 应消费它，序列表从 query 读取。

FRED writer/query 均显式注入有效包配置凭据；包引用失效或缺失不能退回默认 secret。
CLI 的无包调用仍按显式 secret_path 读取既有 FRED 配置。
新增同值批次可确认提交后中断的 pending，经济 revision 不增加；确认时刻使用真实
clock_timestamp，不倒填 observed。只确认本次响应实际返回且值匹配的键，不能仅因
请求窗口覆盖缺行就确认。DOL parser v3 规范化 Unicode 负号，未知格式仍 fail-closed。
旧具名材料读取补标 official_archive；parser 身份独立，不改变连续数值修订。

### 日历来源与故障语义

主入口为 `https://oui.doleta.gov/unemploy/archive.asp`，备用为
`https://oui.doleta.gov/unemploy/claims_arch.asp`。两者真实 HTML 均含周四 8:30 Eastern
规则及明确周三例外 2026-11-25、2027-11-10、2027-11-24。程序解析规则段与例外表，
逐周生成计划，不只硬编码这些日期。未知规则段、表头、时间、日期/星期或版式拒绝。
页面标记 EST，执行按 America/New_York 的 Eastern 墙钟处理 DST，并保留原标签与解释。

规则适用起点为取得日，生成上界保守限定到最新明确例外所属年末，当前为 2027-12-31；
这是实现的有效期边界，不能据此承诺官方永远没有新增例外。缓存最多 24 小时，
正常 6 小时刷新，失败分别按 300/900/3600/21600 秒退避；有效缓存仍可用，过期阻断执行。
保留原始 bytes、URL、SHA256、真实取得时间和原始响应头（生产 fetcher），
日历接受记录另有不可变原文 locator。只在显式 worker tick 刷新，GET 不刷新。
官方入口间可 fallback；429/配额/安全错误不借备用绕开。没有 FRED 时间 fallback。
取消、执行后改期及冲突保持阻断；滚动生成范围前移不会把既有事件误当取消。
计划以名义周四 `rule_slot` 识别同一周规则，节假日例外变动不会另建可重跑计划；
兼容既有日期型 target key，并保留阶段尝试、结果及保护。未执行计划可由主日历明确改期，
备用日历与既有计划冲突则保持阻断，重复主/备刷新均不自动解除保护。
已取得的历史计划及原始证据保留；未变计划保留首次 acquired_at，变更版本留在同一
日历状态的 event_history 中。历史 query 按每个版本的实际取得截止选取，
后续刷新既不隐藏早已知晓的计划，也不把新改期提前泄漏到旧截止。

计划保持 planned_only，参考周为空；只有实际来源报告/系列确定初请与续请各自所属周。
当前官方 PDF `https://www.dol.gov/ui/data.pdf?os=v0`（或无 query 同路径）支持真实留存的
`official_current_report_snapshot`，具名档案另为 official_archive。
若 PDF header 仍属于上一事件，返回 source_not_updated，不能冒充新发布或伪造档案 URL。
无具名共识仍 surprise=null；官方实际发布时刻仍未知，不能拿 8:30 计划代填。

### 显式进程入口和验证边界

`mdf_cpi_timeseries_web.py` 新增 `--enable-claims-timeseries`、
`--allow-claims-local-read/--allow-claims-collect/--allow-claims-source-query`，
以及独立 claims-schedule-path/job-directory/history-start/artifacts-path/request-control-path。
默认不启用，不修改现有 18761 服务。统一 worker 使用
`mdf_cpi_timeseries_worker.py --asset claims --opt-in-claims-worker`，独立路径必填，
可加 `--once` 做一次有界 tick；真实采集另需 `--allow-collect`、包启用和有效 FRED 凭据。
公共 config/package/environment/只读身份参数沿用既有入口。
启用策略仅持久化 enabled，不冒称进程已运行；暂停进程仍写 paused 心跳。
目标阶段六系列最近八周更新，之后低优先级 init 补缺历史；最多八次阶段尝试，
每轮预留六 FRED 加一 DOL 各最多三尝试，滚动 24 小时预留上限 84。

第 3 段真实新增来源仅两个 DOL 官方日历请求，均 200、每次一次；未重取 FRED 或 PDF。
真实六系列形状和当前 PDF 复用前段不可变材料；当前 PDF v3 在隔离 Claims schema
导入/六 actual 回读/幂等后已清理。真实本地 HTTP facade 读取 6,279 条观测，
2026-10-10 至 10-31 共 24 个目标中 15 个有值、9 个来源末端缺行；零来源请求，
连接 default_transaction_read_only=on，GET 没有创建 schedule/jobs 文件。
实际统一 worker CLI 已执行一次 paused tick，只在隔离 artifacts 写心跳，未启用采集。

确定性测试覆盖严格参数/权限/非持久补源/同截止/任务幂等与中断、旧报告重试、
六 actual 周、日历规则/假日/DST/fallback/有效缓存/过期/429/取消改期、空闲暂停心跳。
隔离 PostgreSQL 测试真实模拟第一事务提交后第二事务中断，再以新同值批次恢复；
不增加 revision，不倒填 available，不确认未返回的键。所有临时 schema 均清理。
具体最终测试数量和运行证据见 ignored `artifacts/segment3/简报.md` 与日志。

未启动全仓 pytest。仍需下一段统一 UI、DEV 真正到期/重试 worker 联调、
Playwright+Chromium 宽窄窗、独立 odin_dba 登录核验、整包 deterministic 回归和 PR 准备。
未来自然发布不能本晚实证，不能把 due/retry/holiday 的确定性测试当自然发布现场验收。
本段没有生产部署、其他族状态写入、服务替换、push 或 merge。


## 第4段：统一工作台与真实序列化合同

Claims 以 `mdf.macro.dol.claims@r1` 接入既有资产查询/发布记录/API说明、
CollectionWorkbench 新建采集/周期任务/执行记录；复用 CpiTimeSeries、
CpiTimeSeriesCollection、CpiStrategyWorkbench、MacroReleaseRecords、DateField、
MultiSelect、请求预览、复制图标及原始 HTTP 正文视图。既有七族默认参数不变。
运行总览、平台管理、其他数据族和生产部署不在本段范围。

提供者 FRED、原机构 DOL-ETA；资产和目标标题使用“美国初请与续请失业金”。
来源入口严格匹配原生 Claims operation、source、binding、asset 及禁止静默fallback的合同。
查询六系列默认全选，各系列 latest_weeks 分别显示，不将续请少一周判为整包不可用。
默认最近约52周可编辑，使用自然日起止范围及周六 week_ending，不装入月/季度字段。
初始化取 status 的20日历年预置、允许编辑，说明来源28天计算缓冲，不将240个月当周数。

查询适配器消费真实 `rows`、coverage、Number字符串及派生对象；整体按截止周倒序、
同周稳定。普通水平、官方四周均值均保留 Number；千位分隔、保留有效小数并去无意义尾零。
周数值差、周变化比率、本地四周均值分别列示；ratio仅展示时转百分比，原正文保留十进制字符串。
官方均值系列不再计算“均值的本地四周均值”。FRED `provider_missing` 与
`local_not_provided`、零、计算缺基期、零分母和补源失败分别保留。
`local_not_provided` 本身不能证明是从未采集还是来源未返回，页面明示这一合同限制。
表单当前请求显式 local_only=true、as_of_basis=available；只有服务授权后可显式临时补源。
历史强制本地和 available cutoff；全部操作数同截止。来源资格变化会取消在途补源、
清除临时补源选择并恢复本地请求预览，避免资格刷新后的旧请求结果错位。

发布接口现支持严格相交过滤：

- `event_id`：完整 `dol-claims-YYYY-MM-DD`；其他族编号及非法日期拒绝。
- `reference_week`：原生周六 YYYY-MM-DD，按材料覆盖过滤，可能覆盖 actual 或 prior；
  不接受 reference_month，也不按发布日期推算初请/续请周。
- `source_version`：精确 YYYY-MM-DD 来源版本，保留日期精度。
- `as_of`：带时区、包含边界的 MDF available 截止；`read_mode=event/source_history`。

后端行关联额外返回 reference_week、roles、source_version、schedule_basis 与
scheduled_publication_at；只有该 series/week 是材料确认 actual 时才关联其报告embargo计划。
prior 修订关联只提供材料链接，不冒称该参考周的首发时间；未来全局日历不填历史行。
关联仍为 scope_only / continuous_version_bound=false，event actual 不替代当前连续数值。
事件详情分别展示本次actual、材料明确引述的官方原前值、本次报告前值；无证据的原前值、
MDF事前值和实际发布时刻保持unknown。来源历史模式可独立选择同一事件的每份已保存证据。
原文定位使用材料页码、参考周表头和片段，不伪造文本位置或发布时间。

采集沿用同一双栏布局、确认勾选、请求编号与执行简报，发送 init/update 四键合同。
周期启停仅一次 PUT；策略 enabled 与worker心跳分开。执行详情消费紧凑周频回执、各系列
latest_week、缺周真实总量、最多16个样本及截断标志，材料结果与数值回读独立显示。
未来计划参考周为空时仍正常呈现发布日期，并写明“参考周待发布材料确认”。
日历来源为可换行普通超链接，沿用共享布局。

`tests/claims_ui_wire.py` 经真实 Claims router / JSONResponse 产生公开合成响应，
`tests/fixtures/claims_ui_http.json`由 Python 重建并逐项对照；仅日历raw_locator的临时
根目录规范化为公开fixture标记，非 provider 原始材料或DEV数据。
前端直接使用这些 serialized响应验证成功、缺值、历史、来源失败、真实release过滤、
采集/执行回执、日历空参考周及真实目录绑定，避免两端各写一份不一致mock。
OpenAPI与生成TypeScript已包含 Claims 请求路径，API说明、预览、curl与实际请求对应。

本段未请求新外部来源、未接触DEV PostgreSQL或18761运行服务、未启动真实到期worker、
未运行Playwright/Chromium宽窄窗现场验收、未跑全仓pytest；这些属于下一联调段及整包收尾。
具体测试结果与剩余入口见ignored `artifacts/segment4/简报.md`。
