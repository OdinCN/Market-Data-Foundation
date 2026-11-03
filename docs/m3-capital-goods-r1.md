# 美国耐用品与核心资本品 R1：来源、存储与查询基础

本包第2段已补齐三项有界真实来源证据，并完成独立存储、当前/历史查询、受控单批采集及真实 DEV 回读基础。仍非完整页面或全族产品验收。使用既有 MDF 控制面，消费者语义按 MBO 的宏观 PRD 与 2026-11-01 语义确认；既有九族、能力配置、策略和运行服务未改变。

| 指标 | Census 分类 / 类型 | 实际口径与来源证据 |
|---|---|---|
| 美国耐用品新订单 | `MDM / NO` | 耐用品新订单净额；第1段两个端点均取得两个月真实行，第2段 m3 单批再次取得 |
| 非国防除飞机资本品新订单 | `NXA / NO` | 非国防资本品减非国防飞机制造业；第2段 advm3、m3 单独 NO 请求均 200、两行 |
| 同口径资本品出货 | `NXA / VS` | 同一 NXA 分类的出货金额；第2段 advm3、m3 单独 VS 请求均 200、两行 |

三项均为美国、月频、SA、百万当期美元、名义金额、非年化水平。新订单扣除取消订单，可以为有符号有限值；真实零、缺值和非法数值严格区分。出货按制造商产品销售的净销售金额定义，不能用订单代替。

分类依据为本段取得并留存的 [Census 聚合序列组成](https://www.census.gov/manufacturing/m3/historical_data/aggseries.pdf)：MDM 为耐用品，NXA 为 NDE 非国防资本品减去 36F 非国防飞机制造业，不额外扩大剔除范围。净订单和出货定义见本段留存的 [Census Definitions](https://www.census.gov/manufacturing/m3/definitions/index.html)。单位依据为第1段留存的 [历史序列页](https://www.census.gov/manufacturing/m3/historical/timeseries.html)。FRED `DGORDER / NEWORDER / ANXAVS` 仍只是具名参考，未接入或回退。

## 第3段增量与明确边界

长范围按一个指标和至多相邻两个月生成请求；每次调用最多六次来源请求，取得、验证并逐片存储后才进入下一片。数值输入和数据库锁分离，Garage 验证不持有 PostgreSQL 家族锁。新 `collect` 是一次新的更新，会重新核验范围内已有值；输出 `collection_id`，同一次调用因失败或预算中止后，用 `collect --resume-id <collection_id>` 和相同端点、起止范围续跑，仅跳过该采集已确认的分片。相同值不产生经济修订；变化值按取得顺序形成修订。达到单次预算时返回 partial、余量和恢复身份，CLI 退出码为 2；存储结果未知退出码为 1。

每个片段的采集身份、范围、预期值与确认状态随无密钥批次元数据保存在本包已有 batches 记录；原始材料仍按既有 Garage 合同验证。写入后可用确认失败时，同一采集的续跑仅确认其已提交分片，采用实际恢复时间并核对回读。范围不匹配的恢复身份拒绝；没有已持久化分片的身份不会跳过任何请求。同一 collection_id 应由一个调用方顺序续跑；跨进程并发编排尚未接入。

当前查询的临时补源也逐指标、逐小窗执行且最多六次。204、超时等失败不丢弃已读取的本地值或之前成功的临时片段；响应保留所有指标/月缺口、受控错误分类与来源请求计数，不含原始异常或 Key，临时补源不落库。遇到限流、传输失败或未知尝试数即停止，留下未处理缺口。历史截止只读本地，继续区分 observed 与 available；预报与完整报告仍独立。

已在现有预览主机挂载 `/api/r5/m3/timeseries/query` 和 `/api-help` 候选路由，需要主机显式注入并授权 `M3QueryHost`；默认无绑定时返回 503，不证明当前环境已启用。查询沿用 `start/end`、`series`、`local_only`、`query_mode`、`as_of`、`as_of_basis`，并可显式选择数据集。当前补源需现有来源准入授权；历史始终本地。OpenAPI 参数、API-help 和合成 TestClient 响应已核对。未添加 M3 资产目录注册、Web 页面、采集 jobs 路由或策略：不得把候选查询路由当成整包已接入或后台任务可执行。本段没有运行浏览器验收。

第3段无 Census 实时请求；利用第2段保留原文在 DEV 验证一个单指标片段：Garage GET/PUT/GET 三次，新增一条本包批次元数据，数值新增与经济修订均为零；同材料重放零新增，现有六条指标值均可回读。该操作不改写旧族、共享配置或权限。离线测试覆盖新更新发现变动、同值幂等、跨年长范围、部分失败与同采集续跑、确认失败恢复、来源 204 部分返回和既有预览路由注入。详细证据在 `artifacts/m3-segment3/`，控制面回执在事务 `artifacts/segment3/review.md`。

## 来源与版本合同

明确选择 `https://api.census.gov/data/timeseries/eits/advm3`（预报）或 `/m3`（完整报告）。固定 get 包含 `data_type_code,time_slot_id,seasonally_adj,category_code,cell_value,error_data`；筛选美国、SA=yes、error_data=no、一个分类和**一个类型**。每个数值请求仍为最多两个相邻月份。三项采集一次逻辑批次需要三个请求，使用同一端点、同一窗口，不拼接端点。

第1段 NXA/NO,VS 组合返回 204；第2段四次单独 NO/VS 请求全部成功。当前适配因此禁止组合类型，不能把原 204 解释为分类不存在或来源无值。第1段四次遗漏 time_slot_id 的 400 和一次猜测 slot0 筛选的 204 均保留；请求不发送 slot0 过滤。依据为主控提供的 [advm3 示例](https://api.census.gov/data/timeseries/eits/advm3/examples.html)、[m3 示例](https://api.census.gov/data/timeseries/eits/m3/examples.html) 以及已留存的元数据和真实行。

响应 `time` 为参考月；`time_slot_id=0` 是已观察响应维度，保留原值而不解释成发布版本号。未返回 program_code，不补造。重复筛选列各允许一至两次且必须一致，其他列唯一；冲突、重复单元格、越界月、错分类/类型/季调/地理、误差数据、未知列和槽位均拒绝。空值与已识别缺值标记保留为空，缺行局部投影为空，不填零。

[官方日历](https://www.census.gov/manufacturing/m3/release_schedule.html) 分别给出预报 8:30 和完整报告 10:00，日期不同。日历不绑定当前 API 数值为特定事件 actual。两端点返回值即使相同也不合并版本。所有本次旧月数据均标为 `current_source_snapshot`；官方事件和来源版本日期保持空。原始参考月、逐请求 MDF 取得时间、可用确认时间分别保留，不把首次捕获或本地 revision=1 当作官方首发，不计算缺少事件与具名事前共识证据的 surprise。

## 存储、查询和回读

`m3_capital_goods_store.py` 使用独立 schema `mdf_m3_capital_goods_dev_r1`，含 batches、revisions、latest 和 m3_timeseries_rates。迁移沿用显式 ensure_schema、schema owner 核验、短事务和家族专属 advisory lock，不授权新数据库权限。数据库 month 为月首日 `date`，对外仍为 `YYYY-MM`。dataset 是版本键的一部分，预报与完整报告互不覆盖。

三个来源响应连同无密钥请求和逐响应取得时间封装成可精确恢复原始字节的批次材料，复用 `VerifiedGarageRawStore` 的 GET-before-PUT 与读回校验。raw、观察另留忽略的 artifacts；Git 仅保留合成 fixture。批次内容身份用于同材料重放；经济修订只由同指标/月/端点的金额或缺失状态变化产生，重复捕获同值保留原捕获和可用时间。

数值事务先提交，available_at 在后续事务确认。若确认失败，值保留为未确认可用；正常同批重放再次确认，使用**本次**确认时间，不倒填、不插入修订。`replay --material` 可恢复保留材料并沿用原取得时间；公开的 `confirm_batch_availability` 也可确认指定已提交批次。未确认较新值不会默默展示旧版可用值。确定性测试覆盖确认失败、重放恢复、恢复前后历史截止与 PostgreSQL 已存在批次分支。

`m3_capital_goods_timeseries.py` 提供当前查询、指定 observed/available 截止的本地历史查询、受控采集与同材料导入回读。内部 source snapshot 的 obtained_at 映射为存储/对外 observed_at；消费者没有新增 obtained 截止口径。历史查询永不回源；当前查询默认本地，显式 allow_upstream 可补缺口且**不落库**。collect/import 需要显式写入授权，保存后逐项核对回读。latest 的变化率视图提供同端点相邻月百分比，分母为零或输入未确认可用时为空。

本地查询已支持最多 2400 个月的窗口，不把本次两个月实采上限当作产品历史范围上限。第3段已增加长范围有界来源分片与同一次采集的续跑；策略运行留后续。脚本 `uv run python scripts/m3_capital_goods.py --help` 说明 prepare-schema/query/collect/replay；默认查询本地，历史截止参数为 `--as-of` 与 `--basis observed|available`，写入命令需 `--confirm-dev-write`，来源访问需显式选定现有 Census request-control 状态。第4段已提供显式启用的 M3 资产、范围查询和人工采集入口；页面与任务仍须按下述边界继续核验。

## 本段实测与范围

第1段累计 16 次 HTTP、9 次数值的原始失败/成功记录和取得时间保留在 `artifacts/segment1`，未覆盖。

第2段累计 **12 次真实 HTTP，其中 Census 9 次（7 次数值、2 次定义材料），另含现有 Garage 原始材料验证 3 次**。先取得两个端点的 NXA/NO、NXA/VS 共四次成功请求，再以 m3 三个逐项请求取得一个两月三指标批次；所有数值窗口均为 2025-01 至 2025-02，无自动重试。Garage 三次交换为一次批次材料的 GET/PUT/GET，不含凭据或签名信息的输出。来源访问已停止。

真实 DEV 联调已完成：既有连接器核验数据库/角色、192.168.10.4:5432 与 TLS 后，新增上述独立 schema；写入 **1 个批次、6 条指标月记录、0 条经济修订**，可用回读为 6/6；同材料重放新增 0、修订 0，仍回读 6/6；另以新进程 CLI 从保留材料重放成功，阻断 HTTP 后仍新增 0、修订 0、回读 6/6。数据库 month 类型为 date，变化率视图有六行，捕获前历史截止返回 0 条可用记录。原始批次材料另经现有 Garage 精确读回验证。此为真实 DEV 数据写入，不是隔离测试；未改写既有九族记录、凭据、共享能力或策略。

本段证据在工作树 `artifacts/segment2`，包括 review.md、tests.json、source-observations.json、dev-result.json、逐用例终态、来源 raw 和 DEV 回读；事务目录 `/srv/market-data-foundation-control/transactions/MDF-M3-20261103-R1/artifacts/segment2` 提供同名可读副本。早期测试记录器相对路径错误和修复前 Ruff 失败均保留，最终回归另记，不覆盖失败日志。

剩余工作为长范围来源采集、完整 Web API/页面接入、调度日历、策略进程与执行追踪，以及 MDF-DEV Playwright + Chromium 宽窄窗验收。真实发布档案、事件修订与共识按证据渐进补齐。G.17、工业产出和产能利用率留后续包；本段未启动自动 worker、18761 预览或部署，未推送。


## 第4段：共同入口与剩余验收

M3 作为独立 Census 能力加入现有资产和接入包目录；来源、凭据和本地读取均需显式绑定，旧族默认不变。共同范围查询可选择 m3 完整报告或 advm3 预报，按月份倒序显示三项金额。当前页面明确请求临时补源且不保存，历史页面按 available 截止只读本地；来源失败保留本地结果与缺口。临时来源点的 observed_at 是本次观察，available_at 为 null。单次页面补源最多六个片段并共享 15 秒来源预算；非空源及零值按十进制字符串处理。页面暂只展示金额，历史同截止变化率查询尚未接入。

人工采集入口使用既有持久任务回执及同一个 collection_id；每次执行最多六个来源片段，未完成需显式按原任务恢复，新更新重新核验。任务完成和数值覆盖不等价；空值和未取得数据不补零。当前人工采集只选完整报告 m3；预报采集入口尚未装配。DEV 预览参数为 `--allow-m3-local-read`、`--allow-m3-source-query`、`--allow-m3-collect`，并需独立的 `--m3-job-directory` 与明确的 `--m3-request-control-path`；不自动启用或启动周期 worker。

本段 Chromium 使用隔离内存数据及虚构来源验证查询和人工任务，不代表真实 DEV 采集写入或 Garage 回读。真实历史月正文、同截止变化率、执行简报完整细节、周期策略与进程、官方事件绑定以及真实 DEV 浏览器全链路仍需后续逐项验收。历史值快照不作官方首次发布；本包尚未达到完整交付门禁。
