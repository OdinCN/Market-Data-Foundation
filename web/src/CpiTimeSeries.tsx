import {
  buildClaimsQueryRequest,
  queryClaimsRange,
  claimsLatest,
  shiftDay,
  dayRangeValid,
} from "./claimsTimeSeriesAdapter";
import { gdpEstimateText } from "./macroTimeSeriesProfile";
import {
  QuarterField,
  previousCompleteQuarter,
  quarterRangeValid,
  shiftQuarter,
} from "./QuarterField";
import { buildGdpQueryRequest, queryGdpRange } from "./gdpTimeSeriesAdapter";
import {
  monthlyRangeResult,
  type RangePoint,
  type RangeResult,
} from "./rangeQueryModel";
import { decimalDisplayText, retailDecimalText } from "./decimalValue";
import { CPI_PROFILE, type MacroProfile } from "./macroTimeSeriesProfile";
import { formatBeijingTime, beijingInputToIso } from "./beijingTime";
import { DateField } from "./DateField";
import { MultiSelect } from "./MultiSelect";
import { useEffect, useRef, useState } from "react";
import type { Transport } from "./api";
import { releaseReasonText } from "./macroReleaseLinks";
import { RelatedReleaseLinks } from "./RelatedReleaseLinks";
import { MacroReleaseRecords } from "./MacroReleaseRecords";
import {
  buildCpiQueryRequest,
  latestMonth,
  queryCpiRange,
  rangeValid,
  shiftMonth,
  type CpiAvailability,
  type CpiPoint,
  type CpiPublicationEvent,
  type CpiScope,
  type CpiHttpExchange,
} from "./cpiTimeSeriesApi";
import {
  CpiQueryApiHelp,
  HttpExchangePanel,
  RequestPreview,
} from "./CpiQueryDetails";
import { Tabs } from "./WorkflowTable";
import { newestMonthFirst } from "./queryTableOrder";

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
    provider_missing: "FRED提供方返回缺值",
    local_not_provided: "本地未采集 / 来源未返回（合同未区分，见来源状态）",
    zero_denominator: "比较基期为零，无法计算比率",
    official_null: "官方空值",
    local_record_missing: "本地尚无记录",
    local_availability_unconfirmed: "本地可用确认不足",
    same_cutoff_endpoint_unavailable: "同一截止下比较端点缺值",
    source_unavailable: "未公布 / 来源缺值",
    withdrawn: "来源已撤回",
    availability_evidence_insufficient: "当时可用证据不足",
    not_observed: "本地尚无记录",
    source_operand_unavailable: "金额输入缺值（见原始响应状态）",
    comparison_level_unavailable: "比较基期金额缺值",
    current_level_unavailable: "当前金额缺值",
    comparison_outside_input: "比较基期不在输入范围内",
    comparison_level_zero: "比较基期金额为0，无法计算变化率",
  })[reason ?? ""] ?? "本地尚无记录";
const globalMeasures = [
  { value: "index_value", label: "指数" },
  { value: "change_1m_ratio", label: "1月环比" },
  { value: "change_3m_ratio", label: "3月变化" },
  { value: "change_6m_ratio", label: "6月变化" },
  { value: "change_12m_ratio", label: "12月同比" },
] as const;
const employmentMeasures = [
  { value: "value", label: "数值（单位见指标）" },
  { value: "net_change_1m_thousand_jobs", label: "岗位月净增（千岗位）" },
  { value: "change_1m_percentage_points", label: "比率月变化（百分点）" },
  { value: "change_1m_hours", label: "周工时月变化（小时）" },
  { value: "mom_pct", label: "时薪环比（%）" },
  { value: "yoy_pct", label: "时薪同比（%）" },
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
  if ((event.status ?? event.state) === "unknown")
    return "计划时间待定（TBA / 未知）";
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
  value: unknown,
  ratio: boolean,
  unit: string,
  retail = false,
) =>
  typeof value !== "number" && typeof value !== "string"
    ? "—"
    : `${ratio ? (Number(value) * 100).toFixed(2) : retail ? retailDecimalText(value) : decimalDisplayText(value)}${unit}`;

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
  initialReleaseEvent,
  initialReleaseEvidence,
}: {
  transport: Transport;
  profile?: MacroProfile;
  availability: CpiAvailability;
  initialReleaseEvent?: string | null;
  initialReleaseEvidence?: string | null;
}) {
  const claims = profile.id === "claims";
  const gdp = profile.id === "gdp";
  const consumption = profile.id === "consumption";
  const employment =
    profile.id === "employment" || consumption || gdp || claims;
  const retail = profile.id === "retail";
  const measures = claims
    ? [
        { value: "value", label: "原生数值（Number）" },
        { value: "weekly_change", label: "周数值变化（Number）" },
        { value: "weekly_change_ratio", label: "周变化比率（%）" },
        { value: "local_four_week_average", label: "本地四周均值（Number）" },
      ]
    : gdp
      ? [
          { value: "value", label: "原生值（金额 / % / 百分点）" },
          { value: "change_1q_ratio", label: "水平季变（非年化）" },
          { value: "change_4q_ratio", label: "水平年变" },
        ]
      : consumption
        ? [
            { value: "value", label: "数值（单位见指标）" },
            ...globalMeasures.filter((m) => m.value !== "index_value"),
            {
              value: "change_1m_percentage_points",
              label: "储蓄率月变化（百分点）",
            },
            {
              value: "change_12m_percentage_points",
              label: "储蓄率年变化（百分点）",
            },
          ]
        : employment
          ? employmentMeasures
          : retail
            ? [
                ...globalMeasures.map((item) =>
                  item.value === "index_value"
                    ? { ...item, label: "金额（百万当期美元）" }
                    : item,
                ),
                {
                  value: "official_mpcsm_percent",
                  label: "官方 MPCSM（percent）",
                },
              ]
            : profile.id === "pce"
              ? [
                  ...globalMeasures,
                  { value: "annualized_3m_ratio", label: "3月年化变化" },
                  { value: "annualized_6m_ratio", label: "6月年化变化" },
                ]
              : globalMeasures;
  const baskets = [
    ...new Map(
      profile.series.map((s) => [
        s[2],
        {
          value: s[2],
          label: s[2] === "headline" ? "总体" : s[1].split(" · ")[0],
        },
      ]),
    ).values(),
  ];
  const [series, setSeries] = useState([
    ...(profile.defaultQuerySeries ?? profile.series.map((s) => s[0])),
  ]);
  const cpiSeries = profile.series;
  const availableAdjustments = adjustments.filter((item) =>
    profile.series.some((s) => s[3] === item.value),
  );
  const latest = claims
    ? claimsLatest(availability.data)
    : gdp
      ? previousCompleteQuarter()
      : latestMonth(availability.data);
  const shiftPeriod = claims ? shiftDay : gdp ? shiftQuarter : shiftMonth;
  const validRange = claims
    ? dayRangeValid
    : gdp
      ? quarterRangeValid
      : rangeValid;
  const periodLabel = claims ? "截止周" : gdp ? "季度" : "月份";
  const [start, setStart] = useState(
    latest ? shiftPeriod(latest, claims ? -364 : gdp ? -7 : -11) : "",
  );
  const [end, setEnd] = useState(latest);
  const [basket, setBasket] = useState(["headline", "core"]);
  const [adjustment, setAdjustment] = useState(
    availableAdjustments.map((item) => item.value),
  );
  const [measure, setMeasure] = useState<string[]>(
    (profile.id === "pce" ? globalMeasures : measures).map(
      (item) => item.value,
    ),
  );
  const [columnsEdited, setColumnsEdited] = useState(false);
  const [history, setHistory] = useState(false);
  const [asOf, setAsOf] = useState("");
  const [result, setResult] = useState<RangeResult | null>(null);
  const [failure, setFailure] = useState(false);
  const [pending, setPending] = useState(false);
  const [page, setPage] = useState(0);
  const [tab, setTab] = useState(initialReleaseEvent ? "releases" : "query");
  useEffect(() => {
    if (initialReleaseEvent) setTab("releases");
  }, [initialReleaseEvent]);
  const [resultTab, setResultTab] = useState("table");
  const [exchange, setExchange] = useState<CpiHttpExchange | null>(null);
  const edited = useRef(false);
  const active = useRef<AbortController | null>(null);
  useEffect(() => {
    if (latest && !edited.current) {
      setStart(shiftPeriod(latest, claims ? -364 : gdp ? -7 : -11));
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
  }, [
    transport,
    availability.data?.web_capabilities.query,
    gdp || claims ? availability.data?.source_allowed : undefined,
    gdp || claims ? availability.failure : undefined,
  ]);
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
    validRange(start, end) &&
    basket.length > 0 &&
    adjustment.length > 0 &&
    measure.length > 0 &&
    (!employment || series.length > 0) &&
    (!history || !!beijingInputToIso(asOf));
  const scope: CpiScope & { local_only?: boolean } = {
    start,
    end,
    ...(employment ? { series: series.join(",") } : {}),
    basket: basket.length === baskets.length ? "all" : basket.join(","),
    ...(columnsEdited ? { columns: measure.join(",") } : {}),
    adjustment:
      adjustment.length === adjustments.length ? "all" : (adjustment[0] ?? ""),
    measure: consumption ? "native" : "index",
    query_mode: history ? "history" : "current",
    ...(gdp || claims
      ? {
          local_only: history,
        }
      : {}),
    ...(history
      ? {
          as_of: beijingInputToIso(asOf),
          as_of_basis: "available",
          vintage_mode: "available",
        }
      : {}),
  };
  const request = claims
    ? buildClaimsQueryRequest(scope)
    : gdp
      ? buildGdpQueryRequest(scope)
      : buildCpiQueryRequest(scope, profile);
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
      const onExchange = (value: CpiHttpExchange) => {
        if (active.current === controller && !controller.signal.aborted)
          setExchange(value);
      };
      const response = claims
        ? await queryClaimsRange(
            transport,
            scope,
            controller.signal,
            onExchange,
          )
        : gdp
          ? await queryGdpRange(transport, scope, controller.signal, onExchange)
          : monthlyRangeResult(
              await queryCpiRange(
                transport,
                scope,
                controller.signal,
                onExchange,
                profile,
              ),
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
    ? newestMonthFirst(
        [
          ...result.points.map((point) => ({
            ...point,
            point,
            reason: undefined as string | undefined,
          })),
          ...result.missing.map((gap) => ({ ...gap, point: null })),
        ].sort(
          (a, b) =>
            cpiSeries.findIndex((s) => s[0] === a.series_id) -
            cpiSeries.findIndex((s) => s[0] === b.series_id),
        ),
        (row) => row.period,
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
      <h3>
        {profile.label.startsWith("美国")
          ? profile.label
          : `美国 ${profile.label}`}{" "}
        · 范围查询
      </h3>
      <Tabs
        label={`${profile.label} 消费者页签`}
        values={[
          ["query", "查询数据"],
          ["api", "API说明"],
          ["releases", "发布记录"],
        ]}
        value={tab}
        onChange={setTab}
      />
      {tab === "releases" && (
        <MacroReleaseRecords
          key={initialReleaseEvent ?? "all"}
          transport={transport}
          profile={profile}
          initialEventId={initialReleaseEvent}
          initialEvidenceId={initialReleaseEvidence}
        />
      )}
      <div hidden={tab !== "api"}>
        <CpiQueryApiHelp
          profile={profile}
          availability={availability}
          request={request}
        />
      </div>
      <div hidden={tab !== "query"}>
        <p className="muted">
          当前查询优先读取本地，缺口自动向 {profile.provider}{" "}
          请求临时补源，不保存；来源访问受服务授权约束。
          历史截止查询只读已入库记录。
        </p>
        <CpiAvailabilityNotice profile={profile} availability={availability} />
        {profile.historyNotice && (
          <p className="small">{profile.historyNotice}</p>
        )}
        {availability.data && (
          <>
            <p>
              已保存观测 {availability.data.observation_count.toLocaleString()}{" "}
              条；各口径{claims ? "全库最新有值截止周" : `最新${periodLabel}`}：
            </p>
            <ul className="cpi-coverage">
              {cpiSeries.map(([id, title]) => (
                <li key={id}>
                  <span>{title}：</span>
                  <span className="cpi-coverage-period">
                    {(claims
                      ? availability.data!.latest_weeks?.[id]
                      : gdp
                        ? availability.data!.latest_quarters?.[id]
                        : availability.data!.latest_months[id]) ??
                      "尚无本地记录"}
                  </span>
                </li>
              ))}
            </ul>
            {claims && (
              <p className="small">
                此处为全库最新有值截止周；执行详情中的最新周仅限该次采集范围。初请与续请的参考周分别取自来源，续请通常滞后一周，末端差异本身不表示采集失败。
              </p>
            )}
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
            {gdp ? (
              <QuarterField
                label="开始季度"
                value={start}
                onChange={(value) => change(() => setStart(value))}
              />
            ) : (
              <DateField
                label={claims ? "开始日期（截止周范围）" : "开始月份"}
                type={claims ? "date" : "month"}
                value={start}
                onChange={(value) => change(() => setStart(value))}
              />
            )}
            {gdp ? (
              <QuarterField
                label="结束季度"
                value={end}
                onChange={(value) => change(() => setEnd(value))}
              />
            ) : (
              <DateField
                label={claims ? "结束日期（截止周范围）" : "结束月份"}
                type={claims ? "date" : "month"}
                value={end}
                onChange={(value) => change(() => setEnd(value))}
              />
            )}
            {employment ? (
              <MultiSelect
                label={
                  claims
                    ? "申领系列"
                    : gdp
                      ? "GDP指标"
                      : consumption
                        ? "消费与收入指标"
                        : "就业指标"
                }
                options={profile.series.map((s) => ({
                  value: s[0],
                  label: s[1],
                }))}
                value={series}
                onChange={(value) => change(() => setSeries(value))}
              />
            ) : (
              <>
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
              </>
            )}
            <MultiSelect
              label="显示指标"
              options={measures}
              value={measure}
              onChange={(value) => {
                if (
                  result?.selected_columns &&
                  value.some(
                    (column) => !result.selected_columns!.includes(column),
                  )
                ) {
                  change(() => {
                    setMeasure(value);
                    setColumnsEdited(true);
                  });
                } else {
                  setMeasure(value);
                  setColumnsEdited(true);
                }
              }}
            />
          </div>
          <label className="cpi-history-toggle">
            <input
              type="checkbox"
              checked={history}
              onChange={(event) =>
                change(() => {
                  setHistory(event.target.checked);
                })
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
          {employment && !series.length && (
            <p role="alert">
              请至少选择一项{gdp ? "GDP" : consumption ? "消费与收入" : "就业"}
              指标。
            </p>
          )}
          {!employment && (!basket.length || !adjustment.length) && (
            <p role="alert">统计篮子和季节调整均须至少选择一项。</p>
          )}
          {!measure.length && <p role="alert">请至少选择一项显示指标。</p>}
          {start && end && !validRange(start, end) && (
            <p role="alert">
              请选择有效的起止{periodLabel}，开始不得晚于结束，
              {claims ? "范围至多36600天（100年上限）。" : "范围须少于200年。"}
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
          {claims
            ? "按六条原生FRED系列查询，使用自然日范围及周六截止周，不发送月份参数。"
            : gdp
              ? "按所选原生系列查询；显示指标仅控制表格列，不发送月度参数或 columns。"
              : consumption
                ? "按所选原生系列查询；金额为百万美元年率水平，储蓄率为百分数及百分点变化。"
                : employment
                  ? "按所选就业系列查询，各指标保留原生单位。"
                  : retail
                    ? "按所选口径查询，计算复用六项原始输入。"
                    : "按所选统计口径查询，请求使用 measure=index。"}
          {!gdp && !claims && (
            <>
              更改显示指标后，请求会发送 columns
              限定返回字段；标识、来源和缺值信息保留。
            </>
          )}
          缺值不补零。
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
        {(profile.id === "retail" || claims) && result?.source_failure && (
          <p role="alert">
            <strong>
              {profile.provider} 补取失败，已保留本地数据，部分返回。
            </strong>
            缺口仍为空，不能视为官方缺值。
            {result.source_failure.classification}
            {result.source_failure.http_status !== null &&
              ` · HTTP ${result.source_failure.http_status}`}
            {result.source_failure.retry_at &&
              ` · 可重试时间：${formatBeijingTime(result.source_failure.retry_at)}`}
          </p>
        )}
        {profile.id === "retail" && !!result?.source_no_content?.length && (
          <p role="alert">
            <strong>Census 本次未返回记录（HTTP 204）。</strong>
            已保留已有数据和全部月份，缺口仍为空；不代表官方缺值或已发布。
          </p>
        )}
        {result?.release_records_missing?.map((reason) => (
          <p key={reason} role="status">
            发布材料状态：{releaseReasonText(reason)}；连续数值结果保留。
          </p>
        ))}
        <div hidden={resultTab !== "table"}>
          {result && columns.length > 0 && (
            <section aria-label={`${profile.label} 范围结果`}>
              <h4>
                {result.start_period} 至 {result.end_period}
              </h4>
              <p role="status">
                共 {rows.length} 条（{claims ? "截止周" : gdp ? "季度" : "月"} ×
                口径），有效观测{" "}
                {gdp || claims
                  ? result.points.filter(
                      (point) => point.availability === "available",
                    ).length
                  : result.points.length}{" "}
                条，缺值{" "}
                {gdp || claims
                  ? result.points.filter(
                      (point) => point.availability === "missing",
                    ).length
                  : result.missing.length}{" "}
                条。
                {profile.id === "retail" &&
                (result.source_failure || result.source_no_content?.length)
                  ? "来源补取未完成，当前为部分返回。"
                  : (gdp || claims
                        ? result.complete
                        : !result.missing.length) &&
                      !selectedCalculationMissing.length
                    ? "所选指标数据完整。"
                    : "所选指标存在缺值。"}
              </p>
              <p className="small">
                {claims
                  ? "所有原生数值与周差、本地均值均为Number；周变化比率以百分比显示。官方均值是独立系列，不计算均值的本地四周均值。"
                  : gdp
                    ? "金额为百万2017链价美元（SAAR）；官方年化季环比为%，贡献为百分点。水平季变/年变以%显示，未经年化；其他六项不计算变化。"
                    : consumption
                      ? "金额为百万当期/2017链价美元，季调年率水平；变化为累计比率。储蓄率为百分数，其变化列为百分点。"
                      : employment
                        ? "CES 千岗位及月净增；失业率、劳动参与率为百分数及百分点变化；时薪为美元/小时及环同比；平均每周工时为小时及小时变化。"
                        : "变化率以百分比显示；3月、6月为相隔对应月数的累计变化，未经年化。"}
              </p>
              <div className="table-wrap">
                <table
                  className="macro-result-table"
                  aria-label={`${profile.label} 范围数据`}
                >
                  <thead>
                    <tr>
                      <th>{periodLabel}</th>
                      <th>口径</th>
                      {columns.map((column) => (
                        <th key={column.value}>{column.label}</th>
                      ))}
                      {profile.id === "ppi" && <th>官方状态</th>}
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
                            g.period === row.period &&
                            columns.some((column) => column.value === g.metric),
                        );
                        return (
                          <tr key={`${row.period}:${row.series_id}`}>
                            <td>
                              {row.period}
                              <RelatedReleaseLinks
                                links={row.point?.related_release_events}
                                profile={profile}
                              />
                            </td>
                            <td>
                              {
                                cpiSeries.find(
                                  (s) => s[0] === row.series_id,
                                )?.[1]
                              }
                              {!history &&
                                (row.point?.availability === "unpersisted" ||
                                  ((gdp || claims) &&
                                    row.point?.origin === "upstream")) && (
                                  <span className="cpi-temporary-value">
                                    临时来源值
                                  </span>
                                )}
                            </td>
                            {columns.map(({ value: field }) => (
                              <td key={field}>
                                {valueText(
                                  row.point?.[field as keyof RangePoint],
                                  field.endsWith("ratio"),
                                  claims
                                    ? field.endsWith("ratio")
                                      ? "%"
                                      : ""
                                    : gdp
                                      ? field.endsWith("ratio") ||
                                        row.point?.value_unit === "percent"
                                        ? "%"
                                        : row.point?.value_unit ===
                                            "percentage_points"
                                          ? " 百分点"
                                          : ""
                                      : employment
                                        ? field.endsWith("ratio") ||
                                          field === "mom_pct" ||
                                          field === "yoy_pct"
                                          ? "%"
                                          : consumption &&
                                              field === "value" &&
                                              row.series_id === "A072RC"
                                            ? "%"
                                            : ""
                                        : field === "index_value"
                                          ? ""
                                          : "%",
                                  claims ||
                                    (gdp &&
                                      field === "value" &&
                                      row.point?.value_unit ===
                                        "million_chained_2017_USD") ||
                                    retail ||
                                    (consumption &&
                                      field === "value" &&
                                      row.series_id !== "A072RC"),
                                )}
                              </td>
                            ))}
                            {profile.id === "ppi" && (
                              <td>
                                {row.point?.provider_status?.preliminary
                                  ? "初步值（官方脚注）"
                                  : row.point?.provider_status?.revised
                                    ? "修订值（官方脚注）"
                                    : "官方状态未注明"}
                                {row.point?.provider_status?.footnotes
                                  ?.length ? (
                                  <details>
                                    <summary>官方脚注</summary>
                                    {row.point.provider_status.footnotes.map(
                                      (note, i) => (
                                        <p key={i}>
                                          {note.code} {note.text}
                                        </p>
                                      ),
                                    )}
                                  </details>
                                ) : null}
                              </td>
                            )}
                            <td>
                              {claims
                                ? row.point?.claims_plans?.length
                                  ? row.point.claims_plans.map((t) => (
                                      <p key={t}>
                                        计划发布（材料标注）：
                                        {formatBeijingTime(t)}
                                      </p>
                                    ))
                                  : "未获得对应截止周公布值的计划时间证据"
                                : gdp
                                  ? row.point?.calendar_plans?.length
                                    ? row.point.calendar_plans.map(
                                        (event, i) => (
                                          <p key={i}>
                                            {gdpEstimateText(
                                              event.estimate_kind,
                                            )}{" "}
                                            ·{" "}
                                            {(
                                              {
                                                CONFIRMED: "计划已确认",
                                                TENTATIVE: "暂定",
                                                CANCELLED: "已取消",
                                              } as Record<string, string>
                                            )[event.status] ??
                                              event.status}{" "}
                                            · 计划：
                                            {formatBeijingTime(
                                              event.release_at,
                                            )}
                                          </p>
                                        ),
                                      )
                                    : "无对应季度日历计划"
                                  : (publicationText({
                                      month: row.period,
                                      publication_event:
                                        row.point?.publication_event ??
                                        events.find(
                                          (event) =>
                                            event.reference_month ===
                                            row.period,
                                        ),
                                    }) ?? "无对应月份记录")}
                            </td>
                            <td>
                              {(gdp || claims) &&
                              row.point?.availability === "missing"
                                ? missingText(
                                    row.point.missing_reason ?? undefined,
                                  )
                                : !row.point
                                  ? missingText(row.reason)
                                  : gaps.length
                                    ? `计算基期缺值：${[...new Set(gaps.map((g) => `${g.required_period}（${missingText(g.reason)}）`))].join("、")}`
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
                  来源：{profile.provider}；本次未保存。
                  {gdp &&
                    "连续序列 estimate_kind=unknown，event_binding_status=unconfirmed；日历计划不证明官方公布值或首发。"}
                  来源请求 {result.provider_requests} 次。
                  {history
                    ? `入库截止：${formatBeijingTime(result.as_of)}。`
                    : "展示当前查询值。"}
                  {claims
                    ? "行内时间仅取自材料对应该系列和截止周公布值的计划发布时间；不以未来全局日历填历史行，不证明当前FRED版本当时已知。原机构DOL-ETA。"
                    : "时间来自官方日历，不表示当前修订值在该时已知；历史截止仍按入库时间。"}
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
