import { CPI_PROFILE, type MacroProfile } from "./macroTimeSeriesProfile";
import { formatBeijingTime, beijingInputToIso } from "./beijingTime";
import { DateField } from "./DateField";
import { MultiSelect } from "./MultiSelect";
import { useEffect, useRef, useState } from "react";
import type { Transport } from "./api";
import {
  buildCpiQueryRequest,
  latestMonth,
  queryCpiRange,
  rangeValid,
  shiftMonth,
  type CpiAvailability,
  type CpiPoint,
  type CpiPublicationEvent,
  type CpiResult,
  type CpiScope,
  type CpiHttpExchange,
} from "./cpiTimeSeriesApi";
import {
  CpiQueryApiHelp,
  HttpExchangePanel,
  RequestPreview,
} from "./CpiQueryDetails";
import { Tabs } from "./WorkflowTable";

export function CpiAvailabilityNotice({
  availability,
  profile = CPI_PROFILE,
}: {
  availability: CpiAvailability;
  profile?: MacroProfile;
}) {
  if (availability.failure)
    return (
      <div role="alert">
        <p>
          {availability.data
            ? "服务状态暂未刷新，显示上次成功核实的信息；不代表已保存数据过期。"
            : `${profile.label} 服务状态未知，首次读取失败，当前不能确认查询或采集资格。`}
        </p>
        {availability.refresh && (
          <button type="button" onClick={availability.refresh}>
            重新核实
          </button>
        )}
      </div>
    );
  if (availability.busy && !availability.data)
    return <p role="status">正在核实 {profile.label} 服务资格…</p>;
  if (!availability.data)
    return (
      <p role="status">
        {profile.label} 服务状态待核实，当前不能确认查询或采集资格。
      </p>
    );
  return null;
}
export const missingText = (reason?: string) =>
  ({
    source_unavailable: "未公布 / 来源缺值",
    withdrawn: "来源已撤回",
    availability_evidence_insufficient: "当时可用证据不足",
    not_observed: "本地尚无记录",
  })[reason ?? ""] ?? "本地尚无记录";
const measures = [
  { value: "index_value", label: "指数" },
  { value: "change_1m_ratio", label: "1月环比" },
  { value: "change_3m_ratio", label: "3月变化" },
  { value: "change_6m_ratio", label: "6月变化" },
  { value: "change_12m_ratio", label: "12月同比" },
] as const;
const baskets = [
  { value: "headline", label: "总体" },
  { value: "core", label: "核心" },
];
const adjustments = [
  { value: "sa", label: "季调" },
  { value: "nsa", label: "未季调" },
];
const hasEvidence = (value: unknown): boolean => {
  if (typeof value === "string") return value.trim().length > 0;
  if (Array.isArray(value)) return value.some(hasEvidence);
  if (value && typeof value === "object")
    return Object.values(value).some(hasEvidence);
  return false;
};
export function publicationText(
  point: Pick<CpiPoint, "month" | "publication_event">,
): string | null {
  const event = point.publication_event;
  if (
    !event ||
    event.reference_month !== point?.month ||
    !hasEvidence(event.evidence)
  )
    return null;
  if ((event.status ?? event.state) === "cancelled") return "已取消";
  const time = event.time ?? event.publication_time;
  if (typeof time !== "string") return null;
  const formatted = formatBeijingTime(time, "");
  if (!formatted) return null;
  const state = event.status ?? event.state;
  const label =
    state === "scheduled" || state === "planned"
      ? "计划发布"
      : state === "published" || state === "released"
        ? "已发布"
        : null;
  return label ? `${label}：${formatted}` : null;
}
const valueText = (
  value: string | number | null | undefined,
  ratio: boolean,
  unit: string,
) =>
  value == null
    ? "—"
    : `${ratio ? (Number(value) * 100).toFixed(2) : value}${unit}`;

export function CpiTimeSeries(
  props: Parameters<typeof CpiTimeSeriesContent>[0],
) {
  return (
    <CpiTimeSeriesContent {...props} key={(props.profile ?? CPI_PROFILE).id} />
  );
}
function CpiTimeSeriesContent({
  transport,
  profile = CPI_PROFILE,
  availability,
}: {
  transport: Transport;
  profile?: MacroProfile;
  availability: CpiAvailability;
}) {
  const cpiSeries = profile.series;
  const availableAdjustments = adjustments.filter((item) =>
    profile.series.some((s) => s[3] === item.value),
  );
  const latest = latestMonth(availability.data);
  const [start, setStart] = useState(latest ? shiftMonth(latest, -11) : "");
  const [end, setEnd] = useState(latest);
  const [basket, setBasket] = useState(["headline", "core"]);
  const [adjustment, setAdjustment] = useState(
    availableAdjustments.map((item) => item.value),
  );
  const [measure, setMeasure] = useState<string[]>(
    measures.map((item) => item.value),
  );
  const [history, setHistory] = useState(false);
  const [asOf, setAsOf] = useState("");
  const [result, setResult] = useState<CpiResult | null>(null);
  const [failure, setFailure] = useState(false);
  const [pending, setPending] = useState(false);
  const [page, setPage] = useState(0);
  const [tab, setTab] = useState("query");
  const [resultTab, setResultTab] = useState("table");
  const [exchange, setExchange] = useState<CpiHttpExchange | null>(null);
  const edited = useRef(false);
  const active = useRef<AbortController | null>(null);
  useEffect(() => {
    if (latest && !edited.current) {
      setStart(shiftMonth(latest, -11));
      setEnd(latest);
    }
  }, [latest]);
  useEffect(() => () => active.current?.abort(), []);
  useEffect(() => {
    active.current?.abort();
    active.current = null;
    setResult(null);
    setPending(false);
    setExchange(null);
  }, [transport, availability.data?.web_capabilities.query]);
  function change(edit: () => void) {
    edited.current = true;
    active.current?.abort();
    active.current = null;
    setResult(null);
    setExchange(null);
    setFailure(false);
    setPending(false);
    setPage(0);
    edit();
  }
  const valid =
    rangeValid(start, end) &&
    basket.length > 0 &&
    adjustment.length > 0 &&
    measure.length > 0 &&
    (!history || !!beijingInputToIso(asOf));
  const scope: CpiScope = {
    start,
    end,
    basket: basket.length === baskets.length ? "all" : (basket[0] ?? ""),
    adjustment:
      adjustment.length === adjustments.length ? "all" : (adjustment[0] ?? ""),
    measure: "index",
    query_mode: history ? "history" : "current",
    ...(history
      ? {
          as_of: beijingInputToIso(asOf),
          as_of_basis: "available",
          vintage_mode: "available",
        }
      : {}),
  };
  const request = buildCpiQueryRequest(scope, profile);
  async function submit() {
    if (pending || !valid || availability.data?.web_capabilities.query !== true)
      return;
    const controller = new AbortController();
    active.current = controller;
    setPending(true);
    setFailure(false);
    setResult(null);
    setExchange(null);
    setPage(0);
    try {
      const response = await queryCpiRange(
        transport,
        scope,
        controller.signal,
        (value) => {
          if (active.current === controller && !controller.signal.aborted)
            setExchange(value);
        },
        profile,
      );
      if (!controller.signal.aborted) setResult(response);
    } catch {
      if (!controller.signal.aborted) setFailure(true);
    } finally {
      if (active.current === controller) {
        setPending(false);
        active.current = null;
      }
    }
  }
  const rows = result
    ? [
        ...result.points.map((point) => ({
          ...point,
          point,
          reason: undefined as string | undefined,
        })),
        ...result.missing.map((gap) => ({ ...gap, point: null })),
      ].sort(
        (a, b) =>
          a.month.localeCompare(b.month) ||
          cpiSeries.findIndex((s) => s[0] === a.series_id) -
            cpiSeries.findIndex((s) => s[0] === b.series_id),
      )
    : [];
  const columns = measures.filter((item) => measure.includes(item.value));
  const selectedCalculationMissing =
    result?.calculation_missing.filter((gap) =>
      columns.some((column) => column.value === gap.metric),
    ) ?? [];
  const events: CpiPublicationEvent[] = Array.isArray(
    result?.publication_events,
  )
    ? result.publication_events.filter(
        (event) =>
          !!event && typeof event === "object" && !Array.isArray(event),
      )
    : [];
  const pageSize = 40;
  return (
    <section
      aria-label={`${profile.label} 范围查询`}
      className="ordinary-query cpi-timeseries macro-range-query"
    >
      <h3>美国 {profile.label} · 范围查询</h3>
      <Tabs
        label={`${profile.label} 消费者页签`}
        values={[
          ["query", "查询数据"],
          ["api", "API说明"],
        ]}
        value={tab}
        onChange={setTab}
      />
      <div hidden={tab !== "api"}>
        <CpiQueryApiHelp
          profile={profile}
          availability={availability}
          request={request}
        />
      </div>
      <div hidden={tab !== "query"}>
        <p className="muted">
          当前查询优先读取本地，缺口可向 {profile.provider}
          查询，不保存。历史截止查询只读已入库记录。
        </p>
        <CpiAvailabilityNotice profile={profile} availability={availability} />
        {profile.historyNotice && (
          <p className="small">{profile.historyNotice}</p>
        )}
        {availability.data && (
          <>
            <p>
              已保存观测 {availability.data.observation_count.toLocaleString()}{" "}
              条；各口径最新月份：
            </p>
            <ul className="cpi-coverage">
              {cpiSeries.map(([id, title]) => (
                <li key={id}>
                  {title}：
                  {availability.data!.latest_months[id] ?? "尚无本地记录"}
                </li>
              ))}
            </ul>
            {!availability.data.web_capabilities.query && (
              <p role="alert">
                当前服务未开放 {profile.label} 查询。请核实服务资格。
              </p>
            )}
          </>
        )}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!e.currentTarget.checkValidity()) return;
            void submit();
          }}
        >
          <div className="field-grid">
            <DateField
              label="开始月份"
              type="month"
              value={start}
              onChange={(value) => change(() => setStart(value))}
            />
            <DateField
              label="结束月份"
              type="month"
              value={end}
              onChange={(value) => change(() => setEnd(value))}
            />
            <MultiSelect
              label="统计篮子"
              options={baskets}
              value={basket}
              onChange={(value) => change(() => setBasket(value))}
            />
            <MultiSelect
              label="季节调整"
              options={availableAdjustments}
              value={adjustment}
              onChange={(value) => change(() => setAdjustment(value))}
            />
            <MultiSelect
              label="显示指标"
              options={measures}
              value={measure}
              onChange={setMeasure}
            />
          </div>
          <label className="cpi-history-toggle">
            <input
              type="checkbox"
              checked={history}
              onChange={(event) =>
                change(() => setHistory(event.target.checked))
              }
            />
            <span>历史截止查询</span>
          </label>
          {history && (
            <div className="cpi-history-fields">
              <p className="small">按入库时间查看截止时已保存的数据。</p>
              <DateField
                label="截止时间（北京时间）"
                type="datetime-local"
                value={asOf}
                required
                onChange={(value) => change(() => setAsOf(value))}
              />
            </div>
          )}
          {(!basket.length || !adjustment.length || !measure.length) && (
            <p role="alert">统计篮子、季节调整和显示指标均须至少选择一项。</p>
          )}
          {start && end && !rangeValid(start, end) && (
            <p role="alert">
              请选择有效的起止月份，开始不得晚于结束，范围须少于 200 年。
            </p>
          )}
          <button
            className="primary"
            disabled={
              pending ||
              !valid ||
              availability.data?.web_capabilities.query !== true
            }
          >
            查询数据
          </button>
        </form>
        <p className="small">
          显示指标只控制表格列；实际请求固定 measure=index，不发送列选择参数。
        </p>
        <RequestPreview request={request} />
        {pending && <p role="status">正在查询 {profile.label}…</p>}
        {failure && (
          <p role="alert">
            本次查询结果未知。服务不可用或返回范围无法核实，请稍后重新查询。
          </p>
        )}
        {(result || exchange) && (
          <Tabs
            label={`${profile.label} 结果视图`}
            values={[
              ["table", "表格"],
              ["raw", "原始响应"],
            ]}
            value={resultTab}
            onChange={setResultTab}
          />
        )}
        {exchange && (
          <HttpExchangePanel
            exchange={exchange}
            showBody={resultTab === "raw"}
          />
        )}
        {failure && !exchange && (
          <p>未收到可读取的 HTTP 响应，无法提供 HTTP 状态或响应正文。</p>
        )}
        <div hidden={resultTab !== "table"}>
          {result && columns.length > 0 && (
            <section aria-label={`${profile.label} 范围结果`}>
              <h4>
                {result.start_month} 至 {result.end_month}
              </h4>
              <p role="status">
                共 {rows.length} 条（月 × 口径），有效观测{" "}
                {result.points.length} 条，缺值 {result.missing.length} 条。
                {!result.missing.length && !selectedCalculationMissing.length
                  ? "所选指标数据完整。"
                  : "所选指标存在缺值。"}
              </p>
              <p className="small">
                变化率以百分比显示；3月、6月为相隔对应月数的累计变化，未经年化。
              </p>
              <div className="table-wrap">
                <table aria-label={`${profile.label} 范围数据`}>
                  <thead>
                    <tr>
                      <th>月份</th>
                      <th>口径</th>
                      {columns.map((column) => (
                        <th key={column.value}>{column.label}</th>
                      ))}
                      <th>官方日历时间</th>
                      <th>缺值说明</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows
                      .slice(page * pageSize, (page + 1) * pageSize)
                      .map((row) => {
                        const gaps = result.calculation_missing.filter(
                          (g) =>
                            g.series_id === row.series_id &&
                            g.month === row.month &&
                            columns.some((column) => column.value === g.metric),
                        );
                        return (
                          <tr key={`${row.month}:${row.series_id}`}>
                            <td>{row.month}</td>
                            <td>
                              {
                                cpiSeries.find(
                                  (s) => s[0] === row.series_id,
                                )?.[1]
                              }
                              {!history &&
                                row.point?.availability === "unpersisted" && (
                                  <span className="cpi-temporary-value">
                                    临时来源值
                                  </span>
                                )}
                            </td>
                            {columns.map(({ value: field }) => (
                              <td key={field}>
                                {valueText(
                                  row.point?.[field],
                                  field.endsWith("ratio"),
                                  field === "index_value" ? "" : "%",
                                )}
                              </td>
                            ))}
                            <td>
                              {publicationText({
                                month: row.month,
                                publication_event:
                                  row.point?.publication_event ??
                                  events.find(
                                    (event) =>
                                      event.reference_month === row.month,
                                  ),
                              }) ?? "未获取"}
                            </td>
                            <td>
                              {!row.point
                                ? missingText(row.reason)
                                : gaps.length
                                  ? `计算基期缺值：${[...new Set(gaps.map((g) => `${g.required_month}（${missingText(g.reason)}）`))].join("、")}`
                                  : "—"}
                            </td>
                          </tr>
                        );
                      })}
                  </tbody>
                </table>
              </div>
              {!rows.length && <p>当前范围没有可显示记录。</p>}
              <div className="actions" aria-label={`${profile.label} 结果分页`}>
                <button disabled={page === 0} onClick={() => setPage(page - 1)}>
                  上一页
                </button>
                <span>
                  第 {page + 1} /{" "}
                  {Math.max(1, Math.ceil(rows.length / pageSize))} 页，每页最多{" "}
                  {pageSize} 条
                </span>
                <button
                  disabled={(page + 1) * pageSize >= rows.length}
                  onClick={() => setPage(page + 1)}
                >
                  下一页
                </button>
              </div>
              <details>
                <summary>来源与时间详情</summary>
                <p>
                  来源：{profile.provider}；本次未保存。来源请求{" "}
                  {result.provider_requests} 次。
                  {history
                    ? `入库截止：${formatBeijingTime(result.as_of)}。`
                    : "展示当前查询值。"}
                  时间来自官方日历，不表示当前修订值在该时已知；历史截止仍按入库时间。
                  来源观察时间仅记录收到响应的时点，不作为历史截止依据。
                </p>
              </details>
            </section>
          )}
        </div>
      </div>
    </section>
  );
}
