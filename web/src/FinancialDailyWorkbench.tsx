import {
  financialStateText as label,
  financialJobOrigin,
  financialJobTitle,
  financialJobSearch,
  financialJobSources,
  financialJobResult,
} from "./financialJobPresentation";
import { DateField } from "./DateField";
import type { FinancialHelp } from "./FinancialApiHelp";
import { FinancialDailyQuery } from "./FinancialDailyQuery";
import { FINANCIAL_PREFIX as prefix } from "./financialQuery";
import { ExecutionFacts } from "./ExecutionFacts";
import { formatBeijingTime } from "./beijingTime";
import { useEffect, useState, type ReactNode } from "react";
import type { Navigate, Route } from "./workflowRoute";
import type { Transport } from "./api";
import { useVisiblePolling } from "./useVisiblePolling";
import { Fact, Tabs, Technical, WorkflowTable } from "./WorkflowTable";
import { requestId } from "./CpiTimeSeriesCollection";

export const FINANCIAL = {
  identity: "mdf.macro.financial-conditions.daily",
  revision: "r1",
};
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
  acquired_start?: string;
  acquired_end?: string;
  latest_source_observation?: string | null;
  deferred_after?: string | null;
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
  started_at?: string | null;
  finished_at?: string | null;
  updated_at?: string | null;
  origin?: string | null;
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
export type FinancialSummary = {
  as_of: string;
  observation_count: number;
  revision_count: number;
  last_available_at: string | null;
  series: (Coverage & { last_available_at?: string | null })[];
};
export function financialReason(
  step: Pick<Step, "classification" | "retry_at">,
) {
  const code = step.classification;
  if (code === "financial_origin_future_window_not_acquired")
    return "此段尚未进入美国东部观测日；等待来源日期到达，后续增量取得，不重抓已有历史。";
  if (code?.includes("cool") || code?.includes("rate_limit"))
    return `来源限流或冷却${step.retry_at ? `至 ${formatBeijingTime(step.retry_at)}` : ""}；到期后续跑同一作业。`;
  if (code === "financial_http_failure")
    return "具名来源暂未成功响应；已有确认值保留，冷却后续跑本段。";
  if (code?.includes("unknown") || code === "execution_interrupted")
    return "执行或持久确认被中断，结果尚未核实；核实同一作业并复用已留存原件恢复，不能另建任务冒充。";
  if (code?.includes("credential") || code?.includes("permission"))
    return "此段来源准入或既有凭据不可用；核实对应来源配置后续跑，其他来源结果保留。";
  return code
    ? `本段未完成（${code}）；核实对应来源或存储后续跑同一作业，已确认值保留。`
    : "";
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
  const collecting = route.view === "collection",
    tab = route.fields.get("tab") ?? (collecting ? "new" : "local");
  const [consumerTab, setConsumerTab] = useState("query"),
    [start, setStart] = useState(""),
    [end, setEnd] = useState(""),
    [action, setAction] = useState("init"),
    [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false),
    [failure, setFailure] = useState<string | null>(null),
    [uncertain, setUncertain] = useState<string | null>(null);
  const status = useVisiblePolling(
    (signal) => financialCall<Status>(transport, "/status", signal),
    [transport, generation],
    !detailOnly,
    undefined,
    false,
  );
  const summary = useVisiblePolling(
    (signal) => financialCall<FinancialSummary>(transport, "/summary", signal),
    [transport, generation],
    !collecting,
  );
  const jobs = useVisiblePolling(
    (signal) =>
      financialCall<{ jobs: FinancialJob[] }>(transport, "/jobs", signal),
    [transport, generation],
    !detailOnly,
  );
  const jobId = route.fields.get("job");
  const detail = useVisiblePolling(
    (signal) =>
      financialCall<{ job: FinancialJob }>(transport, `/jobs/${jobId}`, signal),
    [transport, jobId, generation],
    !!jobId,
  );
  const job = detail.data?.job;
  const help = useVisiblePolling(
    (signal) => financialCall<FinancialHelp>(transport, "/api-help", signal),
    [transport, generation],
    consumerTab === "api" && !collecting,
    undefined,
    false,
  );
  useEffect(() => {
    if (status.data && !start) {
      setConfirmed(false);
      setStart(status.data.defaults.start);
      setEnd(status.data.defaults.end);
    }
  }, [status.data, start]);
  function go(
    view: "assets" | "collection",
    nextTab: string,
    id: string | null = null,
  ) {
    navigate(
      view,
      {
        asset: FINANCIAL.identity,
        asset_revision: "r1",
        operation: null,
        tab: nextTab,
        job: id,
        reference: null,
        as_of: null,
        execution_asset:
          nextTab === "executions" ? `${FINANCIAL.identity}@r1` : null,
      },
      view === route.view,
    );
  }
  async function submit(resume?: string) {
    if (busy || uncertain || (!resume && !confirmed)) return;
    setBusy(true);
    setFailure(null);
    const request_id = resume ?? requestId();
    try {
      const r = await financialCall<{ job: FinancialJob }>(
        transport,
        resume ? "/jobs/resume" : "/jobs",
        undefined,
        resume ? { request_id } : { request_id, action, start, end },
      );
      go("collection", "executions", r.job.request_id);
      jobs.refresh();
      detail.refresh();
    } catch (error) {
      setFailure(
        error instanceof Error
          ? error.message
          : "financial_request_outcome_unknown",
      );
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
      const r = await financialCall<{ job: FinancialJob }>(
        transport,
        `/jobs/${uncertain}`,
      );
      setUncertain(null);
      setFailure(null);
      go("collection", "executions", r.job.request_id);
    } catch (error) {
      setFailure(
        error instanceof Error ? error.message : "financial_read_failed",
      );
    } finally {
      setBusy(false);
    }
  }
  const failureNotice = (failure ||
    status.failure ||
    jobs.failure ||
    detail.failure ||
    summary.failure) && (
    <p role="alert">
      {failure
        ? failure === "financial_job_range_invalid"
          ? "日期区间无效或超过可采范围，请核对日期后再提交。"
          : uncertain
            ? "提交结果尚未确认，请先核实原请求。"
            : `请求未完成（${failure}），请核对当前来源或本地服务后重试。`
        : "本地读取暂不可用，请刷新核实，已有持有不据此判为空。"}
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
  );
  const detailPanel = (
    <>
      {failureNotice}
      {!job && (
        <p role="status">
          {detail.busy
            ? "正在读取日频作业…"
            : "请求的日频作业暂不可核实，未改选其他任务。"}
        </p>
      )}
      {job && (
        <FinancialExecutionDetail
          job={job}
          busy={busy || !!uncertain}
          resume={() => void submit(job.request_id)}
          go={go}
        />
      )}
    </>
  );
  if (detailOnly) return detailPanel;
  if (collecting && tab === "new")
    return (
      <section
        aria-label="金融日频 受控采集"
        className="ordinary-query cpi-timeseries"
      >
        <h3>金融条件日频 · 受控采集</h3>
        <p>
          十项一起采集，长范围由后端自动分段；保留每项真实来源、可得范围与修订。
        </p>
        <details>
          <summary>口径与采集说明</summary>
          <FinancialSources status={status.data} />
        </details>
        {failureNotice}
        <div className="workbench">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (e.currentTarget.checkValidity()) void submit();
            }}
          >
            {!embedded && (
              <label>
                采集目标
                <input readOnly value="金融条件日频（十项）" />
              </label>
            )}
            <label>
              采集方式
              <select
                aria-label="采集方式"
                value={action}
                onChange={(e) => {
                  setConfirmed(false);
                  setAction(e.target.value);
                }}
              >
                <option value="update">更新指定范围</option>
                <option value="init">初始化采集（默认20日历年）</option>
              </select>
            </label>
            <div className="field-grid">
              <DateField
                label="采集开始日期"
                type="date"
                value={start}
                onChange={(v) => {
                  setConfirmed(false);
                  setStart(v);
                }}
                required
              />
              <DateField
                label="采集结束日期"
                type="date"
                value={end}
                onChange={(v) => {
                  setConfirmed(false);
                  setEnd(v);
                }}
                required
              />
            </div>
            <p className="small">
              默认终点为美国东部当前观测日{" "}
              {status.data?.defaults.end ?? "待核实"}
              ，不是来源已公布承诺；北京时间跨日不拒绝同段已有历史。尚未提供的尾部由后续更新补齐，缺值不补零。
            </p>
            <label className="cpi-collection-confirm">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              <span>我确认将以上范围采集到开发测试库</span>
            </label>
            <button
              type="submit"
              disabled={!confirmed || busy || !!uncertain || !start || !end}
            >
              {busy ? "正在提交…" : "提交日频作业"}
            </button>
          </form>
          <aside className="detail-pane execution-briefs" aria-label="执行简报">
            <h3>执行简报</h3>
            <p className="small">
              时间基准：北京时间（UTC+08:00）。受理不等于采集成功。
            </p>
            {jobs.busy && !jobs.data && <p>正在读取已有作业…</p>}
            {jobs.data?.jobs.length === 0 && (
              <p>暂无执行记录；尚未提交本次采集。</p>
            )}
            {(jobs.data?.jobs ?? []).slice(0, 5).map((j) => (
              <section key={j.request_id} className="execution-brief-card">
                <strong>{financialJobTitle(j)}</strong>
                <dl>
                  <div>
                    <dt>请求编号</dt>
                    <dd className="request-id">{j.request_id}</dd>
                  </div>
                  <div>
                    <dt>创建时间</dt>
                    <dd>{formatBeijingTime(j.created_at)}</dd>
                  </div>
                  <div>
                    <dt>采集范围</dt>
                    <dd>
                      {j.command.start} 至 {j.command.end}
                    </dd>
                  </div>
                  <div>
                    <dt>结果</dt>
                    <dd>
                      {financialJobResult(j)} ·{" "}
                      {j.steps.filter((s) => s.state === "complete").length}/
                      {j.steps.length}
                    </dd>
                  </div>
                </dl>
                <button
                  onClick={() => go("collection", "executions", j.request_id)}
                >
                  查看执行详情
                </button>
              </section>
            ))}
          </aside>
        </div>
      </section>
    );
  return (
    <>
      {!collecting && (
        <section
          aria-label="金融日频 范围查询"
          className="ordinary-query cpi-timeseries macro-range-query"
        >
          <h3>金融条件日频 · 范围查询</h3>
          <Tabs
            label="金融日频 消费者页签"
            values={
              [
                ["query", "查询数据"],
                ["api", "API说明"],
                ["updates", "更新记录"],
              ] as const
            }
            value={consumerTab}
            onChange={setConsumerTab}
          />
          {failureNotice}
          <FinancialDailyQuery
            transport={transport}
            summary={summary.data}
            help={help.data}
            activeTab={consumerTab}
            initialAsOf={route.fields.get("as_of") ?? ""}
          />
          <div hidden={consumerTab !== "updates"}>
            <h4>采集与版本确认记录</h4>
            <p>
              以下是 MDF
              作业记录，不能当作官方发布事件。官方历史首发与逐点公布/可得时刻缺证据时仍未知。
            </p>
            <FinancialJobList
              jobs={jobs.data?.jobs ?? []}
              selected={jobId}
              onSelect={(id) => go("collection", "executions", id)}
            />
          </div>
          <details>
            <summary>十项来源与使用说明</summary>
            <FinancialSources status={status.data} />
          </details>
        </section>
      )}
      {collecting && tab === "executions" && (
        <FinancialJobList
          jobs={jobs.data?.jobs ?? []}
          selected={jobId}
          onSelect={(id) => go("collection", "executions", id)}
        >
          {detailPanel}
        </FinancialJobList>
      )}
    </>
  );
}
function FinancialSources({ status }: { status: Status | null }) {
  return (
    <>
      <p>
        七项原机构输入、MDF 派生通胀补偿、两条信用归档/FRED
        补尾分别标识。百分比原值，信用 bp=百分比×100；归档回补不证明历史首发。
      </p>
      {status?.series.map((m) => (
        <section key={m.series_id}>
          <h4>
            {m.series_id} · {names[m.series_id]}
          </h4>
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
    </>
  );
}
function FinancialJobList({
  jobs,
  selected,
  onSelect,
  children,
}: {
  jobs: FinancialJob[];
  selected: string | null;
  onSelect: (id: string) => void;
  children?: ReactNode;
}) {
  return (
    <WorkflowTable
      label="日频执行记录"
      className={children ? "execution-records" : "financial-table"}
      columns={["任务名称", "创建时间（北京时间）", "采集范围", "结果"]}
      rows={jobs.map((j) => ({
        id: j.request_id,
        search: financialJobSearch(j),
        cells: [
          financialJobTitle(j),
          formatBeijingTime(j.created_at),
          `${j.command.start} 至 ${j.command.end}`,
          financialJobResult(j),
        ],
      }))}
      selected={selected}
      onSelect={onSelect}
    >
      {children}
    </WorkflowTable>
  );
}
function FinancialExecutionDetail({
  job: j,
  busy,
  resume,
  go,
}: {
  job: FinancialJob;
  busy: boolean;
  resume: () => void;
  go: (view: "assets" | "collection", tab: string, id?: string | null) => void;
}) {
  const failed = j.steps.filter((s) =>
    ["failed", "unknown", "outcome_unknown", "cooling_down"].includes(s.state),
  );
  const pending = j.steps.filter((s) => s.state === "pending");
  const running = j.steps.filter((s) => s.state === "running");
  const waiting = j.steps.filter((s) => s.state === "waiting_publication");
  const jobProblem = ["failed", "unknown", "outcome_unknown"].includes(j.state);
  const resumable =
    ["partial", "unknown", "failed"].includes(j.state) &&
    (failed.length > 0 || jobProblem);
  return (
    <section aria-label="日频作业详情">
      <h3>执行详情</h3>
      <ExecutionFacts
        id={j.request_id}
        asset="金融条件日频（十项）"
        source={financialJobOrigin(j)}
        action={j.command.action === "init" ? "初始化采集" : "更新指定范围"}
        start={j.command.start}
        end={j.command.end}
        createdAt={j.created_at}
        startedAt={j.started_at}
        finishedAt={
          ["running", "pending"].includes(j.state)
            ? null
            : (j.finished_at ?? (j.state === "complete" ? j.updated_at : null))
        }
        result={financialJobResult(j)}
        classification={
          jobProblem && j.classification
            ? financialReason({ classification: j.classification })
            : null
        }
      />
      <dl>
        <Fact name="数据来源（步骤绑定）">
          {financialJobSources(j).join("、") || "未记录"}
        </Fact>
      </dl>
      <p>
        已确认 {j.steps.filter((s) => s.state === "complete").length}/
        {j.steps.length} 段；本作业累计来源请求 {j.provider_requests}
        。执行段完成不表示所有日历日都有公布值；来源尾部、非观测日和短历史独立留缺。
      </p>
      {(pending.length > 0 || running.length > 0) && (
        <section aria-label="执行进度">
          <p>
            采集中 {running.length} 段；等待执行 {pending.length}{" "}
            段。待执行步骤尚无失败事实。
          </p>
        </section>
      )}
      {waiting.length > 0 && (
        <section aria-label="等待来源公布">
          <h4>等待来源公布</h4>
          {waiting.map((s, i) => (
            <p key={i}>
              {names[s.series_id]} · {s.start} 至 {s.end}
              ：等待来源日期或公布；保留已确认历史，由后续更新补齐。
            </p>
          ))}
        </section>
      )}
      {(failed.length > 0 || jobProblem) && (
        <section aria-label="失败与恢复">
          <h4>失败与恢复</h4>
          {jobProblem && (
            <p role="alert">
              {j.classification
                ? financialReason({ classification: j.classification })
                : "作业结果未核实或失败原因未记录；请刷新同一记录核对后恢复。"}
            </p>
          )}
          {failed.map((s, i) => (
            <p key={i} role="alert">
              {names[s.series_id]} · {s.start} 至 {s.end}：{label(s.state)}。
              {financialReason(s)}
            </p>
          ))}
          {resumable && (
            <button disabled={busy} onClick={resume}>
              续跑此作业
            </button>
          )}
        </section>
      )}
      {j.classification && !jobProblem && (
        <details>
          <summary>保留的异常分类</summary>
          <p>
            先前记录：{j.classification}
            。该分类独立保留，不代表当前作业失败；当前状态以本次回读为准。
          </p>
        </details>
      )}
      <button onClick={() => go("assets", "local")}>查看关联资产 →</button>
      <button onClick={() => go("collection", "executions")}>
        返回执行列表
      </button>
      {j.coverage && (
        <details>
          <summary>各指标覆盖与缺口</summary>
          <CoverageTable rows={j.coverage.series} />
        </details>
      )}
      <details>
        <summary>逐段进度与原件</summary>
        <ul className="financial-steps">
          {j.steps.map((s, i) => (
            <li key={i}>
              <strong>{s.series_id}</strong> {s.start} 至 {s.end} ·{" "}
              {label(s.state)} · 来源请求 {s.provider_requests ?? 0} · 有效值{" "}
              {s.valid_rows ?? "待回读"}
              {s.acquired_end && (
                <p>
                  本段已取得区间：{s.acquired_start} 至 {s.acquired_end}
                  ；原文最新观测：{s.latest_source_observation ?? "未取得"}。
                </p>
              )}
              {s.deferred_after && (
                <p>
                  请求中的后续日期尚未进入来源观测日，等待后续更新；不会把它们写成未来值。
                </p>
              )}
              {s.classification && <p>{financialReason(s)}</p>}
              {s.result && (
                <Technical>
                  <pre className="financial-json">
                    {JSON.stringify(
                      { batch: s.result, retained: s.retained },
                      null,
                      2,
                    )}
                  </pre>
                </Technical>
              )}
            </li>
          ))}
        </ul>
      </details>
    </section>
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
