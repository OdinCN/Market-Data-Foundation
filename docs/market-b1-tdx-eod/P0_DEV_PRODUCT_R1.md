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
15:10 后只有三市当日交易所日历都证明开市才派发确定性日槽，先目录后重叠增量。
失败槽保留原作业等待显式恢复。`worker_online/heartbeat_at/calendar_state` 区分配置和实际执行。
升级宿主前须核对现有槽和运行作业，避免旧宿主继续派发；宿主服务委派不由网页伪造成功。

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
