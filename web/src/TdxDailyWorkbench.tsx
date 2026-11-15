import { TdxApiDocumentation } from "./TdxApiDocumentation";
import {
  DailyDateRange,
  HistoryCutoffFields,
  QueryResultTabs,
  LocalQuerySummary,
  QueryPagination,
} from "./DailyFormControls";
import { RequestPreview, HttpExchangePanel } from "./CpiQueryDetails";
import { requestCpiJson, type CpiHttpExchange } from "./cpiTimeSeriesApi";
import { useEffect, useRef, useState } from "react";
import type { Transport } from "./api";
import type { FamilyProps } from "./RuntimeFamily";
import {
  beijingToday,
  formatBeijingTime,
  toBeijingIso,
  beijingInstantKey,
  beijingInputToIso,
} from "./beijingTime";
import { useVisiblePolling } from "./useVisiblePolling";
import { WorkflowTable } from "./WorkflowTable";

export const TDX_DAILY = {
  identity: "mdf.market.tdx.stock.daily.native",
  revision: "r1",
};
const prefix = "/api/r5/tdx/daily";
const exchanges = ["深市", "沪市", "北交所"];
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
export type TdxJob = {
  strategy_slot?: string | null;
  admission?: { mode?: string };
  retained_responses?: number;
  snapshot_id?: string;
  classification?: string;
  started_at?: string;
  finished_at?: string;
  request_id: string;
  state: string;
  created_at: string;
  origin?: string;
  provider_requests: number;
  step_count?: number;
  complete_steps?: number;
  command: { action: string; start: string; end: string; markets?: number[] };
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
  available_at: string | null;
  observed_at?: string;
  origin?: "local" | "upstream";
  persisted?: boolean;
  is_complete?: boolean;
  bar_state?: string;
  source?: string;
  source_binding?: string;
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
  queried_at?: string;
  snapshot_id?: string | null;
  pagination_basis?: "rows" | "roster_candidates";
  page_end_offset?: number;
  upstream?: {
    issues: { classification: string }[];
    attempted_operations: number;
  };
  query_coverage?: {
    page_candidates: number;
    missing_count: number;
    temporary_rows: number;
    unfinished_rows: number;
    session: string;
    roster: { state: string };
  };
};
type Coverage = {
  applicable?: boolean;
  offset?: number;
  limit?: number;
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
export const tdxStateLabel: Record<string, string> = {
  QUEUED: "已排队",
  INTERRUPTED: "执行中断，可续跑",
  RUNNING: "执行中",
  COMPLETE: "请求范围已完成",
  PARTIAL: "部分完成",
  FAILED: "失败",
  PENDING: "待执行",
};
export async function tdxCall<T>(
  transport: Transport,
  path: string,
  signal?: AbortSignal,
  command?: unknown,
  apiPrefix = prefix,
): Promise<T> {
  const response = await transport(apiPrefix + path, {
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
export function tdxLatestClosedDay() {
  const wall = toBeijingIso(new Date().toISOString());
  const day = wall.slice(0, 10);
  return wall.slice(11, 16) >= "15:05"
    ? day
    : new Date(Date.parse(day + "T00:00:00Z") - 86400000)
        .toISOString()
        .slice(0, 10);
}

function collectionNotDue(end: string, result: Result) {
  const cutoff = toBeijingIso(result.as_of);
  return (
    result.query_coverage?.session !== "market_closed" &&
    (end > cutoff.slice(0, 10) ||
      (end === cutoff.slice(0, 10) && cutoff.slice(11, 16) < "15:35"))
  );
}

export function TdxDailyQuery({ transport, generation, route }: FamilyProps) {
  const initialAsOf = route.fields.get("as_of") ?? "";
  const [direction, setDirection] = useState("history"),
    [market, setMarket] = useState("1"),
    [instrument, setInstrument] = useState("600519");
  const [start, setStart] = useState("1990-01-01"),
    [end, setEnd] = useState(beijingToday);
  const [localOnly, setLocalOnly] = useState(false);
  const querySnapshot = useRef<string | null>(null);
  const previousPages = useRef(new Map<number, number>());
  const [history, setHistory] = useState(Boolean(initialAsOf)),
    [cutoff, setCutoff] = useState(
      toBeijingIso(initialAsOf).replace(/\+08:00$/, "") || initialAsOf,
    );
  const [offset, setOffset] = useState(0),
    [result, setResult] = useState<Result | null>(null),
    [coverage, setCoverage] = useState<Coverage | null>(null);
  const [exchange, setExchange] = useState<CpiHttpExchange | null>(null),
    [resultTab, setResultTab] = useState("table");
  const [busy, setBusy] = useState(false),
    [coverageBusy, setCoverageBusy] = useState(false),
    [error, setError] = useState(""),
    [coverageError, setCoverageError] = useState(false);
  const active = useRef<AbortController | null>(null),
    coverageActive = useRef<AbortController | null>(null),
    editedEnd = useRef(false);
  const summary = useVisiblePolling(
    (signal) =>
      tdxCall<{ markets: Market[]; as_of?: string }>(
        transport,
        "/summary",
        signal,
      ),
    [transport, generation],
    true,
    () => true,
    false,
  );
  const status = useVisiblePolling(
    (signal) =>
      tdxCall<{ defaults: { end: string }; query_defaults?: { end: string } }>(
        transport,
        "/status",
        signal,
      ),
    [transport, generation],
    true,
    () => true,
    false,
  );
  useEffect(() => {
    const end = status.data?.query_defaults?.end ?? status.data?.defaults.end;
    if (end && !editedEnd.current) setEnd(end);
  }, [status.data]);
  useEffect(
    () => () => {
      active.current?.abort();
      coverageActive.current?.abort();
    },
    [],
  );
  function change(edit: () => void) {
    querySnapshot.current = null;
    previousPages.current.clear();
    active.current?.abort();
    coverageActive.current?.abort();
    active.current = null;
    coverageActive.current = null;
    setBusy(false);
    setCoverageBusy(false);
    setResult(null);
    setCoverage(null);
    setExchange(null);
    setError("");
    setCoverageError(false);
    setOffset(0);
    edit();
  }
  useEffect(() => {
    change(() => {
      setHistory(Boolean(initialAsOf));
      setCutoff(
        toBeijingIso(initialAsOf).replace(/\+08:00$/, "") || initialAsOf,
      );
    });
  }, [initialAsOf]);
  const validDay = (v: string) =>
    /^\d{4}-\d{2}-\d{2}$/.test(v) && !!beijingInputToIso(v + "T00:00:00");
  const asOf = history ? beijingInputToIso(cutoff) : "";
  const valid =
    validDay(end) &&
    (direction === "day" ||
      (validDay(start) &&
        start <= end &&
        /^[0-9]{6}$/.test(instrument) &&
        market !== "all")) &&
    (!history || !!asOf);
  function queryPath(nextOffset = offset, paging = false) {
    const q = new URLSearchParams({
      local_only: String(history || localOnly),
      basis: "native_projection",
      limit: "100",
      offset: String(nextOffset),
    });
    if (direction === "history") {
      q.set("market", market);
      q.set("order", "desc");
      q.set("instrument", instrument);
      q.set("start", start);
      q.set("end", end);
    } else {
      q.set("day", end);
      if (market !== "all") q.set("market", market);
    }
    if (history) q.set("as_of", asOf);
    if ((nextOffset > 0 || paging) && querySnapshot.current)
      q.set("snapshot_id", querySnapshot.current);
    return prefix + "/query?" + q;
  }
  function coveragePath(nextOffset = 0) {
    return (
      "/coverage?" +
      new URLSearchParams({
        day: end,
        offset: String(nextOffset),
        limit: "100",
        ...(history ? { as_of: asOf } : {}),
      })
    );
  }
  async function read(nextOffset = 0, paging = false) {
    if (!valid || active.current) return;
    editedEnd.current = true;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    setResult(null);
    setExchange(null);
    setOffset(nextOffset);
    if (nextOffset === 0 && !paging) {
      querySnapshot.current = null;
      previousPages.current.clear();
      coverageActive.current?.abort();
      coverageActive.current = null;
      setCoverage(null);
      setCoverageBusy(false);
      setCoverageError(false);
    }
    try {
      const rows = await requestCpiJson<Result>(
        transport,
        queryPath(nextOffset, paging),
        { signal: controller.signal },
        60000,
        (x) => {
          if (active.current === controller && !controller.signal.aborted)
            setExchange(x);
        },
      );
      if (
        !Array.isArray(rows.rows) ||
        !Number.isInteger(rows.provider_requests) ||
        rows.provider_requests < 0 ||
        ((history || localOnly) &&
          (rows.provider_requests !== 0 ||
            rows.rows.some((r) => r.origin === "upstream"))) ||
        !Number.isInteger(rows.total) ||
        !beijingInstantKey(rows.as_of) ||
        (history && beijingInstantKey(rows.as_of) !== beijingInstantKey(asOf))
      )
        throw Error("tdx_query_contract");
      if (active.current !== controller || controller.signal.aborted) return;
      querySnapshot.current = rows.snapshot_id ?? null;
      if (rows.next_offset !== null)
        previousPages.current.set(rows.next_offset, nextOffset);
      setResult(rows);
      // Coverage is a separate local read. A slow reconciliation must not
      // hold returned rows or their pager, nor restart when rows are paged.
      if (nextOffset === 0 && !collectionNotDue(end, rows))
        void readCoverage(0);
    } catch {
      if (active.current === controller && !controller.signal.aborted)
        setError("本次查询结果尚未核实，请核实服务或来源状态后重新查询。");
    } finally {
      if (active.current === controller) {
        active.current = null;
        setBusy(false);
      }
    }
  }
  async function readCoverage(nextOffset: number) {
    if (coverageActive.current) return;
    const controller = new AbortController();
    coverageActive.current = controller;
    setCoverageBusy(true);
    setCoverageError(false);
    try {
      const report = await tdxCall<Coverage>(
        transport,
        coveragePath(nextOffset),
        controller.signal,
      );
      if (coverageActive.current === controller && !controller.signal.aborted)
        setCoverage(report);
    } catch {
      if (coverageActive.current === controller && !controller.signal.aborted)
        setCoverageError(true);
    } finally {
      if (coverageActive.current === controller) {
        coverageActive.current = null;
        setCoverageBusy(false);
      }
    }
  }
  return (
    <div className="tdx-daily-workbench">
      <p className="muted">
        本地优先，范围缺口仅临时向已绑定 TDX
        源补查，不入库、不创建采集作业。当天盘中、午休及收盘价交易阶段返回实际观测到的未完成日K，15:30
        后未入库值仍标为临时；无真实日K不会用昨日或零值代替。原生价基（native_projection），价格与金额单位千分之一元，成交量为手，收益口径待核验。
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (e.currentTarget.checkValidity()) void read();
        }}
      >
        <div className="field-grid">
          {direction === "history" ? (
            <DailyDateRange
              start={start}
              end={end}
              onStart={(v) => change(() => setStart(v))}
              onEnd={(v) =>
                change(() => {
                  editedEnd.current = true;
                  setEnd(v);
                })
              }
            />
          ) : (
            <DailyDateRange
              singleDay
              start={end}
              end={end}
              onStart={(v) =>
                change(() => {
                  editedEnd.current = true;
                  setEnd(v);
                })
              }
              onEnd={(v) =>
                change(() => {
                  editedEnd.current = true;
                  setEnd(v);
                })
              }
            />
          )}

          <label>
            查询方向
            <select
              aria-label="查询方向"
              value={direction}
              onChange={(e) =>
                change(() => {
                  setDirection(e.target.value);
                  if (e.target.value === "history" && market === "all")
                    setMarket("1");
                })
              }
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
              onChange={(e) => change(() => setMarket(e.target.value))}
            >
              {direction === "day" && <option value="all">三市全部</option>}
              {exchanges.map((name, i) => (
                <option key={i} value={i}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          {direction === "history" && (
            <label>
              证券代码
              <input
                value={instrument}
                required
                pattern="[0-9]{6}"
                onChange={(e) => change(() => setInstrument(e.target.value))}
              />
            </label>
          )}
        </div>

        <label>
          <input
            type="checkbox"
            checked={history || localOnly}
            disabled={history}
            onChange={(e) => change(() => setLocalOnly(e.target.checked))}
          />
          仅本地已保存数据（不补源）
        </label>
        <HistoryCutoffFields
          enabled={history}
          value={cutoff}
          onEnabled={(v) => change(() => setHistory(v))}
          onChange={(v) => change(() => setCutoff(v))}
        />
        {!valid && (
          <p role="status">
            请填写合法日期、证券范围和历史截止；历史截止使用北京时间并支持微秒。
          </p>
        )}
        <button className="primary" disabled={busy || !valid}>
          查询数据
        </button>
      </form>
      {valid ? (
        <RequestPreview request={{ method: "GET", path: queryPath() }} />
      ) : (
        <p role="status">参数未完成，未生成或执行请求。</p>
      )}
      <LocalQuerySummary>
        <button disabled={summary.busy} onClick={summary.refresh}>
          刷新本地汇总
        </button>
        {summary.busy && !summary.data && (
          <p role="status">正在读取本地市场汇总；查询表单已可操作。</p>
        )}
        {summary.failure && (
          <p role="alert">本地汇总读取失败，持有状态待核实。</p>
        )}
        {summary.data && (
          <>
            {summary.data.as_of && (
              <p className="small">
                本地汇总截止（北京时间）：
                {formatBeijingTime(summary.data.as_of)}
              </p>
            )}
            <WorkflowTable
              singlePane
              label="本地市场范围"
              columns={["交易所", "证券", "日线数", "最早", "最近"]}
              rows={summary.data.markets.map((m) => ({
                id: String(m.market),
                search: exchanges[m.market],
                cells: [
                  exchanges[m.market],
                  m.instruments,
                  m.rows,
                  m.first ?? "未知",
                  m.last ?? "未知",
                ],
              }))}
              onSelect={(value) => change(() => setMarket(value))}
            />
          </>
        )}
      </LocalQuerySummary>
      {busy && <p role="status">正在查询日线…</p>}
      {coverageBusy && (
        <p role="status">正在读取覆盖证据；已有日线可继续查看与翻页。</p>
      )}
      {error && <p role="alert">{error}</p>}
      {(result || exchange) && (
        <QueryResultTabs
          label="TDX 日线"
          value={resultTab}
          onChange={setResultTab}
        />
      )}
      {exchange && (
        <HttpExchangePanel exchange={exchange} showBody={resultTab === "raw"} />
      )}
      <div hidden={resultTab !== "table"}>
        {result && (
          <>
            <p>
              {result.pagination_basis === "roster_candidates"
                ? `共 ${result.total} 个名册/本地候选，本页 ${result.rows.length} 根真实日K`
                : `共 ${result.total} 根`}
              ；来源请求 {result.provider_requests} 次。 本地读取截止{" "}
              {formatBeijingTime(result.as_of)}。
              {result.queried_at &&
                ` 查询完成 ${formatBeijingTime(result.queried_at)}。`}
            </p>
            {result.upstream?.issues.length ? (
              <p role="alert">
                临时补查部分未完成：
                {result.upstream.issues.map((i) => i.classification).join("、")}
                。已读本地值保留；未入库值不计入持有覆盖。
              </p>
            ) : null}
            {result.query_coverage && (
              <p role="status">
                本次查询缺口 {result.query_coverage.missing_count} 项；临时日K{" "}
                {result.query_coverage.temporary_rows} 根，未完成{" "}
                {result.query_coverage.unfinished_rows} 根。 名册状态{" "}
                {result.query_coverage.roster.state}
                ；当前阶段{" "}
                {(
                  {
                    pre_open: "盘前",
                    opening_auction: "开盘集合竞价",
                    morning: "上午交易",
                    lunch_break: "午休（当日日K仍未完成）",
                    afternoon: "下午交易",
                    closing_price_session: "收盘价交易阶段（当日日K仍未完成）",
                    after_close: "盘后",
                    historical: "历史日期",
                    future_day: "未来日期",
                    market_closed: "休市/非交易日",
                  } as Record<string, string>
                )[result.query_coverage.session] ??
                  result.query_coverage.session}
                ；本页结果不证明三市全量覆盖。
              </p>
            )}
            <WorkflowTable
              singlePane
              label="日线结果"
              className="query-result-rows"
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
                "来源 / 状态",
                "来源观测（北京时间）",
                "本地可用时间（北京时间）",
              ]}
              rows={result.rows.map((b) => ({
                id: `${b.market}:${b.instrument}:${b.trading_day}`,
                search: `${b.instrument} ${b.trading_day}`,
                cells: [
                  b.trading_day,
                  exchanges[b.market],
                  b.instrument,
                  (b.open / 1000).toFixed(3),
                  (b.high / 1000).toFixed(3),
                  (b.low / 1000).toFixed(3),
                  (b.close / 1000).toFixed(3),
                  b.volume,
                  (b.amount / 1000).toFixed(3),
                  `${b.origin === "upstream" ? "临时 TDX（未入库）" : "本地已发布"} · ${b.is_complete === false ? "未完成日K" : b.origin === "upstream" ? "已收盘日K" : "已保存日K"}${b.source ? ` · ${b.source}` : ""}`,
                  formatBeijingTime(b.observed_at ?? b.received_at),
                  b.origin === "upstream"
                    ? "未入库，无本地可用时间"
                    : formatBeijingTime(b.available_at),
                ],
              }))}
              onSelect={() => {}}
            />
            <QueryPagination
              label="TDX 日线"
              previousOffset={previousPages.current.get(offset)}
              rangeEnd={
                result.pagination_basis === "roster_candidates"
                  ? result.page_end_offset
                  : undefined
              }
              offset={offset}
              total={result.total}
              pageSize={100}
              nextOffset={result.next_offset}
              pending={busy}
              onPage={(value) => void read(value, true)}
            />
          </>
        )}
        {result && collectionNotDue(end, result) && (
          <p>所选日期尚未到 15:35 盘后采集时间。</p>
        )}
        {coverageError && (
          <p role="alert">覆盖证据读取失败，已有日线保留；未据此判定完整。</p>
        )}
        {coverage?.applicable === false && (
          <p>所选结束日期三市休市，当日无需日线。</p>
        )}
        {coverage && coverage.applicable !== false && (
          <>
            <p>
              以下仅核对本地已入库覆盖（临时查询值不计入）：按结束日期的有效期名册与交易日证据对账，未解决{" "}
              {coverage.missing_total} 项。覆盖结论：
              {coverage.complete ? "完整" : "部分或证据不足"}
              。缺少的历史成员和停牌证据单列未知。
            </p>
            <WorkflowTable
              singlePane
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
                search: exchanges[m.market],
                cells: [
                  exchanges[m.market],
                  m.effective_count ?? m.roster_count ?? "未知",
                  m.valid_bars,
                  m.zero_volume_bars,
                  m.failed,
                  m.unknown,
                  m.pre_listing ?? "未知",
                  m.post_delisting ?? "未知",
                  m.confirmed_suspended ?? "未知",
                  m.suspended_with_bars ?? "未知",
                  m.calendar_state ?? "UNKNOWN",
                ],
              }))}
              onSelect={() => {}}
            />
            {coverage.missing_total > 0 && (
              <WorkflowTable
                singlePane
                label="缺项明细"
                columns={["交易所", "证券", "原因"]}
                rows={coverage.missing.map((m) => ({
                  id: `${m.market}:${m.instrument}`,
                  search: `${m.name} ${m.instrument} ${m.classification}`,
                  cells: [
                    exchanges[m.market],
                    `${m.instrument} ${m.name}`,
                    m.classification,
                  ],
                }))}
                onSelect={(id) => {
                  const [m, code] = id.split(":");
                  change(() => {
                    setMarket(m);
                    setInstrument(code);
                    setDirection("history");
                  });
                }}
                pageSize={20}
              />
            )}
            {(coverage.offset ?? 0) + coverage.missing.length <
              coverage.missing_total && (
              <button
                disabled={coverageBusy}
                onClick={() =>
                  void readCoverage(
                    (coverage.offset ?? 0) + coverage.missing.length,
                  )
                }
              >
                下一页缺项
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

export function TdxApiHelp({ transport }: FamilyProps) {
  return <TdxApiDocumentation transport={transport} />;
}
