# TDX 全 A 股盘后日线 DEV 产品 R1

资产为 `mdf.market.tdx.stock.daily.native / r1`，正常 R5 的资产、采集任务、
执行详情和策略入口共用 `/api/r5/tdx/daily`。复用 B1 PostgreSQL 当前事实、
修订历史与批次收据；不建立另一套行情事实。此接入限 DEV。

## 行情与时点

- 明确选择固定 `injoyai/tdx v1.0.0` 派生驱动及一个 TDX endpoint。
  不进行 Provider 回退。驱动构建方法见
  [固定 SDK 构建说明](../p5/tdx-sdk-v100-derived-migration-r1.md)。
  P0 当前执行字节 SHA256 为
  `6b2dbab3a1726bfd45c04d882b74fd47c51971c9ff1bc6b11a180f99fc9c0102`；
  核验并执行同一个 sealed memfd。相邻 build report 不授予执行资格，
  既有 watchlist 两版驱动 pin 保持原身份；其他重建物须重新资格核验并显式更新 P0 pin。
- 股票 Type9 日线；价格和金额为整数厘，成交量为手。
  `native_projection` 尚未取得“不复权”或收益计算资格。
- SDK 的日线 15:00 是交易日期投影，不是实际来源事件时间。
  中国历史夏令时按 `Asia/Shanghai` 验证，合法 15:00+09:00 可入库。
- 显式采集终日不得超过盘后锚点。盘前返回的“当日 15:00”线不会因该标签提前入库。
- `received_at` 保留实际响应接收时间。`available_at` 取事实事务成功确认之后的
  首次批次业务发布时刻。未知提交结果用同一批次身份恢复，发布重放不改变首次时刻。
- 早期批次没有可靠提交时钟时，迁移采用首次能确认事实已提交的较晚时刻。
  不以 pending/admitted 时间倒填可用性。查询、覆盖及汇总遵守同一发布边界。

## 可复现的有效期名册

`scripts/mdf_tdx_roster_import.py` 离线解析已保留原件，并在显式 DEV opt-in 下写入
不可变名册快照。原件、manifest、摘要与运行收据均放在忽略的 `artifacts/` 中。
manifest 的 `valid_through` 为 YYYY-MM-DD，`sources` 每项必须包含：

```json
{
  "kind": "sz_current",
  "url": "https://www.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=1110&TABKEY=tab1",
  "path": "artifacts/exchange-originals/sz-current.xlsx",
  "sha256": "SHA256 of exact retained response bytes",
  "received_at": "actual response receipt timestamp with timezone"
}
```

移交原件缺少可信接收时钟时，可采用首次核实原件的较晚时刻，并明确记录
`clock_basis=conservative_retained_original_verified_at`；不可倒填成未知的原始接收时刻。

必需原件及解析类型：

| kind | 交易所原件 |
| --- | --- |
| `sz_current` | 深交所 `1110/tab1` XLSX，A 股代码、简称、上市日 |
| `sz_terminated` | 深交所 `1793_ssgs/tab2` XLSX，上市日及终止上市日 |
| `sh_main`、`sh_star` | 上交所 `COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L` 完整 JSON，类型 1/8，状态 2,4,5,7,8 |
| `bj_mapping` | 北交所 `service/code_mapping.html`，新旧代码与原精选层日期 |
| `bj_current_page_N` | 北交所当前上市公司接口，必须保留所有页及相同总数和业务日期 |
| `bj_terminated_CODE` | 北交所载明终止上市并摘牌日期的公司公告 PDF |

北交所当前名册使用 POST `https://www.bse.cn/nqxxController/nqxxCnzq.do`，
表单为 `page=N&typejb=T&zqdm=&sortfield=xxzqdm&sorttype=asc&xxfcbj[]=2`。
保留实际参数和方法。漏页、业务日期漂移、总数变化、重复代码或原件摘要不符均拒绝导入。
原精选层上市日早于北交所开市的，北交所有效期从 2021-11-15 起计；旧代码血缘未因此获得资格。

可选 `sz_calendar_2026`、`sh_calendar_2026`、`bj_calendar_2026` 消费交易所年度休市
原文，须具有七个节日区间；周末不作调休交易日。
停复牌证据使用 `sz_suspension_CODE` 的深交所 `1798` JSON 事件及
`sh_suspension_current` 的上交所 `GW_PL_JYTS_TFPXX` JSON。
其 manifest 项另保留精确 `parameters`。深交所须完整保留指定代码查询窗口内的停牌和
取消停牌事件；复牌日从停牌区间排除。连续停牌至多证明到原件实际取得日。
盘中停牌、转债、申赎状态不作为股票全天停牌。

```bash
uv run --locked python scripts/mdf_tdx_roster_import.py \
  --manifest artifacts/exchange-originals/manifest.json \
  --provider-snapshot PROVIDER_SNAPSHOT_ID --dev-opt-in
```

导入快照身份绑定 manifest 原字节及指定 Provider 快照身份，重放保留首次可用时刻。
更换 Provider 对账样本不会冒充同一次导入。正式产品优先选择三市成功的
交易所快照。目录失败或局部结果不能覆盖成功名册；新的 TDX 目录只增加对账证据，
未经交易所确认的候选不会进入正式分母。689 CDR 不属于本资产。
上市日包含、退市日排除。历史分母包含已知退市证券；今日名册的完整性不外推到历史。

## 查询与覆盖

```text
GET /api/r5/tdx/daily/query?market=0&instrument=000001&start=1990-01-01&end=2026-11-12&limit=100
GET /api/r5/tdx/daily/query?day=2026-11-12&limit=100
GET /api/r5/tdx/daily/coverage?day=2026-11-12&limit=100
GET /api/r5/tdx/daily/summary
GET /api/r5/tdx/daily/help
```

查询严格本地读取，返回 `provider_requests=0`；`as_of` 可指定带时区的截止时刻。
页面单股历史按交易日倒序，传入 `order=desc`。API 省略 `order` 时沿用升序。
分页 `offset/limit` 上限 10000。成员按所查日期的上市/退市有效期过滤。
覆盖区分有效行情、零量行情、已证明停牌、交易所休市、上市前、退市后、来源失败及未知。
零量、空响应或来源穷尽都不证明停牌。缺口列表可分页，包含可恢复日窗。
官方全天停牌证据优先于来源投影分类；`suspended_with_bars` 单列来源仍返回行情的
停牌成员，原始查询保留这些值，不把它们计入预期交易行情。
只有该日交易所全集、生命周期、交易日和全部有效成员状态都可证明时，才返回完整。
没有缺线也可能因历史全集证据不足而保持不完整。
市场汇总读取实际可见版本，进入查询页时读取一次，之后可按需刷新；执行进度仍定期更新，
避免历史入库期间不断重扫全库。
正常资产目录只探测是否存在已发布且符合成员有效期的行情，不运行全库汇总。

## 作业与宿主

POST `/jobs` 的 JSON 为 `request_id` UUID、`action`（`universe/init/update`）、
`start/end` 日期、`markets`（0 深/1 沪/2 北）；可用 `instruments` 指定 `market:code`。
写入须是相应 DEV 入口的精确 Origin 与 `application/json`。
`init` 默认从 1990-01-01 到实际盘后日；`update` 默认重叠 14 个自然日。
无显式代码列表的历史初始化也包含已知退市成员，并实际请求来源；空历史逐代码记录，
不计行情覆盖。已终止于增量开始日前的成员无需再次增量。

`GET /jobs` 返回作业摘要；`GET /jobs/{request_id}` 返回步骤、页数、请求数和恢复位置。
`POST /jobs/resume` 只提交原 `request_id`，不创建第二个全量初始化。
检查点按证券落盘，页批次保留稳定身份；进程退出后可识别 INTERRUPTED。
来源原件、观察文件及目录项同步落盘后才交给页检查点；同步失败不能授予该页入库资格。
失败连续出现时断路，等待明确恢复。同一作业用文件锁排除双执行者。
每页有一根重叠边界线；合法的最后一根重叠尾页视为来源穷尽。
集合 COMPLETE 表示请求窗口处理完毕，不能代替历史覆盖证明。

现有 R5 宿主启动参数增加：

```text
--tdx-state-directory artifacts/tdx-p0/runtime
--tdx-driver artifacts/tdx-p0/qualified-driver
--tdx-endpoint EXPLICIT_TDX_HOST:PORT
--allow-tdx-collect
--tdx-dev-port 18767
--tdx-workers 4
```

18766 统一 DEV 预览使用 `--allow-unified-dev-macro-operations`，继承既有 Macro 数据族的权限、作业目录、策略目录与执行窗口。该开关仅在 18766 生效，各族仍沿用各自的能力开关和同源校验。

沿用既有 Macro 配置与参数。TDX 仅接受 DEV 18766 或隔离 18767，默认每作业四个采集
worker，上限八个；同时运行多作业会叠加预算，应结合来源、PG 和磁盘实测配置。
POST `/strategy` 的 `{"enabled":true}` 开启驻留执行；启停配置按 DEV 端口隔离。
A 股统一交易日历确认开市后，北京时间 09:00 刷新行情目录，15:35 复用当天完成的目录，启动一轮全市场日线采集。错过早盘时补目录。当天目录已部分失败或中断时，盘后在原作业上自动续跑一次，仅补未完成市场；仍不完整则留给显式续跑。
目标日 D 的采集范围为 [D−14 个自然日, D]，含两端。当日及历史新增行写入，未变跳过，
窗口内发现的变化按本次发布时间生效。失败时续跑原作业，跳过已成功的股票。
错过窗口或重启后，无游标时从最近有日历证据的交易日开始，已有游标则按开市日接续；
同一槽已完成的作业不重复提交。旧失败保留真实状态供显式续跑，不阻塞后续交易日。
调度仅读取日历投影和作业摘要，不在每个 tick 读取完整成员或执行分片。
失败槽保留原作业等待显式恢复。`worker_online/heartbeat_at/calendar_state` 区分配置和实际执行。
升级宿主前须核对现有槽和运行作业，避免旧宿主继续派发；宿主服务委派不由网页伪造成功。

### A 股统一交易日历

早盘基础名册、盘后日线和交易所证据刷新从同一份本地日历判断当天开休市。
可调度的完整年份按实际可用时点独立保存在 PostgreSQL。相同日期和相同来源
重复发布为无变化；更高权威同口径确认也产生一个新版本，重复确认不重复写入。
不同年度可同时读取。若下一年元旦公告覆盖上年 12 月，以公告发布后的
日历修正对应日期。权威保留按年度版本判断；下一年度元旦明确给出的上年 12 月休市
仍覆盖上一年度按工作日展开的默认结果。
迁移期间，已有名册快照里的交易所日历优先于同年候选的对应日期。已有上交所发布记录可继续回读。
盘后交易所证据刷新仍按市场核验证券，只需要参考清单中的北交所代码映射。

年度维护命令从中国政府网政策文件库查找国务院办公厅年度放假公告，提前生成
沪深北统一排程。周一至周五扣除休市日，调休补班的周末仍休市。

上交所年度休市公告确认或更正年度安排。后续公告按公告日期应用至对应休市区间，
可以延长或替换原安排。已保存的公告随年度版本保留，目录翻页或暂时不可用时继续生效。
当年只取得国务院公告时，可先排程，并在回执中标为待年度确认。新旧官方来源都无
完整基线时，返回 `UNAVAILABLE`，未知日期保持 `CALENDAR_PENDING`。
已保存的官方日历继续可用。已有第三方候选仅保留历史记录。

`--publish` 后的 `publication` 表示实际保留的来源和年度确认状态。
原件、SHA256、采集时间和路径保存在证据中。重复获取相同安排与公告不写新版本。
历史公告离开目录后，可用 `--exchange-annual-url` 和可重复的
`--exchange-supplement-url` 提供上交所官方归档链接。

在 MDF-DEV 项目目录执行。加 `--publish` 才应用 DEV 迁移并发布日历。
`deploy/systemd/mdf-tdx-trading-calendar-dev.{service,timer}` 每日北京时间 08:00
检查当年公告，11、12 月同时准备下一年，重启后补查。通过
`/etc/mdf/tdx-trading-calendar.env` 指定工作树 `MDF_TDX_CALENDAR_PROJECT`，
以及可选的 `MDF_TDX_CALENDAR_GOVERNMENT_URL`。09:00、15:35、16:05 的任务共用该日历。

`--verify-history START END` 只读核对已结束的日期。按日期汇总已发布的沪深北 A 股
正成交量日线，包括每个市场有成交记录的证券数。计划休市却有成交时报告冲突，
缺少成交记录时报告待核，保持原调度日历。原件和回执保存在指定的私有目录。

```sh
uv run --locked python scripts/mdf_tdx_trading_calendar.py \
  --dev-opt-in --year 2027 --artifact-dir artifacts/tdx-trading-calendar
```

已保留同源日线收包可以用 `scripts/mdf_tdx_daily_receive.py --dev-opt-in` 显式入库。
清单绑定收包与 qualification observation 的 SHA256、端点、SDK 和请求身份；仍走
正常 DailyJobs、PgStore 和修订表。原始 received_at 不变，admitted_at 与正式
available_at 使用本次实际时钟，重放保持原批次身份，不倒填发现时刻。
该入口不请求来源、不刷新全历史。实际重新采集继续使用工作台正常单日 update。

交易所名册导入拒绝空枚举及早于目标日期的当前名册回执。对于已合并生命周期的目录
快照，`mdf_tdx_roster_import.py --provider-job` 必须绑定真实已完成目录作业，不能把
历史退市成员误报为当前来源目录。新枚举保留旧退市成员自己的原始证据，不延长旧证据日期。

名册发布后刷新成员统计，避免 PG 对新快照按单一成员选计划。选择性日线查询保留名册
索引，完整汇总只计算一次成员日期。默认汇总按已发布批次数、最后发布时间及名册身份
合并并发读取并复用真实结果，其他进程发布也会失效；保留并显示实际汇总截止时间。
显式历史截止汇总独立计算。此过程不创建来源请求或后台采集执行器。

DEV 数据库为 `192.168.10.4:5432/market_data_foundation_dev`，仅使用既有固定 TLS/角色绑定。
人工只读角色需要命名空间 USAGE，及当前事实、修订、批次收据、名册与
`p0_batch_publications` 的 SELECT；不授予写入。007 发布标记迁移与 008 读取索引
仅可在显式 DEV 迁移下执行。大库可单独以 CONCURRENTLY 建立相同读取索引。
生产和 GitHub CI 不接入此私网能力。

## 验证边界

确定性测试覆盖分页、范围、盘后锚点、恢复、批次发布时点、有效期、漏页、CDR、停复牌、
日历、Origin 与正常 R5 路由。真实 DEV PG 测试需显式
`MDF_TDX_P0_DEV_PG=1` 并选择 `-m postgres_integration`，使用临时命名空间。
真实来源原件及行情只留在忽略目录。Go 构建执行固定 SDK 的离线协议测试和驱动 race 测试。
CI 继续运行仓库卫生、ruff、确定性 pytest、前端类型/格式/契约/测试/构建，不访问私网或真实 Provider。

### 自动日线的晨间名册绑定

15:35 自动日线受理时将目标交易日的确定性 `universe` 周期作业号及其实际
`snapshot_id` 写入同一作业头的 `roster_binding`，首次分片前落盘。仅接受明确
periodic 来源、同目标日、三市完整完成的名册作业；运行和同号恢复核对原号、原快照，
不跟随午间手动刷新或最新名册。名册新发布同时记录 producer；旧快照没有该字段时
沿用实际名册作业输出的原快照关联，并核对发布时钟属于该作业实际执行区间，不回填历史。
快照/来源/范围/完成状态不符或不可读
会留下 PARTIAL 分类，来源零请求，不换快照。未核实的绑定只能核实同一个晨间作业；
该作业真实完成后可显式恢复，不能借别的已完成作业通过。

具名作业全量与 summary GET 均保留两种身份，既有详情展示晨间作业号、名册目标日及
实际发布名册号，不新增回查按钮。旧自动日线已有 snapshot_id 按其原留存读取，不追认为
晨间来源；无留存快照的旧自动槽在恢复时阻断，GET 仍可读。手动范围选择及正常新建
不变。09:00/15:35、交易日历和合法补跑沿用原策略；延迟补跑仍记录实际观察/发布时间，
不将晚取证倒填成早盘时刻。确定性回放见 `tests/test_tdx_morning_binding.py`。

零分片的旧作业仍沿用实记快照，来源缺失也不补造晨间血缘。手动请求成功选定可用
名册后保存 `roster_selected`，首分片前中断不换册；首次选取被拒绝则保留名册诊断，
不标记已选定，可在有效名册到达后原号恢复。兼容旧写者留下的明确拒绝事实；
旧实记选定身份先保留锁定，失败复核不能覆盖该事实并在下一次恢复时换册。
晨间作业头损坏时不覆盖原日志、不发行情请求；`GET /api/r5/tdx/daily/strategy`
的 `blocked` 留存目标日、原号、机器分类及实际发现时间，重建后仍可回读。
仅该原号证据恢复可解除阻断；既有跨日游标可继续下一已核实交易日。
