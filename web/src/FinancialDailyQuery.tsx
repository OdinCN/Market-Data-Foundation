import { useEffect, useRef, useState } from "react";
import type { Transport } from "./api";
import type { CpiHttpExchange } from "./cpiTimeSeriesApi";
import { DateField } from "./DateField";
import { MultiSelect } from "./MultiSelect";
import {
  CodeBlock,
  HttpExchangePanel,
  RequestPreview,
} from "./CpiQueryDetails";
import { FinancialApiHelp, type FinancialHelp } from "./FinancialApiHelp";
import { Tabs } from "./WorkflowTable";
import {
  beijingInputToIso,
  toBeijingInput,
  formatBeijingTime,
} from "./beijingTime";
import { financialStateText } from "./financialJobPresentation";
import {
  FINANCIAL_PREFIX,
  FINANCIAL_NAMES,
  FINANCIAL_FIELDS,
  FINANCIAL_PAGE_SIZE,
  financialQuerySuffix,
  readFinancialQuery,
  type FinancialQueryResult,
  type FinancialScope,
} from "./financialQuery";
import type { FinancialSummary } from "./FinancialDailyWorkbench";

function queryReason(code: string) {
  if (code.includes("derived_inputs"))
    return "同日财政部名义/实际两腿不齐；缺腿公布或恢复后重新查询，不推算缺值。";
  if (code.includes("credential") || code.includes("permission"))
    return "此具名来源准入或凭据不可用；核实对应来源配置后重新查询，不换源。";
  if (code.includes("budget"))
    return "本页补源达到请求限额；缩小区间后重新查询，或使用采集数据保存大范围。";
  if (
    code.includes("admission") ||
    code.includes("cool") ||
    code.includes("rate_limit")
  )
    return "来源限流或冷却；结束冷却后重新查询，不自动重复请求。";
  if (code.includes("http"))
    return "具名来源HTTP请求暂未成功；待来源恢复并结束冷却后重新查询。";
  return `本次补源未完成（${code}）；核实具名来源后重新查询。`;
}
export function FinancialDailyQuery({
  transport,
  summary,
  help,
  activeTab,
  initialAsOf = "",
}: {
  transport: Transport;
  summary: FinancialSummary | null;
  help: FinancialHelp | null;
  activeTab: string;
  initialAsOf?: string;
}) {
  const [start, setStart] = useState(
      new Date(Date.now() - 30 * 86400000).toISOString().slice(0, 10),
    ),
    [end, setEnd] = useState(new Date().toISOString().slice(0, 10));
  const [series, setSeries] = useState(Object.keys(FINANCIAL_NAMES)),
    [fields, setFields] = useState<string[]>(
      FINANCIAL_FIELDS.map((x) => x.value),
    );
  const [history, setHistory] = useState(Boolean(initialAsOf)),
    [cutoff, setCutoff] = useState(
      initialAsOf ? toBeijingInput(initialAsOf) : "",
    );
  const [offset, setOffset] = useState(0),
    [result, setResult] = useState<FinancialQueryResult | null>(null),
    [exchange, setExchange] = useState<CpiHttpExchange | null>(null);
  const [pending, setPending] = useState(false),
    [failure, setFailure] = useState(false),
    [resultTab, setResultTab] = useState("table"),
    [sourcesOpen, setSourcesOpen] = useState(false);
  const active = useRef<AbortController | null>(null);
  function cancel() {
    active.current?.abort();
    active.current = null;
    setPending(false);
    setResult(null);
    setExchange(null);
    setFailure(false);
    setSourcesOpen(false);
  }
  function change(edit: () => void) {
    cancel();
    setOffset(0);
    edit();
  }
  useEffect(() => {
    cancel();
    return () => active.current?.abort();
  }, [transport]);
  useEffect(() => {
    cancel();
    setOffset(0);
    setHistory(Boolean(initialAsOf));
    setCutoff(initialAsOf ? toBeijingInput(initialAsOf) : "");
  }, [initialAsOf]);
  const validDates =
    /^\d{4}-\d{2}-\d{2}$/.test(start) &&
    /^\d{4}-\d{2}-\d{2}$/.test(end) &&
    Number.isFinite(Date.parse(start)) &&
    Number.isFinite(Date.parse(end)) &&
    start <= end &&
    (Date.parse(end) - Date.parse(start)) / 86400000 <= 366 * 30;
  const valid =
    validDates &&
    series.length > 0 &&
    fields.length > 0 &&
    (!history || !!beijingInputToIso(cutoff));
  const scope: FinancialScope = {
    start,
    end,
    series: series.join(","),
    cutoff: history ? beijingInputToIso(cutoff) : "",
    history,
    offset,
  };
  let request: { method: "GET"; path: string } | null = null;
  try {
    if (validDates && series.length && (!history || beijingInputToIso(cutoff)))
      request = {
        method: "GET",
        path: FINANCIAL_PREFIX + financialQuerySuffix(scope),
      };
  } catch {
    /* Invalid draft has no fabricated preview. */
  }
  async function read(nextOffset = 0) {
    if (!valid) return;
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setPending(true);
    setSourcesOpen(false);
    setFailure(false);
    setResult(null);
    setExchange(null);
    setOffset(nextOffset);
    try {
      const value = await readFinancialQuery(
        transport,
        { ...scope, offset: nextOffset },
        controller.signal,
        (x) => {
          if (active.current === controller && !controller.signal.aborted)
            setExchange(x);
        },
      );
      if (active.current === controller && !controller.signal.aborted)
        setResult(value);
    } catch {
      if (active.current === controller && !controller.signal.aborted)
        setFailure(true);
    } finally {
      if (active.current === controller) {
        setPending(false);
        active.current = null;
      }
    }
  }
  const columns = FINANCIAL_FIELDS.filter((x) => fields.includes(x.value));
  return (
    <>
      <div hidden={activeTab !== "api"}>
        <FinancialApiHelp
          help={help}
          start={start}
          end={end}
          series={series.join(",")}
          cutoff={scope.cutoff}
          history={history}
          offset={offset}
        />
      </div>
      <div hidden={activeTab !== "query"}>
        <p className="muted">
          当前查询优先读取本地，缺口自动向已绑定的具名来源临时补足，不保存；历史截止查询只读已入库记录。页面加载与摘要刷新不采集。
        </p>
        {summary && (
          <>
            <p>
              已保存观测 {summary.observation_count?.toLocaleString() ?? "未知"}{" "}
              条；各指标最新有值观测日：
            </p>
            <ul className="cpi-coverage">
              {(summary.series ?? []).map((s) => (
                <li key={s.series_id}>
                  <span>{FINANCIAL_NAMES[s.series_id]}：</span>
                  <span className="cpi-coverage-period">
                    {s.last ?? "尚无本地记录"}
                  </span>
                </li>
              ))}
            </ul>
          </>
        )}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (e.currentTarget.checkValidity()) void read();
          }}
        >
          <div className="field-grid">
            <DateField
              label="开始日期"
              type="date"
              value={start}
              required
              onChange={(v) => change(() => setStart(v))}
            />
            <DateField
              label="结束日期"
              type="date"
              value={end}
              required
              onChange={(v) => change(() => setEnd(v))}
            />
            <MultiSelect
              label="金融指标"
              options={Object.entries(FINANCIAL_NAMES).map(([value, name]) => ({
                value,
                label: `${name} · ${value}`,
              }))}
              value={series}
              onChange={(v) => change(() => setSeries(v))}
            />
            <MultiSelect
              label="显示指标"
              options={FINANCIAL_FIELDS}
              value={fields}
              onChange={setFields}
            />
          </div>
          <label className="cpi-history-toggle">
            <input
              type="checkbox"
              checked={history}
              onChange={(e) => change(() => setHistory(e.target.checked))}
            />
            <span>历史截止查询</span>
          </label>
          {history && (
            <div className="cpi-history-fields">
              <p className="small">
                按 MDF 确认可用时间查看截止时已保存的数据。
              </p>
              <DateField
                label="截止时间（北京时间）"
                type="datetime-local"
                value={cutoff}
                required
                onChange={(v) => change(() => setCutoff(v))}
              />
            </div>
          )}
          {!series.length && <p role="alert">请至少选择一项金融指标。</p>}
          {!fields.length && <p role="alert">请至少选择一项显示指标。</p>}
          {!validDates && (
            <p role="alert">
              请选择有效的起止日期，开始不得晚于结束，日期差最多366×30日。
            </p>
          )}
          <button className="primary" disabled={pending || !valid}>
            查询数据
          </button>
        </form>
        <p className="small">
          按所选原生系列查询；显示指标只控制表格列，不发不支持的columns或月频变化参数。原值为percent，信用利差另有bp；缺值不补零。
        </p>
        {request ? (
          <RequestPreview request={request} />
        ) : (
          <p role="status">
            请先完成日期、指标及历史截止信息；未生成或执行请求。
          </p>
        )}
        {pending && <p role="status">正在查询金融条件日频…</p>}
        {failure && (
          <p role="alert">
            本次查询结果未知。服务不可用或返回范围无法核实，请稍后重新查询。
          </p>
        )}
        {(result || exchange) && (
          <Tabs
            label="金融条件日频 结果视图"
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
        {!!result?.upstream?.issues.length && (
          <ul aria-label="查询补源缺口">
            {result.upstream.issues.map((x, i) => (
              <li key={i}>
                {x.series_id} · {x.start}..{x.end}：
                {queryReason(x.classification)} 本地结果保留，本次未保存。
              </li>
            ))}
          </ul>
        )}
        <div hidden={resultTab !== "table"}>
          {result && columns.length > 0 && (
            <section aria-label="金融条件日频 范围结果">
              <h4>
                {start} 至 {end}
              </h4>
              <p role="status">
                共 {result.total} 条（日 × 口径）；本页有效观测{" "}
                {result.rows.filter((x) => x.value !== null).length} 条，缺值{" "}
                {result.rows.filter((x) => x.value === null).length}{" "}
                条。上游请求 {result.provider_requests} 次。
              </p>
              <p className="small">
                按观测日倒序，同日按所选指标顺序排列；信用bp=百分比原值×100，其他系列bp不适用。临时来源值尚未保存，无本地版本或确认可用时间。
              </p>
              <div className="table-wrap">
                <table
                  className="macro-result-table"
                  aria-label="金融条件日频 范围数据"
                >
                  <thead>
                    <tr>
                      <th>观测日</th>
                      <th>口径</th>
                      {columns.map((x) => (
                        <th key={x.value}>{x.label}</th>
                      ))}
                      <th>官方公布时间</th>
                      <th>缺值说明</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.rows.map((r) => (
                      <tr
                        key={`${r.observation_date}:${r.series_id}`}
                        data-date={r.observation_date}
                        data-series={r.series_id}
                      >
                        <td>{r.observation_date}</td>
                        <td>
                          {FINANCIAL_NAMES[r.series_id]} · {r.series_id}
                          {r.origin === "upstream" && (
                            <span className="cpi-temporary-value">
                              临时来源值 · 未保存
                            </span>
                          )}
                        </td>
                        {columns.map((x) => (
                          <td key={x.value}>
                            {x.value === "basis_points" &&
                            !r.series_id.startsWith("BAML")
                              ? "不适用"
                              : (r[x.value] ?? "—")}
                          </td>
                        ))}
                        <td>
                          {formatBeijingTime(r.source_publication_at, "未取得")}
                        </td>
                        <td>
                          {r.value === null ? financialStateText(r.state) : "—"}
                          {r.query_classification &&
                            ` · ${r.query_classification}`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {!result.rows.length && <p>当前范围没有可显示记录。</p>}
              <div className="actions" aria-label="金融条件日频 结果分页">
                <button
                  disabled={pending || offset === 0}
                  onClick={() =>
                    void read(Math.max(0, offset - FINANCIAL_PAGE_SIZE))
                  }
                >
                  上一页
                </button>
                <span>
                  第 {Math.floor(offset / FINANCIAL_PAGE_SIZE) + 1} /{" "}
                  {Math.max(1, Math.ceil(result.total / FINANCIAL_PAGE_SIZE))}{" "}
                  页，每页最多 {FINANCIAL_PAGE_SIZE} 条
                </span>
                <button
                  disabled={pending || result.next_offset === null}
                  onClick={() => void read(result.next_offset!)}
                >
                  下一页
                </button>
              </div>
              <details onToggle={(e) => setSourcesOpen(e.currentTarget.open)}>
                <summary>来源与时间详情</summary>
                <p>
                  {result.query_mode === "history"
                    ? `入库截止：${formatBeijingTime(result.as_of)}。`
                    : "展示当前查询值。"}{" "}
                  来源观察、MDF确认可用、官方公布和政策生效各自独立；临时补源未写PG/Garage。覆盖统计只代表本地持有，不含临时值。
                </p>
                {sourcesOpen &&
                  result.rows.map((r) => (
                    <details key={`${r.observation_date}:${r.series_id}`}>
                      <summary>
                        {r.observation_date} · {r.series_id} 来源证据
                      </summary>
                      <dl>
                        <dt>来源与身份</dt>
                        <dd>
                          {r.original_agency} / {r.distributor} ·{" "}
                          {r.identity ?? "未取得"}
                        </dd>
                        <dt>出处</dt>
                        <dd>{r.attribution}</dd>
                        <dt>保存状态 / 本地版本</dt>
                        <dd>
                          {r.origin === "upstream"
                            ? "上游临时值，未保存"
                            : r.revision != null
                              ? `本地持有 · ${r.revision}`
                              : "无本地已确认版本"}
                        </dd>
                        <dt>MDF首次观察</dt>
                        <dd>{formatBeijingTime(r.observed_at, "未取得")}</dd>
                        <dt>MDF确认可用</dt>
                        <dd>{formatBeijingTime(r.available_at, "未取得")}</dd>
                        <dt>官方公布 / 来源可得</dt>
                        <dd>
                          {formatBeijingTime(r.source_publication_at, "未取得")}{" "}
                          / {formatBeijingTime(r.source_available_at, "未取得")}
                        </dd>
                        <dt>政策生效日</dt>
                        <dd>{r.policy_effective_date ?? "不适用或未取得"}</dd>
                        <dt>原件定位</dt>
                        <dd>{r.raw_locator ?? "未保存或无已确认原件"}</dd>
                      </dl>
                      <CodeBlock
                        text={JSON.stringify(r, null, 2)}
                        copyLabel={`复制 ${r.observation_date} ${r.series_id} 观测字段`}
                      />
                    </details>
                  ))}
              </details>
            </section>
          )}
        </div>
      </div>
    </>
  );
}
