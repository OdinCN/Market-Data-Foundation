# I-2 R1 — Source discovery candidate / Owner-local

绑定 Session #15 MDF Product Control / MDF-DEV / Owner-local；基线
`e8faac58b8d97594109ed2dc63ea110afb36cfce`。只改 I-2 写集。
本候选未发布、未 adoption、未安装 timer，无 PG/Garage 写入。

## 实际结果与 effect ledger

| 区域 | 本次证据 | 结论 / 下一门 |
|---|---|---|
| 新离线工程 | `tdx_market_*`、独立 Go 研究 Source、Host CLI、一次性 unit 模板 | deterministic / Go race 候选；集中独立 Host QA 另行执行 |
| 沙箱真实尝试 | `artifacts/i2-after-close-quote-run-r1/`；新 run `d0a48afe0b2044f6872b7c4ec79a0874` | 单次 TCP probe 为 `access_restricted`；0 connection attempts / 0 GetQuote / 0 batches；执行环境受限，不判 Provider 失败 |
| 具名 Host 盘后 retained | `/srv/market-data-foundation/artifacts/mdf-i2-afterclose-snapshot-20261022-r1/`；原 run `56785b1c49fa46749708114142c98b0c` | 原报告 2 GetQuote / 2 Go ACK；新离线 intake 不复用该 run 的执行额度 |
| 独立 retained 核对 | `artifacts/i2-retained-host-intake-final/`；inspection `7b2a722559b04559b04120d8c0ffb1c7` | config / 原始 IPC / receipt / final report digest 与重建一致；当前读回 stale；新增 Provider 请求 0 |
| 新目录 / 历史日线 | `artifacts/i2-build-final/source.build.json` | 新研究入口已离线编译和 race 检验；真实执行待 Host 独立审查候选 digest，未声称实际下载成功 |
| Universe / Calendar | 有界 partial/closure、独立日期/session gate 和 D1 端口 | 当前完整成员、历史成员、身份映射、交易所日历均 UNKNOWN |
| 明日真实刷新 | 2026-10-23 09:35+ / 13:30+，SH/SZ 分别日历 intake | 尚未执行；须 Host 审查、独立日历材料和新 run；仅提供 unit 候选 |
| 持久层 | 本地 ignored artifacts exact readback | PG writes=0 / Garage writes=0；I-3 的 DEV 准入不由本线代行 |

Host retained 证券为 `sh600519 / sz000001 / sz000002`，实际两轮本地接收时刻为
2026-10-22 18:07:52.954306235 / 18:07:59.947027614 北京时间。
两个 IPC digest 不同，但排除 SDK 本机 `Kline.Time` 后，经济内容 digest 相同；
逐证券比较没有经济字段变化，`content_refresh=NOT_PROVEN`。
`source_event_time/source_freshness/stream_live_qualification` 均 UNKNOWN。
这些数据是闭市后读到的最新已知结果，不代表 2026-10-22 来源事件发生时间。

沙箱真实尝试的早期 wrapper 曾记录 `COMPLETED_BOUNDED_OBSERVATION` / exit 0，
它只代表有界过程已返回；同一不可变目录里的 quote report 明确为
`connection_budget_exhausted / unavailable / 0 batches`，不是任何 live PASS。
已修正 final candidate：无完整 readable batches 时返回 `STOPPED_CLASSIFIED` 和非零退出；
新增实际负例覆盖。原收据不改写，不重跑或回填此耗尽 run。

## 文件清单

- `src/market_data_foundation/qualification/tdx_market_plan.py`：冻结 source/endpoint/请求形状/品种/预算，SDK endpoint 只读核验、独立日历窗口门。
- `src/market_data_foundation/qualification/tdx_market_collect.py`：显式 one-shot、sealed 候选执行、原始请求/响应与 projection 分层、effect ledger、run 去重、失败 partial stdout 保留。
- `src/market_data_foundation/qualification/tdx_market_evidence.py`：A-STD retained admission、目录闭合/partial revision、native 日线质量发现、D2 `Page` 端口和 quote 同证券字段差异。
- `src/market_data_foundation/qualification/tdx_market_retained.py`：具名 Host manifest 只读 intake、receipt/report 两路重建、原时钟和当前 age。
- `scripts/mdf_i2_source.go` / `scripts/mdf_i2_source_test.go`：独立协议研究 Source / net.Pipe、预算、截断、decoder panic、race 测试。
- `scripts/mdf_i2_build_source.py`：固定 SDK v1.0.0 / Go 1.25.3、离线构建、独立 build manifest。
- `scripts/mdf_i2_collect.py`：`plan / collect / intake-quotes` CLI。
- `scripts/mdf_i2_observe.service.in` / `scripts/mdf_i2_observe.timer.in`：Host 审查用一次性模板，未安装。
- `tests/test_i2_market_source.py`：安全限额、时钟/日历、目录、D2、留存恢复、零结果等负例。
- 本文、`RUNBOOK.md` 及 `QA.md`。

## 语义依据、合同与共享差异

只读核对 pinned SDK `v1.0.0@2a9000681f5a9896ea98e4de8bc4904f1dce78ba`：
`hosts.go`、`client.go:GetCount/GetCode/GetKline/GetQuote`、
`protocol/frame.go`、`model_count.go`、`model_code.go`、`model_kline.go`、
`model_quote.go`、`unit.go:GetTime`、`types.go:TypeKlineDay`、`workday.go`。
研究 Source 仅 import 该 pin 的 **protocol package**，直接执行冻结帧，
离线 builder 把 pinned hosts.go 的地址集合和摘要编入候选，未知 IP 即使直接启动
候选也被拒绝；父进程还会独立核验冻结端点和同一 hosts.go。
不初始化 SDK Client、Workday、SQLite、cron、自动 hosts 排序或任何隐式 redial。
因此它是独立待审研究候选，不冒充原 Driver，也不需要修改 waiter/Driver 信任根。
Quote 使用原 `FramedProcess` / `verified_driver`，v1.0.0 原 pin
`13cfe86189ea2309bf1dc6d7c096c390021195b577f804d90f18f0c4c64a0a93` 保持。

研究 Source 仅执行 SH=1 / SZ=0 的 GetCount/GetCode、股票 decoder 的
TypeKlineDay=9 GetKline；GetCode 固定 1000 行，Kline 最多 800 行。
不调用 GetCodeAll/GetKlineAll，不使用 BJ 报告替代路径；ETF/指数/板块日线、
BJ、HK/期货/ExHq 未由本线获得资格，留待 I-4 或新显式能力决策。

`directory_inputs` 返回已有 A-STD typed responses / PageAssembly，能直接交给
既有 `snapshot_universe`，但 MemberSelection、独立稳定身份/类别证据和 D3
MaterialLink 必须由集成方提供。未用代码前缀编造全市场或历史 Universe。
`daily_page` 需同一 Source/Binding/implementation/endpoint、原请求代码/offset/count、
明确 retained line digest、实际 known_at 及每行本地日期证据；返回 **derived** Page，
raw wire 引用独立留存。无传输、补采或 Publication 副作用；单位、事件时钟、复权和
分页终止均 UNKNOWN，D2 不能把它当完整历史成功或任意窗口覆盖。

Calendar gate 是 Host 已独立审查材料的 digest intake，不验证公告真实性，
也不自动升格 A-CAL / D1 CalendarRevision。需 SH/SZ 各自 OPEN/session 证据，
精确日期、reviewed/expiry、原材料 SHA；UNKNOWN、局部材料、闭市、午休、
日界或预算跨收盘一律拒绝。未用 Workday.Is(false)、无 bar 或周末推导闭市。

共享改动建议：原 pinned quote Driver 没有 socket-response byte counter/cap，
本候选明确记录 quote IPC 上限和请求 bytes，**quote transport raw response bytes UNKNOWN**；
它保留的 quote raw 是原始 IPC/source_projection，不是 TDX socket raw。
若集中审查要求 quote 也有 hard socket byte budget / 原始协议响应，须由单一集成人
对 Go main 新候选实现、重新 QA/pin。I-2 没有改原 Driver 来绕过此门。

## 验证与交付状态

使用现有 uv-managed Python 3.14.8，不改系统 Python、pyproject 或 uv.lock。
新本地 venv 的 offline sync 因缺缓存包未完成；检查实际使用
`UV_PROJECT_ENVIRONMENT=/srv/market-data-foundation/.venv` / `uv run --offline --no-sync`。
已执行 I-2 38 项专项及 346 项合同/runtime 定向回归、全仓 Ruff/check/format、
含 untracked 的 repository hygiene，以及独立 Source Go `-race` 测试和离线 build。
全量 pytest 在既有 ASGI/AnyIO HTTP 测试阻塞，已中断，不能记全量 PASS；
具体有界复现及最终门禁见 `QA.md`。
无 Git commit/push、CI、部署、运行激活或 timer 安装。集中 Host full QA、独立专业复核、
同字节集成/Git/CI 和 AO 裁定均为下一 gate。


## 已冻结但未执行的下一轮候选

- 盘后 Source：`artifacts/i2-after-close-plan-final/plan.json`，run
  `560484e8369f40a1b790ab20e83d592c`；2026-10-22，14 Source calls + 1 quote。
- 明日 AM：`artifacts/i2-20261023-am-plan-final/plan.json`，run
  `c373a88b737f4ecbb6b137a954f2cc4a`；2026-10-23 AM，Source calls=0 / quote rounds=3。
- 明日 PM：`artifacts/i2-20261023-pm-plan-final/plan.json`，run
  `7e21c45dbde74766bdc13035b749e9f0`；2026-10-23 PM，Source calls=0 / quote rounds=3。
- 最终独立 Source 二进制：`artifacts/i2-build-final/source`，SHA256
  `d046a7b0a13b73d726bab9f679ffef0fad650697dcff752018fec47eb4999623`。

这些计划未消耗 Provider 额度，也没有被安装为 timer。Host 必须独立核对候选字节、
预算和相应日历材料；after-close 日期或 AM/PM 窗口失配即拒绝，不能延迟到另一窗口
继续运行旧计划。若日期已过，重新作显式短程决策和新计划，不改旧计划/旧运行证据。
