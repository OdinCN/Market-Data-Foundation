import type { Transport } from "./api";
import { GDP_PROFILE } from "./macroTimeSeriesProfile";
import {
  CpiServiceFailure,
  requestCpiJson,
  type CpiStatus,
  type CpiScope,
  type CpiHttpExchange,
  type CpiJob,
  uuidValid,
} from "./cpiTimeSeriesApi";
import { isDecimalString } from "./decimalValue";
import {
  quarterValid,
  quarterRangeValid,
  quarterCount,
  shiftQuarter,
} from "./QuarterField";
import type { RangePoint, RangeResult } from "./rangeQueryModel";

export type GdpScope = Pick<
  CpiScope,
  "start" | "end" | "series" | "query_mode" | "as_of" | "as_of_basis"
> & { local_only?: boolean };

export type GdpStatus = Omit<
  CpiStatus,
  "latest_months" | "initialization" | "collection_scope"
> & {
  latest_quarters: Record<string, string | null>;
  initialization: {
    start: string;
    end: string;
    quarters: number;
    anchor_basis: string;
    preset_id: string;
    available: boolean;
  };
  collection_scope: { series: string; max_quarters: number };
};
export type GdpPoint = {
  series_id: string;
  quarter: string;
  value: string | null;
  value_unit: "million_chained_2017_USD" | "percent" | "percentage_points";
  statistic: string;
  origin?: "local" | "upstream";
  availability: "available" | "missing";
  missing_reason: string | null;
  estimate_kind: "unknown";
  event_binding_status: "unconfirmed";
  available_at?: string | null;
  observed_at?: string;
  change_1q_ratio: string | null;
  change_4q_ratio: string | null;
  calculation_missing: Record<string, string>;
  publication_events: {
    reference_quarter: string;
    estimate_kind: string;
    release_at: string;
    status: string;
  }[];
  related_releases: {
    event_id: string;
    source_version: NonNullable<
      RangePoint["related_release_events"]
    >[number]["source_version"];
    estimate_kind: string;
    roles: string[];
    association: "scope_only";
    continuous_version_bound: false;
  }[];
};
export type GdpResult = {
  start_quarter: string;
  end_quarter: string;
  series_ids: string[];
  points: GdpPoint[];
  status: "complete" | "partial" | "unavailable";
  coverage: {
    expected: number;
    available: number;
    official_null: number;
    missing: number;
  };
  query_mode: "current" | "history";
  as_of: string | null;
  as_of_basis: "observed" | "available";
  local_only: boolean;
  provider_requests: number;
  provider_attempts: number;
  source_failure: string | null;
};
const ids = GDP_PROFILE.series.map(([id]) => id);
const count = (value: number) => Number.isInteger(value) && value >= 0;
const decimal = (value: unknown) => value === null || isDecimalString(value);
export async function readGdpStatus(
  transport: Transport,
  signal: AbortSignal,
): Promise<CpiStatus> {
  const value = await requestCpiJson<GdpStatus>(
    transport,
    `${GDP_PROFILE.basePath}/status`,
    { signal },
    20000,
  );
  if (
    !value ||
    !value.latest_quarters ||
    Array.isArray(value.latest_quarters) ||
    Object.keys(value.latest_quarters).length !== ids.length ||
    !Object.entries(value.latest_quarters).every(
      ([id, q]) => ids.includes(id) && (q === null || quarterValid(q)),
    ) ||
    ![value.observation_count, value.revision_count, value.batch_count].every(
      count,
    ) ||
    typeof value.source_allowed !== "boolean" ||
    !value.initialization ||
    !quarterRangeValid(value.initialization.start, value.initialization.end) ||
    !count(value.initialization.quarters) ||
    typeof value.initialization.available !== "boolean" ||
    typeof value.initialization.anchor_basis !== "string" ||
    !value.collection_scope ||
    !count(value.collection_scope.max_quarters) ||
    typeof value.web_capabilities?.query !== "boolean" ||
    typeof value.web_capabilities?.collect !== "boolean"
  )
    throw new CpiServiceFailure("gdp_status_contract_invalid");
  // Common availability view retains native quarters; never put quarters in latest_months.
  return {
    observation_count: value.observation_count,
    revision_count: value.revision_count,
    batch_count: value.batch_count,
    web_capabilities: value.web_capabilities,
    latest_months: {},
    latest_quarters: value.latest_quarters,
    quarter_initialization: value.initialization,
    collection_scope: value.collection_scope,
    source_allowed: value.source_allowed === true,
  };
}
export function buildGdpQueryRequest(scope: GdpScope) {
  const history = scope.query_mode === "history" || !!scope.as_of;
  const params = new URLSearchParams({
    start: scope.start,
    end: scope.end,
    series: scope.series ?? ids.join(","),
    query_mode: history ? "history" : "current",
    local_only: String(history || scope.local_only !== false),
    ...(history ? { as_of_basis: "available", as_of: scope.as_of ?? "" } : {}),
  });
  return {
    method: "GET" as const,
    path: `${GDP_PROFILE.basePath}/query?${params}`,
  };
}
export function normalizeGdpResult(
  result: GdpResult,
  scope: GdpScope,
): RangeResult {
  const selected = scope.series?.split(",") ?? ids;
  const history = scope.query_mode === "history" || !!scope.as_of;
  const expected = quarterCount(scope.start, scope.end) * selected.length;
  if (
    !result ||
    result.start_quarter !== scope.start ||
    result.end_quarter !== scope.end ||
    result.query_mode !== (history ? "history" : "current") ||
    result.local_only !== (history || scope.local_only !== false) ||
    result.as_of_basis !== (history ? "available" : "observed") ||
    (history
      ? Date.parse(result.as_of ?? "") !== Date.parse(scope.as_of ?? "")
      : result.as_of !== null) ||
    ![result.provider_requests, result.provider_attempts].every(count) ||
    ((history || scope.local_only !== false) &&
      (result.provider_requests !== 0 || result.provider_attempts !== 0)) ||
    !Array.isArray(result.series_ids) ||
    new Set(result.series_ids).size !== selected.length ||
    !selected.every((id) => result.series_ids.includes(id)) ||
    !Array.isArray(result.points) ||
    result.points.length !== expected ||
    !result.coverage ||
    result.coverage.expected !== expected ||
    ![
      result.coverage.available,
      result.coverage.official_null,
      result.coverage.missing,
    ].every(count) ||
    result.coverage.available +
      result.coverage.official_null +
      result.coverage.missing !==
      expected ||
    !["complete", "partial", "unavailable"].includes(result.status) ||
    !(
      result.source_failure === null ||
      typeof result.source_failure === "string"
    )
  )
    throw new CpiServiceFailure("gdp_result_contract_invalid");
  const keys = new Set<string>();
  for (const point of result.points) {
    const index = ids.indexOf(point.series_id);
    const unit =
      index < 2
        ? "million_chained_2017_USD"
        : index === 2
          ? "percent"
          : "percentage_points";
    const statistic =
      index < 2
        ? "SAAR_level"
        : index === 2
          ? "official_annualized_qoq"
          : "contribution_to_annualized_gdp_growth";
    const key = `${point.quarter}:${point.series_id}`;
    if (
      !selected.includes(point.series_id) ||
      !quarterValid(point.quarter) ||
      point.quarter < scope.start ||
      point.quarter > scope.end ||
      keys.has(key) ||
      ![point.value, point.change_1q_ratio, point.change_4q_ratio].every(
        decimal,
      ) ||
      point.value_unit !== unit ||
      point.statistic !== statistic ||
      (point.origin === "upstream" &&
        (history || scope.local_only !== false)) ||
      point.estimate_kind !== "unknown" ||
      point.event_binding_status !== "unconfirmed" ||
      !["available", "missing"].includes(point.availability) ||
      (point.availability === "available" &&
        (point.value === null || point.missing_reason !== null)) ||
      (point.availability === "missing" &&
        typeof point.missing_reason !== "string") ||
      (index >= 2 &&
        (point.change_1q_ratio !== null || point.change_4q_ratio !== null)) ||
      !point.calculation_missing ||
      !Object.entries(point.calculation_missing).every(
        ([metric, reason]) =>
          index < 2 &&
          ["change_1q_ratio", "change_4q_ratio"].includes(metric) &&
          typeof reason === "string",
      ) ||
      !Array.isArray(point.related_releases) ||
      !Array.isArray(point.publication_events) ||
      !point.publication_events.every(
        (event) =>
          event.reference_quarter === point.quarter &&
          typeof event.estimate_kind === "string" &&
          Number.isFinite(Date.parse(event.release_at)),
      ) ||
      !point.related_releases.every(
        (link) =>
          typeof link.event_id === "string" &&
          link.association === "scope_only" &&
          link.continuous_version_bound === false,
      )
    )
      throw new CpiServiceFailure("gdp_result_contract_invalid");
    keys.add(key);
  }
  const points: RangePoint[] = result.points.map((point) => ({
    ...point,
    period: point.quarter,
    value: point.availability === "available" ? point.value : null,
    related_release_events: point.related_releases.map((link) => ({
      ...link,
      evidence_ids: [],
      reason: "continuous_version_unbound",
    })),
    calendar_plans: point.publication_events,
  }));
  return {
    start_period: result.start_quarter,
    end_period: result.end_quarter,
    series_ids: result.series_ids,
    points,
    missing: [],
    calculation_missing: result.points.flatMap((point) =>
      Object.entries(point.calculation_missing).map(([metric, reason]) => ({
        series_id: point.series_id,
        period: point.quarter,
        metric,
        reason,
        required_period: shiftQuarter(
          point.quarter,
          metric === "change_1q_ratio" ? -1 : -4,
        ),
      })),
    ),
    complete: result.coverage.available === expected,
    coverage_complete: result.coverage.missing === 0,
    persisted: false,
    provider_requests: result.provider_requests,
    provider_attempts: result.provider_attempts,
    vintage_mode: result.as_of_basis,
    as_of: result.as_of,
    query_mode: result.query_mode,
    source_failure: result.source_failure
      ? {
          classification: result.source_failure,
          http_status: null,
          retry_at: null,
        }
      : null,
  };
}
export async function queryGdpRange(
  transport: Transport,
  scope: GdpScope,
  signal: AbortSignal,
  onExchange?: (value: CpiHttpExchange) => void,
) {
  const request = buildGdpQueryRequest(scope);
  const result = await requestCpiJson<GdpResult>(
    transport,
    request.path,
    { signal },
    20000,
    onExchange,
  );
  return normalizeGdpResult(result, scope);
}

// GDP receipts retain native quarterly fields, including top-level source gaps.
export function validGdpJob(job: CpiJob): boolean {
  const instant = (v: unknown) =>
    typeof v === "string" && Number.isFinite(Date.parse(v));
  const result = job?.result;
  const coverage = result?.coverage;
  return (
    !!job &&
    "asset" in job &&
    job.asset === "gdp" &&
    "origin" in job &&
    ["manual", "schedule"].includes(String(job.origin)) &&
    uuidValid(job.request_id) &&
    job.job_id === job.request_id &&
    ["init", "update"].includes(job.action) &&
    quarterRangeValid(job.start, job.end) &&
    ["running", "complete", "failed", "unknown"].includes(job.state) &&
    instant(job.created_at) &&
    [job.started_at, job.finished_at].every((v) => v === null || instant(v)) &&
    (job.classification === null ||
      (typeof job.classification === "string" &&
        /^[a-z0-9_]{1,100}$/.test(job.classification))) &&
    (result === null ||
      (typeof result === "object" &&
        [
          result.provider_requests,
          result.provider_attempts,
          result.saved_points,
          result.saved_revisions,
        ].every((v) => v === undefined || count(v)) &&
        (result.source_missing === undefined ||
          (Array.isArray(result.source_missing) &&
            result.source_missing.every(
              (p) => typeof p.series_id === "string" && quarterValid(p.quarter),
            ))))) &&
    (job.state !== "complete" ||
      (!!result?.readback &&
        result.readback.verified === true &&
        job.finished_at !== null &&
        result.readback.as_of_basis === "available" &&
        !!coverage &&
        [
          coverage.expected,
          coverage.available,
          coverage.official_null,
          coverage.missing,
        ].every(count) &&
        coverage.expected === quarterCount(job.start, job.end) * ids.length &&
        coverage.expected ===
          coverage.available + coverage.official_null + coverage.missing))
  );
}
