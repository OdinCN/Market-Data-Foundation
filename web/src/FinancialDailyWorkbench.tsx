import { useEffect, useState } from "react";
import type { Navigate, Route } from "./workflowRoute";
import type { Transport } from "./api";
import { useVisiblePolling } from "./useVisiblePolling";
import { WorkflowTable } from "./WorkflowTable";
import { requestId } from "./CpiTimeSeriesCollection";

export const FINANCIAL = {
  identity: "mdf.macro.financial-conditions.daily",
  revision: "r1",
};
const prefix = "/api/r5/financial/timeseries";
type Coverage = {
  series_id: string;
  valid_rows: number;
  explicit_missing: number;
  first: string | null;
  last: string | null;
  calendar_days_without_row: number;
  gap_explanation: string;
};
type Step = {
  series_id: string;
  start: string;
  end: string;
  state: string;
  source: string;
  provider_requests?: number;
  classification?: string;
  retry_at?: string;
  result?: { batch_id: string; inserted_versions: number };
  valid_rows?: number;
  retained?: { raw_sha: string; metadata_sha: string; observed_at: string };
};
export type FinancialJob = {
  request_id: string;
  state: string;
  classification?: string;
  created_at: string;
  provider_requests: number;
  command: { action: string; start: string; end: string };
  steps: Step[];
  coverage?: { series: Coverage[]; as_of: string };
};
type Metric = {
  series_id: string;
  title: string;
  active_binding: {
    attribution: string;
    required_notice?: string;
    affiliation_notice?: string;
    identity: string;
    source_url: string;
  };
};
type Status = {
  defaults: { start: string; end: string };
  series: Metric[];
  cutoff_semantics: string;
};
type Point = {
  series_id: string;
  observation_date: string;
  value: string | null;
  basis_points: string | null;
  state: string;
  origin: string | null;
  revision: number | null;
  observed_at: string | null;
  available_at: string | null;
  is_copyrighted: boolean | null;
  raw_locator: string | null;
  identity: string | null;
  attribution: string;
  provider_extra: unknown;
};
type Query = {
  rows: Point[];
  as_of: string;
  total: number;
  next_offset: number | null;
  provider_requests: number;
  coverage: { series: Coverage[] };
};
const names: Record<string, string> = {
  DFEDTARL: "政策目标下限",
  DFEDTARU: "政策目标上限",
  EFFR: "有效联邦基金利率",
  SOFR: "担保隔夜融资利率",
  DGS2: "2 年名义收益率",
  DGS10: "10 年名义收益率",
  DFII10: "10 年实际收益率",
  T10YIE: "MDF 通胀补偿",
  BAMLC0A0CM: "投资级 OAS",
  BAMLH0A0HYM2: "高收益 OAS",
};
const states: Record<string, string> = {
  pending: "等待",
  running: "进行中",
  complete: "完成",
  partial: "部分完成，可续跑",
  failed: "失败",
  unknown: "结果待核实",
  cooling_down: "来源冷却中",
  observed: "有值",
  provider_missing: "来源明确缺值",
  provider_field_absent: "原文未提供该期限字段",
  non_observation_day: "非观测日",
  not_collected: "未采集",
  not_known_at_cutoff: "截止前尚未知",
  outside_provider_range: "来源范围之外",
  not_yet_available_from_provider: "来源尚未提供",
  derived_inputs_unavailable: "派生输入缺失",
  not_returned_calendar_unconfirmed: "未返回，日历待核实",
  request_failed: "请求失败",
  commit_pending: "持久确认待恢复",
};
const label = (s: string) => states[s] ?? s;
class FinancialRequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}
export async function financialCall<T>(
  transport: Transport,
  path: string,
  signal?: AbortSignal,
  command?: unknown,
): Promise<T> {
  const response = await transport(
    prefix + path,
    command === undefined
      ? { method: "GET", cache: "no-store", signal }
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(command),
        },
  );
  const result = await response.json();
  if (!response.ok || result.status !== "OK")
    throw new FinancialRequestError(
      /^[a-z_]{1,100}$/.test(result.classification)
        ? result.classification
        : "financial_request_outcome_unknown",
      response.status,
    );
  return result;
}
export function FinancialDailyWorkbench({
  route,
  navigate,
  transport,
  generation,
  embedded = false,
  detailOnly = false,
}: {
  route: Route;
  navigate: Navigate;
  transport: Transport;
  generation: number;
  embedded?: boolean;
  detailOnly?: boolean;
}) {
  const collecting = route.view === "collection";
  const tab = route.fields.get("tab") ?? (collecting ? "new" : "local");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [action, setAction] = useState("init");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [uncertain, setUncertain] = useState<string | null>(null);
  const [queryStart, setQueryStart] = useState(
    new Date(Date.now() - 30 * 86400000).toISOString().slice(0, 10),
  );
  const [queryEnd, setQueryEnd] = useState(
    new Date().toISOString().slice(0, 10),
  );
  const [cutoff, setCutoff] = useState(route.fields.get("as_of") ?? "");
  const [sid, setSid] = useState("DGS10");
  const [query, setQuery] = useState<Query | null>(null);
  const [point, setPoint] = useState<Point | null>(null);
  const status = useVisiblePolling(
    (signal) => financialCall<Status>(transport, "/status", signal),
    [generation],
    true,
    undefined,
    false,
  );
  const jobs = useVisiblePolling(
    (signal) =>
      financialCall<{ jobs: FinancialJob[] }>(transport, "/jobs", signal),
    [generation],
    collecting && !detailOnly,
  );
  const jobId = route.fields.get("job");
  const detail = useVisiblePolling(
    (signal) =>
      financialCall<{ job: FinancialJob }>(transport, `/jobs/${jobId}`, signal),
    [jobId, generation],
    !!jobId,
  );
  const job = detail.data?.job;
  const [help, setHelp] = useState<unknown>(null);
  useEffect(() => {
    if (status.data && !start) {
      setStart(status.data.defaults.start);
      setEnd(status.data.defaults.end);
    }
  }, [status.data, start]);
  function go(view: "assets" | "collection", nextTab: string) {
    navigate(
      view,
      {
        asset: FINANCIAL.identity,
        asset_revision: "r1",
        operation: null,
        tab: nextTab,
        job: null,
        reference: null,
        as_of: null,
        execution_asset:
          nextTab === "executions" ? `${FINANCIAL.identity}@r1` : null,
      },
      view === route.view,
    );
  }
  async function submit(resume?: string) {
    if (busy || uncertain) return;
    setBusy(true);
    setFailure(null);
    const request_id = resume ?? requestId();
    try {
      const result = await financialCall<{ job: FinancialJob }>(
        transport,
        resume ? "/jobs/resume" : "/jobs",
        undefined,
        resume ? { request_id } : { request_id, action, start, end },
      );
      navigate("collection", {
        asset: FINANCIAL.identity,
        asset_revision: "r1",
        tab: "executions",
        execution_asset: `${FINANCIAL.identity}@r1`,
        job: result.job.request_id,
      });
      jobs.refresh();
      detail.refresh();
    } catch (error) {
      setFailure(
        error instanceof Error
          ? error.message
          : "financial_request_outcome_unknown",
      );
      // Keep the same identity after uncertain transport; never auto-submit again.
      if (
        !(
          error instanceof FinancialRequestError &&
          error.status >= 400 &&
          error.status < 500
        )
      )
        setUncertain(request_id);
    } finally {
      setBusy(false);
    }
  }
  async function verify() {
    if (!uncertain) return;
    setBusy(true);
    try {
      const result = await financialCall<{ job: FinancialJob }>(
        transport,
        `/jobs/${uncertain}`,
      );
      setUncertain(null);
      setFailure(null);
      navigate("collection", {
        asset: FINANCIAL.identity,
        asset_revision: "r1",
        tab: "executions",
        execution_asset: `${FINANCIAL.identity}@r1`,
        job: result.job.request_id,
      });
    } catch (error) {
      setFailure(
        error instanceof Error ? error.message : "financial_read_failed",
      );
    } finally {
      setBusy(false);
    }
  }
  async function read(offset = 0) {
    setBusy(true);
    setFailure(null);
    try {
      const p = new URLSearchParams({
        start: queryStart,
        end: queryEnd,
        series: sid,
        offset: String(offset),
        limit: "200",
      });
      if (cutoff) p.set("as_of", new Date(cutoff).toISOString());
      const result = await financialCall<Query>(transport, `/query?${p}`);
      setQuery(result);
      setPoint(null);
    } catch (error) {
      setFailure(
        error instanceof Error ? error.message : "financial_read_failed",
      );
    } finally {
      setBusy(false);
    }
  }
  const failureNotice = (
    <>
      {(failure || status.failure || jobs.failure || detail.failure) && (
        <p role="alert">
          {failure ?? "本地读取暂不可用，请刷新核实。"}
          {uncertain && (
            <>
              {" "}
              请求身份：{uncertain}。结果未确认，不会自动重复提交。
              <button disabled={busy} onClick={verify}>
                核实原请求
              </button>
            </>
          )}
        </p>
      )}
    </>
  );
  const jobDetail = (
    <>
      {detailOnly && failureNotice}
      {detailOnly && !job && (
        <p role="status">
          {detail.busy
            ? "正在读取日频作业…"
            : "请求的日频作业暂不可核实，未改选其他任务。"}
        </p>
      )}
      {job && (
        <section aria-label="日频作业详情">
          <h3>日频作业详情</h3>
          <p>
            作业 {job.request_id} · {label(job.state)} · 完成{" "}
            {job.steps.filter((s) => s.state === "complete").length}/
            {job.steps.length} 段 · 实际上游请求 {job.provider_requests}
          </p>
          {job.classification && <p role="alert">{job.classification}</p>}
          {["partial", "unknown"].includes(job.state) && (
            <button
              disabled={busy || !!uncertain}
              onClick={() => submit(job.request_id)}
            >
              续跑此作业
            </button>
          )}
          {job.coverage && <CoverageTable rows={job.coverage.series} />}
          <details open={job.state !== "complete"}>
            <summary>逐段进度、失败与原件定位</summary>
            <ul className="financial-steps">
              {job.steps.map((s, i) => (
                <li key={i}>
                  <strong>{s.series_id}</strong> {s.start} 至 {s.end} ·{" "}
                  {s.source} · {label(s.state)} · 请求{" "}
                  {s.provider_requests ?? 0} · 有效值 {s.valid_rows ?? "待回读"}
                  {s.classification && (
                    <p role="alert">
                      {s.classification}；仅影响本段。
                      {s.retry_at
                        ? `冷却至 ${s.retry_at}，之后续跑。`
                        : "核对来源/存储恢复后续跑同一作业。"}
                    </p>
                  )}
                  {s.result && (
                    <details>
                      <summary>持久批次与原件</summary>
                      <pre className="financial-json">
                        {JSON.stringify(
                          { batch: s.result, retained: s.retained },
                          null,
                          2,
                        )}
                      </pre>
                    </details>
                  )}
                </li>
              ))}
            </ul>
          </details>
        </section>
      )}
    </>
  );
  if (detailOnly) return jobDetail;
  return (
    <>
      {(!embedded || !collecting) && (
        <div className="toolbar">
          {!embedded && <h2>金融条件日频 · 十项</h2>}
          <button onClick={() => go("collection", "new")}>采集数据</button>
          <button onClick={() => go("assets", "local")}>查询数据</button>
          <button onClick={() => go("collection", "executions")}>
            执行详情
          </button>
          <button
            onClick={async () => {
              try {
                setHelp(await financialCall(transport, "/api-help"));
              } catch {
                setFailure("financial_api_help_unavailable");
              }
            }}
          >
            API 说明
          </button>
        </div>
      )}
      <p>
        原机构输入、政策生效投影与 MDF
        派生分别标识。百分比原值；信用另列基点。仅人工作业，无日频常驻执行器。
      </p>
      {failureNotice}
      {help !== null && (
        <details open>
          <summary>真实 API 入口与合同</summary>
          <pre className="financial-json">{JSON.stringify(help, null, 2)}</pre>
        </details>
      )}
      {collecting && tab === "new" && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
          className="collection-form"
        >
          <h3>新建日频作业</h3>
          {!embedded && (
            <label>
              采集目标
              <input value="金融条件日频（十项）" readOnly />
            </label>
          )}
          <label>
            执行方式
            <select
              aria-label="执行方式"
              value={action}
              onChange={(e) => setAction(e.target.value)}
            >
              <option value="init">初始化（默认 20 日历年）</option>
              <option value="update">增量更新（所选区间，覆盖近期修订）</option>
            </select>
          </label>
          <div className="field-grid">
            <label>
              起始观测日
              <input
                type="date"
                required
                value={start}
                onChange={(e) => setStart(e.target.value)}
              />
            </label>
            <label>
              结束观测日
              <input
                type="date"
                required
                value={end}
                onChange={(e) => setEnd(e.target.value)}
              />
            </label>
          </div>
          <p>
            一次提交十项，按真实可得起点处理。信用初始化使用指定固定归档并由
            FRED
            接尾段；重试仅续跑未完成段。历史回补不证明历史首发，缺值不补零。
          </p>
          <button
            type="submit"
            disabled={busy || !!uncertain || !start || !end}
          >
            {busy ? "正在提交…" : "提交日频作业"}
          </button>
        </form>
      )}
      {collecting && tab === "executions" && (
        <>
          <WorkflowTable
            className="financial-table"
            label="日频执行记录"
            columns={["作业", "窗口", "进度", "上游请求"]}
            rows={(jobs.data?.jobs ?? []).map((j) => ({
              id: j.request_id,
              search: j.request_id,
              cells: [
                j.request_id,
                <>
                  {j.command.start}
                  <br />
                  {j.command.end}
                </>,
                <>
                  {label(j.state)} ·{" "}
                  {j.steps.filter((s) => s.state === "complete").length}/
                  {j.steps.length}
                </>,
                String(j.provider_requests),
              ],
            }))}
            selected={jobId}
            onSelect={(id) =>
              navigate("collection", {
                asset: FINANCIAL.identity,
                asset_revision: "r1",
                tab: "executions",
                execution_asset: `${FINANCIAL.identity}@r1`,
                job: id,
              })
            }
          >
            {jobDetail}
          </WorkflowTable>
        </>
      )}
      {!collecting && (
        <>
          <form
            className="collection-form"
            onSubmit={(e) => {
              e.preventDefault();
              void read();
            }}
          >
            <h3>本地日频查询</h3>
            <div className="field-grid">
              <label>
                指标
                <select
                  aria-label="指标"
                  value={sid}
                  onChange={(e) => setSid(e.target.value)}
                >
                  {Object.entries(names).map(([key, name]) => (
                    <option key={key} value={key}>
                      {key} · {name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                起始观测日
                <input
                  type="date"
                  required
                  value={queryStart}
                  onChange={(e) => setQueryStart(e.target.value)}
                />
              </label>
              <label>
                结束观测日
                <input
                  type="date"
                  required
                  value={queryEnd}
                  onChange={(e) => setQueryEnd(e.target.value)}
                />
              </label>
              <label>
                MDF 可用截止（可空，带时区 ISO）
                <input
                  placeholder="留空为最新本地确认"
                  value={cutoff}
                  onChange={(e) => setCutoff(e.target.value)}
                />
              </label>
            </div>
            <button disabled={busy}>查询本地数据</button>
          </form>
          {query && (
            <>
              <p>
                截止 {query.as_of} · 本地日历行 {query.total} · 上游请求{" "}
                {query.provider_requests}。来源发布时间未知不以 MDF
                观察时间代替。
              </p>
              <button onClick={() => setCutoff(query.as_of)}>固定此截止</button>
              <button disabled={busy} onClick={() => read()}>
                刷新本地结果
              </button>
              <WorkflowTable
                className="financial-table"
                label="日频观测结果"
                columns={["观测日", "值（%） / 信用 bp", "状态", "版本"]}
                rows={query.rows.map((r) => ({
                  id: r.observation_date,
                  search: r.observation_date,
                  cells: [
                    r.observation_date,
                    <>
                      {r.value ?? "—"}
                      {r.basis_points !== null && ` / ${r.basis_points} bp`}
                    </>,
                    label(r.state),
                    String(r.revision ?? "—"),
                  ],
                }))}
                selected={point?.observation_date}
                onSelect={(id) =>
                  setPoint(
                    query.rows.find((r) => r.observation_date === id) ?? null,
                  )
                }
              />
              {query.next_offset !== null && (
                <button
                  disabled={busy}
                  onClick={() => read(query.next_offset!)}
                >
                  下一页
                </button>
              )}
              <CoverageTable
                rows={query.coverage.series.filter((r) => r.series_id === sid)}
              />
              {point && (
                <details open>
                  <summary>来源、首次观察/可用及原件</summary>
                  <pre className="financial-json">
                    {JSON.stringify(point, null, 2)}
                  </pre>
                </details>
              )}
            </>
          )}
        </>
      )}
      <details>
        <summary>十项来源与使用说明</summary>
        {status.data?.series.map((m) => (
          <section key={m.series_id}>
            <h4>
              {m.series_id} · {names[m.series_id]}
            </h4>
            <p>{m.active_binding.identity}</p>
            <p>{m.active_binding.attribution}</p>
            {m.active_binding.required_notice && (
              <p>{m.active_binding.required_notice}</p>
            )}
            {m.active_binding.affiliation_notice && (
              <p>{m.active_binding.affiliation_notice}</p>
            )}
            <a
              href={m.active_binding.source_url}
              target="_blank"
              rel="noreferrer"
            >
              来源说明
            </a>
          </section>
        ))}
        <p>{status.data?.cutoff_semantics}</p>
      </details>
    </>
  );
}
function CoverageTable({ rows }: { rows: Coverage[] }) {
  return (
    <WorkflowTable
      className="financial-table"
      label="逐项覆盖与缺口"
      columns={["指标", "有效 / 缺值", "实际有效范围", "未返回日历日"]}
      rows={rows.map((r) => ({
        id: r.series_id,
        search: r.series_id,
        cells: [
          <>
            {r.series_id}
            <br />
            {names[r.series_id]}
          </>,
          `${r.valid_rows} / ${r.explicit_missing}`,
          <>
            {r.first ?? "—"}
            <br />
            {r.last ?? "—"}
          </>,
          <>
            {r.calendar_days_without_row}
            <details>
              <summary>缺口含义</summary>
              {r.gap_explanation}
            </details>
          </>,
        ],
      }))}
      onSelect={() => {}}
    />
  );
}
