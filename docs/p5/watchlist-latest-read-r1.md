# 本地非生产 Watchlist latest accepted Read

`market_data_foundation.runtime.watchlist_read.read_latest_watchlist` 提供一个
有限本地读取接口，revision 为 `mdf.nonproduction.watchlist.latest.v1`。
消费者需要 **MDF-dev 上可读取的私有 session artifacts 目录**；它不是网络已公开的
MBO Runtime/API，没有监听端口，没有 MBO 消费实施或接入完成声明。
不产生 Source/Revision Admission、SourceBinding、DatasetVersion 或 Publication；
已验收的 `mdf.consumer.read.v1` 保持原约定。

## 消费入口

在 uv 管理的 Python 3.14.8 环境中，显式传入一个 session 的绝对目录，
不扫描 artifacts 根目录，不选择其它 session，不隐式回退到 report 或 Provider。
路径各级均不可为 symlink，不允许 `..`。上层可自行显式选择 session，
本接口只授权读取传入的这一个目录。

```python
from market_data_foundation.runtime.watchlist_read import read_latest_watchlist

view = read_latest_watchlist(
    "/srv/market-data-foundation/artifacts/tdx-mvp-<session-id>"
)
if view["status"] == "observed":
    for member in view["members"]:
        print(member["security"], member["quote"]["prices_cny"]["close"])
```

明确的本地 CLI（使用已由 uv 准备的环境，不在 Read 时安装或同步依赖）：

```bash
.venv/bin/python -B -m market_data_foundation.runtime.watchlist_read \
  --session-dir /srv/market-data-foundation/artifacts/tdx-mvp-<session-id>
```

stdout 是单个 JSON 对象；`observed` 返回退出码 0，其它结果返回 1。
CLI 的 `-B` 禁止 Python 导入缓存写入；嵌入式消费者如需整个解释器启动过程也无
缓存写入，应在启动时设置 `PYTHONDONTWRITEBYTECODE=1`。Read 函数自身只执行
只读文件打开/加载与内存解析，不写文件、不联网、不导入 SDK、不启动子进程。
uv 启动器的环境/缓存管理不属于 Read，因此此处直接使用 uv 环境内的解释器。

旧 session 没有 `read-ready.json`，可明确选择 final-report 兼容入口：

```bash
.venv/bin/python -B -m market_data_foundation.runtime.watchlist_read \
  --session-dir /srv/market-data-foundation/artifacts/tdx-mvp-<session-id> \
  --completed-report
```

Python 对应参数为 `completed_report=True`。此模式只读取已归档的 `report.json`，
**无法消费正在运行的旧 producer 的 latest**，不会由 raw batch 存在推断接受。
报告须有完整 ACK 标识与一致的来源、成员读取、时钟和冻结 SHA；失败终止报告
只能返回 unavailable。默认模式不会因 receipt 损坏/缺失而调用此模式。
逐轮 `connection_peer`、整数 `quote_elapsed_ns` 与冻结 B 一致，
`known_provenance` 与共享 Observation 重建结果一致；旧报告无需逐轮
`byte_count` 字段。成功终态要求 `session_complete=True`、
`stop_reason=budget_exhausted`、整数 `child_exit=0`、合法 complete 事件及
与最后批 outcome 一致的 health；最后批若为 failure，仍返回 unavailable。
历史未成功报告即使 health 因旧实现保留 `readable_snapshot`，或 child_exit 为
null，也只返回 unavailable，不暴露旧 quote。矛盾报告返回固定分类
`invalid_acceptance_evidence`，不返回部分批或私有错误文本。
两个入口都可在 producer 正常退出后使用。`read_at` 可显式传 UTC datetime
做确定性归档检查；它是评估本地年龄的时刻，不是历史 as-of 选择器，
早于任何已接受 known/producer Read 的时刻会拒绝。

## 接受与原子可见性

现有 real producer CLI 已连接 `AcceptanceReceiptWriter`，仍须主控另行明确授权
并显式 `--live` 才能启动；消费者不启动它。嵌入式 producer 要显式配对
`session.run(adapter, archive=archive, on_read_ready=AcceptanceReceiptWriter(session_dir))`。
`archive` 必须先以不可覆盖方式保存同一 B 原字节并完成 flush/fsync。
仅传 archive 而不接 callback 的旧调用不会自动产生 receipt。

producer 在全成员同批 Ingress/Read 完成、A 发出且收到合法 Go `ack_received`
之后，才将该轮加入 receipt 的连续接受清单。receipt 只保存有限 config、
逐批 SHA/长度、known/Read 时刻、分类和 producer 状态，不复制行情。
Source failure 批的 ACK 表示已经消费 unavailable 结果，不表示行情成功。
其它回调只更新状态，不能将未 ACK 的批加入清单。

新批等待 ACK 时状态为 pending_ack；连接/采样失败、拒收、IPC 中断或异常退出
均更新 unavailable。读取会阻断旧值的健康返回，`quote` 置 null，仍提供最后
已接受批的身份以便诊断；不会把最近失败藏在旧成功后。恢复成功必须遵守原
Ingress 的新 segment/新连接/新 sample 基线，保留最后连接与采样错误分类。
Reader 将采样错误的 round、segment 和分类与最后接受的真实失败批核对；
未接受的采样失败只能属于下一轮，且 pending_ack/unavailable 才可保留该记录。
连接错误 round 必须为 0；较新且尚未接受的 segment 不能与 accepted/ended
健康状态并存。合法恢复后仍保留历史错误，状态由最后批决定。错误坐标越界、
ledger 与冻结失败批不一致或健康状态掩盖新失败，均以固定分类拒绝整份证据。

写入者在同目录创建 0600 临时文件，完整写入、flush、fsync，再 atomic replace
`read-ready.json`，随后 fsync 目录；正常 CLI 的 session 目录为 0700、原批文件
为 0600 并单独 fsync。已有 artifacts 的 Git ignore 继续覆盖这些文件。
Reader 固定打开一版完整 receipt，使用 dirfd 和 O_NOFOLLOW 打开限定文件，
不接触 producer 的临时文件。并发 replace 可以让一次 Read 看到前一版完整
receipt，下一次再看到新一版；不是跨文件的实时事务或通知服务。

进程被强杀、主机崩溃或存储无法写入时，producer 无法保证刷新状态。Reader
不做进程探测/心跳，`producer_liveness=UNKNOWN`，producer_state 仅指最后记录的
状态。遗留成功结果只能在本地 TTL 内为 observed，随后 stale；不能据此判断
producer 仍在运行或上游仍在更新。receipt 写入失败会终止 producer 的正常路径。

## 输出与边界

| 字段 | 含义 |
| --- | --- |
| status | observed / stale / unavailable / no_data；调用方首先据此判定可用性 |
| batch_status | 最后完整接受批的 sample / duplicate / failure，或无批时 unavailable |
| empty | 没有可供消费的 quote；失败观察仍可能有 batch 身份 |
| batch | session 内 batch_id、round、segment、原字节 SHA-256/长度、候选 acquisition、派生 SDK release、endpoint、peer、parser revision、来源和时钟 provenance |
| clocks / read_at | producer requested/received/frozen/known/producer_read_at 与 consumer read_at，均本地时钟 |
| local_known_age_seconds | consumer read_at 减本批 known |
| local_received_age_seconds / stale | 共用状态机的本地接收年龄；严格大于 config.ttl_seconds 才 stale，TTL 为 1–300 秒 |
| members | 合法成员集合；每个成员状态、stale、value_acquisition、value_received_at 和解析结果 |
| last_errors | 最后连接/采样错误的固定分类、round 与 segment，恢复后仍保留 |

价格 Last/Open/High/Low/Close 与成交额复用 SDK decoded integer li → CNY 的
共用精确 Decimal 解析，输出十进制字符串。Volume 保持 SDK 整数，经济单位
`sdk_volume_count_UNKNOWN`；五档输出 `[decoded price integer li, SDK Number]`，
Number 的经济单位 `sdk_level_count_UNKNOWN`，不声称股/手。
不返回完整 SDK feed、ServerTime/Active 原值、原始异常文本或私有绝对目录。
失败时成员的 value_acquisition/value_received_at/年龄可能指向状态机保留的
先前值，仅作诊断关联；该值的 quote 仍为 null，不冒充本失败批的新值。
`source_event_clock`、`server_time`、`upstream_freshness`、`source_freshness` 均 UNKNOWN；
SDK Kline.Time 仅为客户端采样时间。任何本地 age 都不是交易所传输延迟。

Reader 复用 `WatchlistIngress` 的冻结重建路径和原 Snapshot parser/状态机，
逐批重建去重、故障与 segment，再复核全成员在 producer Read 时的状态。
默认 receipt 上限 128 KiB、每批 128 KiB、最多 120 批/32 成员，单次最多读
15 MiB 批数据；legacy report 上限 2 MiB，兼容校验可能读取批文件两次。
重复 key、非有限数、截断、SHA/长度/身份漂移、时间逆序/未来 known、非法成员、
symlink、硬链接、非普通文件和越界路径均 fail closed，不返回部分快照。
错误只暴露固定分类。SHA 保证字节身份，不证明交易所真实性；目录及 producer
必须由受信任的本地使用者管理，receipt 不防具有该目录写权限者主动伪造接受记录。

## 验证入口

```bash
UV_CACHE_DIR=/tmp/mdf-latest-uv uv run --offline --no-sync pytest -q \
  tests/test_watchlist_latest_read.py tests/test_tdx_watchlist_runtime.py
UV_CACHE_DIR=/tmp/mdf-latest-uv uv run --offline --no-sync pytest -q
UV_CACHE_DIR=/tmp/mdf-latest-uv uv run --offline --no-sync ruff check .
UV_CACHE_DIR=/tmp/mdf-latest-uv uv run --offline --no-sync ruff format --check .
UV_CACHE_DIR=/tmp/mdf-latest-uv uv run --offline --no-sync python scripts/check_repository_hygiene.py
```

测试使用 author-created Go envelopes、显式时钟、真实管道中的 synthetic child
与 ACK/barrier；不调用 Provider。宿主 retained 盘中 artifacts 可显式通过
completed-report 模式只读检查，不复制到 Git，也不构成新 live 证明。
环境限制导致不能执行的测试仍须保留为 NONPASS，并由主控在合格宿主复验。
独立专业 review、正常 Git/CI 与任何后续 MBO 接入均由主控另行处理。
