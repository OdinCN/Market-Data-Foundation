# Fuyao R1：最终离线裁定；evidence collection COMPLETE

产品只读 baseline：`175d1a0037312ebe734b73763f601d2469b0bb01`。本次裁定 fresh-read 实际证据，新增 HTTP 请求 **0**。
Evidence collection **COMPLETE / 24 total HTTP attempts**：r01 为外部既有单次请求，
r02–r16 与 r18–r24 均各单次 HTTP 200 / business code 0 / COLLECTED。
r17 单次 HTTP 200 / business code 3002，最终状态 `UNRESOLVED_DATA_NOT_READY`，没有重试。
r18–r24 已 tested and collected；24 次 live attempts 已关闭，**no further provider requests authorized**。
成功传输与业务回执不自动构成 capability qualification；历史 r17 STOP 账本保持原字节。

3002 的“数据尚未准备”语义来自 AO 已接受的官方研究，本次未联网。
保留 r17 的冻结 scope：600000.SH、2024-01-02..2024-01-10。
仅有 attempt-17/ledger 失败回执，成功 metadata.json/data.json 不存在；失败账本未保留
provider request_id，摘要明确为 null，不猜造该标识。已有 r01–r16 provider request_id
保留在 ignored 摘要中。本事务没有重试、补零或模拟；当前不授权任何进一步 provider 请求。

唯一资格实现/访问路径为 `fuyao-single-attempt-https-r1`：固定目标 Fuyao HTTPS GET → stdlib
外部单次执行器，Python **3.14.8**。`@hithink-tech/hithink-finance-cli` **0.1.13**
仅作公开能力/schema 发现证据（Node v24.21.0），不是第二个实测 transport 实现。
**没有 Primary binding、production adapter、deployment 或 DB effect，交付未开始。**

## 能力矩阵

QUALIFIED_WITH_LIMITATIONS 仅表示本文限定样本用途；UNRESOLVED 表示证据不足或语义未成立。
NOT_SUPPORTED 仅适用于当前明确命名的公开 A 股远端访问路径，不是供应商全局能力判断。
NOT TESTED 是执行状态，不作为新的最终 disposition；没有任何 QUALIFIED_CANDIDATE。

| 能力 | 最终 disposition | 依据与边界 |
| --- | --- | --- |
| security.universe | UNRESOLVED | r02–r07 共 12 行、仅 4 个不同 SH 标识；SZ/BJ 条件仍返回 SH。offset 0/2 可见不同小页，但过滤语义与三市场覆盖未通过；无全市场完整性或分页耗尽证明。 |
| security.metadata | QUALIFIED_WITH_LIMITATIONS | r08/r09 精确匹配 600000.SH/000001.SZ，ticker/name/exchange/a-share/CNY 可用，可显式映射 XSHG/XSHE；r08 list_date=null，r09 为 1991-04-03。缺上市/退市/停牌状态完整性，限两样本身份参考。 |
| quote.snapshot | QUALIFIED_WITH_LIMITATIONS | r10 两标识、total=2；OHLC 边界、涨跌差及百分比算术通过。仅静态字段合理性，成交量/额单位未独立证明，无逐证券交易日期、盘口深度或实时新鲜度门禁。 |
| bars.daily | QUALIFIED_WITH_LIMITATIONS | r11/r12 各 7 行、2024-01-02..2024-01-10；身份、1d/none 标签、日期升序唯一、请求窗、有限非负值及 OHLC 通过。时间戳为上海午夜；单位、独立价格真值、长历史及停牌完整性未建立。 |
| bars.intraday | NOT_SUPPORTED | 仅当前固定 0.1.13 公开 A 股远端访问路径未暴露分钟线；本轮 0-call/NOT TESTED。期货/期权 intraday 属不同资产范围，不推断 Fuyao 全局不支持。 |
| trades.intraday | NOT_SUPPORTED | 仅当前固定 0.1.13 公开 A 股远端访问路径未暴露逐笔成交；本轮 0-call/NOT TESTED，不推断其他接口或供应商全局不支持。 |
| corporate_actions | UNRESOLVED | r17 单次 HTTP 200/business 3002=数据尚未准备，child_stop_reason=business_failure；仅失败账本，无 metadata/data 或事件行。scope 已保留，本事务不重试、不补零、不模拟。 |
| sector.industry | QUALIFIED_WITH_LIMITATIONS | r21 industry tag 实测 320 个唯一 thscode/name；r22 选中 catalog 中 881101.TI（种植业与林业）。仅 THS taxonomy bounded sample；无层级、全量完整性、历史版本或 TDX 映射证明。 |
| sector.concept | QUALIFIED_WITH_LIMITATIONS | r23 cn_concept tag 实测 390 个唯一 thscode/name；r24 选中 catalog 中 885311.TI（智能电网）。仅 THS taxonomy bounded sample；无全量完整性、历史版本或 TDX 映射证明。 |
| sector.membership | QUALIFIED_WITH_LIMITATIONS | r22/r24 分别返回 881101.TI 的 29 行和 885311.TI 的 278 行；thscode/ticker/name 完整且身份唯一。仅 THS taxonomy bounded sample；响应无组身份回显、权重、有效日期或历史成员证明，不证明全市场覆盖。 |
| adjustment.factors | UNRESOLVED | 无远端无量纲因子证据；corporate-actions path 含 adjustment-factors 不证明因子能力，且 r17 无数据。market.adjustment-factors 仅 local，不能作为 remote 资格。 |
| bars.adjusted | UNRESOLVED | r13–r16 与 r11/r12 日期一致、OHLC 均变化、volume/turnover 逐行相同。仅证实模式差异；事件、锚点、算法及跨事件连续性未证明，forward/backward 不等同已资格 qfq/hfq。 |
| finance.snapshot | UNRESOLVED | 0-call/NOT TESTED；market.snapshot 是行情快照，不是财务快照；r18–r20 三表实测不能推导 indicators/valuation 或财务快照能力。 |
| finance.f10 | UNRESOLVED | 0-call/NOT TESTED；无 F10 实测接口/文本证据，symbol metadata 或三表均不能替代。 |
| financial.statements | QUALIFIED_WITH_LIMITATIONS | r18/r19/r20 三表分别各 2 行，600000.SH、annual/FY、2025/2024、CNY 及有限数值/显式 null 结构通过。仅返回三表结构样本；report_date_ms 不认证为公告日，publication/update/unit 字段未返回，单位、修订、时点可用性和独立真值未证明。 |
| calendar.trading_days | QUALIFIED_WITH_LIMITATIONS | r01 241 个升序唯一日期，2025-10-09..2026-09-30，仅约一年实测范围。date_ms 对应上海午夜，UTC 日期为前一天；不可当作开收盘时间，不证明历史节假日、多市场日历或官方完整性。 |

## 实际字段与日期裁定

r02/r03 请求 SH，r04/r05 请求 SZ，r06/r07 请求 BJ；各 market 均 offset 0/2、limit 2。
六页共 12 行，去重仅四个 SH 标识：600000/600004/600006/600007。
SZ/BJ 四页 exchange 均为 SH；不能把请求中的市场写为已观察覆盖。
各 offset 返回不同的两行，但没有 count/尾页证明，complete/exhausted 保持 null。
r08 的 600000.SH list_date=null，而 r02 有 1999-11-10；不能用列表静默填补搜索缺失。
r09 的 000001.SZ list_date=1991-04-03；两者 end_date/last_trade_date/last_delivery_date
均 null。MIC 映射只限响应确认的 SH→XSHG、SZ→XSHE，没有 BJ 身份资格。

r10 正好返回两只目标股票，total=2。价格边界、last-prev=change 与 change/prev 的
百分比算术通过；不证明官方真值。provider data.timestamp 为
`2026-10-03T14:34:44+00:00`，没有逐证券交易时刻/日期，
不能把响应时间当成实时新鲜度。volume/turnover 数值与价格量纲相容不等于独立单位证明，
normalized units 留空；没有盘口深度资格。

r11–r16 每段 7 行，上海日期为 2024-01-02/03/04/05/08/09/10，均升序唯一、
在冻结请求窗内，OHLC 有限非负且 low≤open/close≤high。
历史响应不提供每行 thscode，以顶层身份核对；请求 start/end 为上海本地日界。
date_ms 对应上海午夜（UTC 前一天 16:00），不能用 UTC 日期截断为交易日。
无官方日历/独立行情比对，未证明长历史、停牌状态、缺口完整性或全市场覆盖；不填日期。

r13/r14 forward 与 r15/r16 backward 每只股票分别与 none 对齐，28 个 OHLC 字段
全部改变，7 行 volume 与 turnover 均逐行相同。样本内价格符合仿射关系（非算法证明）：
600000.SH forward≈none−1.151；backward≈8.5768683×none+40.945739978；
000001.SZ forward≈none−2.172；backward≈125.02114200576×none+194.08818810689。
精确残差保留在摘要。窗口内的数值关系不能确定事件依据、起算日、锚点或跨事件处理，
r17 无事件数据；不能据模式名认证 qfq/hfq，也不能从价格关系导出远端因子资格。

r01 实际 241 行，date 字段覆盖 20251009..20260930、升序无重复；公开 schema 的
one-year 与该约一年范围一致，没有支持请求历史 calendar 的参数。
所有 date_ms 都是相应 date 的上海 00:00（UTC 前一天 16:00），与本轮日线相同，
仅为实测日期编码约定；不能直接截取 UTC 日期或视为交易 session 时间。
完整节假日和历史窗口没有独立证明。

### 三表逐表实测

三次请求均为 600000.SH、period=annual、limit=2；各自两行、fiscal_year 为 2025/2024、
fiscal_period=FY、currency=CNY、ticker=600000。period_end_ms 的上海日期分别为
2025-12-31/2024-12-31，六个日期编码都是上海午夜；这只验证编码与年度标签一致。
顶层 timestamp 三表均为 1767110400000（上海 2025-12-31），没有更新或采集时刻语义证明。

- r18 balance-sheet：15 个行字段。assets_total、total_debt、holder_equity_total 为有限数值；
  total_current_assets、non_current_nets_total、cash、accounts_receivable 两行均 null。
  report_date_ms 分别解码为 2026-08-28/2026-03-31。
- r19 income：21 个行字段。operating_income、operating_expenses、operating_profit、
  interest_expenses、profit_total、income_tax_expense、net_profit、parent_holder_net_profit、
  basic_eps 为有限数值；operating_costs、sales_fee、manage_fee、
  research_and_development_expenses 两行均 null。report_date_ms 两行均为 2026-03-31。
- r20 cash-flow：14 个行字段。act_cash_flow_net、invest_cash_flow_net、financing_cash_flow_net、
  pay_fixed_assets_etc_cash、pay_dividends_profits_interest_cash、cash_equivalents_net_addition
  六个值字段均有限、包含合法负数，无行字段 null。report_date_ms 两行均为 2026-03-31。

每行另有 thscode/ticker/period/fiscal_year/fiscal_period/report_date_ms/period_end_ms/currency
八个共同字段。实际完整字段、原始日期数值、空值计数及各行安全数值保留在归一化摘要。
publication/update/date/unit 等独立字段没有返回；不能从 report_date_ms 名字认证公告日，
不能把 timestamp 当更新时间，CNY 不证明金额缩放或 EPS 单位。尤其 2024/2025 的
report_date_ms 相同（income/cash-flow），balance-sheet 又不同，不能据此建立历史时点可用性。
三表仅 QUALIFIED_WITH_LIMITATIONS 的结构样本，不证明完整表项、修订版本、会计口径、
独立财务真值或跨表勾稽；不填补 null。finance.snapshot/finance.f10 均保持 UNRESOLVED。

### THS taxonomy 有界样本

r21 请求精确 tag=`industry`，返回 320 个唯一 thscode/name；r23 请求精确 tag=`cn_concept`，
返回 390 个唯一 thscode/name。tag 是实际请求条件，响应没有 tag 字段；没有 parent/level、
完整性或 taxonomy 版本字段。r22 的实际选中组 `881101.TI`（种植业与林业）在 industry catalog 中；
r24 的 `885311.TI`（智能电网）在 cn_concept catalog 中，不能将 concept tag 写为其他名字。
r22 返回 29 个唯一成员（SH=12、SZ=14、BJ=3）；r24 返回 278 个唯一成员（SH=102、SZ=165、BJ=11）。
各成员只有 thscode/ticker/name，ticker 与 thscode 前缀一致，suffix 为 SH/SZ/BJ。
成员响应没有选中组回显、权重、纳入/剔除日期、历史版本或有效区间，组关联只由实际请求与
对应响应建立。sector.industry/concept/membership 均仅 THS taxonomy bounded sample；
这些成员中的 BJ 不修复 security.universe 的过滤缺陷，不证明全市场或成员全集完整性。
THS 与既有 TDX taxonomy 永远独立，没有映射或互相借用资格。

当前 A 股外部路径没有公开分钟/逐笔；capabilities 中期货/期权 intraday 不属于本样本范围。
本地 market.adjustment-factors 及远端 raw-events path 字样均不能建立 remote factors。
财务快照、F10、三表是独立能力，不能由证券 metadata 或行情 snapshot 推导。

## 证据、可重复性与验证边界

原始响应只保留于授权 ignored 路径：r01 `/srv/market-data-foundation/artifacts/fuyao-r1/live-r01-auth/`；
r02–r16 `artifacts/fuyao-r1/live-r02-r24-001/rNN/`；r17 仅同目录账本/预约回执；
r18–r24 `artifacts/fuyao-r1/live-r18-r24-cont-001/rNN/`。
归一化摘要、evidence-references.json 及 SHA256 manifest（排除自身）位于 `artifacts/fuyao-r1/final-adjudication-002/`。
摘要只保留安全白名单字段与离线字段检查，没有凭据、provider message 或原始异常。
输入缺少 HTTP 时延和采集时刻，所以采用明确命名的规范化证据摘要，不伪造 observation v2
的 latency/observed_at；现有 observation v2 合约与 JSON Schema 保持原字节。
实际连接 IP 未采集，connected endpoint 保持未知；固定 target 只是尝试目标。

Manifest 绑定实际输入、公开 discovery、当前候选源码/测试/报告/矩阵及规范化摘要字节，
排除 manifest 自身。request-plan/fixtures、single-attempt executor 与 TDX 字节保持执行后冻结；
`execution-provenance.json` 保存 r01、r02–r17、r18–r24 实际执行源码哈希。交付前仅对
`fuyao_r1_live_runner.py` 与 `fuyao_r1_continuation.py` 做 `ruff format` 机械格式化；
`post-live-formatting.json` 记录 before/after SHA256，且两文件 AST SHA256 前后分别完全一致。
该格式化发生在 24 次 provider attempts 全部关闭之后，没有新增 provider 请求，也不改写历史执行身份。
plan 里 LIVE NOT STARTED 是历史冻结意图，当前状态以本报告和矩阵为准。
`fuyao_r1.py` 的 pre-live gate 保留零请求行为，不代表外部已经执行的 live 阶段未启动。
本次未读取 secret/TLS/control-root，未发 provider 请求，未修改产品仓库，未 commit/push。

确定性测试验证 16 能力覆盖、报告/矩阵一致、非绑定状态、24 次关闭与 r17 无重试边界、三表/THS 反例、
日期/过滤/复权反例及 JSON Schema。测试不依赖本地 live artifacts；实际证据审计另行离线执行。
canonical Python 3.14.8 的 Fuyao、generic qualification/schema、broad non-live pytest、ruff、
format、hygiene、frozen 字节和产品 baseline/clean 的实测结果记录于本事务
`adjudication-finish-003/RESULT.md`，不把确定性测试当成新增 live 证据。
