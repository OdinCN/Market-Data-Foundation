# TDX 日线 PG-only 批量候选 R2（沿用原文件名）

R2 仅修正 R1 reviewer 指出的一个 P1 和三个 P2，代码范围固定十文件。四项 RED 反例在原候选实现上均失败，修正后 GREEN；具体命令、exit 和计数见本事务 `outputs/EO_RETURN_R2.md`。本候选基于 `9fff275719bb2ac85d6472eb132d8a69ef4563d8`，仅做离线开发与冻结 Source 文件只读核查。没有执行 PG DDL/DML、Garage、Source 请求、Git 交付、生产部署。真实 PG SQL、并发与性能验证状态为 **待 DEV SQL gate**；本地 reference oracle 和调用记录测试不能证明数据库 SQL 已执行成功。Host 最终字节检查及独立专业 read-only review 由 AO 后置。

## 文件与数据合同

- `db/tdx_daily/001_current.sql`：独立 schema 候选，严格三个表：当前行情、来源/批次/证券刷新、更正日志。没有修改旧 I3。
- `002_stage.sql` / `003_classify.sql` / `004_apply.sql` / `005_refresh.sql`：同事务 TEMP staging、集合分类、实际变更与 OLD/NEW 同事务落表、刷新收据。
- `storage/tdx_daily_pg.py`：原生整数模型、受控来源优先级、psycopg 3 COPY、事务接口、有界 cold read、独立因子输入门禁，以及明确命名的离线参考模型。
- `integration/tdx_daily_ingest.py`：固定路径、固定 Host 回执 SHA 的只读 B1 适配，调用已交付 `qualification.tdx_market_evidence.admit_line`，没有重写 wire 解码。
- `scripts/mdf_b1_tdx_pg_only_import.py`：只提供 `--offline-frozen`，无 DSN、连接或实仓写入入口。
- `tests/test_tdx_daily_pg.py`：合成 deterministic oracle、psycopg 调用合同/失败退出检查、SQL 文本约束检查及 Host-local 冻结文件只读复演。CI 没有外部文件时仅跳过最后一类本地证据测试，不访问 LAN。

唯一键为 `(market,instrument,period,trading_day,basis)`，source/run/request 不入键。当前只允许 `period=1d`、`basis=native_projection`、market 0/1、六位原生 instrument code；这不是已获准的证券分类或稳定跨供应商证券身份。价格/金额按 Owner 已验证的 pinned Go TDX v1.0.0 SDK 原生有符号整数（千分之一元）保存，Volume 按 SDK 原生“手”，Type9 不做分钟线 `/100`。单位版本固定为 `go-tdx-v1.0.0-native-int-v1`，无 Python float 转换。

事实数值仅为 Open/High/Low/Close/Volume/Amount。`Last` 是分页 prev-close 解码上下文，页首 0 合法，不进入事实 digest、唯一键或同批同键数值比较。非经济的 Order/UpCount/DownCount 也不投射到 OHLCVA 事实；原始投射证据仍在冻结文件内。只在刷新记录中按 Source 请求存小型 SHA 摘要（request/response wire、line、projection），不按每根 K 保存整帧。原始真实 artifacts 不复制入 Git。

`native_projection` 明确保留复权意义 UNKNOWN；它没有把厂商原生投射宣称为经独立验证的不复权基准。因子不会改变这些行。仅提供带 market/instrument/effective_day、精确有理数、source/revision 的独立 `AdjustmentFactor` 输入模型；默认可信来源集合为空，拒绝所有实际因子准入。本轮没有因子表、真实因子、复权查询产物或复权端到端 PASS。

## 批次、优先级与重试

冻结适配器将原 B1 协议发现 binding 明确接为本候选的 `tdx-v100-primary` 控制角色，固定优先级 100。代码内不可逐批配置的 `SOURCE_REGISTRY` 另外仅保留两个离线反例角色 `controlled-secondary` / `controlled-peer`，均为 10；它们没有真实供应商资格。未知名称拒绝，Batch 不带 rank，`write_batch` 和 `OfflineCurrent.apply` 均无 policy 参数；`SourcePolicy` 仅为固定描述对象，构造不同映射即拒绝，绕过构造得到的对象也不能修改权威绑定。此映射仅供本轮离线候选，不构成 Source activation 或正式来源资格。`Batch` 是内部已验证输入合同；未来 Source 适配器必须提供真实完整性/质量及证据校验，不能仅凭外部输入的两个布尔字段授信。当前唯一实际适配器只接受 pinned B1 文件。

完整意味着这次冻结请求范围内每页返回全部 32 行、每个计划请求均有 confirmed wire、通过现有 admission 和 OHLC/日期等检查；并非已获得全历史 exhaustion、Universe 或 Calendar。单批最多 64 请求、64 个证券作用域、51200 输入行，每证券至多 800 个不同日期；当前只接受每证券一个请求。多请求未提供逐根日 K 归属映射，统一 fail closed，禁止擅取其最大时钟。请求证据至多 64 KiB。非空、int64、OHLC 顺序、非负量额、时钟先后、日期范围在 Python 校验；staging 的 NOT NULL/CHECK、SQL 日期范围筛查和集合重复检查再次约束入库。所有 SQL identifier 固定，无可输入 schema/table/SQL 字符串。

未来受权调用 `write_batch` 必须提供空闲 psycopg 3 连接，不能嵌入已有事务。函数开 READ COMMITTED 事务，设 lock_timeout 5 秒、statement_timeout 30 秒，取得固定 namespace advisory xact lock `(721603,1)`；所有本候选 writer 均须遵守该锁。它一次覆盖 native daily 小作用域全部来源，以简单串行化消除分类后竞态；排名校验、收据查询、分类在锁取得后执行；分类内 current 与确认历史使用同一语句 snapshot。writer 在本批证券范围校验已有 current 与全部 refresh 的 source/rank 是否等于固定 registry，包括被 BLOCKED 的收据；污染或未知源即异常退出并回滚，重放也不能绕过。reference oracle 在任何变更或重放前校验其保存的来源排名。唯一主键仍负责约束事实身份。未经该协议的外部 writer 不在并发保证内。

COPY FROM STDIN 使用 psycopg `copy.write_row`，Python 只流送本批元组，不逐行 SELECT/UPDATE。SQL 对同键不同 OHLCVA/units 拒绝整个事务，同值重复去重。集合分为 NEW/UNCHANGED/CHANGED；另有策略拒绝类 BLOCKED：

- NEW：仅集合 INSERT 当前表。
- UNCHANGED：不 UPDATE 当前行情，不改变它的 value_source、changed_at 或 batch_id；仅写批次/证券级收据。
- CHANGED：较高受控优先级可替换较低来源；同一胜出来源/优先级仅接受比该来源的权威确认时钟更晚的完整合格请求。先对 current 及本批日期的 refresh 按来源汇总确认时钟，再按 rank 降序、该来源时钟降序、来源名称 C 顺序选唯一权威；不会混用另一个低源较晚的时钟。相同 rank 不同来源不能直接改值；同值确认可更新权威，同时间同 rank 按名称定序。SQL 只 UPDATE 这些行，并从 UPDATE RETURNING 集合写 OLD/NEW 六整数数组、旧/新 digest、旧来源/批次/时钟与新批次/时钟，reason 为 `vendor_correction_or_unknown`。
- BLOCKED：低优先级、过时或同一时间且不同值不覆盖，也不产生更正。该批仍记拒绝计数，受拒日期不进入 confirmed_days。

刷新记录只对该批实际非 BLOCKED 的日期作确认，保留 source、采集时间及日期数组。相同数值的高优先级确认也会保护后续覆盖资格，不必改动原行情行。低优先级的晚到采集不能越级。每证券 32 根页面的时钟严格来自已核 SHA 的原始 JSONL row `received_at`。该原文与 request_index、line/wire/request/projection SHA 一起保存在对应证券 evidence；COPY TEMP 显式携带 request_index 与解析后的 received_at，current/稀疏更正/refresh/reference 均使用该请求时钟。Batch.observed_at 的 max(clocks) 仅表示批次完成，admitted_at 仅为检查/事务元信息，不借给任何证券。原文保留纳秒尾数；Python datetime 与 PG18 timestamptz/psycopg3 的仲裁统一使用截至微秒的值，微秒内碰撞无法证明先后时，异值更正被 BLOCKED，不会因精度损失覆盖。以上均不是官方市场事件时钟。SDK Time 只作为固定 UTC+8 的 YYYYMMDD 日投射检查。

确认历史按 staged 证券/日期集合读取：先将每证券日期数组与 refresh 的 confirmed_days 做 overlap 过滤，仅展开这些收据一次，集合连接 staged keys，按键/来源预聚合，再选每键唯一权威。没有每根 K 的相关历史扫描/排序/LIMIT。确认表新增证券作用域 B-tree 与 confirmed_days 的 GIN；这只是候选访问路径，不保证优化器计划或真实吞吐，仍须 PG18 DEV gate 测量。每次完整批次/证券追加一个有限大小的刷新记录，而非逐根更新 last_seen。`(source,batch_id)` 的全批 fingerprint 包含范围、原生事实及请求证据，不包含每次重查可能变化的 admitted_at。同 ID 不同内容或 rank 拒绝；已提交的完全相同批次直接返回 `replay=True`、各写计数 0，不再 COPY/改行情/记日志。因而较旧批次在较新更正之后重试也不会反向覆盖。不同 ID 的重复采集则正常分类 UNCHANGED，刷新批次确认。提交结果不明时由调用者以原冻结身份在新连接重试；函数不在异常事务内自动重放。事务内任何 SQL/COPY/日志/收据失败均随 psycopg 上下文回滚，不能把部分成功当成完整导入。

没有自动创建 canonical Version/Publication，没有跨库协调或缓存平台。刷新收据同时承担重试屏障与确认权威，未来保留/压缩策略必须保持这些语义；本轮不自动清理。稀疏更正日志没有宣称无限期完整的 point-in-time 历史能力。锁时间、表大小、WAL、死元组、吞吐量均未测。

## 离线操作与证据

本机使用已有 uv 管理 Python 3.14.8 环境，以 `--no-sync --offline` 只读复用；无依赖变更。可执行：

```bash
export UV_PROJECT_ENVIRONMENT=/srv/market-data-foundation/.venv
export UV_CACHE_DIR=/tmp/mdf-pg-uv-cache
PYTHONPATH=src uv run --offline --no-sync python scripts/mdf_b1_tdx_pg_only_import.py --offline-frozen
uv run --offline --no-sync pytest tests/test_tdx_daily_pg.py -q
uv run --offline --no-sync ruff check .
uv run --offline --no-sync ruff format --check .
uv run --offline --no-sync python scripts/check_repository_hygiene.py --include-untracked
# 全量命令留作后续验证；R2 未重跑已知会卡住的完整套件。
uv run --offline --no-sync pytest -q
```

R1 曾在空 `/tmp` cache 尝试 `uv run --offline --frozen` 因缓存缺 pydantic-core 返回 1，未联网；随后使用上述已存在的 uv 环境，未修改系统 Python、锁文件或依赖。离线 CLI 输出只含 SHA/计数/资格状态，不打印原始 wire 或异常内容。

实际输入固定为旧 B1 工作树的 `artifacts/b1-go-r7-six-live/` 和 `artifacts/b1-go-r7-thirtytwo-live/`。只读核对原 Host `B1_GO_SIX_R7_RETURN.json`、`B1_GO_THIRTYTWO_R7_RETURN.json`、`B1_REAL_SOURCE_POSTFLIGHT_R8.json`，然后核对 plan/log/ledger 和全部 ledger material SHA/bytes、逐请求 wire/line/projection，含握手共 7+33 组 wire。

参考模型结果应为：32×32 首批 1024 NEW；六股 192 重叠为 192 UNCHANGED；换新批次 ID 重导 1024 UNCHANGED；相同身份重试为无操作。定向测试用明确合成的受控修正场景，只改 10 个质检合格数值，得到 10 CHANGED / 1014 UNCHANGED / 10 OLD/NEW 事件，再重试没有新日志；不把这些合成更正说成厂商实际发过的修正。独立因子模型修订不触碰参考 raw；JSON snapshot 冷重建可复现。SQL 调用记录检查同事务执行顺序和异常退出，未模拟或证明真实 PG 隔离、COPY 类型适配、SQL 可执行性及锁效果。具体 R2 自检 exit 与计数见本事务控制输出 `EO_RETURN_R2.md`；R1 whole deterministic 的 CPI ASGI 卡住及旧 ptrace 四项失败不计作通过。

证券类别/稳定身份、Universe/Calendar、官方事件时钟、复权基准、全历史终止和正式 D1 发布资格仍 UNKNOWN。下一步为 AO 后置 Host 最终字节 focused/ruff/format/hygiene/适用 deterministic、独立专业只读审查，再按单独授权做真实 DEV SQL gate；本候选没有启动这些外部阶段。
