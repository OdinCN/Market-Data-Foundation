import {
  DailyDateRange,
  HistoryCutoffFields,
  QueryResultTabs,
} from "./DailyFormControls";
import { RequestPreview, HttpExchangePanel } from "./CpiQueryDetails";
import { requestCpiJson, type CpiHttpExchange } from "./cpiTimeSeriesApi";
import { useEffect, useRef, useState } from "react";
import type { Transport } from "./api";
import type { FamilyProps } from "./RuntimeFamily";
import {
  formatBeijingTime,
  toBeijingIso,
  beijingInstantKey,
  beijingToday,
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
  request_id: string;
  state: string;
  created_at: string;
  origin?: string;
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
type Help = {
  examples: { history: string; cross_section: string; coverage: string };
  notes: string[];
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
export function TdxDailyQuery({ transport, generation, route }: FamilyProps) {
  const initialAsOf = route.fields.get("as_of") ?? "";
  const [direction, setDirection] = useState("history"),
    [market, setMarket] = useState("1"),
    [instrument, setInstrument] = useState("600519");
  const [start, setStart] = useState("1990-01-01"),
    [end, setEnd] = useState(beijingToday);
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
    [error, setError] = useState(""),
    [coverageError, setCoverageError] = useState(false);
  const active = useRef<AbortController | null>(null),
    editedEnd = useRef(false);
  const summary = useVisiblePolling(
    (signal) => tdxCall<{ markets: Market[] }>(transport, "/summary", signal),
    [transport, generation],
    true,
    () => true,
    false,
  );
  const status = useVisiblePolling(
    (signal) =>
      tdxCall<{ defaults: { end: string } }>(transport, "/status", signal),
    [transport, generation],
    true,
    () => true,
    false,
  );
  useEffect(() => {
    if (status.data?.defaults.end && !editedEnd.current)
      setEnd(status.data.defaults.end);
  }, [status.data]);
  useEffect(
    () => () => {
      active.current?.abort();
    },
    [],
  );
  function change(edit: () => void) {
    active.current?.abort();
    active.current = null;
    setBusy(false);
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
  function queryPath(nextOffset = offset) {
    const q = new URLSearchParams({
      local_only: "true",
      basis: "native_projection",
      limit: "100",
      offset: String(nextOffset),
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
    if (history) q.set("as_of", asOf);
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
  async function read(nextOffset = 0) {
    if (!valid || active.current) return;
    editedEnd.current = true;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    setCoverageError(false);
    setResult(null);
    setExchange(null);
    setOffset(nextOffset);
    if (nextOffset === 0) setCoverage(null);
    try {
      const rows = await requestCpiJson<Result>(
        transport,
        queryPath(nextOffset),
        { signal: controller.signal },
        60000,
        (x) => {
          if (active.current === controller && !controller.signal.aborted)
            setExchange(x);
        },
      );
      if (
        !Array.isArray(rows.rows) ||
        rows.provider_requests !== 0 ||
        !Number.isInteger(rows.total) ||
        !beijingInstantKey(rows.as_of) ||
        (history && beijingInstantKey(rows.as_of) !== beijingInstantKey(asOf))
      )
        throw Error("tdx_local_query_contract");
      if (active.current !== controller || controller.signal.aborted) return;
      setResult(rows);
      if (nextOffset === 0 || !coverage) {
        try {
          const report = await tdxCall<Coverage>(
            transport,
            coveragePath(),
            controller.signal,
          );
          if (active.current === controller && !controller.signal.aborted)
            setCoverage(report);
        } catch {
          if (active.current === controller && !controller.signal.aborted)
            setCoverageError(true);
        }
      }
    } catch {
      if (active.current === controller && !controller.signal.aborted)
        setError("本次查询结果尚未核实，请核实本地服务后重新查询。");
    } finally {
      if (active.current === controller) {
        active.current = null;
        setBusy(false);
      }
    }
  }
  async function pageCoverage() {
    if (!coverage || active.current) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setCoverageError(false);
    try {
      const report = await tdxCall<Coverage>(
        transport,
        coveragePath((coverage.offset ?? 0) + coverage.missing.length),
        controller.signal,
      );
      if (active.current === controller && !controller.signal.aborted)
        setCoverage(report);
    } catch {
      if (active.current === controller && !controller.signal.aborted)
        setCoverageError(true);
    } finally {
      if (active.current === controller) {
        active.current = null;
        setBusy(false);
      }
    }
  }
  return (
    <div className="tdx-daily-workbench">
      <p className="muted">
        仅查询本地已保存日线，不访问上游、不保存。原生价基（native_projection）；价格与金额按千分之一元保存，成交量为手，收益口径待核验。
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
      <details>
        <summary>本地市场汇总</summary>
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
        )}
      </details>
      {busy && <p role="status">正在读取本地日线或覆盖证据…</p>}
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
              共 {result.total} 根；来源请求 {result.provider_requests} 次。截止{" "}
              {formatBeijingTime(result.as_of)}。
            </p>
            <WorkflowTable
              singlePane
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
                "可用时间（北京时间）",
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
                  formatBeijingTime(b.available_at),
                ],
              }))}
              onSelect={() => {}}
              pageSize={25}
            />
            {result.next_offset !== null && (
              <button
                disabled={busy}
                onClick={() => void read(result.next_offset!)}
              >
                下一页
              </button>
            )}
          </>
        )}
        {coverageError && (
          <p role="alert">覆盖证据读取失败，已有日线保留；未据此判定完整。</p>
        )}
        {coverage && (
          <>
            <p>
              按结束日期的有效期名册与交易日证据对账，未解决{" "}
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
            {(coverage.offset ?? 0) + coverage.missing.length <
              coverage.missing_total && (
              <button disabled={busy} onClick={() => void pageCoverage()}>
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
  const help = useVisiblePolling(
    (signal) => tdxCall<Help>(transport, "/help", signal),
    [transport],
    true,
    () => true,
    false,
  );
  return (
    <section aria-label="TDX API说明">
      <h3>GET /api/r5/tdx/daily/query</h3>
      <p>
        证券历史和单日横截面共用查询入口，仅本地读取。价基为
        native_projection（原生价基）；收益口径待核验。as_of 必须包含时区，表示
        MDF 实际保存版本的可用截止。limit / offset 为结果分页；coverage
        使用独立缺项分页。
      </p>
      {help.failure && <p role="alert">API说明读取失败，请刷新核实。</p>}
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
    </section>
  );
}
