# I-1 candidate return — 2026-10-22

> 本文件保留原 R1 候选的历史验证记录。独立 Reviewer 后续发现唯一 P2：Calendar
> 矛盾证据可经 rebuild 进入 D4。该项修正、当前命令/门禁和待独立复核的同字节候选
> 见 [P2_RETURN.md](P2_RETURN.md) 与 [P2_CANDIDATE_SHA256.md](P2_CANDIDATE_SHA256.md)；
> 下述原 34/706 项记录不作为修正后候选的新 PASS。

Session #15 MDF Product Control；Board Owner-local；I-1 worktree；基线
`e8faac58b8d97594109ed2dc63ea110afb36cfce`。候选尚未 commit/push，等待集中
同字节集成、独立 Host QA 和专业复核。本记录不改变 P3/P4/P5 或 runtime/phase 状态。

## 实际文件

- `src/market_data_foundation/integration/market_vertical.py`：D2 receipt intake、
  原身份核实接收前校验、D3 assessment/Version 构造、只读提交核实、具名 retained 回放。
- `src/market_data_foundation/integration/market_vertical_read.py`：真实 D3 Memory
  ledger 到既有 D4 MarketReader 的离线只读适配。
- `tests/test_i1_market_vertical.py`：deterministic 端到端及反例。
- `docs/r5-i7-i1/PORT.md`：精确方法、字段、限制和跨线 interface delta。
- `docs/r5-i7-i1/RETURN.md`：本候选结果与 effect ledger。

未修改 D1/D2/D3/D4、Go 主入口、Driver pin、web/app、依赖或 uv.lock。

## 已观察工程证据

最终源码的纵向回归：`706 passed, 7 warnings in 239.56s`，其中全部 34 个 I-1
用例通过。实际调用链是 D1 Plan → D2 SQLite Journal/
DailyRunner → D3 retain/readback/assess/commit/publish/rebuild → 现有 D4 Reader，
未用 D4 SyntheticProjection 代替 D3。

| 情形 | 实际断言 |
|---|---|
| single/batch × exact/as-of | 同一 Version/Publication/range，每自然日可读，私有 URI 不外泄 |
| 零成交、闭市和空响应 | 零成交行保留；无行情必须有显式 calendar/NO_TRADE；UNKNOWN 不能发布 |
| UNKNOWN / late FOUND / restart | 原身份只读 reconcile；显式接受；成功部分无重复 fetch |
| 原身份冲突和乱序 | Source/Universe/Calendar 请求不混用；前置拒绝无 Journal 写；PAGE_DRIFT retain 为 FAILED |
| Version/Publication ACK 丢失 | AFTER_COMMIT 注入；按原身份 COMMITTED；幂等不变；不可用仍 UNKNOWN |
| 原料/receipt/head 损坏 | Consumer fail-closed，无修复、补采或换版本 |
| 撤回和历史读取 | 当前撤回阻断历史/as-of 和 exact 内容，不拼接其他版本 |
| 时钟和 handoff | A/T0 固定，未来 A 拒绝；checkpoint/restore 精确读回，旧 generation cursor 拒绝 |
| 签名分页 | 连续无跳重、同 token 幂等；篡改/query 变化拒绝；新增/撤回/恢复/资格/generation 变化终止旧查询 |
| 跨 namespace / batch 部分失败 | 原身份 item 健康，foreign item 无行情；错误冻结 unit 被拒绝 |
| 独立 Version 资格 | 未发布 sibling 不阻断健康 Version；一项失去资格不污染共享 Source 的健康 item |
| 材料预算和读侧效果 | 超限在 raw read 前拒绝；patch Writer/Worker/socket/Popen 后读取仍成功 |
| 全局覆盖 | assessment + publication + material readback 才 COMPLETE；撤回后 UNKNOWN |
| retained 入口 | 仅 named synthetic-retained 测试，校验 raw/metadata 摘要；synthetic decoder 拒绝 retained 发布 |
| 缺失 retained / 混合 Job | 缺失输入保持 UNKNOWN，不能推为 ABSENT；单 manifest 不混 Job/budget |

仓库静态检查通过：Ruff check；format `336 files already formatted`；hygiene
`373 tracked and untracked files`；`git diff --check` 无错误。

全量 deterministic 未通过完成门：在既有
`tests/test_cpi_scheduling.py::test_http_acceptance_auth_strict_input_and_no_execution`
处长时间停滞（日志此前 1578 个通过进度点，无完整 pytest summary），手动中止，
exit 130。该测试未引用 I-1；单独 `timeout 20s` 重现 exit 124，faulthandler 堆栈为
AnyIO worker 等待 queue、主线程 asyncio selector 等待。该证据只证明当前环境的
既有 HTTP 路径未完成，不断言其根因或全仓测试通过。需集中 Host 排查并完成全量门。
没有修改或 skip 该共享测试，也没有以减少测试集冒充 full PASS。

最终通过的 706 项集合包含 I-1、D1/D2/D3/D4 和既有时钟回归。
7 个 warnings 来自既有 D2 fork 故障测试的 Python
多线程进程 fork DeprecationWarning；这些测试已通过，不构成市场资格。

## 命令和运行环境

Python 是现有 uv 管理环境的 3.14.8（未改系统 Python）。尝试新隔离环境的
`uv sync --offline --frozen` 因新 cache 缺少 psycopg-binary wheel 失败，无网络下载，
无依赖修改；实际验证使用已存在的主仓库 uv 环境，通过 uv 运行当前 worktree 测试。
pytest 的仓库 pythonpath 指向当前 `.` 和 `src`。

```bash
export UV_CACHE_DIR=/tmp/mdf-i1-uv-cache
uv run --no-project /srv/market-data-foundation/.venv/bin/python -VV
uv run --no-project /srv/market-data-foundation/.venv/bin/python -m pytest tests/test_i1_market_vertical.py -q
uv run --no-project /srv/market-data-foundation/.venv/bin/python -m pytest -q
timeout 20s uv run --no-project /srv/market-data-foundation/.venv/bin/python -m pytest tests/test_cpi_scheduling.py::test_http_acceptance_auth_strict_input_and_no_execution -q -o faulthandler_timeout=5
timeout 300s uv run --no-project /srv/market-data-foundation/.venv/bin/python -m pytest tests/test_i1_market_vertical.py tests/test_r5_i6_d2_runner.py tests/test_r5_i6_d3_material_projection.py tests/test_r5_i6_d4_market.py tests/test_r5_i6_integration_clocks.py tests/test_r5_i6_market_contracts.py -q -o faulthandler_timeout=60 --junitxml=artifacts/i1-validation/vertical-regression-final.xml
uv run --no-project /srv/market-data-foundation/.venv/bin/ruff check .
uv run --no-project /srv/market-data-foundation/.venv/bin/ruff format --check .
uv run --no-project /srv/market-data-foundation/.venv/bin/python scripts/check_repository_hygiene.py --include-untracked
git diff --check
```

pytest 沿用仓库默认 `not live_provider and not postgres_integration`。
运行日志只在 ignored `artifacts/i1-validation/`，不进入 Git。尚未运行 GitHub CI；
本线没有 Go 变更，不启动 Go 或 source sampling 工作。
`vertical-regression-final.log/.xml` 是最终源码回归证据；
`candidate-manifest.json` 记录 5 个交付文件与上述证据的 SHA-256、基线与运行解释器，
供集中集成人核对同字节候选。该 manifest 本身不替代独立专业复核。

## Effect ledger / UNKNOWN / 下一道门

| 区域 | 本轮实际效果 | 资格与恢复边界 |
|---|---|---|
| Source | 真实 Provider 请求 0；synthetic 有界请求及 named synthetic-retained replay | 无真实 TDX/ExHq 下载或 freshness 资格；时钟 UNKNOWN 保持 None |
| 本地 D2 | pytest 临时目录中的 SQLite journal/claim；正常和故障注入记录 | 原 Job restore/reconcile 和无重复采集已验证；不声称多 Host durability |
| D3 | MemoryMaterialBytes/MemoryMarketLedger；显式 retain、commit、publication 和 backup/restore | 不等于持久 PG/Garage；ACK 不等于 Consumer PASS |
| DEV PG / Garage | 连接 0、写入 0、DDL 0、删除 0 | I-3 adapter/namespace/真实 readback 尚未接入；资格 UNKNOWN |
| 定时/live窗口 | timer 0、服务启动 0、部署/激活/交易 0 | 本线不安装明日 live timer |
| Git / 依赖 | 仅上述 5 个新文件；无 commit/push、依赖修改、跨 worktree 写入 | 集中集成与 Hosted CI 待执行 |

失败恢复证据来自现有 D2/D3 故障点和原身份核实，而非重试新身份。暂存原料或
UNKNOWN receipt 保留原状态；没有清理既有材料。原真实素材、source clocks、经济
语义、真实 Universe/Calendar 准入及物理存储资格未在本轮证明。

下一道门：集中集成人评审 PORT 中的 D2 前置校验差异、D3/D4 canonical/native/
digest/资格读口；以 I-2 提供的原始 retained receipt 和明确能力 decoder 重放，
以 I-3 物理 writer/read adapter 完成专属 DEV 资格，并对集中候选做独立 Host QA、
专业复核和同字节 Git/CI。Memory bridge 的保守月级预算和 private canonical 接缝
不能作为生产查询性能或上线资格。本工单不宣称生产全链已经上线。
