# PPI 连续时间序列速览（开发验证）


当前研究字段扩展及迁移/补史入口见[五族研究字段扩展 R1](macro-research-fields-r1.md)；下文保留此前合同与验收记录。
共同入口与审查路径见 [五族基线指南](macro-family-development-review.md)；本文保留 PPI 差异及历史验证证据。

截至 2026-10-30，PPI 已完成开发环境真实初始化、重复更新及有界 worker 验证。
本文区分代码入口、已验证结果与待验证能力；开发验证不代表常驻生产部署或自然发布捕获完成。

| 篮子 | 季调 | BLS series ID |
| --- | --- | --- |
| headline（别名 overall）最终需求总量 | sa | WPSFD4 |
| headline | nsa | WPUFD4 |
| core 最终需求剔除食品和能源 | sa | WPSFD49104 |
| core | nsa | WPUFD49104 |

核心仍包含贸易服务；不是再剔除贸易服务的 FD49116 口径，不拼接 SOP 旧序列。
四条序列真实历史起点尚未核实，profile 的 `earliest_month=None`；不承诺完整 20 年。
历史起点未知仍允许初始化请求预置：默认 240 个月、范围可编辑，实际覆盖以来源返回及本地回读为准。
预置只是请求范围，不是完整覆盖证明；无发布或本地确认锚点时，按请求日历前一月生成范围。
开发验证初始化范围为 2006-10 至 2026-09，已入库并回读 802 条观测；
请求范围 960 个“月×序列”位置中 158 个来源未返回，12 个月计算缓冲期另缺 48 个位置。
重复更新为 0 新增、0 修订；这些结果不证明四条序列的精确历史起点或完整 20 年覆盖。
部分真实返回经持久化与本地回读核验后，任务也可为 `complete`；须检查
`result.coverage_complete=false`、`completion=source_coverage_partial` 及
`result.source_missing`、`result.readback.missing/source_unavailable` 等缺口说明，任务完成不等于覆盖完整。
CPI/PPI 共用覆盖摘要与默认折叠的缺口明细，区分来源未返回、来源空值及待核实本地缺口；
计算缓冲期单列，不计入请求范围覆盖。

`change_1m_ratio/change_3m_ratio/change_6m_ratio/change_12m_ratio` 为 1/3/6/12 月累计变化，
按同系列、同截止点的 `I(t)/I(t-k)-1` 计算，非年化；API `0.0312 = 3.12%`。
计算向前保留 12 个月缓冲；来源缺值保留，不补零或插值，端点不足时 ratio 为 `null`。
`--measure` 决定额外的 `value`，各 ratio 字段仍保留；旧 `mom/yoy` 使用百分数单位。
检查 `complete/measure_complete`、`missing/calculation_missing/source_unavailable`，不能只看请求成功。

当前查询本地优先，临时上游缺口仅合并本次响应，不落库；`--local-only` 禁止上游补取。
开发验证已确认临时空缺查询到达上游、返回 0 条且未入库；返回正值的临时补取尚未验证。
历史查询只读本地，按入库后确认可用时点 `available_at` 截止，分子分母使用同一截止点。
UI 使用北京时间，请求时点显式带 `+08:00`，后台使用 UTC；`YYYY-MM` 是参考月。
本地 `revision=1` 不代表官方首发，缺少过去证据就保留缺失；官方全部 vintage 仍不支持。

从仓库根使用已准备的 uv 环境（uv 管理的 Python 3.14.8）；以下仅查看帮助，不连接数据库或来源：

```sh
export PYTHONPATH="$PWD/src"
uv run --locked --no-sync --offline python -B scripts/ppi_timeseries.py --help
uv run --locked --no-sync --offline python -B scripts/ppi_timeseries.py query --help
```

已有独立 CLI：`scripts/ppi_timeseries.py` 绑定 `PPI_PROFILE`，复用 `cpi_timeseries_cli.py`；
没有 `--profile` 或 `--asset` 参数。支持 `init`、带区间的 `update`、`query/status/confirm-availability`。
帮助中仍列出的 `serve/schedule/signals` 及无区间 `update` 会返回 `ppi_command_unsupported`，勿照搬 CPI 命令。

下例只供已有授权本地库的环境使用，命令示例未逐条执行核验。先设置 `PPI_CONFIG_DIR`（私有配置目录）、
`PPI_PACKAGE_STORE`（私有 package store 目录）和 `PPI_ENVIRONMENT_ID`（环境 ID）；配置及凭据不入 Git。
`ppi` 仅为本文 shell 简写；顶层配置在子命令前，`--offline` 只限制 uv 下载。

```sh
ppi() {
  uv run --locked --no-sync --offline python -B scripts/ppi_timeseries.py \
    --runtime "$PPI_CONFIG_DIR/runtime.toml" --secret "$PPI_CONFIG_DIR/secret.toml" \
    --package-store-directory "$PPI_PACKAGE_STORE" --environment-id "$PPI_ENVIRONMENT_ID" "$@"
}
ppi query --start 2026-09 --end 2026-09 --basket all --adjustment all --measure change_12m_ratio --local-only
ppi query --start 2026-09 --end 2026-09 --basket core --adjustment sa --query-mode history --as-of-basis available --as-of '2026-10-29T20:00:00+08:00' --local-only
```

查询默认总量 SA；`--basket headline|overall|core|all` 与 `--adjustment sa|nsa|all` 可组合。
也可用 `--series WPSFD4`，与 basket/adjustment 互斥。采集与确认会写入，须显式执行；CLI 不自动建表。
上游能力要求单独启用 macro/ppi 并绑定 BLS 凭据，不能借用 CPI 开关或静默换 provider。

前端已有 PPI 查询、采集、策略与执行视图；Web 前缀为 `/api/r5/ppi/timeseries`：
GET `status/query/schedule/strategy/jobs/executions/api-help`，POST `jobs`，PUT `strategy`。
`web/ppi_timeseries.py` 是共享操作的 profile 校验适配层，不是服务启动器。
Host 须显式配置 PPI 启用、读写授权、独立调度/任务路径及历史核对起点；未启用返回 503。
真实入口为 `scripts/mdf_cpi_timeseries_web.py`（Host）与 `scripts/mdf_cpi_timeseries_worker.py`（worker）。
两者基础必填参数仍为 `--config-directory`、`--package-store-directory`、`--environment-id`、
`--pg-role`、`--pce-schema-owner`、`--pce-schema-oid`，以及 CPI 的 `--schedule-path`、`--job-directory`。
Host 还须 `--opt-in-timeseries-web`，并在这些参数基础上增补六项：
`--enable-ppi-timeseries --ppi-schedule-path ABS --ppi-job-directory ABS`、
`--ppi-history-start YYYY-MM --allow-ppi-local-read --allow-ppi-collect`；PPI 路径须为独立绝对路径。
worker 使用 `--asset ppi --opt-in-ppi-worker`，保留上述基础必填参数，再传
`--ppi-schedule-path ABS --ppi-job-directory ABS --ppi-history-start YYYY-MM`；
CPI/PPI 调度与任务路径均须为彼此独立的绝对路径，可选 `--allow-collect`、`--poll-seconds 5`。
PPI worker 仅 tick PPI 策略；策略文件为 PPI state 同目录的 `ppi-strategy.json`，策略须显式启用。

PPI 首发后四个月可修订，季调每年回修五年，参见 [BLS FAQ](https://www.bls.gov/ppi/faqs/questions-and-answers.htm)
及 [季调说明](https://www.bls.gov/ppi/seasonal-adjustment/)。
调度代码具备发布窗口分阶段快采、当期持久化确认与本地回读，再按配置起点分批核对历史的能力；
策略需显式启用和外部执行循环，受 BLS 配额与重试约束。
开发环境已验证官方日历获取、策略启用和有界 worker 心跳推进；自然发布捕获尚未验证，启用策略不等于采集完成。
PPI 未实现 CPI Errata 信号发现/导入；历史核对不等于恢复未采到的官方中间版本。
官方参考：[四序列口径](https://www.bls.gov/ppi/fd-id/ppi-final-demand-intermediate-demand-aggregation-indexes-by-title-and-series-id.htm)、
[PPI 发布日历](https://www.bls.gov/schedule/news_release/ppi.htm)。
