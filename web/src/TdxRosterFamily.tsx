import { tdxCanReopen } from "./tdxJobPresentation";
import {
  useCollectionRecords,
  collectionRecent,
  CollectionBriefs,
  CollectionFormLock,
  collectionCanReopen,
} from "./CollectionLifecycle";
import { useTdxPendingJob } from "./useTdxPendingJob";
import { useCollectionSubmit } from "./useCollectionSubmit";
import {
  TdxRosterFacts as RosterFacts,
  type RosterStatus,
} from "./TdxRosterFacts";
import { useEffect, useRef, useState } from "react";
import type { FamilyProps, RuntimeFamily } from "./RuntimeFamily";
import { DateField } from "./DateField";
import {
  HistoryCutoffFields,
  QueryPagination,
  QueryResultTabs,
  CollectionColumns,
  ExecutionBriefCard,
} from "./DailyFormControls";
import { RequestPreview, HttpExchangePanel } from "./CpiQueryDetails";
import { requestCpiJson, type CpiHttpExchange } from "./cpiTimeSeriesApi";
import { WorkflowTable } from "./WorkflowTable";
import { useVisiblePolling } from "./useVisiblePolling";
import { beijingInputToIso, formatBeijingTime } from "./beijingTime";
import {
  tdxCall,
  tdxLatestClosedDay,
  tdxStateLabel,
  type TdxJob,
} from "./TdxDailyWorkbench";
import { TdxExecutionDetail } from "./TdxDailyFamily";
import { TdxApiDocumentation } from "./TdxApiDocumentation";
import { requestId } from "./CpiTimeSeriesCollection";
import { tdxJobOrigin, rosterJobScope, tdxBriefs } from "./tdxJobPresentation";
import { keyOf } from "./workflowModel";

export const TDX_ROSTER = {
  identity: "mdf.market.tdx.stock.roster",
  revision: "r1",
};
const prefix = "/api/r5/tdx/roster";
const title = "沪深北 A 股基础名册";
const fields = {
  asset: TDX_ROSTER.identity,
  asset_revision: "r1",
  operation: null,
};
const exchanges = ["深市", "沪市", "北交所"];
type Status = RosterStatus;
type Result = Status & {
  rows: {
    market: number;
    instrument: string;
    name: string;
    matched_changes?: {
      old_name: string;
      new_name: string;
      effective_date: string;
      source_url?: string;
    }[];
    matched_observed_names?: string[];
    membership_state: string;
    evidence: Record<string, unknown>;
  }[];
  search_as_of?: string;
  total: number;
  offset: number;
  next_offset: number | null;
  provider_requests: number;
  sources: Record<string, unknown>[];
};
const call = <T,>(
  transport: FamilyProps["transport"],
  path: string,
  signal?: AbortSignal,
  command?: unknown,
) => tdxCall<T>(transport, path, signal, command, prefix);
export function TdxRosterQuery({ transport, generation }: FamilyProps) {
  const [exchange, setExchange] = useState<CpiHttpExchange | null>(null);
  const [requestedPath, setRequestedPath] = useState<string | null>(null);
  const [selectedDay, setDay] = useState<string | null>(null),
    [market, setMarket] = useState("all"),
    [search, setSearch] = useState("");
  const [history, setHistory] = useState(false),
    [cutoff, setCutoff] = useState("");
  const [view, setView] = useState("table"),
    [result, setResult] = useState<Result | null>(null),
    [busy, setBusy] = useState(false),
    [notice, setNotice] = useState("");
  const active = useRef<AbortController | null>(null);
  const status = useVisiblePolling(
    (signal) => call<Status>(transport, "/status", signal),
    [transport, generation],
  );
  const day =
    selectedDay ?? status.data?.default_query_day ?? tdxLatestClosedDay();
  let suffix: string | null = null;
  try {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) throw Error("date");
    const q = new URLSearchParams({ day, offset: "0", limit: "100" });
    if (market !== "all") q.set("market", market);
    if (search) q.set("search", search);
    if (history) q.set("as_of", beijingInputToIso(cutoff));
    suffix = "/query?" + q;
  } catch {
    /* Invalid drafts have no executable preview. */
  }
  useEffect(() => {
    active.current?.abort();
    setBusy(false);
    setResult(null);
    setExchange(null);
    setRequestedPath(null);
    setNotice("");
  }, [day, market, search, history, cutoff, generation]);
  useEffect(() => () => active.current?.abort(), []);
  async function read(offset = 0, page = false) {
    if (!suffix) return;
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setNotice("");
    setResult(null);
    setExchange(null);
    const url = new URL(prefix + suffix, window.location.origin);
    url.searchParams.set("offset", String(offset));
    if (page && result?.snapshot_id)
      url.searchParams.set("snapshot_id", result.snapshot_id);
    if (page && result?.search_as_of)
      url.searchParams.set("as_of", result.search_as_of);
    setRequestedPath(url.pathname + url.search);
    try {
      const data = await requestCpiJson<Result>(
        transport,
        url.pathname + url.search,
        { signal: controller.signal },
        60000,
        (value) => {
          if (!controller.signal.aborted) setExchange(value);
        },
      );
      if (!controller.signal.aborted) setResult(data);
    } catch (error) {
      if (!controller.signal.aborted)
        setNotice(error instanceof Error ? error.message : "读取失败");
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  return (
    <div>
      <p>
        基础名册与日线共用已发布快照；查询零来源请求。历史代码沿革、停牌和完整历史范围不能由当前目录推断。
      </p>
      <details>
        <summary>本地名册摘要</summary>
        {status.busy && !status.data ? (
          <p role="status">正在读取名册…</p>
        ) : status.data ? (
          <RosterFacts value={status.data} />
        ) : (
          <p role="alert">名册状态读取失败</p>
        )}
      </details>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void read();
        }}
      >
        <p className="small">
          默认按最近已核实的名册日期查询，可手动选择其他日期。
        </p>
        <div className="field-grid">
          <DateField
            type="date"
            label="名册日期"
            value={day}
            onChange={setDay}
            required
          />
          <label>
            交易所
            <select value={market} onChange={(e) => setMarket(e.target.value)}>
              <option value="all">沪深北全部</option>
              {exchanges.map((x, i) => (
                <option key={x} value={i}>
                  {x}
                </option>
              ))}
            </select>
          </label>
          <label>
            代码或名称
            <input
              placeholder="简称、曾用名、全拼或简拼"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
        </div>
        <HistoryCutoffFields
          enabled={history}
          value={cutoff}
          onEnabled={setHistory}
          onChange={setCutoff}
        />
        <button className="primary" disabled={busy || !suffix}>
          查询数据
        </button>
      </form>
      {suffix && (
        <RequestPreview
          request={{ method: "GET", path: requestedPath ?? prefix + suffix }}
        />
      )}
      {busy && <p role="status">正在读取本地名册…</p>}
      {notice && <p role="alert">{notice}；未改用其他来源。</p>}
      {exchange && (
        <HttpExchangePanel exchange={exchange} showBody={view === "raw"} />
      )}
      {result && (
        <>
          <RosterFacts value={result} />
          <p>
            匹配 {result.total} 只证券；来源请求 {result.provider_requests}。
          </p>
          <QueryPagination
            label="TDX 名册"
            offset={result.offset}
            total={result.total}
            pageSize={100}
            nextOffset={result.next_offset}
            pending={busy}
            onPage={(n) => void read(n, true)}
          />
          <QueryResultTabs label="TDX 名册" value={view} onChange={setView} />
          {view === "table" && (
            <WorkflowTable
              singlePane
              className="query-result-rows"
              pageSize={100}
              onSelect={() => {}}
              label="名册结果"
              columns={[
                "交易所",
                "代码",
                "名册名称",
                "匹配的名称记录",
                "该日成员状态",
                "上市日期",
                "退市日期",
              ]}
              rows={result.rows.map((m) => ({
                id: `${m.market}:${m.instrument}`,
                search: `${m.instrument} ${m.name}`,
                cells: [
                  exchanges[m.market],
                  m.instrument,
                  m.name,
                  [
                    ...(m.matched_changes ?? []).map(
                      (c) =>
                        c.effective_date +
                        " " +
                        c.old_name +
                        " → " +
                        c.new_name +
                        (c.source_url?.startsWith("https://fk.tdx.com.cn/")
                          ? "（通达信）"
                          : c.source_url?.startsWith("https://www.szse.cn/")
                            ? "（深交所）"
                            : ""),
                    ),
                    ...(m.matched_observed_names ?? []).map(
                      (n) => "名册曾记录 " + n,
                    ),
                  ].join("；") || "—",
                  m.membership_state === "UNKNOWN" &&
                  m.evidence.listing_date &&
                  m.evidence.valid_through &&
                  String(m.evidence.valid_through) < day
                    ? "当日待核实（最后确认在市：" +
                      String(m.evidence.valid_through) +
                      "）"
                    : ((
                        {
                          ACTIVE: "在市",
                          NOT_YET_LISTED: "尚未上市",
                          DELISTED_OR_TERMINATED: "已退市或终止上市",
                          UNKNOWN: "成员身份证据不足",
                        } as Record<string, string>
                      )[m.membership_state] ?? m.membership_state),
                  String(m.evidence.listing_date ?? "未核实"),
                  String(m.evidence.delist_date ?? "未记录"),
                ],
              }))}
            />
          )}
        </>
      )}
    </div>
  );
}
function TdxRosterCollection({ transport, navigate, generation }: FamilyProps) {
  const submission = useCollectionSubmit();
  const pending = useTdxPendingJob(TDX_ROSTER.identity);
  const [confirmed, setConfirmed] = useState(false),
    [busy, setBusy] = useState(false),
    [notice, setNotice] = useState("");
  const intent = useRef<string | null>(pending.id),
    writing = useRef(false);
  const [receipt, setReceipt] = useState<TdxJob | null>(null);
  function accepted(job: TdxJob) {
    if (!intent.current || job.request_id !== intent.current) return;
    setReceipt(job);
    setNotice("");
    setConfirmed(false);
    jobs.refresh();
  }
  const status = useVisiblePolling(
    (signal) => call<Status>(transport, "/status", signal),
    [transport, generation],
  );
  const jobs = useVisiblePolling(
    (signal) => call<{ jobs: TdxJob[] }>(transport, "/jobs", signal),
    [transport, generation],
  );
  const open = (id: string) =>
    navigate("collection", {
      ...fields,
      tab: "executions",
      job: id,
      execution_asset: keyOf(TDX_ROSTER),
      execution_schedule: "all",
      schedule: null,
    });
  async function submit() {
    if (
      writing.current ||
      !confirmed ||
      intent.current ||
      pending.blocked ||
      !status.data?.collect ||
      status.stale
    )
      return;
    const id = requestId();
    if (!pending.reserve(id)) return;
    writing.current = true;
    setBusy(true);
    setNotice("");
    intent.current = id;
    try {
      const receipt = await submission.submit(
        id,
        () => call<TdxJob>(transport, "/jobs", undefined, { request_id: id }),
        () => call<TdxJob>(transport, "/jobs/" + id),
        (value) => value.request_id === id,
      );
      accepted(receipt);
    } catch {
      setNotice("刷新回执未核实；保留原请求编号，回查前不重复提交。");
    } finally {
      writing.current = false;
      setBusy(false);
    }
  }
  const currentJobs = useCollectionRecords([
    ...(receipt ? [receipt] : []),
    ...(jobs.data?.jobs ?? []),
  ]);
  const tracked =
    currentJobs.find((j) => j.request_id === pending.id) ??
    (receipt?.request_id === pending.id ? receipt : null);
  const visibleJobs = collectionRecent(currentJobs, pending.id);
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
      aria-label="TDX 基础名册 刷新"
    >
      <h3>{title} · 刷新</h3>
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
                    void call<TdxJob>(transport, "/jobs/" + intent.current)
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
            {jobs.busy && !jobs.data && (
              <p role="status">正在读取真实名册作业…</p>
            )}
            {jobs.failure && <p role="alert">作业读取失败，记录待核实。</p>}
            {jobs.data && !jobs.data.jobs.length && !receipt && (
              <p>暂无名册刷新记录。</p>
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
                    title={`${title} · ${tdxJobOrigin(j)}`}
                    scope={rosterJobScope(j)}
                    createdAt={j.created_at}
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
          onSubmit={(e) => {
            e.preventDefault();
            if (status.data?.collect && !status.stale) void submit();
          }}
        >
          <CollectionFormLock locked={busy || !!pending.id}>
            <p>
              采集目标：沪深北 A
              股基础名册。方式：完整刷新三市来源目录；不采集日线。与旧
              action=universe 共用实际实现、作业锁及发布。
            </p>
            <label>
              采集方式
              <select
                value="refresh"
                disabled={busy || !!intent.current}
                onChange={() => {}}
              >
                <option value="refresh">完整刷新三市名册</option>
              </select>
            </label>
            <p>
              沪深 GetCount/GetCode、北交所 GetBJDirectory
              的原件分别留存；交易所上市/退市与北交所新旧代码证据独立核定。目录刷新不会延长交易所证据有效期，不自动取得已退市全历史名册。
            </p>
            <p>采集范围：深市、沪市、北交所的 A 股来源目录（提交时快照）。</p>
            {status.failure && <p role="alert">名册采集资格读取失败。</p>}
            {status.data && !status.data.collect && (
              <p role="status">当前采集能力未启用。</p>
            )}
            <details>
              <summary>当前名册证据</summary>
              {status.data && <RosterFacts value={status.data} />}
            </details>
            <label className="cpi-collection-confirm">
              <input
                type="checkbox"
                checked={confirmed}
                disabled={busy || !!intent.current}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              我确认刷新三市名册到开发测试库
            </label>
            <button
              className="primary"
              disabled={
                busy ||
                !!pending.id ||
                pending.blocked ||
                !confirmed ||
                !status.data?.collect ||
                status.stale
              }
              type="submit"
            >
              提交采集任务
            </button>

            <p>交易日北京时间 09:00 刷新三市目录；本表单不会改变周期策略。</p>
          </CollectionFormLock>
        </form>
      </CollectionColumns>
    </section>
  );
}
export const TDX_ROSTER_FAMILY: RuntimeFamily = {
  collection: {
    protocol: "roster",
    strategy: { id: "tdx-roster-auto", path: "/api/r5/tdx/daily/strategy" },
    path: "/api/r5/tdx/roster/jobs",
    resumePath: "/api/r5/tdx/roster/jobs/resume",
  },
  asset: TDX_ROSTER,
  title,
  Query: TdxRosterQuery,
  ApiHelp: (props) => (
    <TdxApiDocumentation transport={props.transport} roster />
  ),
  Collection: TdxRosterCollection,
  ExecutionDetail: (props) => (
    <TdxExecutionDetail
      {...props}
      familyAsset={TDX_ROSTER}
      apiPrefix={prefix}
      familyTitle={title}
    />
  ),
  async readExecutions(transport, signal) {
    const { jobs } = await call<{ jobs: TdxJob[] }>(transport, "/jobs", signal);
    return jobs.map((j) => ({
      id: j.request_id,
      facts: j,
      createdAt: j.created_at,
      title: `${title} · ${tdxJobOrigin(j)}`,
      search: `${title} ${j.request_id} ${j.state}`,
      schedule: j.origin === "periodic" ? "tdx-roster-auto" : undefined,
      scope: rosterJobScope(j),
      result: `${tdxStateLabel[j.state] ?? j.state} · 来源请求 ${j.provider_requests}`,
    }));
  },
  async readPeriodic(transport, signal) {
    const value = await tdxCall<{
      enabled: boolean;
      heartbeat_at?: string;
      worker_online: boolean;
    }>(transport, "/strategy", signal);
    if (
      typeof value.enabled !== "boolean" ||
      typeof value.worker_online !== "boolean"
    )
      throw Error("strategy_unverified");
    return value;
  },
  periodic(value, props, refresh) {
    const data = value as {
      enabled: boolean;
      heartbeat_at?: string;
      worker_online: boolean;
    };
    return {
      title: `${title} · 盘前目录刷新`,
      row: {
        id: "tdx-roster-auto",
        search: `${title} 09:00`,
        cells: [
          title,
          <span>
            复用日线策略：{data.enabled ? "已开启" : "已暂停"}；执行器
            {data.worker_online ? "在线" : "离线"}
          </span>,
          formatBeijingTime(data.heartbeat_at, "未记录"),
        ],
      },
      refresh,
      detail: (
        <section aria-label="TDX 名册 周期详情">
          <p>
            09:00 Asia/Shanghai 刷新三市行情目录。15:35
            日线复用当日已完成的目录作业；错过早盘时先补目录。上市、停牌和正式更名以交易所对应日期的证据为准。
          </p>
        </section>
      ),
    };
  },
};
