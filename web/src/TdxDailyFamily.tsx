import { tdxCanReopen } from "./tdxJobPresentation";
import {
  useCollectionRecords,
  collectionRecent,
  CollectionBriefs,
  CollectionFormLock,
  collectionCanReopen,
  ExecutionDetailShell,
} from "./CollectionLifecycle";
import { useTdxPendingJob } from "./useTdxPendingJob";
import { useCollectionSubmit } from "./useCollectionSubmit";
import { TdxRosterFacts, type RosterStatus } from "./TdxRosterFacts";
import {
  DailyDateRange,
  CollectionColumns,
  ExecutionBriefCard,
  dailyRangeError,
  rangeWithDefaults,
} from "./DailyFormControls";
import { tdxJobOrigin, rosterJobScope, tdxBriefs } from "./tdxJobPresentation";
import { useEffect, useRef, useState } from "react";
import type { FamilyProps, RuntimeFamily } from "./RuntimeFamily";
import {
  TDX_DAILY,
  TdxDailyQuery,
  TdxApiHelp,
  tdxCall,
  tdxStateLabel,
  tdxLatestClosedDay,
  type TdxJob,
} from "./TdxDailyWorkbench";
import { requestId } from "./CpiTimeSeriesCollection";
import { formatBeijingTime, beijingToday } from "./beijingTime";
import { keyOf } from "./workflowModel";
import { useVisiblePolling } from "./useVisiblePolling";
import { Fact, Technical, WorkflowTable } from "./WorkflowTable";
import { StrategySwitch } from "./StrategySwitch";
const title = "沪深北 A 股原生日线";
const exchanges = ["深市", "沪市", "北交所"];
function actionTitle(job: TdxJob) {
  if (job.strategy_slot === "review") return "16:00 盘后复核（旧策略）";
  if (job.strategy_slot === "update") return "盘后日线采集";
  return (
    { init: "历史初始化", update: "盘后增量", universe: "刷新三市名册" }[
      job.command.action
    ] ?? job.command.action
  );
}
const fields = {
  asset: TDX_DAILY.identity,
  asset_revision: TDX_DAILY.revision,
  operation: null,
};
const strategyId = "tdx-daily-auto";
function updateRange(end: string) {
  return {
    start: new Date(Date.parse(end + "T00:00:00Z") - 14 * 86400000)
      .toISOString()
      .slice(0, 10),
    end,
  };
}
type Status = {
  roster?: RosterStatus | null;
  enabled: boolean;
  collect: boolean;
  defaults: { start?: string; end: string };
};
type Strategy = {
  enabled: boolean;
  worker_online: boolean;
  heartbeat_at?: string;
  calendar_state?: string;
};

export function TdxDailyCollection({
  transport,
  navigate,
  generation,
}: FamilyProps) {
  const submission = useCollectionSubmit();
  const pending = useTdxPendingJob(TDX_DAILY.identity);
  const [action, setAction] = useState<"init" | "update">("update");
  const [drafts, setDrafts] = useState(() => {
    const end = tdxLatestClosedDay();
    return {
      init: { start: "1990-01-01", end },
      update: updateRange(end),
    };
  });
  const [confirmed, setConfirmed] = useState(false),
    [busy, setBusy] = useState(false),
    [notice, setNotice] = useState("");
  const edited = useRef({
      init: { start: false, end: false },
      update: { start: false, end: false },
    }),
    writing = useRef(false),
    intent = useRef<string | null>(pending.id);
  const [receipt, setReceipt] = useState<TdxJob | null>(null);
  function accepted(job: TdxJob) {
    if (!intent.current || job.request_id !== intent.current) return;
    setReceipt(job);
    setNotice("");
    setConfirmed(false);
    jobs.refresh();
  }
  const status = useVisiblePolling(
    (signal) => tdxCall<Status>(transport, "/status", signal),
    [transport, generation],
  );
  const jobs = useVisiblePolling(
    (signal) => tdxCall<{ jobs: TdxJob[] }>(transport, "/jobs", signal),
    [transport, generation],
  );
  useEffect(() => {
    const end = status.data?.defaults.end;
    if (!end || pending.id) return;
    const { start } = updateRange(end);
    setDrafts((old) => ({
      init: rangeWithDefaults(
        old.init,
        { start: status.data?.defaults.start ?? "1990-01-01", end },
        edited.current.init,
      ),
      update: rangeWithDefaults(
        old.update,
        { start, end },
        edited.current.update,
      ),
    }));
  }, [status.data]);
  const range = drafts[action];
  useEffect(() => setConfirmed(false), [action, range.start, range.end]);
  const error =
    range.start && range.start < "1990-01-01"
      ? "采集开始日期不得早于 1990-01-01。"
      : dailyRangeError(
          range,
          366 * 200,
          status.data?.defaults.end ?? beijingToday(),
        );
  function change(key: "start" | "end", value: string) {
    edited.current[action][key] = true;
    setConfirmed(false);
    setDrafts((old) => ({
      ...old,
      [action]: { ...old[action], [key]: value },
    }));
  }
  function open(id: string) {
    navigate("collection", {
      ...fields,
      tab: "executions",
      job: id,
      execution_asset: keyOf(TDX_DAILY),
      execution_schedule: "all",
      schedule: null,
    });
  }
  const currentJobs = useCollectionRecords([
    ...(receipt ? [receipt] : []),
    ...(jobs.data?.jobs ?? []),
  ]);
  const tracked =
    currentJobs.find((j) => j.request_id === pending.id) ??
    (receipt?.request_id === pending.id ? receipt : null);
  useEffect(() => {
    if (!tracked || tracked.command.action === "universe") return;
    const command = tracked.command;
    const mode = command.action as "init" | "update";
    setAction(mode);
    setConfirmed(false);
    setDrafts((old) => ({
      ...old,
      [mode]: { start: command.start!, end: command.end! },
    }));
  }, [tracked?.request_id]);
  const visibleJobs = collectionRecent(
    currentJobs.filter((j) => j.command.action !== "universe"),
    pending.id,
  );
  function newIntent() {
    if (!pending.id || !tracked || !tdxCanReopen(tracked) || busy || jobs.stale)
      return;
    try {
      pending.release(pending.id);
    } catch {
      setNotice("本机原请求编号无法核实，未释放意图，请保留原号回查。");
      return;
    }
    intent.current = null;
    submission.reset();
    setConfirmed(false);
    setNotice("");
  }
  return (
    <section
      className="ordinary-query cpi-timeseries"
      aria-label="TDX 日频 受控采集"
    >
      <h3>{title} · 受控采集</h3>
      {status.data?.roster && (
        <details>
          <summary>日线使用的基础名册</summary>
          <TdxRosterFacts value={status.data.roster} />
          <button
            onClick={() =>
              navigate("assets", {
                asset: "mdf.market.tdx.stock.roster",
                asset_revision: "r1",
                operation: null,
              })
            }
          >
            查询基础名册
          </button>
        </details>
      )}
      <CollectionColumns
        briefs={
          <CollectionBriefs>
            {pending.blocked && (
              <p role="alert">本机请求编号无法核实，采集已暂停。</p>
            )}
            {notice && <p role="alert">{notice}</p>}
            {pending.id && !tracked && (
              <>
                <code>{pending.id}</code>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    void tdxCall<TdxJob>(transport, "/jobs/" + intent.current)
                      .then(accepted)
                      .catch(() =>
                        setNotice("原请求仍不可核实；请保留编号，不重发。"),
                      )
                  }
                >
                  核实原请求
                </button>
              </>
            )}
            <p className="small">
              以下为真实近期作业，触发来源单列；缺少来源记录时保留待核实。
            </p>
            {jobs.busy && !jobs.data && <p role="status">正在读取执行简报…</p>}
            {jobs.failure && (
              <p role="alert">执行简报读取失败，已有记录待核实。</p>
            )}
            {jobs.data && !currentJobs.length && (
              <p>暂无采集记录；尚未提交本次采集。</p>
            )}
            <ol className="execution-brief-list">
              {visibleJobs.map((j) => (
                <li key={j.request_id}>
                  <ExecutionBriefCard
                    id={j.request_id}
                    current={j.request_id === pending.id}
                    state={j.state}
                    onReopen={newIntent}
                    canReopen={!jobs.stale && tdxCanReopen(j)}
                    reopenDisabled={busy || pending.blocked}
                    title={`${title} · ${tdxJobOrigin(j)} · ${actionTitle(j)}`}
                    createdAt={j.created_at}
                    start={j.command.start}
                    end={j.command.end}
                    result={`${tdxStateLabel[j.state] ?? j.state}${j.classification ? ` · ${j.classification}` : ""}`}
                    onSelect={() => open(j.request_id)}
                  />
                </li>
              ))}
            </ol>
          </CollectionBriefs>
        }
      >
        <form
          className="collection-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (
              writing.current ||
              intent.current ||
              !confirmed ||
              error ||
              !status.data?.collect ||
              status.stale ||
              !event.currentTarget.checkValidity()
            )
              return;
            const id = requestId();
            if (!pending.reserve(id)) return;
            writing.current = true;
            intent.current = id;
            setBusy(true);
            setNotice("");
            void (async () => {
              try {
                const job = await submission.submit(
                  id,
                  () =>
                    tdxCall<TdxJob>(transport, "/jobs", undefined, {
                      request_id: id,
                      action,
                      ...range,
                    }),
                  () => tdxCall<TdxJob>(transport, "/jobs/" + id),
                  (value) => value.request_id === id,
                );
                accepted(job);
              } catch {
                setNotice("提交回执未核实，请按原请求编号核实。");
              } finally {
                setBusy(false);
                writing.current = false;
              }
            })();
          }}
        >
          <CollectionFormLock locked={busy || !!pending.id}>
            <label>
              采集方式
              <select
                aria-label="采集方式"
                value={action}
                disabled={busy || !!intent.current}
                onChange={(e) => {
                  setAction(e.target.value as typeof action);
                  setConfirmed(false);
                }}
              >
                <option value="update">更新指定范围（盘后增量）</option>
                <option value="init">初始化采集</option>
              </select>
            </label>
            <div className="field-grid">
              <DailyDateRange
                key={action}
                collection
                start={range.start}
                end={range.end}
                onStart={(v) => change("start", v)}
                onEnd={(v) => change("end", v)}
                min="1990-01-01"
                max={status.data?.defaults.end}
                disabled={busy || !!intent.current}
              />
            </div>
            <p className="small">
              {action === "init"
                ? "初始化服务预置范围：1990 年至最近盘后日，可自行修改。"
                : "更新默认回看 14 日，可编辑起止日期。"}
            </p>
            <p className="small">
              沪深北三市；原生价基（native_projection），收益口径待核验。逐页持久保存，中断后在执行记录续跑未完成分片。日期按北京时间盘后窗口核定。
            </p>
            {error && range.end && <p role="alert">{error}</p>}
            {status.busy && !status.data && (
              <p role="status">正在核实采集资格与盘后日期…</p>
            )}
            {status.failure && (
              <p role="alert">TDX 采集资格读取失败，请重读核实。</p>
            )}
            {status.data && !status.data.collect && (
              <p role="status">当前采集能力未启用。</p>
            )}
            <label className="cpi-collection-confirm">
              <input
                type="checkbox"
                checked={confirmed}
                disabled={busy || !!intent.current}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              <span>我确认将以上范围采集到开发测试库</span>
            </label>
            <button
              className="primary"
              disabled={
                busy ||
                !!pending.id ||
                pending.blocked ||
                !confirmed ||
                !!error ||
                !status.data?.collect ||
                status.stale
              }
            >
              {busy ? "正在提交…" : "提交 TDX 日线 采集"}
            </button>
          </CollectionFormLock>
        </form>
      </CollectionColumns>
    </section>
  );
}
export function TdxExecutionDetail({
  transport,
  route,
  navigate,
  generation,
  familyAsset = TDX_DAILY,
  apiPrefix = "/api/r5/tdx/daily",
  familyTitle = title,
  execution,
}: FamilyProps & {
  familyAsset?: typeof TDX_DAILY;
  apiPrefix?: string;
  familyTitle?: string;
}) {
  const fields = {
    asset: familyAsset.identity,
    asset_revision: familyAsset.revision,
    operation: null,
  };
  const id = route.fields.get("job");
  const selectedIdentity = `${apiPrefix}:${id}`;
  const selectedId = useRef(selectedIdentity);
  selectedId.current = selectedIdentity;

  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const writing = useRef(false);
  useEffect(() => {
    selectedId.current = selectedIdentity;
    writing.current = false;
    setBusy(false);
    setNotice("");
    return () => {
      selectedId.current = "";
    };
  }, [selectedIdentity]);

  const detail = useVisiblePolling(
    async (signal) => {
      const job = await tdxCall<TdxJob>(
        transport,
        "/jobs/" + encodeURIComponent(id!) + "?view=summary",
        signal,
        undefined,
        apiPrefix,
      );
      if (
        job.request_id !== id ||
        (job.command.action === "universe") !== apiPrefix.endsWith("/roster")
      )
        throw Error("tdx_execution_identity_mismatch");
      return job;
    },
    [transport, generation, id, apiPrefix],
    !!id,
  );
  const status = useVisiblePolling(
    (signal) =>
      tdxCall<Status>(transport, "/status", signal, undefined, apiPrefix),
    [transport, generation, apiPrefix],
    !!detail.data &&
      ["PARTIAL", "FAILED", "INTERRUPTED"].includes(detail.data.state),
  );
  const facts =
    execution?.id === id ? (execution.facts as TdxJob | undefined) : undefined;
  const verifiedFacts =
    facts?.request_id === id &&
    (facts.command.action === "universe") === apiPrefix.endsWith("/roster")
      ? facts
      : null;
  const job = detail.data ?? verifiedFacts;
  if (!job)
    return (
      <p role={detail.failure ? "alert" : "status"}>
        {detail.failure
          ? "请求的 TDX 执行记录当前不可核实，未改选其他任务。"
          : "正在读取 TDX 执行详情…"}
      </p>
    );
  return (
    <section aria-label="TDX 执行详情">
      <ExecutionDetailShell
        id={job.request_id}
        assetKey={keyOf(familyAsset)}
        asset={familyTitle}
        source="TDX"
        trigger={tdxJobOrigin(job)}
        action={actionTitle(job)}
        start={job.command.start}
        end={job.command.end}
        scope={
          job.command.action === "universe" ? rosterJobScope(job) : undefined
        }
        createdAt={job.created_at}
        startedAt={job.started_at}
        finishedAt={job.finished_at}
        result={tdxStateLabel[job.state] ?? job.state}
        classification={job.classification}
        onResult={() =>
          navigate("assets", {
            ...fields,
            tab: "local",
            job: job.request_id,
            package: null,
          })
        }
        onResume={
          !["COMPLETE", "RUNNING", "QUEUED"].includes(job.state) &&
          ["PARTIAL", "FAILED", "INTERRUPTED"].includes(job.state)
            ? () => {
                if (writing.current) return;
                const selected = job.request_id;
                const owner = selectedIdentity;
                writing.current = true;
                setBusy(true);
                setNotice("");
                void tdxCall(
                  transport,
                  "/jobs/resume",
                  undefined,
                  { request_id: selected },
                  apiPrefix,
                )
                  .catch(
                    () =>
                      selectedId.current === owner &&
                      setNotice(
                        "续跑回执未核实，正在自动跟踪原任务；不会自动重试。",
                      ),
                  )
                  .finally(() => {
                    if (selectedId.current === owner) {
                      writing.current = false;
                      setBusy(false);
                      detail.refresh();
                    }
                  });
              }
            : undefined
        }
        resumeLabel={
          job.command.action === "universe"
            ? "按原请求恢复名册刷新"
            : "续跑未完成分片"
        }
        confirmResume
        actionsDisabled={
          busy ||
          !detail.data ||
          detail.stale ||
          status.data?.collect !== true ||
          status.stale
        }
      >
        {detail.failure && <p role="alert">执行详情已陈旧，请重读核实。</p>}
        {!detail.data && !detail.failure && (
          <p role="status">正在核实当前任务状态…</p>
        )}
        <dl>
          <Fact name="发布名册">{job.snapshot_id ?? "尚未发布或未记录"}</Fact>
          <Fact name="来源请求">{job.provider_requests}</Fact>
          <Fact name="分片进度">
            {job.complete_steps ?? "未记录"} /{" "}
            {job.step_count ?? job.steps?.length ?? "未记录"}
          </Fact>
          {job.admission?.mode === "retained_qualified_receipts" && (
            <Fact name="已读取留存响应">{job.retained_responses ?? 0}</Fact>
          )}
          {job.command.action !== "universe" && (
            <Fact name="价基">
              原生价基（native_projection），收益口径待核验
            </Fact>
          )}
        </dl>
        <Technical>{job.request_id}</Technical>
        {notice && <p role="alert">{notice}</p>}
        {detail.data ? (
          <TdxStepTable
            key={`${apiPrefix}:${job.request_id}`}
            job={job}
            transport={transport}
            apiPrefix={apiPrefix}
            generation={generation}
          />
        ) : (
          <p role="status">正在读取采集分片…任务事实与操作已可查看。</p>
        )}
      </ExecutionDetailShell>
    </section>
  );
}

function TdxStepTable({
  job,
  transport,
  apiPrefix,
  generation,
}: {
  job: TdxJob;
  transport: FamilyProps["transport"];
  apiPrefix: string;
  generation: number;
}) {
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState({ search: "", offset: 0 });
  const page = useVisiblePolling(
    async (signal) => {
      const value = await tdxCall<{
        request_id: string;
        steps: NonNullable<TdxJob["steps"]>;
        total: number;
        offset: number;
        next_offset: number | null;
      }>(
        transport,
        `/jobs/${job.request_id}?${new URLSearchParams({ view: "steps", search: query.search, offset: String(query.offset), limit: "25" })}`,
        signal,
        undefined,
        apiPrefix,
      );
      if (
        value.request_id !== job.request_id ||
        !Array.isArray(value.steps) ||
        !Number.isInteger(value.total) ||
        value.total < 0 ||
        value.offset !== query.offset ||
        value.steps.length !==
          Math.min(25, Math.max(0, value.total - query.offset)) ||
        value.next_offset !==
          (query.offset + 25 < value.total ? query.offset + 25 : null) ||
        (!query.search &&
          job.step_count != null &&
          value.total !== job.step_count) ||
        (job.step_count! > 0 &&
          !query.search &&
          !value.steps.length &&
          query.offset < job.step_count!)
      )
        throw Error("tdx_steps_unverified");
      return value;
    },
    [
      transport,
      generation,
      apiPrefix,
      job.request_id,
      query.offset,
      query.search,
    ],
    job.steps_paging === true,
    undefined,
    ["RUNNING", "QUEUED"].includes(job.state) && !query.search,
  );
  const steps = job.steps_paging ? page.data?.steps : job.steps;
  return (
    <>
      {job.steps_paging && !page.data && !page.failure && (
        <p role="status">正在读取采集分片…任务事实与操作已可查看。</p>
      )}
      {page.failure && (
        <p role="alert">采集分片读取失败，不能据此判断作业为空或已完成。</p>
      )}
      {query.search && (
        <p className="muted">搜索结果按本次读取显示，可刷新更新。</p>
      )}
      {!job.steps_paging && !steps && (
        <p role="alert">此历史作业未提供分片，详情尚未核实。</p>
      )}
      {steps && (
        <WorkflowTable
          singlePane
          label="采集分片"
          columns={[
            "交易所",
            "证券",
            "状态",
            "实得起始",
            "实得截止",
            "页数",
            "边界或失败",
          ]}
          rows={steps.map((s, i) => ({
            id: String(i),
            search: `${s.instrument} ${s.classification} ${s.state}`,
            cells: [
              exchanges[s.market],
              s.instrument ?? "全名册",
              tdxStateLabel[s.state] ?? s.state,
              s.first ?? "未知",
              s.last ?? "未知",
              s.pages,
              s.classification ?? s.boundary ?? "待执行",
            ],
          }))}
          pageSize={job.steps_paging ? undefined : 25}
          searchControl={
            job.steps_paging ? (
              <form
                className="searchbar"
                onSubmit={(event) => {
                  event.preventDefault();
                  const search = draft.trim();
                  if (search === query.search && query.offset === 0)
                    page.refresh();
                  else setQuery({ search, offset: 0 });
                }}
              >
                <input
                  aria-label="搜索采集分片"
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  maxLength={200}
                />
                <button>搜索分片</button>
                <button
                  type="button"
                  onClick={() => {
                    setDraft("");
                    setQuery({ search: "", offset: 0 });
                  }}
                >
                  清除
                </button>
              </form>
            ) : undefined
          }
        />
      )}
      {job.steps_paging && page.data && (
        <div className="actions" aria-label="采集分片分页">
          <button
            disabled={page.busy || query.offset === 0}
            onClick={() =>
              setQuery({ ...query, offset: Math.max(0, query.offset - 25) })
            }
          >
            前一页分片
          </button>
          <span>
            分片 {query.offset + (page.data.total ? 1 : 0)}–
            {query.offset + page.data.steps.length} / {page.data.total}
          </span>
          <button
            disabled={page.busy || page.data.next_offset === null}
            onClick={() =>
              setQuery({ ...query, offset: page.data!.next_offset! })
            }
          >
            后一页分片
          </button>
        </div>
      )}
    </>
  );
}
function TdxStrategySwitch({
  data,
  props,
  refresh,
}: {
  data: { strategy: Strategy; status: Status };
  props: FamilyProps;
  refresh: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const writing = useRef(false);
  useEffect(() => setUncertain(false), [data.strategy]);
  return (
    <span className="strategy-control">
      <span className="auto-control-line">
        <span className="small">自动采集</span>
        <StrategySwitch
          asset={title}
          enabled={data.strategy.enabled}
          saving={busy}
          uncertain={uncertain}
          disabled={
            busy || uncertain || props.stale === true || !data.status.collect
          }
          onToggle={() => {
            if (writing.current) return;
            writing.current = true;
            setBusy(true);
            void tdxCall(props.transport, "/strategy", undefined, {
              enabled: !data.strategy.enabled,
            })
              .catch(() => setUncertain(true))
              .finally(() => {
                writing.current = false;
                setBusy(false);
                refresh();
              });
          }}
        />
      </span>
      <span>
        运行情况：{data.strategy.worker_online ? "执行器在线" : "执行器离线"}
      </span>
      {uncertain && (
        <span role="alert">策略回执未核实，请刷新核对当前状态。</span>
      )}
    </span>
  );
}
export const TDX_FAMILY: RuntimeFamily = {
  collection: {
    protocol: "tdx",
    strategy: { id: strategyId, path: "/api/r5/tdx/daily/strategy" },
    path: "/api/r5/tdx/daily/jobs",
    resumePath: "/api/r5/tdx/daily/jobs/resume",
  },
  asset: TDX_DAILY,
  title,
  Query: TdxDailyQuery,
  ApiHelp: TdxApiHelp,
  Collection: TdxDailyCollection,
  ExecutionDetail: TdxExecutionDetail,
  async readExecutions(transport, signal) {
    const { jobs } = await tdxCall<{ jobs: TdxJob[] }>(
      transport,
      "/jobs",
      signal,
    );
    return jobs
      .filter((j) => j.command.action !== "universe")
      .map((j) => ({
        id: j.request_id,
        facts: j,
        createdAt: j.created_at,
        title: `${title} · ${tdxJobOrigin(j)} · ${actionTitle(j)}`,
        schedule: j.origin === "periodic" ? strategyId : undefined,
        search: `${title} ${j.request_id} ${tdxJobOrigin(j)} ${actionTitle(j)} ${tdxStateLabel[j.state] ?? j.state}`,
        start: j.command.start ?? "三市名册",
        end: j.command.end ?? "提交时点",
        result: `${tdxStateLabel[j.state] ?? j.state} · 完成分片 ${j.complete_steps ?? "未记录"}/${j.step_count ?? "未记录"} · 来源请求 ${j.provider_requests}`,
      }));
  },
  async readPeriodic(transport, signal) {
    const [strategy, status] = await Promise.all([
      tdxCall<Strategy>(transport, "/strategy", signal),
      tdxCall<Status>(transport, "/status", signal),
    ]);
    if (
      typeof strategy.enabled !== "boolean" ||
      typeof strategy.worker_online !== "boolean"
    )
      throw Error("strategy_unverified");
    return { strategy, status };
  },
  periodic(value, props, refresh) {
    const data = value as { strategy: Strategy; status: Status };
    return {
      title: `${title} · 盘后自动采集`,
      row: {
        id: strategyId,
        search: `${title} 盘后自动采集`,
        cells: [
          <span className="strategy-target">{title}</span>,
          <TdxStrategySwitch data={data} props={props} refresh={refresh} />,
          <span>
            最近心跳（北京时间）：
            <br />
            {formatBeijingTime(data.strategy.heartbeat_at, "未记录")}
          </span>,
        ],
      },
      refresh,
      detail: (
        <section aria-label="TDX 周期任务详情">
          <p>
            策略{data.strategy.enabled ? "已启用" : "已暂停"}；执行器
            {data.strategy.worker_online ? "在线" : "离线"}。交易日北京时间
            15:35
            复用早盘名册采集一次日线；当日目录尚未采集时先补目录。采集范围从交易日往前
            14 个自然日至当日，
            同批比对历史数据，有变化才写入修订。失败可续跑，已成功的股票直接跳过。
          </p>
          <p>
            交易日证据：{data.strategy.calendar_state ?? "未知"}；最近心跳：
            {formatBeijingTime(data.strategy.heartbeat_at, "未记录")}
          </p>
        </section>
      ),
    };
  },
};
