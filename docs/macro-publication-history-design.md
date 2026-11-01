# MACRO 发布历史设计：CPI 样本基线

本基线描述已实现的 CPI 当前查询、四期限 ratio、本地修订回放与事件调度合同；
本文件前述数值与信号合同以 CPI 为范围；五族当前实现与来源差异统一见
[五族开发与审查指南](macro-family-development-review.md)，不据 CPI 推广所有来源字段。
历史 live 验收仅是历史事实，不承诺当前行数、数据范围、权限、服务或 worker 状态。
使用入口见 [Web 快速上手](cpi-webui-quickstart-zh.md) 与 [CLI / 本机 API](cpi-timeseries-cli-zh.md)。

## 来源与版本

BLS 当前 API 返回历史月份的当前值，不能据此恢复官方全部 vintage。
初始化本地 revision=1 不等于官方首发；只追加真正观察到的数值/必要脚注变化，相同内容不增统计修订。
未采到的中间版本仍未知；官方通用首发档案摄取、完整覆盖和指定官方发布版本查询仍 unsupported。
`official` 查询返回 `cpi_timeseries_official_vintage_unsupported`，不能把本地回放当官方发布版本回测。
提供方按能力绑定，存储故障或来源失败不触发其他 provider 回退。

## 查询、时间与单位

- 当前查询本地优先，真实缺口可临时读取上游，仅合并本次响应，不持久化业务数据或原始材料；local_only 禁止补取。落库须显式 init/update 或调度采集。
- strict 历史查询仅按本地 available_at 截止；as_of 禁止上游，缺历史证据就返回缺失。available 无截止值也只读取本地已确认数据。
- observed_at 是实际获取时间；available_at 是实际确认可消费时间。兼容 observed 回放不等于 strict 可消费历史，也不是官方发布时间或原始 commit 时间。
- 旧基线只能显式确认实际现在，不倒填过去；普通迁移/只读查询不自动补证据。晚导入旧稿不能进入此前 MDF available 回放。
- 时间点展示使用 Asia/Shanghai（+08:00），后端可保存/返回 UTC；YYYY-MM 是参考月，内部 DATE 月首键不表示每月一日观测。
- change_1m_ratio、change_3m_ratio、change_6m_ratio、change_12m_ratio 均按同系列、同 cutoff 的有效指数计算 `I(t)/I(t-k)-1`，累计、不年化。
- Decimal 最终 ROUND_HALF_UP 到 ratio 四位；`0.0312=3.12%`。任一端点缺失为 null，中间月缺失不妨碍有效两端计算。
- 来源 bls_pct_* 保留原百分数和精度；旧 mom/yoy 的 `3.12` 仍为 3.12%，不静默变为 ratio，也不宣称派生值比官方更准确。

batches 保存请求、来源哈希及获取时间；revisions 追加保存版本；latest 和 cpi_timeseries_rates 是当前视图。
历史 ratio 须先从 revisions 为分子、分母分别选择同一 cutoff 的有效版本；只过滤当前视图的分子时间会混入后来修订的分母。
来源明确缺值保存 NULL/source_unavailable，不补零、不插值、不无限重抓；API 漏行和普通缺值不自动构成撤回。

## 发布、历史核对与信号

| 触发 | 处理合同 |
| --- | --- |
| 初始化 | 四条全国 CPI-U 总体/核心、SA/NSA 保存当前基线；init 补缺，update 显式刷新 |
| 目标月到期 | 有界重试，持久化并确认实际可消费时间，再按 available 本地回读；确认失败保持未完成 |
| 目标阶段完成后 | 从固定 history_start 到发布参考月核对全部配置历史；历史阶段独立重试 |
| 年度季调 | 纳入发布后的历史核对，依官方材料确认；不默认每日全量或固定 +1h/+24h 重抓 |
| Errata 更正/撤回 | 先核验全国四序列交集、月份、生效时间、预期值/撤回证据，再执行受影响范围 |
| 信号通道恢复 | 安排持久化逻辑补偿，可由月度历史核对满足；不因每次超时全量重抓 |

目标可用与历史完成分开；HTTP 200、来源有值和 observed 覆盖都不能替代 available 确认。
重试预算耗尽保留 pending_exhausted；官方明确缺值可终结采集，但不代表 target_available=true。
逻辑补偿不保证崩溃下恰好一次网络请求；同值重试不新增统计修订。
未公告变化可能到下次成功月度核对才发现，两次获取间的短暂版本仍可能漏失。

## 日历与运行边界

BLS ICS 是发布时间主来源，按来源时区处理夏令时后转为时间点；失败保留降级缓存，不回退 HTML 发布时间。
ICS 不含参考月：优先复用已确认映射，仅在必要时获取官方辅助材料；未知则 pending，不按发布月减一猜测。
新事件须相同发布时间唯一映射；绑定 UID 后才接受较新的 SEQUENCE/修订时间改期，不按邻近日期合并。
短覆盖或缺席不等于取消，不删除既有计划；只有身份已确认的明确取消证据才暂停未完成任务。
重复刷新不重置预算，完成任务不因重复日历重开。

预置周期策略须显式启用并配套独立 worker；页面状态不代表进程常驻，地址/端口/Origin 属于 DEV 部署配置。
真实未来发布事件仍待自然验证；离线时钟、合成数据和历史 live 结果不能代替真实未来发布验收。

## 来源参考

以下链接用于查阅来源合同，本文不据此承诺当前在线状态或数据覆盖：

- [BLS API v2](https://www.bls.gov/developers/api_signature_v2.htm)
- [Errata](https://www.bls.gov/errata/)、[ICS 日历](https://www.bls.gov/schedule/news_release/bls.ics)、[CPI 参考月日历](https://www.bls.gov/schedule/news_release/cpi.htm)
- [CPI 计算说明](https://www.bls.gov/cpi/factsheets/cpi-math-calculations.pdf)

## 五族官方日历展示

五族连续查询共用“官方日历时间”：参考月份必须来自各族明确官方证据，历史和未来事件
均可从本地日历缓存展示，与采集任务是否存在或执行无关。普通 GET 不刷新来源、
不写 state、不创建任务；无记录与待定时间分别显示，取消保留取消状态，冲突不展示时间。
保留 `publication_event` API 字段；`scheduled` 不随时钟流逝自动变为 `published`。
日历时间不代表当前修订值当时已知；历史截止仍使用入库可用时间，Web 时间为北京时间。

BLS 历史展示证据可通过 `scripts/import_official_calendar_history.py` 显式导入本地
官方参考月表或年度/月度列表（`Index for Month Year`），每次限定最多 120 个月且
仅接受已经过去的日历时间。证据保存在现有 calendar 的 `display_history`，不会把
历史月份加入可执行队列，也不会修改已有采集/重试进度；正常日历刷新保留此证据。
下载、来源核验和 state 导入由操作者执行，命令本身不联网；未知月份、缺失时间、
无法解析的取消行和互相冲突的官方材料不作推算。
就业复用明确 Employment Situation 绑定；PCE 使用 BEA 日历，零售使用 Census
Data Month / 年度 Period Covered，不能套用 BLS ICS 字段。
零售历史与未来日历、证据截止和只读查询边界见
[连续零售合同](census-retail-timeseries-r1.md)；共同检查与测试见上述五族入口。
