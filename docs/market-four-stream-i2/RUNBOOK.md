# I-2 Host one-shot intake / runbook

全部命令从本候选 worktree 根目录执行。不要改变原 SDK/Driver pin。
以下 `CANDIDATE_SHA` 必须取自独立 Host 审查后的二进制字节，不能仅相信 adjacent
build report。`CALENDAR_SHA` 则由 Host 审查具名日期及 SH/SZ 两份原材料后冻结。
本目录不含价格、原始 Provider 材料、账户或密钥。

## 1. 离线 QA 与研究 Source build

```bash
export UV_PROJECT_ENVIRONMENT=/srv/market-data-foundation/.venv
export UV_CACHE_DIR=/tmp/mdf-i2-uv-cache
uv run --offline --no-sync python scripts/check_repository_hygiene.py --include-untracked
uv run --offline --no-sync python -m ruff check .
uv run --offline --no-sync python -m ruff format --check .
uv run --offline --no-sync python -m pytest -q
uv run --offline --no-sync python scripts/mdf_i2_build_source.py \
  --sdk-checkout /var/tmp/mdf-tdx-sdk-v100-20261021 \
  --go /var/tmp/mdf-tdx-live-20261011/toolchain/go/bin/go \
  --module-cache /var/tmp/mdf-tdx-live-20261011/pkg/mod \
  --output artifacts/i2-host-reviewed-build/source
```

构建只用本地 module cache；Go tests 是 net.Pipe，不接 Provider。
原 pinned quote Driver：
`/srv/market-data-foundation/artifacts/tdx-sdk-v100-git-delivery/tdx-driver-main-20261021`。
该路径可变不授信，父进程总是按原 pin hash/seal 同字节后执行。
研究候选另用 memfd seal 和 Host 显式 digest，不加入生产 Driver 信任表。

## 2. 冻结新的盘后计划并执行一次

将下列占位 `CANDIDATE_SHA` 换为已经审查的 digest。每次 plan 自动创建全新 run_id。
目录页默认每市场 2 页，是有界发现，不宣称完整市场。`--directory-pages` 可在已核实
GetCode 能力内显式调整（最多每市场 25，最终仍受全局 64 requests / 64000 items 限制）；
`--daily-count` 上限 800；样本可具名扩大到原 Driver 支持的 32 个股票。
不对 SH/SZ 之外市场猜测相同能力，不凭 prefix 授予稳定身份或类别资格。

```bash
uv run --offline --no-sync python scripts/mdf_i2_collect.py plan \
  --sdk-checkout /var/tmp/mdf-tdx-sdk-v100-20261021 \
  --endpoint 124.71.187.122:7709 --collector-sha256 CANDIDATE_SHA \
  --symbols sh600519,sh600000,sh601318,sz000001,sz000002,sz300750 \
  --directory-pages 2 --daily-count 32 \
  --output artifacts/i2-host-after-close-plan
uv run --offline --no-sync python scripts/mdf_i2_collect.py collect --live \
  --plan artifacts/i2-host-after-close-plan/plan.json \
  --sdk-checkout /var/tmp/mdf-tdx-sdk-v100-20261021 \
  --candidate artifacts/i2-host-reviewed-build/source \
  --host-reviewed-collector-sha256 CANDIDATE_SHA \
  --driver /srv/market-data-foundation/artifacts/tdx-sdk-v100-git-delivery/tdx-driver-main-20261021 \
  --output artifacts/i2-host-after-close-run
```

该 6 证券计划冻结：Source 14 method calls + 1 handshake，8 个目录/计数请求、
6 个 GetKline；请求 bytes 377，响应 wire cap 4 MiB，最大 4196 items，
Source 1 socket / 120 秒。Quote 另有 1 probe / 1 connection / 1 GetQuote、
1 handshake、请求 bytes 77、IPC payload cap 131072 bytes、30 秒。
全部串行，总时间计划 156 秒（含 teardown allowance），无 failover、无重试。
实际创建的 `start.json.budgets` 是最终权威预算；quote transport raw response bytes
仍 UNKNOWN，不将 IPC 上限冒充网络 bytes 上限。

Source 请求/响应分别存 `source-N-request.bin / source-N-response.bin`，完整 Go JSONL
存 `source-stdout.jsonl`，projection/assessment 为 derived；每个 write exact readback。
异常 partial stdout 单独保留。未完成行/decoder failure 不准入。
`effect-ledger.json` 保存停因和所有新增物料引用；`observations.json` 采用已有
capability/fixture/observation 合同，离线复检不冒充新 live 请求。
本地 `artifacts/i2-run-identities/` 独占保留 run_id；不能换 output 重用额度。
不同 worktree/Host 之间的总 ledger 仍由 AO 串行 intake，不能跨根复制运行同一计划。

若仅有原 quote Driver 可审查，plan 可显式 `--quote-only`。此时 Source budget=0，
收据明确 NOT_ATTEMPTED；不是目录/历史下载的替代成功。

## 3. 名称明确的 retained intake / D1–D2

`intake-quotes` 仅接受绝对具名目录及包含 config/report/read-ready/每个 batch 的精确
SHA manifest。它复制到全新 ignored 目录并独立读回，双路重建 acceptance；保留原
source_run_id，另建 inspection_id，不接 Provider，不重写源目录和 known_at。

```bash
uv run --offline --no-sync python scripts/mdf_i2_collect.py intake-quotes \
  --source-directory /srv/market-data-foundation/artifacts/mdf-i2-afterclose-snapshot-20261022-r1 \
  --manifest artifacts/i2-retained-host-manifest-r1/manifest.json \
  --output artifacts/i2-new-retained-inspection
```

D1：调用 `tdx_market_evidence.directory_inputs`，再提供明确 `MemberSelection` 和
D3 `MaterialLink` 给既有 `tdx_security_directory.universe.snapshot_universe`。
空页/count bracket 合格只说明标准目录枚举闭合；same-count constituent drift、
类别、全市场权威性、历史成员和生命周期仍 UNKNOWN。
D2：调用 `daily_page(PageRequest, plan=..., source_index=..., line=...,
retained_reference=..., expected_line_sha256=..., known_at=..., day_assertions=...)`。
每个 row 的 `day_assertions` 是 `(index, YYYY-MM-DD, proof_ref)`，显式接受并留证
SDK TypeKlineDay 的本地日标签；不把 SDK 15:00 当真实事件发生时刻。
结果 Page.terminal=None，D2 不可把 partial 下载裁成完整历史。
本线未创建正式 canonical Material/Version/Publication，也没有存储 namespace 写入。

## 4. 明日 AM / PM 的独立计划与日历 intake

分别生成两份 `--mode session --day 2026-10-23` 的新 plan，分别指定
`--session-window AM` / `--session-window PM`，二者 run_id/output 各不相同。
已冻结的最终候选及 run ID 见 `RETURN.md`，仅冻结计划不产生 Provider effect。
Session mode 自动 Source budget=0，仅对已经检查的 quote capability 做 3 轮、5 秒间隔、
30 秒最大时长；对同证券经济字段比较，至少两次相邻轮次变化才记内容刷新观察。
未知 Source event time 继续 UNKNOWN，不自动产生 stream live PASS。

Host 独立审查后提供如下 intake **结构**，不能直接拿此示例充证据：

```json
{
  "schema": "mdf.i2.calendar-host-intake.v1",
  "day": "2026-10-23",
  "reviewed_at": "<actual aware timestamp>",
  "expires_at": "<aware expiry after the selected window>",
  "markets": [
    {"market": "SH", "state": "OPEN", "authority": "<named reviewed exchange source>",
     "material_sha256": "<actual retained evidence digest>",
     "sessions": [["09:30", "11:30"], ["13:00", "15:00"]]},
    {"market": "SZ", "state": "OPEN", "authority": "<separate reviewed source>",
     "material_sha256": "<actual retained evidence digest>",
     "sessions": [["09:30", "11:30"], ["13:00", "15:00"]]}
  ]
}
```

官方年休市通知单独不足以证明某一自然日 session OPEN；Host 需结合市场正常会话规则、
覆盖该日期的安排和可能的特别停市更正，有明确来源/原材料/known_at 及闭合边界。
若只取得局部休市通知，closure=PARTIAL，无法提供本 OPEN intake，timer 不安装。

脚本启动和 quote 前再次校验同一 intake hash、日期、有效时间、SH/SZ 状态和本地
窄窗口（AM 09:35–11:30、PM 13:30–15:00）；整个预算加 5 秒须位于具名窗口内，
AM 计划不会在 PM 执行；after-close 计划也绑定冻结日期。
没有未来时间等待、常连循环或无限重试。若延迟到午休/收盘，立即停止。

先审查 `.service.in/.timer.in`，再由 Host 将占位替换为绝对路径，AM `@WINDOW@`=
`09:35:00`、PM=`13:30:00`。`@QUALIFIED_UV_ENV@` 指向已具备依赖的
uv-managed Python 3.14.8 环境，`@PRIVATE_UV_CACHE@` 指向专属可写缓存。
模板中没有 Install/enable 部分：不在 Codex 沙箱执行
systemctl、daemon-reload 或安装任何 unit。Host 安装动作须等本候选、脚本、预算和
日历 intake 全部前置检查完成；Persistent=false / Restart=no / TimeoutStartSec=150。

## 5. 失败与恢复

每条能力独立 STOP；网络 access_restricted 属执行环境限制，不算 Provider failure。
若 source 发生未知效果，先检查原 run 的 raw/ledger/receipt；不重新运行同一 run。
需要另一短程探索时先核实新方法/端点证据，创建新 plan 和总 ledger 预算；无自动替换源。
本地留存失败不把价格/原文写 Git，不删除已有材料；精确 readback 失败保留 UNKNOWN。
本线没有自动 cleanup、补采、修复、Publication、adoption、生产激活或持久化动作。
