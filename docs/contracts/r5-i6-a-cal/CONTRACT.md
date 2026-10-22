# A-CAL 离线材料合同 v1

绑定 `e7b64b382cbd8492b3e98785014270f2326ee2d3`、批次 R1、共享接口 R1、R2 §9.1–9.5。模块路径为 `market_data_foundation.candidates.market_calendar_evidence`。本实现接受显式规范 JSON 材料；不抓取公告、不调用 SDK、不构造交易所全年日历。

## 材料输入

`parse_material(raw: bytes, receipt: MaterialReceipt) -> ParsedMaterial`。

Receipt 必须提供调用方已有的 Source/Binding/Material Key、`calendar.trading_days` capability、synthetic/retained mode、producer operation、upstream version/revision、decoder revision、source ref、SHA-256、字节长度，以及独立的 obtained/received/known 时钟。`obtained_at <= received_at <= known_at`；known_at 可为 None，此时材料不能参与任何 as-of 结论。可选 source_published_at 不晚于 obtained_at；它不替代 known_at。所有时钟必须有时区；时序验证、可见性、supersession 和 D1 provenance 最大时间均按 UTC 瞬时比较，避免相同 ZoneInfo 的 DST 重复小时按墙钟排序；receipt 保留原始时区表示。LIVE mode 被拒绝；receipt 不建立 Source 注册、使用权或保留权。

以下完全虚构示例仅展示输入格式；日期不是实际 SH 安排：

```json
{
  "schema": "mdf.calendar.evidence.v1",
  "market": "SH",
  "timezone": "Asia/Shanghai",
  "effective": {"start": "2024-01-02", "end_exclusive": "2024-01-03"},
  "days": [{
    "claim_id": "synthetic-row-1",
    "day": "2024-01-02",
    "state": "OPEN",
    "evidence_kind": "NOTICE",
    "arrangement": "synthetic-session",
    "source_ref": "synthetic-document:line-1",
    "sessions": [{
      "id": "day",
      "start": "2024-01-02T09:00:00",
      "end": "2024-01-02T15:00:00",
      "start_fold": null,
      "end_fold": null
    }],
    "complete_after": "2024-01-02T15:30:00",
    "complete_after_fold": null,
    "supersedes": []
  }]
}
```

- 字段严格匹配；每行可选 `native_fields` object 只保留在原始字节中，不解释经济单位。每行有 `/days/N` locator。额外未知顶层/行/session 字段、重复 JSON key、NaN/Infinity/数值溢出、坏 UTF-8、坏日期、区间外行均拒绝。JSON 解码后、字段解释与 revision 编码前，所有字符串及键（含嵌套 native_fields）都检查孤立 surrogate，统一拒绝为 `ContractError("invalid_material_unicode")`，不附带原异常或字段内容；合法 surrogate pair 和直接 UTF-8 均可解析，原始字节不变。
- effective 是交易日标签的半开有效区间；每个日期都需显式行。不会将周末、空 days、缺 bar、另一市场或有效区间内的空洞补成休市。
- 输入状态为 OPEN/HALF_DAY/CLOSED/UNKNOWN。输出可为 CONFLICT。OPEN/HALF_DAY 至少一个 session；CLOSED/UNKNOWN 没有 session；UNKNOWN 没有完成时点。HALF_DAY 是材料明确声明，程序不从时长猜测。
- NOTICE/SESSION_SCHEDULE 可以构成有来源的候选结论；这只是材料种类，不是来源真实性或权利验证。MISSING_BARS/SDK_WORKDAY 不论声称 OPEN 或 CLOSED，都不能单独建立开闭市结论，也不能推翻直接材料。
- 本地时钟是完整 `YYYY-MM-DDTHH:MM:SS`，时区单独具名；不接受无时区字符串的默认宿主解释。不存在的 DST 时钟拒绝；重复时钟要求 fold=0/1。非重复时钟不得填 fold=1。session 和 cutoff 均转换并保留 UTC，session 使用 `[start,end)`，同一行 session 不可重叠。
- session 自然日与交易日标签分别提供。允许虚构的周五夜盘归属下周一，或外盘跨午夜；不会自动寻找 next_workday，也不会把 SHFE/INE/合约 session 合并。完整性依赖调用方提交的具名 session 范围。
- complete_after 是显式可用截止，不从最后一根 K 或固定收盘钟推导。可缺失，此时完成状态 UNKNOWN；已给出时不能早于最后 session 结束或交易日当地日期。跨日 session、晚到/结算延迟需由材料明确给出。

## 选择、修订和截止

`CalendarEvidence(tuple[ParsedMaterial, ...])` 对每份材料重新解析校验，拒绝伪造的解析对象、重复材料，以及同 Material Key 不同内容/元数据。材料顺序规范化，原始行顺序和原始字节保留。

- `resolve(market, day, as_of)` 返回 `DayEvidence`，含状态、稳定 reason、时区、session、cutoff、completion、所有可见 claim refs 和材料 revision。`known_at <= as_of` 才可见，未来或未知 known_at 材料不泄漏到结果。无隐式来源替换。
- Claim ref 为 `<ParsedMaterial.revision>:<claim_id>`。revision 绑定原始摘要、receipt、有效期、parser revision、行内容及实际解析的 UTC 时点；系统 tzdb 改变 UTC 结果时，revision 随之变化。当前没有固定发行版 tzdb 包；使用现有 Python/Host ZoneInfo，重新解析后才可入 book。
- 同日、同语义重复行保留所有 refs；不同状态、arrangement、session/fold/cutoff 或时区均保留 CONFLICT。较晚 known_at 不自动获胜。
- supersedes 只接受同日可见直接材料 ref、同 Source/Binding/mode/时区和严格更晚 known_at；缺目标、同钟、未来目标、跨市场/日期/来源均返回 UNKNOWN。旧材料不删除；历史 as-of 仍可复放。
- `snapshot(market, window, as_of)` 每个自然日输出一个结果。对 book 内该市场全部可见交易日的有效直接声明检查 session 跨日期重叠，包括同日 CONFLICT 或无效 supersession 导致 UNKNOWN 的未决日期；已有效替代的声明及非直接材料的 sessions 不参与。相关日期变成 CONFLICT，清除可执行 sessions/cutoff，保留该重叠连通分量的全部 refs。缩小查询窗口不能隐藏已提交的冲突。
- `completed_cutoff(...)` 返回连续已完成前缀、首个阻断日期及完整日期证据；到达显式 cutoff 的瞬间为 COMPLETE。在 UNKNOWN/CONFLICT、缺 cutoff 或 PENDING 处停止，不能取后续已完成日期的最大值。
- `locate_session(market, at, window, as_of)` 返回 MATCH/OUTSIDE_KNOWN_SESSIONS/UNKNOWN/CONFLICT。window 是调用方明确的交易日搜索范围；UNKNOWN 日期不从范围中剔除。OUTSIDE_KNOWN_SESSIONS 只表示该有界范围内未命中，不代表休市或全市场 session 完备。

book、DayEvidence、CompletionCutoff 与 D1CalendarInput 的 revision 均绑定 `a-cal.selection.v2`；本次修正未决日期的跨日期冲突传播和 DST 瞬时时序。未来选择/完成日语义变更需新算法 revision。

## 冻结 D1 输入与验证

`to_d1(calendar_id, market, window, as_of) -> D1CalendarInput`；`validate_d1(candidate, calendar_id, market, window, as_of)` 重新派生后做 canonical bytes 精确比对。

- `D1CalendarInput.calendar` 是现有 `domain_contracts.market_universe.CalendarRevision`，schema 为 `a-cal.d1-projection.v1`；其 known_at 是快照 as-of，而原材料各时钟仍在 receipt。OPEN/HALF_DAY -> is_open=True；CLOSED -> False；UNKNOWN/CONFLICT 不写入 D1 days，仍逐日留在 sidecar `days` 中。
- `CalendarDay.evidence` 精确引用 sidecar `DayEvidence.revision`；session、原行 locator、来源、修订和 cutoff 可沿 ref 回到原材料。D1 provenance 包含相关可见材料；若跨日期冲突涉及窗口外材料，也列入其 lineage。
- 只允许一个 Source/Binding/mode/decoder revision 与一个时区投影到一份 D1 provenance；多来源结果可在 A-CAL 中保留，但 D1 转换明确报 integration_required，不默选来源。
- qualification 始终 UNKNOWN，包括 synthetic。D1 的 coverage classifier 因此继续 UNKNOWN；适配器不产生市场休市资格、Provider 可执行性或 Publication 权限。所有变更过的日期、身份、资格、证据等都无法通过精确输入验证。

容量：单材料 ≤1MiB、≤8192 行、有效期/查询窗口 ≤8192 自然日；book ≤64 材料、≤8MiB 原始字节、≤32768 行；每行 ≤32 session、≤64 supersession refs。超限拒绝，绝不截断。解析和 book 构建在内存中；不承诺真实持久化或生产性能上限。

每次市场快照在冲突日期展开前，还累计该市场全部材料日期的 lineage 成本（含非冲突日，不以查询窗口裁剪）：逐日 evidence_refs 与 material_revisions 合计 ≤262144 个引用槽，逐日 `[evidence_refs, material_revisions]` 的 canonical JSON 字节合计 ≤16MiB。冲突组按“完整引用集合 × 组内日期数”计费，即使内存共享 tuple，序列化仍重复计入。任一超限均整体报 `ContractError("calendar_lineage_limit")`，包括单日 resolve、snapshot、completed_cutoff、locate_session、to_d1；不截断证据、不返回部分结果。每个冲突连通组只排序和构造一次不可变 lineage tuple，组内所有日期共享。预算针对材料日期的 lineage，不是整个响应或进程内存的字节上限；查询补出的无材料 UNKNOWN 日期及其他响应字段不计入这个 lineage 字节数。原始材料仍完整留在 book，未可见材料不贡献引用。

## TDX 边界

`parse_tdx_workday(raw, receipt)` 接受精确 v1.0.0 / `2a9000681f5a9896ea98e4de8bc4904f1dce78ba` / producer `tdx.Workday` 的 retained/synthetic 原生投影：`id:int64`、`unix:int64`、`date:string`；可选 `is_workday:bool|null` 是另外保留的 Is 调用结果，不是 WorkdayModel 原生字段。原始 bytes、整数和日期字符串不转换；calendar_state 和 clock_semantics 始终 UNKNOWN。

字段依据是父合同 `UPSTREAM_V100_FIELD_STRUCT_INDEX_R1.json` 的 `workday.go:256–260/WorkdayModel`，并继承 `UPSTREAM_V100_FIELD_SEMANTICS_GAP_AUDIT_R1.md` 的 raw/UNKNOWN 原则。实际只读 `workday.go` 的 Update 使用 `GetIndexDayAll("sh000001")`，Is 查询本地 cache；故 false 既不证明 SH 闭市，也不证明 SZ/BJ/HK/期货/外盘闭市。未初始化 Workday、客户端、SQLite、cron 或任何 Provider 请求。
