import { useEffect, useRef, useState } from "react";
import type { Transport } from "./api";
import type { Navigate, Route } from "./workflowRoute";
import { useVisiblePolling } from "./useVisiblePolling";
import { WorkflowTable, Technical } from "./WorkflowTable";
import { requestId } from "./CpiTimeSeriesCollection";

export const TDX_DAILY = {
  identity: "mdf.market.tdx.stock.daily.native",
  revision: "r1",
};
const prefix = "/api/r5/tdx/daily";
const exchange = ["深市", "沪市", "北交所"];
type Step = {
  market: number;
  instrument?: string;
  state: string;
  first?: string;
  last?: string;
  offset?: number;
  boundary?: string;
  classification?: string;
  pages?: number;
  rows?: number;
};
type Job = {
  request_id: string;
  state: string;
  created_at: string;
  provider_requests: number;
  step_count?: number;
  complete_steps?: number;
  command: { action: string; start: string; end: string };
  steps?: Step[];
};
type Bar = {
  market: number;
  instrument: string;
  trading_day: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  amount: number;
  received_at: string;
  available_at: string;
};
type Market = {
  market: number;
  roster_count?: number;
  instruments?: number;
  rows?: number;
  first?: string;
  last?: string;
  valid_bars?: number;
  zero_volume_bars?: number;
  failed?: number;
  unknown?: number;
  effective_count?: number;
  pre_listing?: number;
  post_delisting?: number;
  confirmed_suspended?: number;
  suspended_with_bars?: number;
  calendar_state?: string;
  complete?: boolean;
};
type Result = {
  rows: Bar[];
  total: number;
  next_offset: number | null;
  as_of: string;
  provider_requests: number;
};
type Coverage = {
  complete: boolean;
  markets: Market[];
  missing_total: number;
  missing: {
    market: number;
    instrument: string;
    name: string;
    classification: string;
  }[];
};
type Help = {
  examples: { history: string; cross_section: string; coverage: string };
  notes: string[];
};
const stateLabel: Record<string, string> = {
  QUEUED: "已排队",
  INTERRUPTED: "执行中断，可续跑",
  RUNNING: "执行中",
  COMPLETE: "请求范围已完成",
  PARTIAL: "部分完成",
  FAILED: "失败",
  PENDING: "待执行",
};
async function call<T>(
  transport: Transport,
  path: string,
  signal?: AbortSignal,
  command?: unknown,
): Promise<T> {
  const response = await transport(prefix + path, {
    method: command === undefined ? "GET" : "POST",
    cache: "no-store",
    signal,
    ...(command === undefined
      ? {}
      : {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(command),
        }),
  });
  const value = await response.json();
  if (!response.ok)
    throw new Error(
      typeof value.classification === "string"
        ? value.classification
        : "tdx_request_failed",
    );
  return value as T;
}
export function TdxDailyWorkbench({
  transport,
  route,
  navigate,
}: {
  transport: Transport;
  route: Route;
  navigate: Navigate;
}) {
  const [generation, refresh] = useState(0);
  const tab =
    route.view === "assets" ? "query" : (route.fields.get("tab") ?? "new");
  const [direction, setDirection] = useState("history"),
    [market, setMarket] = useState("1"),
    [instrument, setInstrument] = useState("600519");
  const [start, setStart] = useState("1990-01-01"),
    [end, setEnd] = useState(
      new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Shanghai" }).format(
        new Date(),
      ),
    );
  const [asOf, setAsOf] = useState(""),
    [action, setAction] = useState("init"),
    [result, setResult] = useState<Result | null>(null),
    [coverage, setCoverage] = useState<Coverage | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const selected = route.fields.get("job");
  const summary = useVisiblePolling(
    (signal) => call<{ markets: Market[] }>(transport, "/summary", signal),
    [transport],
    tab === "query",
    () => true,
    false,
  );
  const status = useVisiblePolling(
    (signal) =>
      call<{ enabled: boolean; collect: boolean; defaults: { end: string } }>(
        transport,
        "/status",
        signal,
      ),
    [transport, generation],
    true,
  );
  const defaultsApplied = useRef(false);
  useEffect(() => {
    if (status.data?.defaults.end && !defaultsApplied.current) {
      defaultsApplied.current = true;
      setEnd(status.data.defaults.end);
    }
  }, [status.data?.defaults.end]);
  const jobs = useVisiblePolling(
    (signal) => call<{ jobs: Job[] }>(transport, "/jobs", signal),
    [transport, generation],
    tab === "executions",
  );
  const detail = useVisiblePolling(
    (signal) => call<Job>(transport, "/jobs/" + selected, signal),
    [transport, generation, selected],
    tab === "executions" && !!selected,
  );
  const strategy = useVisiblePolling(
    (signal) =>
      call<{ enabled: boolean; worker_online: boolean; heartbeat_at?: string }>(
        transport,
        "/strategy",
        signal,
      ),
    [transport, generation],
    tab === "periodic",
  );
  const help = useVisiblePolling(
    (signal) => call<Help>(transport, "/help", signal),
    [transport],
    true,
  );
  const go = (target: string, job?: string) =>
    navigate(target === "query" ? "assets" : "collection", {
      asset: TDX_DAILY.identity,
      asset_revision: TDX_DAILY.revision,
      tab: target === "query" ? "local" : target,
      job: job ?? null,
    });
  async function perform(work: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await work();
      refresh((g) => g + 1);
    } catch (e) {
      setError(e instanceof Error ? e.message : "tdx_request_failed");
    } finally {
      setBusy(false);
    }
  }
  async function query(offset = 0) {
    const q = new URLSearchParams({
      local_only: "true",
      basis: "native_projection",
      limit: "100",
      offset: String(offset),
    });
    if (direction === "history") {
      q.set("market", market);
      q.set("instrument", instrument);
      q.set("start", start);
      q.set("end", end);
    } else {
      q.set("day", end);
      if (market !== "all") q.set("market", market);
    }
    if (asOf) q.set("as_of", asOf);
    setResult(await call<Result>(transport, "/query?" + q));
    setCoverage(
      await call<Coverage>(
        transport,
        "/coverage?" +
          new URLSearchParams({ day: end, ...(asOf ? { as_of: asOf } : {}) }),
      ),
    );
  }
  return (
    <div className="tdx-daily-workbench">
      <h2>沪深北 A 股盘后日线</h2>
      <p>
        原生价基 ·
        价格与金额按千分之一元保存，成交量为手。收益口径待核验；按已留存的交易所有效期名册与日历对账，缺少的历史成员和停牌证据单列未知。
      </p>
      <nav className="tabs" aria-label="TDX 日线操作">
        {[
          ["query", "本地查询"],
          ["new", "采集数据"],
          ["periodic", "周期策略"],
          ["executions", "执行记录"],
        ].map(([key, label]) => (
          <button
            key={key}
            aria-current={tab === key ? "page" : undefined}
            onClick={() => go(key)}
          >
            {label}
          </button>
        ))}
      </nav>
      {(!status.data?.enabled ||
        error ||
        status.failure ||
        summary.failure ||
        jobs.failure ||
        detail.failure ||
        strategy.failure) && (
        <p role="alert">
          {error ||
            status.failure ||
            summary.failure ||
            jobs.failure ||
            detail.failure ||
            strategy.failure ||
            "TDX 开发能力未启用"}
        </p>
      )}
      {tab === "query" && (
        <>
          <button disabled={summary.busy} onClick={() => summary.refresh()}>
            刷新本地汇总
          </button>
          <WorkflowTable
            label="本地市场范围"
            columns={["交易所", "证券", "日线数", "最早", "最近"]}
            rows={(summary.data?.markets ?? []).map((m) => ({
              id: String(m.market),
              search: exchange[m.market],
              cells: [
                exchange[m.market],
                m.instruments,
                m.rows,
                m.first ?? "未知",
                m.last ?? "未知",
              ],
            }))}
            onSelect={(value) => setMarket(value)}
          />
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void perform(() => query());
            }}
          >
            <label>
              查询方向
              <select
                value={direction}
                onChange={(e) => {
                  setDirection(e.target.value);
                  if (e.target.value === "history" && market === "all")
                    setMarket("1");
                }}
              >
                <option value="history">单股历史</option>
                <option value="day">某日横截面</option>
              </select>
            </label>
            <label>
              交易所
              <select
                aria-label="交易所"
                value={market}
                onChange={(e) => setMarket(e.target.value)}
              >
                {direction === "day" && <option value="all">三市</option>}
                {exchange.map((name, i) => (
                  <option key={i} value={i}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            {direction === "history" && (
              <>
                <label>
                  证券代码
                  <input
                    value={instrument}
                    pattern="[0-9]{6}"
                    onChange={(e) => setInstrument(e.target.value)}
                  />
                </label>
                <label>
                  开始日期
                  <input
                    type="date"
                    value={start}
                    onChange={(e) => setStart(e.target.value)}
                  />
                </label>
              </>
            )}
            <label>
              {direction === "day" ? "交易日" : "结束日期"}
              <input
                type="date"
                value={end}
                onChange={(e) => setEnd(e.target.value)}
              />
            </label>
            <label>
              可用截止（含时区，可留空）
              <input
                value={asOf}
                placeholder="2026-11-12T23:00:00+08:00"
                onChange={(e) => setAsOf(e.target.value)}
              />
            </label>
            <button disabled={busy}>查询本地日线</button>
          </form>
          {result && (
            <>
              <p>
                共 {result.total} 根；来源请求 {result.provider_requests}{" "}
                次。截止 {result.as_of}。
              </p>
              <WorkflowTable
                label="日线结果"
                columns={[
                  "交易日",
                  "交易所",
                  "代码",
                  "开盘",
                  "最高",
                  "最低",
                  "收盘",
                  "成交量（手）",
                  "成交额（元）",
                  "可用时间",
                ]}
                rows={result.rows.map((b) => ({
                  id: `${b.market}:${b.instrument}:${b.trading_day}`,
                  search: `${b.instrument} ${b.trading_day}`,
                  cells: [
                    b.trading_day,
                    exchange[b.market],
                    b.instrument,
                    (b.open / 1000).toFixed(3),
                    (b.high / 1000).toFixed(3),
                    (b.low / 1000).toFixed(3),
                    (b.close / 1000).toFixed(3),
                    b.volume,
                    (b.amount / 1000).toFixed(3),
                    b.available_at,
                  ],
                }))}
                onSelect={() => {}}
                pageSize={25}
              />
              {result.next_offset !== null && (
                <button
                  disabled={busy}
                  onClick={() => void perform(() => query(result.next_offset!))}
                >
                  下一页
                </button>
              )}
            </>
          )}
          {coverage && (
            <>
              <p>
                按查询日有效期对账，未解决 {coverage.missing_total}{" "}
                项。覆盖结论：{coverage.complete ? "完整" : "部分或证据不足"}。
              </p>
              <WorkflowTable
                label="按交易所覆盖"
                columns={[
                  "交易所",
                  "当日成员（含有效期未知）",
                  "有效 K 线",
                  "零量 K 线",
                  "失败",
                  "状态未知",
                  "未上市",
                  "已退市",
                  "已证实停牌",
                  "停牌但来源有线",
                  "交易日证据",
                ]}
                rows={coverage.markets.map((m) => ({
                  id: String(m.market),
                  search: exchange[m.market],
                  cells: [
                    exchange[m.market],
                    m.effective_count ?? m.roster_count ?? "未知",
                    m.valid_bars,
                    m.zero_volume_bars,
                    m.failed,
                    m.unknown,
                    m.pre_listing ?? "未知",
                    m.post_delisting ?? "未知",
                    m.confirmed_suspended ?? 0,
                    m.suspended_with_bars ?? 0,
                    m.calendar_state ?? "UNKNOWN",
                  ],
                }))}
                onSelect={() => {}}
              />
              <WorkflowTable
                label="缺项明细（首 100 项）"
                columns={["交易所", "证券", "原因"]}
                rows={coverage.missing.map((m) => ({
                  id: `${m.market}:${m.instrument}`,
                  search: `${m.name} ${m.instrument} ${m.classification}`,
                  cells: [
                    exchange[m.market],
                    `${m.instrument} ${m.name}`,
                    m.classification,
                  ],
                }))}
                onSelect={(id) => {
                  const [m, code] = id.split(":");
                  setMarket(m);
                  setInstrument(code);
                  setDirection("history");
                }}
                pageSize={20}
              />
            </>
          )}
        </>
      )}
      {tab === "new" && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void perform(async () => {
              const record = await call<Job>(transport, "/jobs", undefined, {
                request_id: requestId(),
                action,
                ...(action === "universe"
                  ? {}
                  : {
                      start:
                        action === "update"
                          ? new Date(Date.parse(end) - 14 * 86400000)
                              .toISOString()
                              .slice(0, 10)
                          : start,
                      end,
                    }),
              });
              go("executions", record.request_id);
            });
          }}
        >
          <label>
            采集方式
            <select value={action} onChange={(e) => setAction(e.target.value)}>
              <option value="init">历史初始化</option>
              <option value="update">盘后增量（回看 14 日）</option>
              <option value="universe">刷新三市名册</option>
            </select>
          </label>
          {action !== "universe" && (
            <>
              {action === "init" && (
                <label>
                  开始日期
                  <input
                    type="date"
                    value={start}
                    onChange={(e) => setStart(e.target.value)}
                  />
                </label>
              )}
              <label>
                结束日期
                <input
                  type="date"
                  value={end}
                  max={status.data?.defaults.end}
                  onChange={(e) => setEnd(e.target.value)}
                />
              </label>
            </>
          )}
          <p>
            三市完整来源名册；初始化与增量逐页持久保存。中断后在执行记录续跑失败分片。
          </p>
          <button disabled={busy || !status.data?.collect}>提交采集</button>
        </form>
      )}
      {tab === "periodic" && (
        <>
          <p>
            策略{strategy.data?.enabled ? "已启用" : "已暂停"}；执行器
            {strategy.data?.worker_online ? "在线" : "离线"}。工作日候选窗口
            15:10（北京时间）后刷新名册并更新最近 14
            日；实际开市与停牌另需证据。
          </p>
          <p>最近心跳：{strategy.data?.heartbeat_at ?? "未知"}</p>
          <button
            disabled={busy || !status.data?.collect}
            onClick={() =>
              void perform(async () => {
                await call(transport, "/strategy", undefined, {
                  enabled: !strategy.data?.enabled,
                });
              })
            }
          >
            {strategy.data?.enabled ? "暂停策略" : "启用策略"}
          </button>
        </>
      )}
      {tab === "executions" && (
        <>
          <WorkflowTable
            label="TDX 执行记录"
            columns={["提交时间", "方式", "状态", "已完成分片", "来源请求"]}
            rows={(jobs.data?.jobs ?? []).map((j) => ({
              id: j.request_id,
              search: `${j.request_id} ${j.command.action} ${j.state}`,
              cells: [
                j.created_at,
                j.command.action,
                stateLabel[j.state] ?? j.state,
                `${j.complete_steps}/${j.step_count}`,
                j.provider_requests,
              ],
            }))}
            selected={selected}
            onSelect={(id) => go("executions", id)}
            pageSize={15}
          />
          {detail.data && (
            <>
              <h3>执行详情</h3>
              <p>
                {stateLabel[detail.data.state] ?? detail.data.state} ·{" "}
                {detail.data.command.start} 至 {detail.data.command.end}
              </p>
              <Technical>{detail.data.request_id}</Technical>
              <button
                disabled={
                  busy ||
                  ["COMPLETE", "RUNNING", "QUEUED"].includes(detail.data.state)
                }
                onClick={() =>
                  void perform(async () => {
                    await call(transport, "/jobs/resume", undefined, {
                      request_id: detail.data!.request_id,
                    });
                  })
                }
              >
                续跑未完成分片
              </button>
              <button onClick={() => go("query")}>查看本地结果</button>
              <WorkflowTable
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
                rows={(detail.data.steps ?? []).map((s, i) => ({
                  id: String(i),
                  search: `${s.instrument} ${s.classification} ${s.state}`,
                  cells: [
                    exchange[s.market],
                    s.instrument ?? "全名册",
                    stateLabel[s.state] ?? s.state,
                    s.first ?? "未知",
                    s.last ?? "未知",
                    s.pages,
                    s.classification ?? s.boundary ?? "待执行",
                  ],
                }))}
                onSelect={() => {}}
                pageSize={25}
              />
            </>
          )}
        </>
      )}
      <details>
        <summary>API 帮助与示例</summary>
        <p>
          证券历史和单日横截面共用查询入口；仅本地读取。可用截止为 MDF
          实际保存的版本时间。
        </p>
        {help.data && (
          <>
            <pre>
              {help.data.examples.history}
              {"\n"}
              {help.data.examples.cross_section}
              {"\n"}
              {help.data.examples.coverage}
            </pre>
            <ul>
              {help.data.notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          </>
        )}
      </details>
    </div>
  );
}
