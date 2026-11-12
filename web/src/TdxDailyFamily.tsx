import {
  DailyDateRange,
  CollectionColumns,
  ExecutionBriefCard,
  dailyRangeError,
} from "./DailyFormControls";
import { tdxJobOrigin } from "./tdxJobPresentation";
import { useEffect, useRef, useState } from "react";
import type { FamilyProps, RuntimeFamily } from "./RuntimeFamily";
import {
  TDX_DAILY,
  TdxDailyQuery,
  TdxApiHelp,
  tdxCall,
  tdxStateLabel,
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
const actionTitle = (action: string) =>
  ({ init: "历史初始化", update: "盘后增量", universe: "刷新三市名册" })[
    action
  ] ?? action;
const fields = {
  asset: TDX_DAILY.identity,
  asset_revision: TDX_DAILY.revision,
  operation: null,
};
const strategyId = "tdx-daily-auto";
type Status = {
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
  const [action, setAction] = useState<"init" | "update" | "universe">(
    "update",
  );
  const [drafts, setDrafts] = useState({
    init: { start: "1990-01-01", end: "" },
    update: { start: "", end: "" },
  });
  const [confirmed, setConfirmed] = useState(false),
    [busy, setBusy] = useState(false),
    [notice, setNotice] = useState("");
  const edited = useRef({ init: false, update: false }),
    writing = useRef(false),
    intent = useRef<string | null>(null);
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
    const start = new Date(Date.parse(end + "T00:00:00Z") - 14 * 86400000)
      .toISOString()
      .slice(0, 10);
    setDrafts((old) => ({
      init: edited.current.init
        ? old.init
        : { start: status.data?.defaults.start ?? "1990-01-01", end },
      update: edited.current.update ? old.update : { start, end },
    }));
  }, [status.data]);
  const range = drafts[action === "universe" ? "update" : action];
  useEffect(() => setConfirmed(false), [action, range.start, range.end]);
  const error =
    action === "universe"
      ? ""
      : range.start && range.start < "1990-01-01"
        ? "采集开始日期不得早于 1990-01-01。"
        : dailyRangeError(
            range,
            366 * 200,
            status.data?.defaults.end ?? beijingToday(),
          );
  function change(key: "start" | "end", value: string) {
    if (action === "universe") return;
    edited.current[action] = true;
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
  const recent = [...(jobs.data?.jobs ?? [])]
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .slice(0, 5);
  return (
    <section
      className="ordinary-query cpi-timeseries"
      aria-label="TDX 日频 受控采集"
    >
      <h3>{title} · 受控采集</h3>
      <CollectionColumns
        briefs={
          <aside className="detail-pane execution-briefs" aria-label="执行简报">
            <h3>执行简报</h3>
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
                    title={`${title} · ${tdxJobOrigin(j)} · ${actionTitle(j.command.action)}`}
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
            writing.current = true;
            intent.current = requestId();
            setBusy(true);
            setNotice("");
            void (async () => {
              try {
                const job = await tdxCall<TdxJob>(
                  transport,
                  "/jobs",
                  undefined,
                  {
                    request_id: intent.current,
                    action,
                    ...(action === "universe" ? {} : range),
                  },
                );
                open(job.request_id);
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
              <option value="universe">刷新三市名册</option>
            </select>
          </label>
          {action !== "universe" && (
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
          )}
          <p className="small">
            {action === "init"
              ? "初始化服务预置范围：1990 年至最近盘后日，可自行修改。"
              : action === "update"
                ? "更新默认回看 14 日，与既有盘后策略一致；起止日期可编辑，不沿用初始化草稿。"
                : "刷新三市完整来源名册，不采集历史日线。"}
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
              !!intent.current ||
              !confirmed ||
              !!error ||
              !status.data?.collect ||
              status.stale
            }
          >
            {busy ? "正在提交…" : "提交 TDX 日线 采集"}
          </button>
          {notice && <p role="alert">{notice}</p>}
          {notice && (
            <button type="button" onClick={() => open(intent.current!)}>
              回查同一任务
            </button>
          )}
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
}: FamilyProps) {
  const id = route.fields.get("job");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const writing = useRef(false);
  const detail = useVisiblePolling(
    (signal) =>
      tdxCall<TdxJob>(transport, "/jobs/" + encodeURIComponent(id!), signal),
    [transport, generation, id],
    !!id,
  );
  const status = useVisiblePolling(
    (signal) => tdxCall<Status>(transport, "/status", signal),
    [transport, generation],
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
        {title} · {actionTitle(job.command.action)}
      </h3>
      {detail.failure && <p role="alert">执行详情已陈旧，请重读核实。</p>}
      <dl>
        <Fact name="执行状态">{tdxStateLabel[job.state] ?? job.state}</Fact>
        <Fact name="触发来源">{tdxJobOrigin(job)}</Fact>
        <Fact name="创建时间">{formatBeijingTime(job.created_at)}</Fact>
        <Fact name="采集范围">
          {job.command.start ?? "三市名册"} 至 {job.command.end ?? "提交时点"}
        </Fact>
        <Fact name="来源请求">{job.provider_requests}</Fact>
        <Fact name="价基">原生价基（native_projection），收益口径待核验</Fact>
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
          void tdxCall(transport, "/jobs/resume", undefined, {
            request_id: job.request_id,
          })
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
    return jobs.map((j) => ({
      id: j.request_id,
      createdAt: j.created_at,
      title: `${title} · ${actionTitle(j.command.action)}`,
      search: `${title} ${j.request_id} ${actionTitle(j.command.action)} ${tdxStateLabel[j.state] ?? j.state}`,
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
            {data.strategy.worker_online ? "在线" : "离线"}。工作日候选窗口
            15:10（北京时间）后刷新名册并更新最近 14
            日；实际开市与停牌另需证据。
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
