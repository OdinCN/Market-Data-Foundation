# 金融条件日频数据层 r1

提供日频数据链及显式准入的人工工作台/作业。常驻日频执行器另段交付，本模块不会自动启动。
稳定数据集为 `mdf.macro.financial-conditions.daily@r1`。run/attempt 不构成数据集身份。
数据库物理版本 r3 可从已存在 r1/r2 增量升级，不重建或删除原数据；查询不迁移。

## 明确来源绑定

Owner 为内部 MDF-DEV 明确绑定 `mdf.binding.financial-conditions.origin-dev@r1`。
CLI 默认使用此绑定，不是 FRED 失败后的自动替代。十条代码只作指标映射。
`registry` 同时保留 `source_use`（原 FRED 受限状态）和 `active_binding`（本次明确绑定）。
`query` 每行返回实际机构、分发者、身份、来源、请求、出处、条款和必要声明。

| 代码映射 | 实际输入/身份 | 单位与日期 |
|---|---|---|
| DFEDTARL / DFEDTARU | 联储理事会政策变动表；有效状态投影 | Percent；七天；`policy_effective_date` 为具体变更起点 |
| EFFR / SOFR | 纽约联储 Markets Data API，独立原始观测 | Percent；effectiveDate；一般次工作日发布不等于逐点实际可得时刻 |
| DGS2 / DGS10 | 美国财政部 XML 名义 par curve 的 BC_2YEAR / BC_10YEAR | Percent；NEW_DATE；不是逐券盘中报价 |
| DFII10 | 财政部 XML 实际 par curve 的 TC_10YEAR | Percent；NEW_DATE；允许负值 |
| T10YIE | MDF：同日财政部名义 10Y 减实际 10Y；非 FRED 实采 | 百分点；同一 MDF 截止的两腿值、修订、batch、原件与时间均保留 |
| BAMLC0A0CM / BAMLH0A0HYM2 | Owner 指定 GitHub 固定归档初始化 + 显式 FRED v1 尾段/增量；指数提供者 ICE | Percent 原值；bp=Percent×100；保留月末周末值 |

[理事会政策表](https://www.federalreserve.gov/monetarypolicy/openmarket.htm) 和
[出处规则](https://www.federalreserve.gov/disclaimer.htm)；政策单一目标不能伪装成上下限，
无区间证据的早期日期留缺。投影不是当天新公布或原始日观察。
[纽约联储条款](https://www.newyorkfed.org/privacy/termsofuse)允许所述存储/使用，并要求参考利率
出处及特定声明；`active_binding`、已存 metadata、每行 `source_binding` 均包含该声明。
[财政部原生 XML 说明](https://home.treasury.gov/treasury-daily-interest-rate-xml-feed)及政府目录
[名义](https://catalog.data.gov/dataset/interest-rate-statistics-daily-treasury-yield-curve-rates)、
[实际](https://catalog.data.gov/dataset/daily-treasury-real-yield-curve-rates)标注 CC0。
本绑定仅为已授权内部 DEV，不推导新的第三方分发能力。

信用两条的新增取源决定仅授权个人非商业内部 DEV，不代表 ICE 商业服务、付费订阅或对外分发许可。
历史归档固定为 `PStoclet/BBA_TFG_CODE_PABLO_STOCLET` 提交
`5f63f9ad367be0e9d28aac6a7b982500c23ffaa6` 的 `data/05_Credit_Spreads/` 下
`IG_OAS_spread_daily_1990_2024.csv`、`HY_OAS_spread_daily_1990_2024.csv`。
实际归档范围均为 1996-12-31 至 2026-03-17，不以文件名年份推定范围。
初始化只导入选定窗口，再显式以 FRED v1 同名系列接尾段；增量只请求选定区间。
信用 `is_copyrighted` 由 FRED series notes 大小写无关识别 copyright，未取到 metadata 则 null；
归档原件自身未提供该元数据时保持未知。其他八条的 FRED 原系列仍未获存储准入，不静默替换原机构。

## 正常工作台和人工任务

18761 从首页左侧「数据资产」进入目录，在「宏观数据接入包」筛选或搜索「金融」后选中
「金融条件日频（十项）」，在原目录右侧查询本地日频、查看覆盖/来源和 API 说明。
「采集任务」的原有采集目标下拉提供同一登记资产，保留新建采集、周期任务、执行记录页签；
初始化/增量使用日粒度字段及可编辑的二十日历年默认值，执行记录使用原有列表与右侧详情。
选中日频不会替换父目录或采集工作台；其他资产保持各自原粒度。旧资产/作业深链继续有效。
REGISTERED_MANUAL 表示显式装配的人工能力，不等于包策略启用、已有持有或常驻执行器在线；
持有由本地只读证据独立核验，未知和空库也保留入口，不发来源请求。提供采集数据、查询数据、
执行详情及真实 API 说明。默认当天往前 20 日历年，起止观测日可编辑；init 按来源年度分块，
update 独立刷新用户区间，确认的变化保留版本；无值不补零。SOFR 真实起点短于默认窗口，
双边政策范围出现之前无上下限证据，明确保留覆盖缺口。

- `GET /api/r5/financial/timeseries/status`：默认窗口及十项明确绑定。
- `GET /api/r5/financial/timeseries/query`：start/end/series/as_of/offset/limit，本地分页，固定截止。
- `GET /api/r5/financial/timeseries/jobs`、`/jobs/{request_id}`：任务进度、逐段请求、批次及原件身份。
- `POST /api/r5/financial/timeseries/jobs`：规范 UUID request_id、action=init/update、start/end。
- `POST /api/r5/financial/timeseries/jobs/resume`：已有 request_id，复用同一任务，只续未完成段。
- `GET /api/r5/financial/timeseries/api-help`：入口、字段及语义说明。

所有 GET 不创建来源、不取凭据、不发来源请求、不自动恢复作业。写入需独立显式 Web 参数，
复用私网 Host/Origin/JSON 准入；公开只读 Host 仍拒绝 POST。没有日频策略或常驻 worker。
每个已取得分段先将原始/metadata 字节写入校验内容地址，再记录持久恢复种子，之后确认 PG 数值；
确认不确定时续跑原件而不是再次请求。已完成段与重复 request_id 不重复采集。失败段保留分类和
冷却时间，其他段独立结算；中断不构成自动成功，用户明确续跑后按原作业继续。

## 正常数据层 CLI

```bash
uv run --no-sync python scripts/mdf_financial_conditions.py registry
uv run --no-sync python scripts/mdf_financial_conditions.py migrate --opt-in-dev-write
uv run --no-sync python scripts/mdf_financial_conditions.py collect \
  --start 2026-10-23 --end 2026-10-30 --opt-in-dev-write
uv run --no-sync python scripts/mdf_financial_conditions.py query \
  --start 2026-10-23 --end 2026-10-30 --series DFEDTARL EFFR DGS10 DFII10 T10YIE
```

底层 origin collect 用于八条原机构/派生小窗；信用历史与 FRED 尾段由正常人工任务显式编排。
本段实采入口限 31 天；财政部每次窗口须在同一月份，跨月请求明确失败，不默截断。
二十年人工任务使用上述 jobs 入口分块编排。只重试失败系列可指定 `--series`；单独请求 T10YIE
复用同一存储截止下已有的两腿，不隐式采集缺腿。查询不触发来源 HTTP 或 DDL。
共享政策表/名义曲线在同次 collect 中复用原件，实际请求次数独立于八条指标数。

模块分工：`financial_daily_origin.py` 为具名原机构适配与 MDF 派生；
`financial_daily_source.py` 保留原 FRED 解析/准入及通用有界传输控制；
`financial_daily_store.py` 复用 PG 与 Garage；`financial_daily_service.py` 提供部分结算和纯本地查询；
`financial_daily_runtime.py` 使用现有 DEV PG/TLS、Garage 配置，不改凭据或运行配置。
新原机构绑定不读取 FRED 凭据。端点固定，拒绝重定向，不接受客户端任意 URL。

## 时间、范围、缺项与版本

- 原始观测、政策生效投影、MDF 派生分别标识；派生不是 FRED T10YIE 收到值，也不是纯通胀预期。
- 本次回补的历史值不证明初值。逐条实际来源公布/可得时刻未证明时为 null；来源一般发布计划独立展示。
- `observed_at` 为 MDF 首次捕获；`available_at` 为提交后确认可用。默认按后者读取同一截止。
  显式 `basis=observed` 只描述 MDF 观察史，不是官方 vintage；早期截止不借后来数据。
- `response_first/last_observation` 仅为该窗口实际响应；不拿它们当全源历史起止。
  财政部公告的起点保留年精度，全源最后可得日未证明为 null。
- 请求结果、失败原因和 metadata 仅作用于相应请求日期；后来的窄批次不能污染旧窗口。
- 周末非观测、明确来源缺值、未采集、请求失败、提交未知、材料范围未证明分别处理，缺失不补零。
  工作日假期未核实时缺行仍为 `not_returned_calendar_unconfirmed`，不凭缺行推定休市。
- 同内容重放不增经济版本、不重置首次时间；值/缺值变化保留版本。政策实际生效起点变化、
  派生两腿版本变化即使差值相同也保留语义新版本；两腿必须同日、在所记录统一截止可用。
- 原始 HTTP 字节先进入校验的 Garage 内容地址，再做语义解析。每批 metadata 原件独立定位。
- 确认中断的当前值为 `commit_pending`，旧值仅作 `last_confirmed`。`recover --batch-id ...`
  从原批次原件恢复，不发新来源请求、不改首次捕获时间。

## 验证边界

确定性回归：

```bash
uv run --no-sync pytest -q tests/test_financial_daily.py tests/test_financial_daily_origin.py
```

私网测试需要显式双重 opt-in，默认云 CI 排除：

```bash
MDF_FINANCIAL_DAILY_DEV_OPT_IN=1 uv run --no-sync pytest -q \
  tests/test_financial_daily_dev.py --live-provider -m 'live_provider and postgres_integration'
```

测试字节/受控修订只能写独立 `test_financial_daily_*` 命名空间；与产品实际官方样本分开。
本段不改变旧十族策略、进程、配置或 DEV 绝对截止，不以历史初始化冒称自然发布验收。

默认人工窗口以美国东部当前观测日为终点，向前二十个日历年；它不是“当日已公布”的保证。
显式请求跨过该观测日时，原机构通路只取得同段已可采的历史，保留原作业请求区间及实际取得区间；
未来尾部或来源尚未提供的日期留缺，后续增量补齐，不把整段历史归为失败。
金融资产复用正常详情的展开/收起、查询/API/更新记录页签；采集使用共用日历与 DEV 确认、执行简报，
执行详情与其他宏观资产共用身份、范围、北京时间和结果字段。更新记录表示 MDF 采集/确认，不冒称官方发布事件。
