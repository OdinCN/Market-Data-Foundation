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
const title = "沪深北 A 股盘后日线";
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
    if (!intent.current || job.request_id !== intent.current)
      throw Error("request_identity_unconfirmed");
    pending.release(job.request_id);
    setReceipt(job);
    intent.current = null;
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
    if (!end) return;
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
  const recent = tdxBriefs(
    (jobs.data?.jobs ?? []).filter((j) => j.command.action !== "universe"),
    receipt,
  );
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
          <aside className="detail-pane execution-briefs" aria-label="执行简报">
            <h3>执行简报</h3>
            {pending.blocked && (
              <p role="alert">本机请求编号无法核实，采集已暂停。</p>
            )}
            {notice && <p role="alert">{notice}</p>}
            {pending.id && (
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
                  回查同一任务
                </button>
              </>
            )}
            <p className="small">
              时间基准：北京时间（UTC+08:00）。以下为真实近期作业，触发来源单列；缺少来源记录时保留待核实。
            </p>
            {jobs.busy && !jobs.data && <p role="status">正在读取执行简报…</p>}
            {jobs.failure && (
              <p role="alert">执行简报读取失败，已有记录待核实。</p>
            )}
            {jobs.data && !recent.length && (
              <p>暂无采集记录；尚未提交本次采集。</p>
            )}
            <ol className="execution-brief-list">
              {recent.map((j) => (
                <li key={j.request_id}>
                  <ExecutionBriefCard
                    id={j.request_id}
                    current={j.request_id === receipt?.request_id}
                    title={`${title} · ${tdxJobOrigin(j)} · ${actionTitle(j)}`}
                    createdAt={j.created_at}
                    start={j.command.start}
                    end={j.command.end}
                    result={tdxStateLabel[j.state] ?? j.state}
                    onSelect={() => open(j.request_id)}
                  />
                </li>
              ))}
            </ol>
          </aside>
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
                setNotice("提交回执未核实，请回查同一任务；不会自动重试。");
              } finally {
                setBusy(false);
                writing.current = false;
              }
            })();
          }}
        >
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
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const writing = useRef(false);
  const detail = useVisiblePolling(
    (signal) =>
      tdxCall<TdxJob>(
        transport,
        "/jobs/" + encodeURIComponent(id!),
        signal,
        undefined,
        apiPrefix,
      ),
    [transport, generation, id, apiPrefix],
    !!id,
  );
  const status = useVisiblePolling(
    (signal) =>
      tdxCall<Status>(transport, "/status", signal, undefined, apiPrefix),
    [transport, generation, apiPrefix],
  );
  if (!detail.data)
    return (
      <p role={detail.failure ? "alert" : "status"}>
        {detail.failure
          ? "请求的 TDX 执行记录当前不可核实，未改选其他任务。"
          : "正在读取 TDX 执行详情…"}
      </p>
    );
  const job = detail.data;
  return (
    <section aria-label="TDX 执行详情">
      <h3>
        {job.command.action === "universe"
          ? "沪深北 A 股基础名册"
          : familyTitle}{" "}
        · {actionTitle(job)}
      </h3>
      {detail.failure && <p role="alert">执行详情已陈旧，请重读核实。</p>}
      <dl>
        <Fact name="执行状态">{tdxStateLabel[job.state] ?? job.state}</Fact>
        <Fact name="触发来源">{tdxJobOrigin(job)}</Fact>
        <Fact name="创建时间">{formatBeijingTime(job.created_at)}</Fact>
        <Fact name="开始时间">
          {formatBeijingTime(job.started_at, "未记录")}
        </Fact>
        <Fact name="结束时间">
          {formatBeijingTime(job.finished_at, "尚未结束或未记录")}
        </Fact>
        <Fact name="发布名册">{job.snapshot_id ?? "尚未发布或未记录"}</Fact>
        {job.classification && (
          <Fact name="结果分类">{job.classification}</Fact>
        )}
        <Fact name="采集范围">
          {job.command.action === "universe"
            ? rosterJobScope(job)
            : `${job.command.start} 至 ${job.command.end}`}
        </Fact>
        <Fact name="来源请求">{job.provider_requests}</Fact>
        {job.admission?.mode === "retained_qualified_receipts" && (
          <Fact name="已读取留存响应">{job.retained_responses ?? 0}</Fact>
        )}
        {job.command.action !== "universe" && (
          <Fact name="价基">原生价基（native_projection），收益口径待核验</Fact>
        )}
      </dl>
      <Technical>{job.request_id}</Technical>
      <button
        disabled={
          busy ||
          detail.stale ||
          !status.data?.collect ||
          status.stale ||
          ["COMPLETE", "RUNNING", "QUEUED"].includes(job.state)
        }
        onClick={() => {
          if (writing.current) return;
          writing.current = true;
          setBusy(true);
          setNotice("");
          void tdxCall(
            transport,
            "/jobs/resume",
            undefined,
            {
              request_id: job.request_id,
            },
            apiPrefix,
          )
            .catch(() =>
              setNotice("续跑回执未核实，请回查当前任务；不会自动重试。"),
            )
            .finally(() => {
              writing.current = false;
              setBusy(false);
              detail.refresh();
            });
        }}
      >
        续跑未完成分片
      </button>
      <button onClick={() => navigate("assets", { ...fields, tab: "local" })}>
        查看本地结果
      </button>
      <button onClick={detail.refresh}>回查当前任务</button>
      {notice && <p role="alert">{notice}</p>}
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
        rows={(job.steps ?? []).map((s, i) => ({
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
        onSelect={() => {}}
        pageSize={25}
      />
    </section>
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
  collection: { protocol: "tdx", path: "/api/r5/tdx/daily/jobs" },
  asset: TDX_DAILY,
  title: "沪深北 A 股原生日线",
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
        createdAt: j.created_at,
        title: `${title} · ${actionTitle(j)}`,
        search: `${title} ${j.request_id} ${actionTitle(j)} ${tdxStateLabel[j.state] ?? j.state}`,
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
    return { strategy, status };
  },
  periodic(value, props, refresh) {
    const data = value as { strategy: Strategy; status: Status };
    return {
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
          <h3>{title} · 盘后自动采集</h3>
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
          <button
            onClick={() =>
              props.navigate("collection", {
                ...fields,
                tab: "executions",
                execution_asset: keyOf(TDX_DAILY),
                execution_schedule: "all",
                schedule: null,
                job: null,
              })
            }
          >
            查看执行记录 →
          </button>
          <button
            onClick={() =>
              props.navigate("assets", { ...fields, tab: "local" })
            }
          >
            查看关联资产 →
          </button>
        </section>
      ),
    };
  },
};
