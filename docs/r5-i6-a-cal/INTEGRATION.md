# A-CAL 集成端口与已知边界

本 lane 仅新增 `src/market_data_foundation/candidates/market_calendar_evidence/`、`tests/test_r5_i6_a_cal.py` 和两份专属说明；冻结 D1、共享 kernel、Go bridge、依赖及配置均未修改。所有测试材料为 synthetic，没有取得或声称取得交易所完整日历，也没有对 retained 材料来源真实性、Source/Calendar/使用权做资格裁定。

## 单一集成事务所需端口

| 接缝 | 现状 | 精确待集成内容 |
| --- | --- | --- |
| 材料来源 → A-CAL | `parse_material(bytes, MaterialReceipt)` 可直接离线调用 | 由具名 calendar Source 的 adapter 把已有公告/排程解为合同 JSON，提供原材料 receipt、row/source refs 和实际 known_at。公告文档抓取、真实格式解析和 Source/Binding registry 校验不在本 lane。不得凭 NOTICE 标签或 receipt 获得资格。 |
| A-CAL → D1 | `to_d1` 直接构造现有 `CalendarRevision`；`validate_d1` 精确验证 | `market_universe.CalendarDay` 缺少 typed UNKNOWN/CONFLICT/session/交易日归属/cutoff；目前通过 sidecar 与 evidence ref 绑定。未来 schema revision 应加入这些内容引用并提供 sidecar 的持久化/读回接口。多来源 provenance 也需具名合同，当前明确拒绝压成一份。 |
| A-CAL → D2 完成日与计划 | `completed_cutoff(...).completed_through` 可供输入验证；每个自然日证据仍保留 | `market_planning.JobSpec.completed_through` 目前只接收一个 date，检查窗口末日不超过它，不绑定该 date 的证据。集成时须同时传入 `D1CalendarInput.revision`、market/window/as_of、`CompletionCutoff.revision` 及首个阻断日期；拒绝 None/UNKNOWN/CONFLICT/PENDING，不能取最大已完成日绕过空洞。 |
| A-CAL → D1 资格/coverage | 输出始终 qualification=UNKNOWN；现有 `market_coverage.lifecycle_calendar_state` 会继续 UNKNOWN | 未来合格来源、使用/持有条件、指定市场/窗口/session 及独立审查应由单一集成事务形成独立资格引用。不可在此把 CalendarRevision 或 synthetic 标签升为 QUALIFIED，也不可由缺 bar 生成 MARKET_CLOSED。 |
| HK/期货/外盘 → 新产品定义 | A-CAL market 字符串允许精确 venue/合约/session scope | 这不扩大 `AssetClass` 或 `market_planning.DAILY_DEFINITIONS` 的四类定义。上述品种仍需各自 Definition、稳定 instrument/session mapping、Calendar/单位/使用资格；不将 SHFE/INE 或所有期货共用一套夜盘。 |
| Go/TDX bridge | 仅有 `parse_tdx_workday` 原生离线投影解析，固定 AST 类型/commit | 无 Go 改动需求可据此声称已接通；若未来 bridge 保留 WorkdayModel，应传原生 id/unix/date、单列 Is 结果、producer/SDK/decoder/source_ref/raw receipt。绝不能将 Workday.Is bool 直接映射到 D1 is_open。共享 `drivers/tdx/main.go`、build/trust pin 仍需单点事务处理，当前没有对应代码或 live 资格。 |
| A-CAL → D3 材料资产 | raw bytes + receipt + immutable material/claim/day/snapshot revisions 已可读 | 当前无写存储、Material receipt admission、Assessment、Version 或 Publication；Source Key/Material Key 是调用方传入的引用，代码不创建 canonical 资产。持久化与独立读回接入须另行资格。 |

## 验证方式

工作树没有本地 `.venv`；使用已有 uv 管理的 `/srv/market-data-foundation/.venv`，实际 Python 为 3.14.8。无依赖变更，不建新环境、不改系统 Python。可复放命令：

```bash
PYTHONDONTWRITEBYTECODE=1 /srv/market-data-foundation/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_r5_i6_a_cal.py tests/test_r5_i6_market_contracts.py
/srv/market-data-foundation/.venv/bin/ruff check --no-cache src/market_data_foundation/candidates/market_calendar_evidence tests/test_r5_i6_a_cal.py
/srv/market-data-foundation/.venv/bin/ruff format --check --no-cache src/market_data_foundation/candidates/market_calendar_evidence tests/test_r5_i6_a_cal.py
PYTHONDONTWRITEBYTECODE=1 /srv/market-data-foundation/.venv/bin/python scripts/check_repository_hygiene.py --include-untracked
git diff --check
```

测试涵盖：独立 SH/SZ/BJ、HK 半日/特殊闭市、显式夜盘跨周末归属、DST 时钟和 fold、完成日精确边界、连续前缀、未来/未知 known_at、同日冲突/修订链、查询范围外的跨交易日冲突、UNKNOWN 分母、来源混合拒绝、D1 实际 coverage 的保守结果、TDX 字段 pin/类型、字节完整性、重复键/坏结构/坏时钟、容量拒绝、零网络/DB/子进程/写入边界。

## 独立审查缺陷修正

- P1：重叠扫描现在使用全部有效直接声明的 sessions，保留同日冲突/未决日期；冲突及连通分量 lineage 在窗口裁剪和 D1 输出前传播。三条 NOTICE 回归覆盖同日 09:00–15:00 / 10:00–15:00 与次日声明从前日 14:00 开始的重叠，完整及仅次日窗口均阻断 COMPLETE/MATCH/D1 days，并保留窗口外材料。另验证有效 supersession 的旧 sessions 退出检测、无效 supersession 不隐藏 sessions、非直接声明不制造冲突。
- P2：receipt 的 obtained/received/known/publication 时序、替代关系严格先后及 D1 provenance 汇总均按 UTC 瞬时计算。回归覆盖 America/New_York 2024-11-03 两个 fold、墙钟逆序/相等、早于目标的无效修正，以及 local/UTC/混合表示等价复放。选择算法 revision 升为 `a-cal.selection.v2`；冻结 D1 文件无须修改。

后续独立审查的两项同范围修正：

- 冲突 lineage：每组完整 refs/revisions 只排序、冻结一次，组内日期共享 tuple；在逐日 replace 前，累计全部组与非冲突日期的引用槽和 canonical lineage 字节成本。限额分别为 262144 槽、16MiB，超限统一 `calendar_lineage_limit`，不截断材料。回归含 128 日完整共享、2000 日链及五个入口的展开前拒绝、11 份合法输入材料、引用数未超限但长引用字节超限、多组与非冲突日期的累计限额等值/减一边界。
- Unicode：JSON 解码后迭代检查全部字符串及键，孤立 surrogate 在 revision 编码前统一 `invalid_material_unicode`，没有底层异常上下文。回归含高/低 surrogate、倒序/断开组合、market/arrangement/timezone/source_ref/session/native_fields/键名，以及合法 surrogate pair、中文和 UTF-8 原字节保留。共享 `verified_json` 也覆盖离线 TDX 输入。

这两项修正不改变有效输入的选择结果或 canonical 内容，沿用 `a-cal.selection.v2` 与 parser v1。集成调用方需识别新增容量/Unicode 分类，不能在超限后缩小查询窗口来规避市场冲突检查；其余 D1 sidecar、D2 cutoff 证据绑定、资格和持久化接缝仍见上表，无共享文件修改需求。

这里的材料格式是有界规范输入，不是交易所公告自然语言解析器。未知日期、无可信 cutoff、无 Source/Calendar 权利与历史完备性仍是实际缺口。完整 Host/集成 QA、独立 reviewer、最终采用裁定由批次中央集成负责；专属测试通过不等于这些效果已经发生。
