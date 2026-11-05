import { normalizeClaimsReleases } from "./claimsReleaseAdapter";
import { retailDecimalText } from "./decimalValue";
import { QuarterField } from "./QuarterField";
import { normalizeGdpReleases } from "./gdpReleaseAdapter";
import { useEffect, useRef, useState } from "react";
import type { Transport } from "./api";
import type { MacroProfile } from "./macroTimeSeriesProfile";
import {
  formatBeijingTime as beijingTime,
  beijingInputToIso,
} from "./beijingTime";
import { DateField } from "./DateField";
import { releaseReasonText, releaseReasonTexts } from "./macroReleaseLinks";
import { HttpExchangePanel, RequestPreview } from "./CpiQueryDetails";
import type { CpiHttpExchange } from "./cpiTimeSeriesApi";
import { Tabs } from "./WorkflowTable";

type Locator =
  | {
      kind: "official_report_fragment";
      page: number | null;
      reference_header: string;
      excerpt: string;
    }
  | {
      kind: "official_table_cell";
      table: string;
      row: string;
      column: string;
      excerpt: string;
    }
  | {
      kind: "normalized_text_span";
      start: number;
      end: number;
      excerpt: string;
    };

export type Claim = {
  series_id: string;
  statistic: string;
  reference_week?: string;
  reference_month?: string;
  reference_quarter?: string;
  unit: string;
  adjustment: string;
  role: string;
  value: string | null;
  value_identity: string | null;
  estimate_kind?: string;
  reported_value?: string;
  reported_unit?: string;
  normalization?: { display_decimal_places: number };
  continuous_version: { revision: number; batch_id: string } | null;
  evidence_id?: string;
  evidence_ids?: string[];
  confirmation_state?: string;
  association_missing?: string | null;
  mdf_first_captured?: { value: string; versions: unknown[] } | null;
  mdf_pre_event_last_consumable_previous?: {
    value: string;
    versions: unknown[];
  } | null;
  mdf_pre_event_previous_missing?: string | null;
  consensus?: { source: string; value: string } | null;
  surprise?: string | null;
  surprise_missing?: string[];
  locator: Locator;
};
export type Evidence = {
  evidence_id: string;
  raw_sha256: string;
  raw_locator: string;
  obtained_at: string;
  available_at: string;
  parser_revision: string;
  source_url: string;
  retention: {
    kind: string;
    exact_original_retained: boolean;
    coverage: string;
  };
  source_version: {
    value: string;
    precision: "date" | "instant";
    basis: string;
    source: string;
  };
  claims: Claim[];
};
type ValueComparison = {
  series_id: string;
  statistic: string;
  unit: string;
  adjustment: string;
  reference_month: string;
  actual: Claim | null;
  official_previous: Claim | null;
  previous_reference_month: string;
  previous_as_reported_in_this_release: Claim | null;
  revision_difference: string | null;
  revision_history: {
    reference_month: string;
    official_previous: Claim | null;
    previous_as_reported_in_this_release: Claim | null;
    revision_difference: string | null;
    original_basis: string | null;
  }[];
  mdf_pre_event_last_consumable_previous: {
    value: string;
    versions: unknown[];
  } | null;
  mdf_pre_event_previous_basis: string | null;
  missing: string[];
};
export type Release = Evidence & {
  event_id: string;
  reference_week?: string;
  reference_month?: string;
  reference_quarter?: string;
  reference_weeks?: string[];
  reference_months?: string[];
  reference_quarters?: string[];
  estimate_kind?: string;
  previous_estimate_revision_pp?: string | null;
  conflicts?: unknown[];
  prior_event_values?: Claim[];
  value_comparisons?: ValueComparison[];
  shared_publication?: {
    event_id: string;
    current_asset: string;
    member_assets: string[];
    material_digests: string[];
    numeric_snapshot: string;
    other_asset_status: string;
  };
  actual_at: string | null;
  state: "confirmed" | "unknown";
  consensus_snapshots?: {
    source: string;
    series_id: string;
    value: string;
    unit: string;
    obtained_at: string;
    source_available_at: unknown;
    frozen_at: unknown;
    provenance?: { synthetic?: boolean };
  }[];
  missing: string[];
  missing_scopes?: {
    series_id: string;
    reference_week?: string;
    reference_month?: string;
    reference_quarter?: string;
    statistic?: string;
    unit?: string;
    adjustment?: string;
    reason: string;
  }[];
  evidence: Evidence[];
};
export type Records = {
  read_mode: "event" | "source_history";
  as_of: string;
  as_of_basis: "available";
  events: Release[];
  missing: string[];
  provider_requests: number;
  persisted: false;
};
const roles: Record<string, string> = {
  actual: "本次官方公布值",
  official_previously_reported_prior: "官方原前值（本次报告明确引述）",
  prior_as_reported_in_this_event: "本次报告前值（含修订）",
  official_previous: "官方此前公布的前值",
  prior_revised: "上期值（本次修订后）",
  previous_estimate: "同季度前一次估计",
  prior_original: "上季度修订前值",
};

export function MacroReleaseRecords({
  transport,
  profile,
  initialEventId,
  initialEvidenceId,
}: {
  transport: Transport;
  profile: MacroProfile;
  initialEventId?: string | null;
  initialEvidenceId?: string | null;
}) {
  const claims = profile.id === "claims";
  const gdp = profile.id === "gdp";
  const referenceText = (value: {
    reference_quarter?: string;
    reference_week?: string;
    reference_month?: string;
  }) =>
    value.reference_week ??
    value.reference_quarter ??
    value.reference_month ??
    "未知";
  const [cutoff, setCutoff] = useState("");
  const [eventFilter, setEventFilter] = useState(initialEventId ?? "");
  const [sourceDate, setSourceDate] = useState("");
  const [reference, setReference] = useState("");
  const [mode, setMode] = useState("event");
  const recordId = (record: Release, readMode: string) =>
    claims && readMode === "source_history"
      ? `${record.event_id}:${record.evidence_id}`
      : record.event_id;
  const [result, setResult] = useState<Records | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [exchange, setExchange] = useState<CpiHttpExchange | null>(null);
  const activeRequest = useRef<AbortController | null>(null);
  const [failure, setFailure] = useState("");
  const [pending, setPending] = useState(false);
  const [responseView, setResponseView] = useState(false);
  const [request, setRequest] = useState("");
  async function load() {
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    const signal = AbortSignal.any([
      controller.signal,
      AbortSignal.timeout(20000),
    ]);
    setPending(true);
    setFailure("");
    setResult(null);
    setExchange(null);
    setRequest("");
    const params = new URLSearchParams({ read_mode: mode });
    if (cutoff) {
      const value = beijingInputToIso(cutoff);
      if (!value) {
        setPending(false);
        setFailure("请选择有效的北京时间截止");
        return;
      }
      params.set("as_of", value);
    }
    if (eventFilter.trim()) params.set("event_id", eventFilter.trim());
    if (sourceDate) params.set("source_version", sourceDate);
    if (reference)
      params.set(
        claims
          ? "reference_week"
          : gdp
            ? "reference_quarter"
            : "reference_month",
        reference,
      );
    const path = `${profile.basePath}/releases?${params}`;
    setRequest(path);
    const startedAt = performance.now();
    try {
      const response = await transport(path, {
        signal,
        method: "GET",
        cache: "no-store",
        credentials: "same-origin",
      });
      const text = await response.text();
      if (controller.signal.aborted) return;
      setExchange({
        method: "GET",
        url: new URL(path, window.location.origin).href,
        status: response.status,
        bodyText: text,
        elapsedMs: Math.max(0, performance.now() - startedAt),
        completedAt: new Date().toISOString(),
      });
      const raw = JSON.parse(text);
      const data: Records =
        response.ok && claims
          ? normalizeClaimsReleases(raw)
          : response.ok && gdp
            ? normalizeGdpReleases(raw)
            : raw;
      if (!response.ok)
        throw new Error(raw.classification ?? "release_read_failed");
      if (!Array.isArray(data.events) || data.as_of_basis !== "available")
        throw new Error("release_contract_invalid");
      setResult(data);
      setSelected((previous) =>
        data.events.some(
          (event: Release) => recordId(event, data.read_mode) === previous,
        )
          ? previous
          : (() => {
              const match = data.events.find((e) => e.event_id === eventFilter);
              return match ? recordId(match, data.read_mode) : null;
            })(),
      );
    } catch (error) {
      if (!controller.signal.aborted)
        setFailure(
          error instanceof Error ? error.message : "release_read_failed",
        );
    } finally {
      if (!controller.signal.aborted) setPending(false);
    }
  }
  useEffect(() => {
    void load();
    return () => activeRequest.current?.abort();
    // Fetch on mount, then only on explicit reads. Filter edits do not run IO.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profile.id]);
  const event = result?.events.find(
    (item) => recordId(item, result.read_mode) === selected,
  );
  const seriesLabel = (id: string) =>
    profile.series.find(
      ([native]) =>
        native ===
        (profile.id === "retail" && id.startsWith("44X72:") ? "headline" : id),
    )?.[1] ?? "材料所列指标";
  const statistics: Record<string, string> = {
    weekly_level: "周水平",
    official_four_week_average: "官方四周均值",
    index: "指数水平",
    level: "水平",
    mom_pct: "环比",
    yoy_pct: "同比",
    net_change_1m: "月净变动",
    official_annualized_qoq: "官方年化季环比",
  };
  const units: Record<string, string> = {
    Number: "原始计数",
    index: "指数点",
    percent: "%",
    thousand_jobs: "千岗位",
    million_USD: "百万美元",
    "USD/hour": "美元/小时",
    hours: "小时",
  };
  const basisLabel = (basis: string) =>
    ({
      official_report_header: "DOL官方报告表头日期",
      BLS_official_archive_filename: "官方档案文件日期",
      official_material_release_date: "材料声明发布日期",
    })[basis] ?? "未知（来源依据未确认）";
  const tableLabel = (table: string) =>
    ({ cpipress1: "CPI 表1", ppi_nrtablea: "PPI 表A", ces_table: "就业表" })[
      table
    ] ?? table;
  return (
    <section
      aria-label={`${profile.label} 发布记录`}
      style={{ minWidth: 0, overflowWrap: "anywhere" }}
    >
      <p>
        发布材料证明的数值独立于连续序列首次捕获值。日期级档案不证明盘中发布时间；历史读取只使用截止时已可消费的证据。
      </p>
      <label>
        读取类型
        <select
          aria-label="发布读取类型"
          value={mode}
          onChange={(e) => setMode(e.target.value)}
        >
          <option value="event">指定发布事件</option>
          <option value="source_history">来源历史版本（具名档案）</option>
        </select>
      </label>
      <DateField
        label="发布证据截止"
        type="datetime-local"
        value={cutoff}
        onChange={setCutoff}
      />
      <p className="small">
        MDF可消费截止（北京时间）；留空读取当前已持有证据。
      </p>
      <label>
        事件编号
        <input
          aria-label="发布事件编号"
          value={eventFilter}
          onChange={(e) => setEventFilter(e.target.value)}
        />
      </label>
      <DateField
        label="来源版本日期"
        type="date"
        value={sourceDate}
        onChange={setSourceDate}
      />
      {gdp ? (
        <QuarterField
          label="发布参考季度"
          value={reference}
          onChange={setReference}
        />
      ) : (
        <DateField
          label={claims ? "发布参考周（周六）" : "发布参考月"}
          type={claims ? "date" : "month"}
          value={reference}
          onChange={setReference}
        />
      )}
      <button disabled={pending} onClick={() => void load()}>
        读取发布记录
      </button>
      <p>只读本地发布证据；不联网、不写入。</p>
      {claims && (
        <p>
          本次材料公布值、官方原前值与本次报告前值分别列示。MDF事前值缺少实际发布截止及版本证据时保持未知。材料仅覆盖已留存报告；事件值尚未绑定连续版本，不能替代当前序列值。
        </p>
      )}
      {gdp && (
        <p>
          按季度与 advance / second / third 等估计区分。摘要材料目前仅覆盖
          A191RL；其余七项尚无合格档案表，不以日历计划推定实际公布值。连续值
          unknown / 未绑定不代表官方首发。
        </p>
      )}
      {request && <RequestPreview request={{ method: "GET", path: request }} />}
      {exchange && (
        <Tabs
          label={`${profile.label} 发布记录结果视图`}
          values={[
            ["table", "表格"],
            ["raw", "原始响应"],
          ]}
          value={responseView ? "raw" : "table"}
          onChange={(value) => setResponseView(value === "raw")}
        />
      )}
      {pending && <p role="status">正在读取发布证据…</p>}
      {failure && (
        <p role="alert">发布记录读取失败：{releaseReasonText(failure)}</p>
      )}
      {exchange && (
        <HttpExchangePanel exchange={exchange} showBody={responseView} />
      )}
      {failure && !exchange && (
        <p>未收到可读取的 HTTP 响应，无法提供 HTTP 状态或响应正文。</p>
      )}
      {!responseView && result && (
        <>
          <p>
            证据可用截止：{beijingTime(result.as_of)} · 来源请求：
            {result.provider_requests}
          </p>
          {releaseReasonTexts(result.missing).map((reason) => (
            <p key={reason}>缺项：{reason}</p>
          ))}
          <div className="table-wrap">
            <table aria-label="发布事件列表">
              <thead>
                <tr>
                  <th>事件</th>
                  <th>
                    {claims ? "截止周" : gdp ? "参考季度 / 估计" : "参考月"}
                  </th>
                  <th>来源版本 / 精度</th>
                  <th>事件值</th>
                  <th>证据可用时间</th>
                </tr>
              </thead>
              <tbody>
                {result.events.map((item) => (
                  <tr key={recordId(item, result.read_mode)}>
                    <td>
                      <button
                        onClick={() =>
                          setSelected(recordId(item, result.read_mode))
                        }
                      >
                        {item.event_id}
                      </button>
                    </td>
                    <td>
                      {(
                        item.reference_weeks ??
                        item.reference_quarters ??
                        item.reference_months ?? [referenceText(item)]
                      ).join("、")}
                      {item.estimate_kind && ` · ${item.estimate_kind}`}
                    </td>
                    <td>
                      {item.source_version.value} ·{" "}
                      {item.source_version.precision === "date"
                        ? "日期"
                        : "时刻"}
                    </td>
                    <td>
                      {claims && item.conflicts?.length
                        ? "材料存在冲突"
                        : item.state === "confirmed"
                          ? "材料已确认"
                          : "当期值缺证据"}
                    </td>
                    <td>{beijingTime(item.available_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {event && (
            <section aria-label="发布事件详情">
              <h4>{event.event_id}</h4>
              {gdp && (
                <p>
                  季度：{referenceText(event)} · 估计：{event.estimate_kind}
                  ；同季度较前一次估计修订：
                  {event.previous_estimate_revision_pp ?? "缺证据"} 百分点。
                </p>
              )}
              <p>
                实际发布时间：
                {event.actual_at
                  ? beijingTime(event.actual_at)
                  : "未知（未获足够证据）"}
              </p>
              <p>
                来源历史依据：{event.source_version.source} ·{" "}
                {basisLabel(event.source_version.basis)} ·{" "}
                {event.source_version.value}（{event.source_version.precision}）
              </p>
              <p>
                市场共识、预期差和MDF持有值逐口径列出。未具备合格共识时预期差未计算，缺项保留原因。
              </p>
              {releaseReasonTexts(event.missing).map((reason) => (
                <p key={reason}>缺项：{reason}</p>
              ))}
              <details>
                <summary>技术缺项代码</summary>
                <pre>{event.missing.join("\n")}</pre>
              </details>
              {event.shared_publication && (
                <section aria-label="BEA共享发布">
                  <h5>同次BEA发布，独立指标结果</h5>
                  <p>
                    价格、消费与收入共享事件 {event.shared_publication.event_id}{" "}
                    及可复用原文；本响应仅核实当前资产，其他资产结果需分别读取。多次API取得不构成原子官方快照。
                  </p>
                  <p className="small">
                    当前材料内容凭据：
                    {event.shared_publication.material_digests.join(" · ")}
                  </p>
                </section>
              )}
              {!!event.value_comparisons?.length && (
                <section aria-label="发布值与前值对照">
                  <h5>本次公布与修订对照</h5>
                  <p className="small">
                    下列actual由材料独立支持，不要求continuous_version已绑定；缺项不补零。原前值、本次报告前值和MDF事前持有值分别保留。
                  </p>
                  {event.value_comparisons.map((row, i) => (
                    <details
                      key={`${row.series_id}/${row.statistic}/${row.reference_month}`}
                      open={i === 0}
                    >
                      <summary>
                        {seriesLabel(row.series_id)} ·{" "}
                        {statistics[row.statistic] ?? row.statistic} ·{" "}
                        {row.reference_month}
                      </summary>
                      <dl>
                        <div className="fact">
                          <dt>本次官方公布值</dt>
                          <dd>
                            {row.actual?.value ?? "材料冲突或尚未确认"}{" "}
                            {units[row.unit] ?? row.unit}
                          </dd>
                        </div>
                        <div className="fact">
                          <dt>官方原前值（{row.previous_reference_month}）</dt>
                          <dd>
                            {row.official_previous?.value ?? "缺少匹配官方证据"}
                          </dd>
                        </div>
                        <div className="fact">
                          <dt>
                            本次报告前值（{row.previous_reference_month}）
                          </dt>
                          <dd>
                            {row.previous_as_reported_in_this_release?.value ??
                              "本次材料未明确说明"}
                          </dd>
                        </div>
                        <div className="fact">
                          <dt>官方前值修订差</dt>
                          <dd>
                            {row.revision_difference ??
                              "未计算（原前值或本次前值缺证据）"}
                          </dd>
                        </div>
                        <div className="fact">
                          <dt>MDF发布前持有的上期值</dt>
                          <dd>
                            {row.mdf_pre_event_last_consumable_previous
                              ?.value ?? "无足够时间与版本证据"}
                          </dd>
                        </div>
                      </dl>
                      <p className="small">
                        MDF前值依据：
                        {row.mdf_pre_event_previous_basis ===
                        "strictly_before_official_release_date_lower_bound"
                          ? "严格早于官方发布日期下界；不推定盘中顺序"
                          : row.mdf_pre_event_previous_basis ===
                              "strictly_before_proven_event_instant"
                            ? "严格早于已证明的发布时刻"
                            : "时间依据未确认"}
                        。口径：{row.series_id} / {row.statistic} /{" "}
                        {row.adjustment} / {row.unit}。
                      </p>
                      {row.revision_history
                        .filter(
                          (r) =>
                            r.reference_month !== row.previous_reference_month,
                        )
                        .map((r) => (
                          <p key={r.reference_month}>
                            同时涉及 {r.reference_month}：官方原前值{" "}
                            {r.official_previous?.value ?? "缺证据"}
                            ；本次报告前值{" "}
                            {r.previous_as_reported_in_this_release?.value ??
                              "缺证据"}
                            ；修订差 {r.revision_difference ?? "未计算"}。
                          </p>
                        ))}
                      <details>
                        <summary>本项材料与缺项依据</summary>
                        {[
                          row.actual,
                          row.official_previous,
                          row.previous_as_reported_in_this_release,
                        ]
                          .filter((c): c is Claim => !!c)
                          .map((c, index) => (
                            <p key={index}>
                              {roles[c.role] ?? c.role} · {c.reference_month} ·
                              材料 {c.evidence_id ?? "未记录"} ·{" "}
                              {c.locator.excerpt}
                            </p>
                          ))}
                        {row.missing.map((reason) => (
                          <p key={reason}>{releaseReasonText(reason)}</p>
                        ))}
                      </details>
                    </details>
                  ))}
                </section>
              )}
              {!!event.consensus_snapshots?.length && (
                <details>
                  <summary>
                    已留存具名共识快照（{event.consensus_snapshots.length}项）
                  </summary>
                  {event.consensus_snapshots.map((snapshot, i) => (
                    <p key={i}>
                      {snapshot.source}
                      {snapshot.provenance?.synthetic
                        ? "（合成测试）"
                        : ""} · {snapshot.series_id} · {snapshot.value}{" "}
                      {snapshot.unit}；MDF取得于{" "}
                      {beijingTime(snapshot.obtained_at)}
                      。来源可得/冻结依据见实际响应。
                    </p>
                  ))}
                </details>
              )}
              {!!event.conflicts?.length && (
                <p>
                  {claims
                    ? "存在材料冲突；冲突项不选定值，各份材料原始值与原文分别保留。"
                    : "存在材料冲突或解析修正；按明确修正选择，否则保留先前证明值。完整选择轨迹见实际响应。"}
                </p>
              )}
              {!!event.prior_event_values?.length && (
                <details>
                  <summary>上次发布材料的官方前值</summary>
                  {event.prior_event_values.map((c, i) => (
                    <p key={i}>
                      {c.series_id} · {referenceText(c)} · {c.statistic}：
                      {c.value} {c.unit}
                    </p>
                  ))}
                </details>
              )}
              {!!event.missing_scopes?.length && (
                <details>
                  <summary>
                    材料未覆盖口径（{event.missing_scopes.length}项）
                  </summary>
                  {event.missing_scopes.map((scope, i) => (
                    <p key={i}>
                      {referenceText(scope)} · {scope.series_id} ·{" "}
                      {scope.statistic} · {scope.adjustment}
                      ：该口径未在所选官方材料中确认。{gdp && scope.reason}
                    </p>
                  ))}
                </details>
              )}
              <div className="table-wrap">
                <table aria-label="事件公布与修订值">
                  <thead>
                    <tr>
                      <th>指标 / 统计量</th>
                      <th>
                        {claims ? "截止周" : gdp ? "参考季度 / 估计" : "参考月"}
                      </th>
                      <th>角色</th>
                      <th>值 / 单位</th>
                      <th>原文定位</th>
                    </tr>
                  </thead>
                  <tbody>
                    {event.claims.map((claim, index) => (
                      <tr key={index}>
                        <td>
                          {seriesLabel(claim.series_id)} /{" "}
                          {statistics[claim.statistic] ?? claim.statistic}
                          <details>
                            <summary>指标口径编号</summary>
                            <p style={{ overflowWrap: "anywhere" }}>
                              {claim.series_id} · {claim.statistic} ·{" "}
                              {claim.adjustment}
                            </p>
                          </details>
                        </td>
                        <td>{referenceText(claim)}</td>
                        <td>
                          {roles[claim.role] ?? claim.role}
                          {claim.estimate_kind && ` · ${claim.estimate_kind}`}
                        </td>
                        <td>
                          {claims
                            ? claim.value === null
                              ? claim.confirmation_state === "conflict"
                                ? "材料冲突（未选定值）"
                                : "未知（材料未说明）"
                              : retailDecimalText(claim.value)
                            : (claim.value ?? "—")}{" "}
                          {units[claim.unit] ?? claim.unit}
                          {claim.reported_unit === "billion_USD" && (
                            <p className="small">
                              原文：{claim.reported_value} 十亿美元 （显示
                              {claim.normalization?.display_decimal_places}
                              位小数；已换算为百万美元）
                            </p>
                          )}
                          <details>
                            <summary>MDF捕获值、发布前值与共识</summary>
                            <p>
                              首次捕获：
                              {claim.mdf_first_captured?.value ??
                                "未捕获此统计量"}
                              ；首次捕获不等于官方首发。
                            </p>
                            <p>
                              发布前最后可消费的上期值：
                              {claim.mdf_pre_event_last_consumable_previous
                                ?.value ?? "无足够时间及版本证据"}
                            </p>
                            <p>
                              具名共识：
                              {claim.consensus
                                ? `${claim.consensus.source} · ${claim.consensus.value}`
                                : "无合格来源"}
                              ；预期差：{claim.surprise ?? "未计算"}
                            </p>
                            {claim.surprise_missing?.map((r) => (
                              <p key={r}>{releaseReasonText(r)}</p>
                            ))}
                            <p>
                              连续版本：
                              {claim.continuous_version
                                ? `修订 ${claim.continuous_version.revision} · 批次 ${claim.continuous_version.batch_id}`
                                : "未绑定（不以数值相等推断）"}
                            </p>
                            <details>
                              <summary>版本与批次依据</summary>
                              <pre>
                                {JSON.stringify(
                                  {
                                    first: claim.mdf_first_captured,
                                    prior:
                                      claim.mdf_pre_event_last_consumable_previous,
                                  },
                                  null,
                                  2,
                                )}
                              </pre>
                            </details>
                          </details>
                        </td>
                        <td>
                          <details>
                            <summary>
                              {claims && claim.confirmation_state === "conflict"
                                ? "多材料冲突，分别查看原始材料"
                                : claim.locator.kind ===
                                    "official_report_fragment"
                                  ? `报告第${claim.locator.page ?? "未知"}页 · ${claim.locator.reference_header}`
                                  : claim.locator.kind === "official_table_cell"
                                    ? `${tableLabel(claim.locator.table)} · ${claim.locator.row} · ${claim.locator.column}`
                                    : `材料段落 ${claim.locator.start}–${claim.locator.end}`}
                            </summary>
                            {claim.locator.excerpt}
                            {claims &&
                              (claim.evidence_ids ?? [claim.evidence_id]).map(
                                (id) => {
                                  const proof = event.evidence.find(
                                    (p) => p.evidence_id === id,
                                  );
                                  const original = proof?.claims.find(
                                    (c) =>
                                      c.series_id === claim.series_id &&
                                      c.reference_week ===
                                        claim.reference_week &&
                                      c.role === claim.role,
                                  );
                                  return proof && original ? (
                                    <p key={id}>
                                      支撑材料：
                                      <button
                                        type="button"
                                        onClick={() => {
                                          const target =
                                            document.getElementById(
                                              `claims-proof-${proof.evidence_id}`,
                                            ) as HTMLDetailsElement | null;
                                          if (target) {
                                            target.open = true;
                                            target.scrollIntoView?.({
                                              block: "nearest",
                                            });
                                          }
                                        }}
                                      >
                                        {proof.evidence_id}
                                      </button>
                                      ；原文值：
                                      {original.value === null
                                        ? "未知"
                                        : retailDecimalText(original.value)}
                                      ；{original.locator.excerpt}
                                      ；材料摘要：{proof.raw_sha256}
                                    </p>
                                  ) : null;
                                },
                              )}
                            <p>
                              内容身份：{claim.value_identity}
                              ；选定证据：{claim.evidence_id ?? "未记录"}
                            </p>
                          </details>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <h4>证据与解析版本</h4>
              {event.evidence.map((proof) => (
                <details
                  key={proof.evidence_id}
                  id={claims ? `claims-proof-${proof.evidence_id}` : undefined}
                  open={
                    initialEvidenceId === proof.evidence_id ? true : undefined
                  }
                >
                  <summary>
                    {proof.parser_revision} · 可用{" "}
                    {beijingTime(proof.available_at)}
                  </summary>
                  <p>
                    <a href={proof.source_url} target="_blank" rel="noreferrer">
                      正式来源材料
                    </a>{" "}
                    · 取得 {beijingTime(proof.obtained_at)}
                    {claims && ` · 来源版本 ${proof.source_version.value}`}
                  </p>
                  <p>
                    材料留存：
                    {claims
                      ? ({
                          official_archive: "官方档案",
                          official_current_report_snapshot: "官方当前报告快照",
                        }[proof.retention.kind] ?? "已保存材料")
                      : proof.retention.kind}{" "}
                    ·{" "}
                    {proof.retention.exact_original_retained
                      ? "原始HTTP字节（导入方声明）"
                      : "非完整原始HTTP字节"}{" "}
                    · {proof.retention.coverage}
                  </p>
                  <p style={{ overflowWrap: "anywhere" }}>
                    SHA256：{proof.raw_sha256}
                    <br />
                    留存：{proof.raw_locator}
                    <br />
                    证据：{proof.evidence_id}
                  </p>
                  {claims && (
                    <details>
                      <summary>
                        本材料原始公布与修订值（{proof.claims.length}项）
                      </summary>
                      {proof.claims.map((c, i) => (
                        <p key={i}>
                          {seriesLabel(c.series_id)} · {referenceText(c)} ·{" "}
                          {roles[c.role] ?? c.role}：
                          {c.value === null
                            ? "未知（材料未说明）"
                            : retailDecimalText(c.value)}
                          ；{c.locator.excerpt}
                        </p>
                      ))}
                    </details>
                  )}
                  <details>
                    <summary>材料与解析技术字段</summary>
                    <pre
                      style={{
                        whiteSpace: "pre-wrap",
                        overflowWrap: "anywhere",
                      }}
                    >
                      {JSON.stringify(proof.claims, null, 2)}
                    </pre>
                  </details>
                </details>
              ))}
            </section>
          )}
        </>
      )}
    </section>
  );
}
