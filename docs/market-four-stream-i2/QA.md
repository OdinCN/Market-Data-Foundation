# I-2 final candidate QA / Host gates

本页记录当前 I-2 候选，不继承历史 R5-I6 PASS；未做 Git/CI 或生产采用裁定。

| 检查 | 实际结果 |
|---|---|
| Python | uv-managed CPython 3.14.8；共享已安装环境只读使用，无依赖/lock 变更 |
| I-2 专项 | 38 passed |
| I-2 + A-STD + D1 + 原 TDX runtime | 346 passed / 3.83s；最终日志 `artifacts/i2-final-qa/focused-final.txt` |
| Ruff check 全仓 | exit 0，`ruff-check-final.txt` |
| Ruff format --check 全仓 | exit 0，340 files already formatted，`ruff-format-final.txt` |
| Repository hygiene（含 untracked） | exit 0；`hygiene-final.txt`，最终材料清单另见 manifest |
| git diff --check | exit 0；仅新增 I-2 文件，无 tracked 共享改动 |
| Go research Source | Go 1.25.3 / `go test -mod=readonly -race -count=1 -timeout=60s` / 5 tests，通过 |
| Offline Source build | `artifacts/i2-build-final/source.build.json`；protocol pin 和 hosts.go digest 记录在 receipt；无 Provider |
| 完整 deterministic pytest | **未通过完整门**：既有 ASGI/AnyIO 用例阻塞，中断 exit 130；不将已输出的前段通过冒充全量 PASS |
| 独立有界 ASGI 复现 | 精确指定既有测试；10s dump 仍在 AnyIO worker queue / asyncio selector；20s INT + 3s kill 后 exit 137 |
| Provider / 日线 / 目录 live tests | 无 live pytest 混入 CI；本线沙箱 quote probe access_restricted，0 GetQuote；新 Source 待 Host 审查运行 |

最终定向命令（均为真实执行）：

```bash
export UV_PROJECT_ENVIRONMENT=/srv/market-data-foundation/.venv
export UV_CACHE_DIR=/tmp/mdf-i2-uv-cache
uv run --offline --no-sync python -m pytest tests/test_i2_market_source.py -q
uv run --offline --no-sync python -m pytest \
  tests/test_i2_market_source.py tests/test_r5_i6_a_std.py \
  tests/test_r5_i6_market_contracts.py tests/test_tdx_watchlist_runtime.py -q
uv run --offline --no-sync python -m ruff check .
uv run --offline --no-sync python -m ruff format --check .
uv run --offline --no-sync python scripts/check_repository_hygiene.py --include-untracked
```

ASGI 的有界复现命令和日志：

```bash
timeout --signal=INT --kill-after=3s 20s env \
  UV_PROJECT_ENVIRONMENT=/srv/market-data-foundation/.venv \
  UV_CACHE_DIR=/tmp/mdf-i2-uv-cache \
  uv run --offline --no-sync python -m pytest \
  tests/test_cpi_scheduling.py::test_http_acceptance_auth_strict_input_and_no_execution \
  -vv -s -o faulthandler_timeout=10
```

日志留在 `artifacts/i2-final-qa/asgi-host-gate.txt`。该测试通过 httpx ASGITransport
调用既有 CPI app；未发 TDX/PG/Garage 请求。堆栈显示主线程仍在事件循环 select，
AnyIO worker 仍在 queue.get；本候选没有修改此测试或 Web 代码，也没有把它删除、
改 marker 或静默过滤后宣称全仓 PASS。现有 `docs/r5-i5-cpi-scheduling-recovery.md`
及 `docs/r5-i6-d4/VALIDATION.md` 也记录过同类沙箱限制；这些旧记录仅作定位，
不替代本次复现和 Host 重跑。

全量命令是在实现过程中启动的，未完成；最后所有 I-2 Python/Go 改动分别以最终
定向回归和最终 offline build 再验证。集中集成人仍须在最终同字节树上执行 Host
full deterministic、独立 Reviewer 和必要 Go QA。此门未过之前不推动 Git/Hosted CI。

专项负例覆盖：显式 opt-in / 研究候选 digest / 原 Driver pin 路径、未知方法和市场、
SDK endpoint evidence、bool/超限预算、重复 JSON、run ID 重用、原始帧身份/请求形状、
count drift / partial closure、无证据日期、derived/raw 分层、UNKNOWN clocks/units、
date/AM/PM/lunch/expiry/跨收盘拒绝、经济内容排除 SDK 本机时钟、少于两次变化不算刷新、
stopped/empty quote 不返回成功、失败 partial material 留存和 manifest 篡改拒绝。

下一门：Host 核对最终候选 manifest → 具名 after-close Source 一次运行及原始读回 →
独立 SH/SZ 日历 intake → 对应 AM/PM one-shot 审查后安装 → 明日真实内容变化观察。
PG/Garage 准入由 I-3 与集中集成验证；本线不自行持久化或升格资格。
