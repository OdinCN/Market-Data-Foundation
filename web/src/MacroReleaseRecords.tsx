import { useEffect, useState } from "react";
import type { Transport } from "./api";
import type { MacroProfile } from "./macroTimeSeriesProfile";
import { formatBeijingTime as beijingTime } from "./beijingTime";
import { RequestPreview } from "./CpiQueryDetails";

type Claim = {
  series_id: string;
  statistic: string;
  reference_month: string;
  unit: string;
  adjustment: string;
  role: string;
  value: string;
  value_identity: string;
  continuous_version: null;
  locator: { kind: string; start: number; end: number; excerpt: string };
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
  actual_at: string | null;
  state: "confirmed" | "unknown";
  missing: string[];
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
}: {
  transport: Transport;
  profile: MacroProfile;
}) {
  const [cutoff, setCutoff] = useState("");
  const [mode, setMode] = useState("event");
  const [result, setResult] = useState<Records | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [raw, setRaw] = useState("");
  const [failure, setFailure] = useState("");
  const [pending, setPending] = useState(false);
  const [responseView, setResponseView] = useState(false);
  const [request, setRequest] = useState("");
  async function load(signal?: AbortSignal) {
    setPending(true);
    setFailure("");
    setResult(null);
    setRaw("");
    const params = new URLSearchParams({ read_mode: mode });
    if (cutoff.trim()) params.set("as_of", cutoff.trim());
    const path = `${profile.basePath}/releases?${params}`;
    setRequest(path);
    try {
      const response = await transport(path, { signal });
      const text = await response.text();
      if (signal?.aborted) return;
      setRaw(text);
      const data = JSON.parse(text);
      if (!response.ok)
        throw new Error(data.classification ?? "release_read_failed");
      if (!Array.isArray(data.events) || data.as_of_basis !== "available")
        throw new Error("release_contract_invalid");
      setResult(data);
      setSelected((previous) =>
        data.events.some((event: Release) => event.event_id === previous)
          ? previous
          : null,
      );
    } catch (error) {
      if (!signal?.aborted)
        setFailure(
          error instanceof Error ? error.message : "release_read_failed",
        );
    } finally {
      if (!signal?.aborted) setPending(false);
    }
  }
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
    // Fetch on mount, then only on explicit reads. Filter edits do not run IO.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profile.id]);
  const event = result?.events.find((item) => item.event_id === selected);
  return (
    <section aria-label={`${profile.label} 发布记录`}>
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
      <label>
        MDF可用截止（含时区）
        <input
          aria-label="发布证据截止"
          placeholder="2026-11-01T10:00:00Z"
          value={cutoff}
          onChange={(e) => setCutoff(e.target.value)}
        />
      </label>
      <button disabled={pending} onClick={() => void load()}>
        读取发布记录
      </button>
      <p>
        GET <code>{request}</code> · 只读本地；不联网、不写入
      </p>
      {request && <RequestPreview request={{ method: "GET", path: request }} />}
      <button onClick={() => setResponseView(!responseView)}>
        {responseView ? "表格" : "实际响应"}
      </button>
      {pending && <p role="status">正在读取发布证据…</p>}
      {failure && <p role="alert">发布记录读取失败：{failure}</p>}
      {responseView ? (
        <pre
          aria-label="发布记录实际HTTP正文"
          style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}
        >
          {raw}
        </pre>
      ) : (
        result && (
          <>
            <p>
              证据可用截止：{beijingTime(result.as_of)} · 来源请求：
              {result.provider_requests}
            </p>
            {result.missing.map((reason) => (
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
                      <td>{item.reference_month}</td>
                      <td>
                        {item.source_version.value} ·{" "}
                        {item.source_version.precision}
                      </td>
                      <td>
                        {item.state === "confirmed"
                          ? "材料已确认"
                          : "unknown · 当期值缺证据"}
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
                  {event.source_version.basis} · {event.source_version.value}（
                  {event.source_version.precision}）
                </p>
                <p>
                  市场共识缺失；预期差未计算。MDF发布前持有值尚未绑定；不以官方前值替代。
                </p>
                {event.missing.map((reason) => (
                  <p key={reason}>缺项：{reason}</p>
                ))}
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
                            {claim.series_id} / {claim.statistic} /{" "}
                            {claim.adjustment}
                          </td>
                          <td>{claim.reference_month}</td>
                          <td>{roles[claim.role] ?? claim.role}</td>
                          <td>
                            {claim.value} {claim.unit}
                          </td>
                          <td>
                            <details>
                              <summary>
                                材料段落 {claim.locator.start}–
                                {claim.locator.end}
                              </summary>
                              {claim.locator.excerpt}
                              <p>
                                内容身份：{claim.value_identity}
                                ；连续版本：未绑定
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
                  <details key={proof.evidence_id}>
                    <summary>
                      {proof.parser_revision} · 可用{" "}
                      {beijingTime(proof.available_at)}
                    </summary>
                    <p>
                      <a
                        href={proof.source_url}
                        target="_blank"
                        rel="noreferrer"
                      >
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
        )
      )}
    </section>
  );
}
