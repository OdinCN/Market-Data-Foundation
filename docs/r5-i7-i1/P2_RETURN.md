# I-1 Calendar 矛盾证据 P2 有界修正 — 2026-10-22

Project Market-Data-Foundation；Session #15 MDF Product Control；Board Owner-local。
仅原 I1 worktree，依据 Owner 本次必要修正授权。已读取 active `AGENTS.md`、
`BATCH_DIRECTIVE.md`、`SHARED_PORT_R1.md`、原候选 PORT/RETURN，以及
`outputs/REVIEW_I1_INITIAL_R1.return.md` 的唯一 P2。原 R1 记录保留为历史，
本文件说明修正候选；新独立 Reviewer 结论待出，不宣称专业 PASS 或 AO seal。

## 实际修正和调用约束

- `market_vertical.py`：新增纯 `validate_plan_evidence`；`prepare_version`
  新增必填 keyword-only `plan`。先绑定原冻结 unit（含 CalendarRevision 和
  UniverseRevision）、原材料 Job/Shard/route，再要求自然日 evidence 精确分区，
  重验 PointEvidence 引用和 NO_TRADE contract，调用共享 D1
  `classify_coverage(plan, evidence)`，最后才调用 D3 `assess_daily`。
  D1 是唯一 Calendar/lifecycle 分类来源，没有复制闭市、上市或资格推断规则。
  不使用 manifest 为缺失日期填 evidence，也不从相邻月份补齐。
- `market_vertical_read.py`：从 canonical material 的原 Job 查已注册冻结 Plan，
  在 raw read、assessment replay 和内容规范化前执行同一验证。旧数据即使已通过
  D3 commit/publish/rebuild，矛盾 classification 或丢失 evidence 仍拒绝为
  `INDEX_INCONSISTENT`。旧健康 Version、健康相邻月不能拼接成 COMPLETE。
- `test_i1_market_vertical.py`：fixture 传入原 Plan，并允许显式 evidence 变换。
  `test_i1_calendar_evidence.py`：新增 21 项确定性检查，全部使用虚构本地事实。
- `PORT.md`：更新必填 Plan 和 D4 接缝；`RETURN.md`：标注原 R1 历史记录。
  本文和 `P2_CANDIDATE_SHA256.md` 提供当前回执和同字节候选摘要。

没有改 D1/D2/D3/D4 共享文件、I2/I3/I4、依赖、uv.lock 或系统 Python。
共享 D3 没有冻结 Plan，直接绕过 I1 调用其接口仍不提供本项日历 gate；本轮只修
授权的 I1 接缝，测试特意保留该 legacy 路径以证明读侧独立拒绝。

## RED → GREEN 证据

生产代码修改前运行新增反例的初始 13 项：**9 failed, 4 passed in 8.96s**。
9 项失败均为预期异常未抛出，包含：

- 开市日 NO_TRADE_CONFIRMED 改 MARKET_CLOSED 并清除 no_trade_contract，
  D1 明确报 `coverage_explanation_not_evidenced`，原 I1 preparation 却接受。
- Calendar 或 lifecycle UNKNOWN 被填成 MARKET_CLOSED，原 I1 接受。
- 两种矛盾 classification 或空 references 经真实 D3 commit/publish/rebuild，
  batch exact/as-of 的原 I1 D4 接缝仍接受，未抛 `INDEX_INCONSISTENT`。

同次 4 项通过的是合法 NoTrade/真实 Closed 的 batch exact/as-of，以及原有底层
索引一致性对整条 date evidence 丢失的拒绝。后者在新的测试中继续保留；未把这两项
原本已通过的检查记成新修复。RED 日志为 `/tmp/mdf-i1-p2-red-final.txt`。

第一次修正验证 **47 passed in 55.53s**，包含原 34 项 I1 和上述 13 项。
随后补上 8 项冻结 Plan/Job/Shard/CalendarRevision/UniverseRevision、缺失 open/
closed 和重复 date 的检查；篡改和分区错误均断言在 material read 前拒绝，
ledger/Journal/Source calls 不变。最终 I1 共 55 项。

最终 I1 + D1/D2/D3/D4/时钟邻接结果：**727 passed, 7 warnings in 212.04s**，
exit 0。其中 I1 **55 passed**，其余 672 项为 D1/D2/D3/D4 与既有时钟邻接。
7 个 warnings 均为既有 D2 fork 故障用例的 Python DeprecationWarning，测试通过。
日志 `/tmp/mdf-i1-p2-regression.txt` 和 XML `/tmp/mdf-i1-p2-regression.xml`
记录当前源码执行；四个实现/测试文件与运行时捕获的 SHA-256 完全一致。

## 实际命令和静态门

使用现有 uv 管理的 Python **3.14.8** 与 uv **0.12.22**；无安装、同步或下载。
pytest 保留仓库默认排除 `live_provider` 和 `postgres_integration`，禁用仓库
pytest cache 和 bytecode 写入，临时 journal/运行日志/XML 全部位于 `/tmp`。

```bash
export PYTHONDONTWRITEBYTECODE=1
export UV_CACHE_DIR=/tmp/mdf-i1-uv-cache
uv run --no-project /srv/market-data-foundation/.venv/bin/python -VV
uv run --no-project /srv/market-data-foundation/.venv/bin/python -m pytest tests/test_i1_calendar_evidence.py -q -p no:cacheprovider --basetemp=/tmp/mdf-i1-p2-red-final
uv run --no-project /srv/market-data-foundation/.venv/bin/python -m pytest tests/test_i1_market_vertical.py tests/test_i1_calendar_evidence.py tests/test_r5_i6_market_contracts.py tests/test_r5_i6_d2_runner.py tests/test_r5_i6_d3_material_projection.py tests/test_r5_i6_d4_market.py tests/test_r5_i6_integration_clocks.py -q -p no:cacheprovider --basetemp=/tmp/mdf-i1-p2-regression -o faulthandler_timeout=60 --junitxml=/tmp/mdf-i1-p2-regression.xml
uv run --no-project /srv/market-data-foundation/.venv/bin/ruff check --no-cache .
uv run --no-project /srv/market-data-foundation/.venv/bin/ruff format --check --no-cache .
uv run --no-project /srv/market-data-foundation/.venv/bin/python /tmp/mdf-i1-p2-hygiene.py
```

RED 命令在生产修正前执行，使用当时的初始 13 项；最终测试文件另有上述 8 项。
Ruff check 已通过，format 为 **337 files already formatted**。
遵守本次无 Git 指令，hygiene 执行原仓库脚本的全部检查规则，入口 shim 将两个
Git inventory 调用替换为 `rg --files --hidden -g '!.git' -0` 的工作树枚举，
没有执行 Git；Git index 未读取，不宣称已审计强制暂存的 ignored 文件。
最终工作树 hygiene 覆盖 **376 文件**并通过。运行前后工作树文件 SHA-256 比对，
只有上述 8 个 I1 许可文件新增/改变，无删除；其他 368 个原文件摘要不变。
候选摘要见 `P2_CANDIDATE_SHA256.md`，其自身不作递归自校验。

本次完成的是具名 I1/邻接确定性门；没有运行全仓 pytest、GitHub CI 或共享
`test_repository_hygiene.py`（它会运行 Git）。原 R1 全仓 HTTP 测试停滞记录
仍为历史未解事项，本次不将邻接回归称为 full deterministic PASS。

## Effect ledger 与后续复核

| 区域 | 本次效果 |
|---|---|
| 本地工程 | 两个 I1 integration 文件、两个 I1 test 文件及 I1 docs；临时材料仅 /tmp |
| D2/D3/D4 | synthetic SQLite Journal、Memory ledger/projection；正常和损坏数据回放 |
| Provider / PG / Garage | 请求/连接/写入/DDL/删除均 0 |
| Git / 部署 / source admission | Git 命令 0；commit/push/部署/激活/真实准入均 0 |

下一道门是新的独立 Reviewer 对 `P2_CANDIDATE_SHA256.md` 指定的同一源码字节
复核唯一 P2 和邻接结果。测试通过只说明本地 synthetic/retained-memory 接缝；
不产生 Provider、PG/Garage、生产上线或全市场资格。后续若修改源码，须更新摘要
并按修改重新验证，不能复用本候选的测试或 Reviewer 结论。
