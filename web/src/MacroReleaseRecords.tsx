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

type Claim = {
  series_id: string;
  statistic: string;
  reference_month: string;
  unit: string;
  adjustment: string;
  role: string;
  value: string;
  value_identity: string;
  reported_value?: string;
  reported_unit?: string;
  normalization?: { display_decimal_places: number };
  continuous_version: { revision: number; batch_id: string } | null;
  evidence_id?: string;
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
type Evidence = {
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
type Release = Evidence & {
  event_id: string;
  reference_month: string;
  reference_months?: string[];
  conflicts?: unknown[];
  prior_event_values?: Claim[];
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
    reference_month: string;
    statistic: string;
    unit: string;
    adjustment: string;
    reason: string;
  }[];
  evidence: Evidence[];
};
type Records = {
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
  official_previous: "官方此前公布的前值",
  prior_revised: "上期值（本次修订后）",
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
  const [cutoff, setCutoff] = useState("");
  const [eventFilter, setEventFilter] = useState(initialEventId ?? "");
  const [sourceDate, setSourceDate] = useState("");
  const [reference, setReference] = useState("");
  const [mode, setMode] = useState("event");
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
    if (reference) params.set("reference_month", reference);
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
      const data = JSON.parse(text);
      if (!response.ok)
        throw new Error(data.classification ?? "release_read_failed");
      if (!Array.isArray(data.events) || data.as_of_basis !== "available")
        throw new Error("release_contract_invalid");
      setResult(data);
      setSelected((previous) =>
        data.events.some((event: Release) => event.event_id === previous)
          ? previous
          : eventFilter || null,
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
  const event = result?.events.find((item) => item.event_id === selected);
  const seriesLabel = (id: string) =>
    profile.series.find(
      ([native]) =>
        native ===
        (profile.id === "retail" && id.startsWith("44X72:") ? "headline" : id),
    )?.[1] ?? "材料所列指标";
  const statistics: Record<string, string> = {
    index: "指数水平",
    level: "水平",
    mom_pct: "环比",
    yoy_pct: "同比",
    net_change_1m: "月净变动",
  };
  const units: Record<string, string> = {
    index: "指数点",
    percent: "%",
    thousand_jobs: "千岗位",
    million_USD: "百万美元",
    "USD/hour": "美元/小时",
    hours: "小时",
  };
  const basisLabel = (basis: string) =>
    ({
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
      <DateField
        label="发布参考月"
        type="month"
        value={reference}
        onChange={setReference}
      />
      <button disabled={pending} onClick={() => void load()}>
        读取发布记录
      </button>
      <p>只读本地发布证据；不联网、不写入。</p>
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
                  <th>参考月</th>
                  <th>来源版本 / 精度</th>
                  <th>事件值</th>
                  <th>证据可用时间</th>
                </tr>
              </thead>
              <tbody>
                {result.events.map((item) => (
                  <tr key={item.event_id}>
                    <td>
                      <button onClick={() => setSelected(item.event_id)}>
                        {item.event_id}
                      </button>
                    </td>
                    <td>
                      {(item.reference_months ?? [item.reference_month]).join(
                        "、",
                      )}
                    </td>
                    <td>
                      {item.source_version.value} ·{" "}
                      {item.source_version.precision === "date"
                        ? "日期"
                        : "时刻"}
                    </td>
                    <td>
                      {item.state === "confirmed"
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
                  存在材料冲突或解析修正；按明确修正选择，否则保留先前证明值。完整选择轨迹见实际响应。
                </p>
              )}
              {!!event.prior_event_values?.length && (
                <details>
                  <summary>上次发布材料的官方前值</summary>
                  {event.prior_event_values.map((c, i) => (
                    <p key={i}>
                      {c.series_id} · {c.reference_month} · {c.statistic}：
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
                      {scope.reference_month} · {scope.series_id} ·{" "}
                      {scope.statistic} · {scope.adjustment}
                      ：该口径未在所选官方材料中确认。
                    </p>
                  ))}
                </details>
              )}
              <div className="table-wrap">
                <table aria-label="事件公布与修订值">
                  <thead>
                    <tr>
                      <th>指标 / 统计量</th>
                      <th>参考月</th>
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
                        <td>{claim.reference_month}</td>
                        <td>{roles[claim.role] ?? claim.role}</td>
                        <td>
                          {claim.value} {units[claim.unit] ?? claim.unit}
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
                              {claim.locator.kind === "official_table_cell"
                                ? `${tableLabel(claim.locator.table)} · ${claim.locator.row} · ${claim.locator.column}`
                                : `材料段落 ${claim.locator.start}–${claim.locator.end}`}
                            </summary>
                            {claim.locator.excerpt}
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
                  </p>
                  <p>
                    材料留存：{proof.retention.kind} ·{" "}
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
                  <pre
                    style={{
                      whiteSpace: "pre-wrap",
                      overflowWrap: "anywhere",
                    }}
                  >
                    {JSON.stringify(proof.claims, null, 2)}
                  </pre>
                </details>
              ))}
            </section>
          )}
        </>
      )}
    </section>
  );
}
