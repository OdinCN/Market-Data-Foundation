# 发布事件与证据读取：首轮增量

本增量沿用五族连续工作台、版本、任务和日历；增加独立发布事件及不可变材料证据。
首个材料适配器只覆盖 Employment Situation 摘要中的非农月净变动、同时报告的前两月旧值与修订值。
不代表五族产品包完成。其他四族的发布记录返回具名适配缺项；连续查询仍正常工作。

## 读接口

`GET /api/r5/{cpi,ppi,pce,employment,retail}/timeseries/releases`

| 参数 | 含义 |
| --- | --- |
| `event_id` | 精确事件身份，例如 `bls:employment-situation:2026-10-02`；不提供则倒序列出本地证据事件 |
| `as_of` | 含时区的 MDF 可消费截止，含边界；默认当前时间。不是来源版本日期 |
| `read_mode` | `event` 或 `source_history`；后者只读具名档案证据，不借用 latest |
| `source_version` | 可选精确 `YYYY-MM-DD` 档案版本日期；仍受同一个 MDF 截止约束 |

返回 `events`、`missing`、`as_of_basis=available`、`provider_requests=0`、`persisted=false`。
每个事件含全部截止内的 `evidence`，当前解释使用最后可用证据版本。`state=confirmed` 只表示当前适配器已有当期统计量证据，不表示所有统计量齐全或实际发布时间已确认。

`claims` 逐条给出原生系列、`statistic`、参考月、季调、单位、值、角色及规范化原文段落跨度/摘录。
`actual` 是材料中的当期官方统计量；`official_previous` 是材料明说此前公布的前值；
`prior_revised` 在 UI 显示为“上期值（本次修订后）”。这些前值均不是 MDF 发布前持有值。

`value_identity` 仅标识相同指标/统计量/参考月/口径/单位/值的内容；不是新的经济修订号。
`continuous_version` 本轮保持空并返回未绑定缺项；不通过值相等、本地 revision=1 或计划日历自动建立关联。
相同数值可由不同事件和材料关联，不写连续 revisions/batches/latest。

证据有独立身份、取得时间、提交后确认可用时间、留存 SHA256/定位、解析版本和留存类型。
`source_version` 明确 BLS archive 文件名日期依据及 date 精度；不补零点。
材料中的 embargo header 不能证明实际传输时刻，`actual_at` 保持 null。
历史材料晚导入只在本次实际可用时间以后出现；重复同材料/同解析/同留存声明不刷新原时间。
无共识来源时 `consensus/surprise=null`；纯计算合同仅在当期 actual 和事件/统计量/参考月/单位一致、具名且事前冻结的合格共识齐备时计算，不要求前值。

## 显式迁移和导入（由主控操作，本轮 CLI 未连接真实能力）

先审查 `uv run python scripts/macro_release_schema.py` 输出。
显式 `uv run python scripts/employment_timeseries.py prepare-release-schema` 只在既有就业 schema 旁新增
`release_events/release_evidence` 两表，不重建原两表及视图，不授予权限、不抓源。
新增对象的 `odin_dba` SELECT 和既有 schema USAGE 由主控核验；GET 不会建表，缺表只返回具体缺项。
迁移幂等。回退先备份新增证据，再在主控事务中依次移除新 evidence/events 表；原连续表不变。

示例（替换为主控已核验文件；默认只声明普通留存文档）：

```sh
uv run python scripts/employment_timeseries.py import-release \
  --file /absolute/artifacts/empsit_10022026_summary_projection.html \
  --source-url https://www.bls.gov/news.release/archives/empsit_10022026.htm \
  --material-format official_summary_projection
uv run python scripts/employment_timeseries.py releases \
  --event-id bls:employment-situation:2026-10-02 \
  --as-of 2026-11-01T12:00:00Z
```

`--material-format` 支持 `retained_document`（默认，不声明完整原始 HTTP）、
`official_summary_projection`（摘要投影，不含统计表）、`original_http_body`（导入方明确声明原始字节）。
来源 URL 限定 BLS 具日期的 Employment archive，拒绝 latest 和其他 Provider；复用现有 Garage 校验留存与 PG 连接。
导入不是自动 Provider 采集；不调用 BLS API，不使用当前 API 值重建旧事件。

## 本轮验证边界

合成合同走真实 ASGI router/serializer/HTTP正文至共用前端渲染；测试进程禁止外部网络/真实数据库。
主控留存的 2026-09-04、2026-10-02 官方摘要投影已作离线解析；后者另走实际 HTTP→UI。
这些投影不是完整原始 HTTP，也不是本 CLI DEV 直采。真实数据库迁移、持久化回读、只读权限、浏览器像素验收和未来自然发布尚待主控验证。
自动 worker→材料采集/事件导入、连续版本明确绑定及其余统计量/四族材料适配留待后续增量。
