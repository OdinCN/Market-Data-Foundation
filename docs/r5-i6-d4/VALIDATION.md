# D4 离线验证与边界

使用现有 uv 管理的 Python 3.14.8；未改解释器、依赖或 lock。worktree 无本地 `.venv`。分别复跑两个同机现有环境：`/srv/market-data-foundation/.venv` 具备测试/静态工具，缺 FastAPI/Starlette；`/srv/market-data-foundation-control/worktrees/r5-i5-cpi-scheduling-20261020/.venv` 具备 FastAPI 0.119.1、Starlette 0.48.0、Pydantic 2.12.3、httpx 0.28.1。未安装依赖、未访问网络。

可重跑命令（工作目录为本 lane）：

```bash
PYTHONDONTWRITEBYTECODE=1 /srv/market-data-foundation/.venv/bin/python -m pytest -q -rs -p no:cacheprovider tests/test_r5_i6_d4_market.py
PYTHONDONTWRITEBYTECODE=1 /srv/market-data-foundation-control/worktrees/r5-i5-cpi-scheduling-20261020/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_r5_i6_d4_market.py tests/test_r5_i6_market_contracts.py tests/test_p6_consumer_read.py tests/test_p6_consumer_wire.py tests/test_p6_operator_wsgi.py
PYTHONDONTWRITEBYTECODE=1 /srv/market-data-foundation/.venv/bin/ruff check --no-cache .
PYTHONDONTWRITEBYTECODE=1 /srv/market-data-foundation/.venv/bin/ruff format --no-cache --check .
PYTHONDONTWRITEBYTECODE=1 /srv/market-data-foundation/.venv/bin/python scripts/check_repository_hygiene.py --include-untracked
git diff --check
```

## Host QA1 修正

`lanes/d4/qa1.log` 的唯一失败为可选 FastAPI route 测试无条件导入缺失的 `starlette`（其余 102 项通过）。本轮只修正专属测试与 lane 说明，产品模块不变。route 测试先检查 FastAPI/Starlette 是否存在；缺包时明确 skip，包存在后的导入或路由错误仍正常失败。完整 Web 环境仍实际执行已注册路由，不跳过该项验证。

新增确定性负例主动阻断 FastAPI/Starlette 导入，重新执行独立 API 模块，验证模块加载、首读与 cursor 续页均不尝试加载 Web 依赖，且两次读视图均关闭；显式调用 `create_router` 则抛出带缺包名的 `ModuleNotFoundError`。此负例在两个环境均实际运行，不依赖缺包跳过。`pyproject.toml` 和 `uv.lock` 未改；本修正不改变集中 Web 宿主的依赖要求。

专属测试覆盖：单项/批量一致性、namespace 与顺序、自然日闭市/缺口、跨月 exact、指定清单不扩充、重叠/歧义、未发布候选、目录覆盖/计数/摘要、反向格点、canonical descriptor 对照、完整历史及故障、三时间选择/当前撤回、各资格轴、Universe/semantics、相关 mutation/恢复/未来成熟、无关变化、cursor 签名/过期/轮换/位置/重复键/非规范/超限、字节分页前缀/进度、累计预算/512格点分批、ASGI body/错误脱敏、可选 FastAPI route、读侧零索引写及旧全量读取禁用。

测试直接调用 ASGI receive/send 和 FastAPI 已注册 endpoint；不启动网络服务。FastAPI route 测试将 worker dispatcher 注入为同步替身，验证注册与边界转换；不声称验证了真实并发 worker/HTTP server。曾尝试包含既有 CPI schedule suite 的扩展回归，其 TestClient 在本受限执行环境中阻塞，已中断；该次不计入通过数。最终以以上明确列出的确定性集合为准，不声称全树 pytest 或 UI 浏览器联调完成。

缺失目录/材料不触发修复、采集或写入。synthetic adapter 仅 fixture seed 方法可变更数据，Reader 的可调用端口全部只读；其调用计数是测试内存诊断，不是持久查询会话。合成测试不能证明 D3 实际原子写维护、canonical scope/evidence 判定、生产 admission、物理查询成本或存储 durable。

最终结果与接线边界见同笔运行输出和 `docs/contracts/r5-i6-d4/INTEGRATION.md`。无 commit/push/branch/HEAD 变更，无真实 Provider、PG/Garage 或部署效果；不作专业 PASS 自评。

本轮修正后命令结果：精简环境专属测试 **103 passed, 1 skipped**（仅可选 FastAPI route）；完整 Web 环境所列确定性集合 **387 passed**，包含 D4 **104 项**，无 skip，另有既有 WSGI 用例产生的 3 条方法名验证警告。全树 `ruff check`、`ruff format --check`、包含未跟踪文件的 repository hygiene 与 `git diff --check` 均完成。Git HEAD 仍为指定基线；全部新增文件位于本 lane 六个允许前缀内。这些是运行观察，不替代外部独立审查或专业验收。

## 独立审查两项 P2 修正

修改仅涉及 `market_consumer_read/reader.py`、`contracts.py`、专属测试及本 lane 两份说明。修复前先运行新增回归和更新后的 Budget 断言：11 项均复现预期差异（坏字段中断批量、耗时误报 422）。修复后：

- 内容 `classification=[]`、`day=[]` 各覆盖 exact/as-of 两项批量；同步更新内容摘要/长度、投影和 canonical descriptor 以及目录摘要。HTTP 200、整体 `PARTIAL_FAILURE`，坏项为 `INTEGRITY_FAILURE`，健康项全部自然日单元与独立读取完全一致。
- 注入 timer，不实际等待：目录/内容单语句耗时、进入下一语句及分页构建后的事务耗时、事务内及视图退出后的请求耗时，共六个 HTTP 回归，统一要求 HTTP 503 `READ_TIMEOUT`、精确 dimension、仅错误信封和视图关闭。原有累计容量维度用例新增 HTTP 422 断言。
- 上述命令重跑：完整 Web 环境 D4 **114 passed**，D4＋冻结 D1＋旧 Consumer/WSGI 合计 **397 passed**（既有 3 条 WSGI 方法验证警告）；精简环境 **113 passed, 1 skipped**，仅跳过可选 FastAPI route。静态检查及卫生结果见本次工具输出。

真实 D3 adapter 仍须按 `INTEGRATION.md` 的 `MarketProjectionRead.read_view`、选择性点查、canonical 验证、admission/guards 及预算/底层 timeout 映射接线。当前只验证 synthetic 同步返回后的耗时检查，不能中断任意阻塞 adapter，不能证明真实数据库物理成本。未访问 Provider、网络、PG/Garage，未改共享 D1/kernel、`web/app.py`、依赖或 Git HEAD；未 commit/push/branch/deploy。不作独立专业验收结论。
