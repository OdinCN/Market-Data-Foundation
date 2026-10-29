# CPI 连续时间序列：CLI / 本机 API

本文描述代码接口，不承诺服务正在运行。历史 live 验收仅是历史证据，不代表当前数据、权限或监听状态。
WebUI 使用另一套接口，见 [快速上手](cpi-webui-quickstart-zh.md)。
时间点示例采用北京时间 Asia/Shanghai（+08:00）；后端可保存/返回 UTC。`YYYY-MM` 是参考月，不是时点。

## 环境与配置

从仓库根目录执行，使用 uv 管理的 Python 3.14.8（uv 0.12.22）：

```bash
uv sync --locked --group dev
export PYTHONPATH="$PWD/src"
# 以下绝对路径和环境 ID 均须替换为用户自备配置；凭据、状态不进 Git。
export CPI_CONFIG_DIR="/absolute/path/to/private/config"
export CPI_PACKAGE_STORE="/absolute/path/to/private/package-store"
export CPI_ENVIRONMENT_ID="your-dev-environment"
export CPI_SCHEDULE_STATE="$PWD/artifacts/cpi-timeseries/schedule.json"
# 仅对应 CLI serve 的本机 DEV 端口示例，须以实际部署为准。
export CPI_API_BASE="http://127.0.0.1:18765"
cpi() {
  uv run --locked --no-sync --offline python -B scripts/cpi_timeseries.py \
    --runtime "$CPI_CONFIG_DIR/runtime.toml" --secret "$CPI_CONFIG_DIR/secret.toml" \
    --package-store-directory "$CPI_PACKAGE_STORE" --environment-id "$CPI_ENVIRONMENT_ID" \
    --schedule-state "$CPI_SCHEDULE_STATE" "$@"
}
cpi --help
cpi query --help
```

`cpi` 是本文 shell 简写；顶层配置放在子命令前。显式路径避免采用代码中的旧主机默认配置位置。
配置及已授权数据库由用户提供，CLI 不自动建表。涉及上游还要求 package store 中启用 macro/cpi
并配置 BLS 凭据绑定；失败不换 provider。`--offline` 只约束 uv 下载，不禁止业务联网。

| 顶层参数 | 默认值 / 含义 |
| --- | --- |
| `--schedule-state` | `artifacts/cpi-timeseries/schedule.json`；日历、目标、信号和重试状态 |
| `--history-start` | `1999-01`；固定核对起点，已含计算缓冲，不逐轮向前漂移 |
| `--signal-poll-seconds` | `3600`；Errata 检查周期，60–86400 秒 |

同一状态的已保存历史起点优先；不能靠改启动参数静默扩展历史。
Web/worker 的配置、package store、环境 ID、调度及执行记录路径须一致；状态保存在 Git 外或 ignored `artifacts/`。

## 最小只读验收清单

针对已配置本地数据和部署者已核实的服务执行；连接失败如实记录，不自行启停。
以下不写业务数据或调度状态；`--local-only` / `local_only=true` 禁止来源补取。

```bash
cpi status
cpi schedule status
cpi signals status
cpi query --basket all --adjustment all --start 2026-09 --end 2026-09 --measure change_12m_ratio --local-only
curl --fail-with-body --silent --show-error "$CPI_API_BASE/api/cpi/status"
curl --fail-with-body --silent --show-error "$CPI_API_BASE/api/cpi/schedule/status"
curl --fail-with-body --silent --show-error "$CPI_API_BASE/api/cpi/signals/status"
curl --fail-with-body --silent --show-error --get \
  --data-urlencode 'basket=all' --data-urlencode 'adjustment=all' \
  --data-urlencode 'start=2026-09' --data-urlencode 'end=2026-09' \
  --data-urlencode 'measure=change_12m_ratio' --data-urlencode 'local_only=true' \
  "$CPI_API_BASE/api/cpi/query"
```

核对 CLI/API 同范围结果、四期限字段与单位，以及 `complete/measure_complete`、
`missing/calculation_missing/source_unavailable/withdrawn`、target/history/recovery。
退出 0 或 HTTP 200 只证明请求成功；本地查询来源请求/尝试应为 0，不预设行数或最新月份。

## 查询口径与兼容性

月份包含首尾，输入和输出均为 `YYYY-MM`。查询默认总体 SA、`index`。
初始化和显式更新默认总体、核心各 SA/NSA 四条序列：

| 篮子 | 季调 | BLS series ID |
| --- | --- | --- |
| headline（别名 overall） | sa | CUSR0000SA0 |
| headline | nsa | CUUR0000SA0 |
| core | sa | CUSR0000SA0L1E |
| core | nsa | CUUR0000SA0L1E |

`--basket headline|overall|core|all`、`--adjustment sa|nsa|all` 可组合。
显式 `--series` 与 basket/adjustment 互斥；多序列可逗号分隔或重复 `--series`。

| `--measure` / HTTP `measure` | `points[].value` 来源 | `unit` |
| --- | --- | --- |
| index | index_value | index |
| mom | mom_pct（旧接口，百分数两位） | percent |
| yoy | yoy_pct（旧接口，百分数两位） | percent |
| change_1m_ratio | change_1m_ratio | ratio |
| change_3m_ratio | change_3m_ratio | ratio |
| change_6m_ratio | change_6m_ratio | ratio |
| change_12m_ratio | change_12m_ratio | ratio |

每个有效点直接提供四个 ratio 字段，选择 measure 只决定额外的 `value`，不删其他字段。
顶层 `units` 标明字段单位。比率 **`0.0312 = 3.12%`**；旧 `mom/yoy` 的 `3.12` 仍是 3.12%，
不会静默改单位。四项使用同系列、同截止时点的有效指数 `I(t)/I(t-k)-1`，是累计变化、不年化。
Decimal 计算后 ROUND_HALF_UP 到比率四位；百分比展示才乘 100。

BLS 原始 `provider_calculations.pct_changes` / SQL `bls_pct_*` 保留原值和百分数单位，
例如 `3.1` 保留来源一位小数，不覆盖为 MDF 计算的 `3.12`，也不缩放成 `0.031`。
派生百分数两位仅为公开指数计算精度，不声称官方以该精度发布，也不声称更准确。

以下是只读查询示例，均禁止来源补取：

```bash
cpi status
cpi query --start 2020-01 --end 2026-09 --local-only
cpi query --basket core --adjustment nsa --measure yoy --start 2020-01 --end 2026-09 --local-only
cpi query --series CUSR0000SA0 --measure change_3m_ratio --start 2025-01 --end 2026-09 --local-only
cpi query --basket all --adjustment all --measure change_12m_ratio --start 2000-01 --end 2026-09 --local-only
```

`init` 只补缺，重复相同数据不增版；显式 `update` 刷新并保留变更历史，两者沿用旧的 12 个月计算缓冲。
因此 `init --start 2000-01` 会包含 1999-01 起的计算输入。
调度的目标月/固定历史刷新则显式禁用额外缓冲。
当前查询先读本地，真实缺口可补取 BLS，**仅合并到本次返回，不持久化业务数据或原始响应**；
重复相同缺口查询仍可能重新请求。`--local-only` 禁止补取。需要把缺口落盘时显式执行
`init` / `update`（或由 due 驱动相应采集），不能将查询成功当作本地回补完成。
数据库故障不会触发上游透传或切换来源。

可选的联网查询示例（不属于默认只读验收清单）：会使用 BLS 配额，但不落盘。

```bash
cpi query --start 2020-01 --end 2026-09
```

`coverage_complete=true` 表示每月都有来源观测（包括明确缺值）；`complete=true` 表示目标月均有数值。
`measure_complete` 进一步检查所选派生值是否有有效端点。检查 `missing`、`calculation_missing`、
`source_unavailable`、`withdrawn`、`provider_requests/provider_attempts` 和各点 `origin/revision/observed_at/available_at`。
任一端点缺失时 ratio 为 null；中间月缺失但两端有效仍可计算累计变化。

来源明确缺值保存为 NULL/source_unavailable，不能补零、插值或无限重抓；
来源缺值与未采缺口分开。详见 [发布历史设计](macro-publication-history-design.md)。

## 历史时点与官方档案边界

strict 历史查询使用 `--query-mode history --as-of-basis available`：仅从本地选择
截至 as_of 已确认可消费的版本，禁止上游；分子、分母按同一截止时间选版。
as_of 必须带时区；示例时间不保证有记录，缺乏过去证据就返回缺失。

```bash
cpi query --start 2025-08 --end 2025-08 --query-mode history --as-of-basis available --as-of '2026-10-28T20:00:00+08:00' --measure change_1m_ratio --local-only
```

兼容参数 `--vintage-mode observed|available|official` 仍存在：observed 按真实获取时间筛选，
不能当作 strict 可消费历史；available 按确认时间筛选。无截止参数默认 current，
有 as_of 或 available 口径则推断 history；available 无 as_of 也只读本地已确认数据。
`--as-of-basis` 与 `--vintage-mode` 同时提供须一致。
初始化本地 `revision=1` 不等于官方首发；未采到的中间版本无法恢复。
官方全部 vintage、通用首发档案摄取及指定官方发布版本查询仍 unsupported：
`--vintage-mode official` 返回 `cpi_timeseries_official_vintage_unsupported`（CLI 退出 2，HTTP 400）。

## 显式采集与确认（写入）

按采集任务选择执行，不是查询前置步骤。init/update 联网保存批次、原始材料和版本；相同内容不增统计修订。
confirm 只把当前持久化版本未知的 available_at 确认为实际现在，不请求 BLS、不新增统计版本、
不倒填过去，不允许自定义确认时间；确认范围须覆盖计算分母。确认失败保持未完成。

```bash
cpi init --start 2000-01 --end 2026-09
cpi update --start 2026-07 --end 2026-09
cpi confirm-availability --start 1999-01 --end 2026-09
curl --fail-with-body --silent --show-error -H 'Content-Type: application/json' \
  -d '{"start":"2000-01","end":"2026-09"}' "$CPI_API_BASE/api/cpi/init"
curl --fail-with-body --silent --show-error -H 'Content-Type: application/json' \
  -d '{"start":"2026-07","end":"2026-09"}' "$CPI_API_BASE/api/cpi/update"
cpi query --start 2000-01 --end 2026-09 --query-mode history --as-of-basis available --local-only
```

## 两套 HTTP 接口

CLI `serve` 直接调用时间序列服务，无需 Job。`serve --port` 默认 18765，
只绑定 127.0.0.1；这是 DEV 配置，不是固定生产接口。`cpi serve --help` 只查看参数。
只接受本机 peer 与准确 Host，拒绝浏览器 Origin 和代理转发头。
GET 参数不得重复；POST 为 JSON 对象，不含重复键或 URL 查询串，正文最多 64 KiB。
HTTP 400 表示输入/证据错误，403 表示本机边界拒绝，503 表示可分类服务故障；错误不回显原始异常。

| 方法 | 路径 | 操作 | 写入否 |
| --- | --- | --- | --- |
| GET | /api/cpi/status | 本地数据状态 | 否 |
| GET | /api/cpi/query | 含 measure/vintage_mode/as_of/local_only 的区间查询；缺口补取也不落盘 | 否 |
| POST | /api/cpi/init | 补缺并持久化 | 是，业务/材料 |
| POST | /api/cpi/update | 指定区间刷新；`{}` 执行一次 due | 是，业务/调度/材料 |
| POST | /api/cpi/confirm-availability | 显式确认指定范围当前已持久化版本的未知可消费时间为现在 | 是，可用时间 |
| GET | /api/cpi/schedule 或 /api/cpi/schedule/status | 日历、target/history、信号状态 | 否 |
| POST | /api/cpi/schedule/refresh | 刷新官方日历，正文 `{}` | 是，调度/材料 |
| POST | /api/cpi/schedule/due | 最多执行一个到期数据阶段，正文 `{}` | 是，业务/调度/材料 |
| GET | /api/cpi/signals 或 /api/cpi/signals/status | 同一调度快照，包含 signals/events | 否 |
| POST | /api/cpi/signals/refresh | 刷新官方 Errata，正文 `{}` | 是，调度/材料 |
| POST | /api/cpi/signals/import | 显式导入复核证据，正文 `{"notices":[...]}` | 是，复核状态 |
| POST | /api/cpi/signals/dismiss | 终结已复核无关的未执行公告，正文 `{"signal_id":"...","reason":"..."}` | 是，复核状态 |

history_start 与信号周期是启动配置，不能在请求中偷偷修改。

WebUI 使用 `/api/r5/cpi/timeseries` 前缀，不能将上述 POST 路径直接搬到 Web：

| Web 方法与相对路径 | 用途 |
| --- | --- |
| `GET /status`、`GET /query` | 状态与查询；Web API 默认 local_only=true，当前页面显式发 false，历史页面发 true |
| `GET /schedule`、`GET /strategy` | 月份计划、预置周期策略与 worker 状态 |
| `GET /jobs`、`GET /executions` | 手动任务和执行记录 |
| `POST /jobs` | 提交采集任务；正文以页面生成请求及源码契约为准 |
| `PUT /strategy` | `{"enabled":true}` 或 `{"enabled":false}`；可带 history_start，已有基线锁定后不能更改 |

Web query 接受 start/end、basket/adjustment、measure、local_only、query_mode、as_of、
as_of_basis/vintage_mode；显式 series 参数仅 CLI/本机 API 支持。
Web 地址、端口、Origin 和访问授权属于 DEV 部署配置，使用部署者提供的基址与准入条件。
仓库根可用 `uv run --locked python scripts/mdf_cpi_timeseries_web.py --help` 核对参数（沿用 PYTHONPATH）。
配置目录、package store、环境身份、绝对 schedule-path 和 job-directory 均须用户自备。

## 发布调度、公告与 worker（写入）

```bash
cpi schedule refresh
cpi schedule due
cpi update
cpi schedule import /absolute/path/to/official-cpi-calendar.html --acquired-at "$CPI_CALENDAR_ACQUIRED_AT"
cpi signals refresh
cpi signals import artifacts/verified-cpi-notices.json
cpi signals dismiss --signal-id "$CPI_SIGNAL_ID" --reason '已复核无需采集；请替换为实际依据'
```

refresh 联网并写通道/材料/状态；import/dismiss 写证据及复核状态。acquired-at 须为材料真实获取时间。
公告文件为 `{"notices":[...]}`，仅导入人工核验的真实证据；signals status 可只读查看待复核 ID。

| notices[] 主要字段 | 要求 |
| --- | --- |
| evidence_url / evidence_text | 真实 HTTPS BLS 文档及来源摘录，不伪造系列 ID 或数值 |
| kind / verified / scope | correction、annual_revision 或 withdrawal；核验后 true / national |
| series_ids / start_month / end_month | 支持序列的受影响子集与明确 YYYY-MM 范围 |
| effective_at | 官方实际生效时间，带时区；未来生效则等待到期 |
| expected_points | 更正范围每单元的 series_id、month、previous_value、index_value；数值为字符串 |
| scope_mapping / discovered_signal_id | 可选人工映射 / 已发现公告 ID，须与原证据逐项相符 |

每文件 1–100 条、最多 64 KiB；字段验证以 [信号契约源码](../src/market_data_foundation/historical/cpi_revision_signals.py) 为准。
更正须核对全国四序列交集、月份、生效时间和每单元预期值；撤回需明确证据，API 漏行不算撤回。
import 不代表采集已确认，due 执行后仍需值匹配和 available 本地回读。未验证、未执行的无关公告可 dismiss，保留原文与理由。

无区间 update 等同一次 schedule due，不是后台服务。目标月先采集、持久化、确认并按 available 回读，
随后阶段从固定 history_start 核对到发布参考月；target/history 独立重试，达到预算保留 pending_exhausted。
官方明确缺值可终结采集，但不能宣称 target_available=true。年度季调纳入历史核对，不每天全量重抓。
ICS 是发布时间主来源，按官方时区处理夏令时；失败保留降级缓存，不回退 HTML 发布时间。
ICS 不含参考月，须已确认映射或必要官方辅助证据；不按发布月减一猜测，日历缺席不等于取消。
Errata 轻量轮询；通道恢复安排持久化逻辑补偿，可由月度核对满足，不保证崩溃重试恰好一次网络请求。

长期 worker 示例（持续检查并可能联网、写库/状态，须另行安排运行）：

```bash
cpi --history-start 1999-01 --signal-poll-seconds 3600 schedule worker --poll-seconds 5
```

poll-seconds 默认 5、允许 5–3600；实际循环最多间隔 5 秒检查轻量状态，网络操作按持久化到期时间执行。
worker 不自动创建或启用策略。Web 预置策略须显式启用，并配套独立 worker，
其参数见 `uv run --locked python scripts/mdf_cpi_timeseries_worker.py --help`。
真实未来发布事件仍待自然验证；模拟时钟和历史 live 结果不构成未来发布承诺。

## 存储与边界

batches 保存请求/来源哈希及获取时间，revisions 追加保存修订，latest 为当前版本。
`cpi_timeseries_rates` 是当前 ratio 视图，不是历史 SQL 查询接口；历史计算必须先按同一 cutoff
为分子、分母分别选版。月份内部 DATE 月首键仅表示参考月，输出 YYYY-MM。
旧数据缺确认依据时 available_at 保持未知，普通迁移/查询不自动补证据。
参见 [发布历史设计](macro-publication-history-design.md)。
