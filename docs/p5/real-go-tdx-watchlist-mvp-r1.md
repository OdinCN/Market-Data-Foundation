# 非生产 Go/TDX Watchlist MVP

本实现对应 `MDF-P5-REAL-GO-DRIVER-MVP-R1` 修订授权。EO 仅做离线
实现、确定性 QA 和本地编译；Provider 首次运行与实测账本由 PM/Host 调度。
新 CLI 每次必须显式传 `--live`，导入模块、编译和默认 pytest 均不连接 Provider。
没有 daemon、交易、部署、生产启动或自动续跑。

实际链路是 `drivers/tdx/main.go` 的 pinned `tdx.DialWith`/`GetQuote(symbols...)`
→ 单子进程长度帧 → Python `FramedProcess` source adapter →
`RealPeriodicWatchlistSession` → `WatchlistIngress` → 共用
`StockSnapshotCandidate` 原解析器/状态机 → 全成员 exact-batch Read → SHA/ACK。
Go 负责请求 cadence，Python 负责可见性与背压。旧 offline adapter/session
仍保留原入口；旧 schema 默认仍是两证券、固定 endpoint、最多三轮。
新 session schema 是 `mdf.go-tdx.watchlist.session.r1`，动态配置与旧模式分开。

## 构建（只编译和 mock，不发网络请求）

Python 使用仓库 uv 管理的 3.14.8。SDK 固定为
`injoyai/tdx v0.0.87@51c1ed51020fa43727300339dd92cbecf06a7d2e`，Go 固定 1.25.3。
原 SDK checkout 完全只读；临时 overlay 对该 revision 的 `client.go` 应用
`drivers/tdx/sdk-wait-preregister.patch`，派生标识为 `+mdf-wait-preregister-r1`。
固定依赖的 waiter `GetAndDel` 并非原子读删；派生版现在使用同一把
`mdfWaitMu` 保护预注册、Reader `Done` 和超时/写失败清理的完整回调生命周期，
避免回应与清理并发执行同一 waiter。新增 `net.Pipe` 近超时回复交错回归，
并通过 `go test -race`；原 pinned SDK checkout 保持未修改。

不升级依赖、不改系统 Python，没有 Python 依赖变更。旧两股票/三轮 schema
仍接受原版 SDK 标识；新 session 明确要求派生标识，不能冒充未打 patch 原版。

已有 qualified Host 可以执行：

```bash
MDF_SDK=/var/tmp/mdf-tdx-live-20261011/release-v0.0.87
MDF_GO=/var/tmp/mdf-tdx-live-20261011/toolchain/go/bin/go
MDF_MODULE_CACHE=/var/tmp/mdf-tdx-live-20261011/pkg/mod
UV_CACHE_DIR=/tmp/mdf-mvp-uv uv run --offline --no-sync python scripts/build_tdx_driver.py \
  --sdk-checkout "$MDF_SDK" --go "$MDF_GO" --module-cache "$MDF_MODULE_CACHE" \
  --output artifacts/tdx-mvp-build/tdx-driver
```

这些路径仅为宿主示例，可全部替换。产品源码没有固定 checkout 路径。
另一台主机需要先由维护者准备该 revision 的 SDK checkout、Go 1.25.3 与
SDK `go.mod`/`go.sum` 所列依赖的模块缓存；构建脚本不下载依赖。
脚本核验 checkout HEAD、tracked 源码未修改、原 `client.go` 与 patch 输入的
完整 SHA-256；只将 tracked SDK 文件复制进临时 overlay，私有 untracked `cmd/`
不参与构建。patch 先 `git apply --check`，再只应用到 overlay。
临时 driver 模块复用 SDK `go.mod`/`go.sum`，local replace 指向派生副本，
执行 `go test -count=1` / `go build -mod=readonly`；可加 `--race` 检查竞态。
输出旁的 `tdx-driver.build.json` 记录上游 revision、patch/输入/派生源码身份、
构建状态、检测模式及实际编译后才计算的二进制身份；失败标记为 failed。
二进制最终 SHA 不写进源码说明书。
`GOPROXY=off`、`GOSUMDB=off`、`GOTOOLCHAIN=local`；local replace 不写回仓库或 SDK。
Go 输出仅放忽略的 artifacts/。普通 Git/Hosted CI 交付随后由 PM 管理。

## Host 显式启动短会话

**下列命令会触发真实 TCP 探测和真实行情请求，只有 PM/Host 明确启动才执行。**

```bash
UV_CACHE_DIR=/tmp/mdf-mvp-uv uv run --offline --no-sync python \
  -m market_data_foundation.runtime.tdx_watchlist \
  --live --driver artifacts/tdx-mvp-build/tdx-driver \
  --watchlist sh600519,sz000001,sz000002 --period 12 --rounds 3 --duration 60
```

默认 watchlist 为原两证券，但并未锁死；支持 1–32 个不同沪深 A 股代码
（`sh6xxxxx`、`sz0xxxxx`、`sz3xxxxx`）。ETF、指数、北交所和其它能力未纳入本 MVP，
不会调用其它 Provider 补齐。CLI 可覆盖 `--watchlist`、`--endpoints`、
`--period`、`--rounds`、`--duration`、`--output`；所有其它值通过 `--config` JSON 设置。
例如在忽略的 `artifacts/` 下自行保存：

```json
{
  "securities": ["sh600519", "sz000001", "sz000002"],
  "endpoints": [],
  "sdk_candidates": true,
  "sdk_offset": 0,
  "max_probes": 4,
  "max_connections": 3,
  "max_failovers": 1,
  "rounds": 3,
  "period_seconds": 12,
  "duration_seconds": 60,
  "dial_timeout_seconds": 2,
  "quote_timeout_seconds": 3,
  "ack_timeout_seconds": 3,
  "ttl_seconds": 30,
  "start_at": ""
}
```

`start_at` 可设为未来五分钟以内的 UTC RFC3339 时间；空值表示立即开始。
session 结束时间为 start + duration，选点、握手、请求、等待 ACK 都消耗该时间。
没有自动判定交易日/开市；需要指定开市窗口时，由 Host 选择 start/duration。
今晚的返回只可证明连接/末次快照，不能证明开市活跃更新。

| 配置 | 默认 | 硬边界 |
|---|---:|---|
| securities | 原两证券 | 1–32，不重复，全批原子接受 |
| period_seconds | 12 | 1–60 秒，请求开始间隔，ACK 迟到不 catch up |
| rounds | 3 | 1–120，采样尝试上限；SDK 前置失败也消耗一轮 |
| duration_seconds | 60 | 1–900 秒，包括所有 Source 工作 |
| endpoints | 空 | 至多 8 个唯一 host:port |
| sdk_candidates / sdk_offset | true / 0 | 可关闭 SDK 补充候选；offset 0–255 |
| max_probes | 4 | 1–8，单轮顺序 TCP dial，成功即关闭 |
| max_connections | 3 | 1–8，SDK 连接/握手尝试总额，与 probe 分开 |
| max_failovers | 1 | 0–3，GetQuote 故障后的新 observation segment |
| dial/quote/ack timeout | 2/3/3 | 各 0.1–10 秒，且不能越过 session deadline |
| ttl_seconds | 30 | 1–300 秒，只表示本地收到后的 age |

候选先取显式 endpoints，再从 pinned SDK `Hosts[sdk_offset:]` 填到 max_probes。
只探测该有限集合，绝不调用扫描全 Hosts 的 SDK `SortHosts` 或枚举 IP 段。
探测结果记录真实 TCP dial elapsed 和分类错误，按成功 dial 延迟排序。
SDK 建连按排序择优，某 endpoint 拒绝/失败后不再次请求它；有限 fallback 仍属
同一 pinned TDX Provider。若只允许自己的候选，设置 `sdk_candidates:false`。
最大 TCP dial 次数为 max_probes + max_connections；握手/quote writer 也有总额。
TCP probe 成功不等于握手或 quote 成功，延迟亦不是交易所行情延迟。

## IPC、故障与来源

帧格式：`kind:1 byte + payload_length:4 bytes big endian + UTF-8 JSON`。
Go 出站仅 C（成功连接身份，含 socket RemoteAddr peer）、B（冻结 batch）、E（分类事件）；Python 入站
K（一次 config，最多 16 KiB）和 A（逐轮 ACK，Go 最多 8 KiB）。Go 出站
最多 128 KiB；Python 读头后检查长度，拒绝截断、非法 kind、重复 JSON key、非有限数。
不会把 stdout 当日志；依赖 stderr/原始异常文本被屏蔽，观察只保留分类。

B 的 requested/received 环绕完整采样操作（包括 SetDeadline、SDK 调用及有限收尾），
frozen 在返回的 SDK decoded rows 已冻结后。不会将它们称为精确 SDK 调用时钟。
`quote_attempt` 只表示采样前置事件；`sdk_getquote_entered` 在通过 SetDeadline 后、
实际进入 GetQuote 的同一 worker 边界记录。`protocol_write_attempt` 记录 quote
协议 Write wrapper 尝试，随后独立记录 complete / partial / failed 及 requested/written
字节数；返回错误时即使 written 等于 requested 仍记 failed，partial 严格少于 requested。
门控拒绝或零字节失败不会计作完整写入。握手和 probe 不计入 quote 写入账本，
完整 Write 也不证明服务端收到或处理。边界事件在内存中有界记录，worker 返回后由
唯一 IPC writer 按顺序发出，避免并发帧交错。
Python 独立采集 known 与 Read UTC 时钟；real 模式拒绝调用方覆盖 known/Read。
B 使用 `real_go_sdk_decoded_quote` 和 `local_probe_wall_clock_utc`，Python known
为 `local_ingress_wall_clock_utc`。QA 明确使用 `author_created_offline_fixture`，
不能将真实 quote 改标为 fixture。报告复用 qualification 的 `quote.snapshot`
capability / session Fixture ID / 分类 Observation 约定。

每一 B 必须进入共用解析器/状态机，再读取所有成员的同一 batch。成功/重复批
的所有 Read 必须 observed；明确 Source 失败批的 Read 必须 unavailable
（可能保留先前值，绝非新有效行情）。这些 Read 成功后，Python 才发 A；A 指
**消费已完成**，并不宣称失败行情有效。解析错误、缺成员、Read 失败或超 TTL
拒发 ACK，Go 在 ACK timeout 结束，不再执行 GetQuote，也不借此换节点。

A 绑定 session_id、round、segment、冻结 B 原字节 SHA-256、known、每个成员
Read 时间和 accepted。Go 核验全部身份、SHA、时间顺序/非未来与成员数；合法
ACK 后才写 `ack_received` 并进入下一轮。Python 报告区分 `ack_sent` 与
`go_ack_received`；下一 quote_attempt、连接尝试与 B 之前都要求前轮 Go 回执。
C 的 connection.attempts / endpoint / segment 必须严格匹配尚未完成的
connection_attempt 事件；connection_failed 消耗该前置事件。quote 事件只属于当前合法
segment。正常完成必须为 `complete + budget_exhausted`、完整配置轮数及全部 Go ACK；
`stopped` 搭配 budget_exhausted 或矛盾 complete 均拒绝。SHA 是字节完整性标识，
不是交易所认证或来源的密码学证明；运行的是经核验源码构建的本地可信子进程。

quote 的 timeout/disconnected/source_error batch 同样记录、读取为 unavailable
并 ACK，然后关闭连接；观测 elapsed_ns 保留原值，按配置 quote/dial timeout 加
固定 250 ms 收尾容差校验（覆盖 100 ms worker join 及有限调度/关闭开销），
超过这个有限界限仍拒绝。旧固定 schema 的 10 秒校验不变。
下次仅在预算内选择未用候选，生成新 segment 和基线。
重连后的初次成功采样是新 segment 的 sample，即使价格和之前一样。
恢复可以重新安排 request cadence，明确中断连续性，不伪造 gap-free 数据。
每轮以请求开始 + period 排期；普通成功轮若 ACK 导致超时排期则终止，避免突发追补。
SDK 自动 redial 关闭；使用 R2 相同的 dial 内 handshake-only OnConnected
覆盖技巧，不启动 heartbeat writer。握手和 quote 的 socket deadline 均取
操作 timeout 与 session deadline 的最小值。关闭时先关闭底层 socket，让 SDK reader
自行退出、清理缓冲区，并最多等待 100 ms；避免并发 CloseAll 提前回收仍在读取的
SDK 缓冲区。quote worker 无法及时退出时整次会话停止。ACK timeout 关闭读管道并
结束 reader；父死亡 Linux PDEATHSIG/硬 watchdog 终止子进程。Python 始终关闭
管道并 wait，必要时 terminate/kill + reap，没有后台进程残留。

## 本地结果与 QA

CLI 只在新的 `artifacts/tdx-mvp-<session-id>/`（或显式 artifacts 子目录）保存
`config.json`、未经重写的 `batch-N.json` 和 `report.json`；目录 0700、文件 0600。
新的 [latest accepted Read](watchlist-latest-read-r1.md) 还在 Go ACK received 后
原子维护 `read-ready.json`，供运行中消费者只读使用；状态失败会阻断旧值的健康返回。
文件 fsync 后再 replace receipt，并 fsync 目录；Reader 不执行这些生产端写操作。
stdout 仅输出终止原因、读批数、本地快照健康状态与路径。`report.json` 提供每轮来源 endpoint、
segment、实际 socket peer、四个本地时钟、状态/重复/错误、close_cny、local_received_age、
local_known_age、真实 quote_elapsed_ns、派生 SDK 身份和 ACK sent/receipt。
报告使用 `tcp_probe_result_events`、`connection_attempt_events`、`quote_sampling_attempts`、
`observed_sdk_getquote_entries`、`observed_quote_protocol_write_attempts` 和分结果计数。
移除误称 actual_getquote_attempts 的旧字段；前置 attempts 不冒充完整请求写入。
所有原始 quote、观察和二进制均被 Git 忽略，不可提交。内存最多 120 个 batch
观察及 bounded state records，无数据库或 LAN 服务依赖。

```bash
UV_CACHE_DIR=/tmp/mdf-mvp-uv uv run --offline --no-sync pytest -q
UV_CACHE_DIR=/tmp/mdf-mvp-uv uv run --offline --no-sync ruff check .
UV_CACHE_DIR=/tmp/mdf-mvp-uv uv run --offline --no-sync ruff format --check .
UV_CACHE_DIR=/tmp/mdf-mvp-uv uv run --offline --no-sync python scripts/check_repository_hygiene.py
```

Go mock 覆盖有限候选/真实测量方法的延迟排序、错误分类、SHA/ACK 身份拒绝、
超过三轮的背压、断线新 segment 和 framing。Python synthetic pytest 覆盖
动态成员原子解析、重复/故障/本地 age、real/fixture 分离、Read 失败不 ACK、
实际子进程碎片帧/ACK、1/32 成员边界、超长帧、timeout 清理与 opt-in。旧测试仍属于回归套件。
Go 单独编译不触发任何 Provider 请求。

原 R1 盘后两轮真实 Source → Ingress → Read → Go ACK 及原 R2 历史保持原身份；
先前盘后第二轮 duplicate 不证明盘中活跃刷新。2026-10-15 北京时间
10:05:46–10:06:26，PM 已在正常交易时段以派生版 Driver 完成新的三证券九轮
真实非生产采样：每轮真实 Ingress/Read/GO ACK 都成功，全部 27 次读取
`observed/nonstale`；独立 Postflight 对比冻结 SDK 解码 Close、Amount、Volume
确认三证券均有盘中内容变化。有限三个候选节点按实测 TCP 探测延迟择优，
本次实际三次探测、一次 SDK 连接、九次 GetQuote 和九次 ACK，
Go 正常终止。原始数据仅在忽略的私有 artifacts 中；Postflight 由
MDF owner-local controls 独立保存，不进入产品 Git。
这仍不证明交易所事件时间、全天无缺口 SLA，也没有发生真实物理故障切换。
`source_event_clock`、`source_freshness` 始终 UNKNOWN，SDK Kline.Time 是客户端
采样时间、ServerTime opaque，不能以它们推算交易所延迟。
独立专业复核、普通 Git/Hosted CI 与任何生产事项仍由 PM 后续掌管。

## 本次审查修正的离线验证

P1 只修正 SendFrame 的等待顺序：`Wait.Async` 在 Write 前同步注册，单次回调写入
容量 1 的非阻塞通道，独立 timer 管理超时；成功、Write 错误及超时均通过 Done
移除等待键并清理请求缓存。没有 sleep、额外重试或加长 timeout。
真实 pinned SDK + net.Pipe 测试强制有效响应已被 SDK 解码/分发后 Write 才返回；
未 patch SDK 在第 1 轮确定性超时失败，派生版连续 32 轮成功。另测超时/Write 错误
清理、握手 session 边界、SetDeadline 前置失败不计 SDK、部分写入及失败结果。

Go 1.25.3 的离线单元测试（含新增等待/回应竞争测试）、`go test -race` 与派生 binary 编译通过。
两次独立临时 overlay 构建的二进制逐字节一致。Python focused mock + Ingress
为 156 passed；完整默认回归为 2468 passed / 4 failed / 17 deselected，3 warnings。
只剔除四项 ptrace 限制后为 2468 passed / 21 deselected，3 warnings。
实际日志保存在 ignored `artifacts/tdx-mvp-correction-qa/`，不以旧 Host PASS
代替本次执行。
仓库四项 ptrace OS guard 测试仍需在允许 ptrace 的宿主复验；本沙盒 preexec
阶段 EPERM 不是 PASS，未修改防护源码或放宽这些测试。
下列命令仅剔除这四项已核验的环境限制，其它默认 marker 策略保留：

```bash
UV_CACHE_DIR=/tmp/mdf-mvp-uv uv run --offline --no-sync pytest -q \
  --deselect='tests/test_tdx_bounds.py::test_child_cannot_execute_denied_connection_or_overrun[boundary]' \
  --deselect='tests/test_tdx_bounds.py::test_child_cannot_execute_denied_connection_or_overrun[attempt]' \
  --deselect='tests/test_tdx_bounds.py::test_child_cannot_execute_denied_connection_or_overrun[wall]' \
  --deselect='tests/test_tdx_bounds.py::test_trace_follows_threads_without_provider_access'
```

新增文件与受跟踪变更需通过 repository hygiene、Ruff 与差异检查后
方可按 active controls 做普通条件交付。初版专业 R1 及 R2 的历史
`CORRECTION_REQUIRED` 与冻结身份独立留存，不能因修改补丁而改写为 PASS；
最终修订版本需独立版本绑定的专业复审。产品文档不构成 Git/CI 完成证明，
该终态由后续外部 Completion Return 与 Hosted CI 证据报告。
