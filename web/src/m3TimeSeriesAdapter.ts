import type { M3Comparison } from "./M3ComparisonTable";
import type { Transport } from "./api";
import { M3_PROFILE } from "./macroTimeSeriesProfile";
import {
  CpiServiceFailure,
  monthCount,
  monthValid,
  requestCpiJson,
  type CpiHttpExchange,
  type CpiScope,
} from "./cpiTimeSeriesApi";
import { isDecimalString } from "./decimalValue";
import type { RangeResult } from "./rangeQueryModel";

type Scope = CpiScope & {
  dataset: "m3" | "advm3" | "both";
  local_only?: boolean;
};
type Point = {
  series_id: string;
  month: string;
  dataset: string;
  value: string | null;
  status: "available" | "missing";
  reason: string | null;
  origin: "local" | "upstream";
  observed_at: string | null;
  available_at: string | null;
  source: {
    unit: string;
    seasonal_adjustment: string;
    frequency: string;
    report_stage: string;
  };
};
type Response = {
  reports?: Record<string, Response>;
  comparisons?: M3Comparison[];
  comparison_basis?: string;
  points: Point[];
  missing: { series_id: string; month: string; reason: string }[];
  dataset: string;
  report_stage: string;
  status: string;
  query_mode: string;
  as_of: string | null;
  as_of_basis: string;
  local_only: boolean;
  persisted: boolean;
  provider_requests: number;
  provider_attempts: number | null;
  coverage: { expected: number; available: number };
  source_failure: { classification: string; http_status: number | null } | null;
};
const ids = M3_PROFILE.series.map(([id]) => id);
const count = (v: unknown): v is number =>
  typeof v === "number" && Number.isInteger(v) && v >= 0;
export function buildM3QueryRequest(scope: Scope) {
  const history = scope.query_mode === "history";
  const params = new URLSearchParams({
    start: scope.start,
    end: scope.end,
    dataset: scope.dataset,
    series: scope.series ?? ids.join(","),
    query_mode: history ? "history" : "current",
    local_only: String(history),
    as_of_basis: "available",
    ...(history ? { as_of: scope.as_of ?? "" } : {}),
  });
  return {
    method: "GET" as const,
    path: `${M3_PROFILE.basePath}/query?${params}`,
  };
}
export function normalizeM3Result(data: Response, scope: Scope): RangeResult {
  if (scope.dataset === "both") {
    if (
      data?.dataset !== "both" ||
      !data.reports?.m3 ||
      !data.reports?.advm3 ||
      !Array.isArray(data.comparisons) ||
      data.comparison_basis !==
        (scope.query_mode === "history"
          ? "stored_source_snapshots_at_cutoff_not_official_event_revisions"
          : "current_source_snapshots_not_official_event_revisions")
    )
      throw new CpiServiceFailure("m3_joint_contract_invalid");
    const full = normalizeM3Result(data.reports.m3, {
      ...scope,
      dataset: "m3",
    });
    const advance = normalizeM3Result(data.reports.advm3, {
      ...scope,
      dataset: "advm3",
    });
    const selected = scope.series?.split(",") ?? ids;
    const expected = monthCount(scope.start, scope.end) * selected.length;
    if (
      data.persisted !== false ||
      !count(data.provider_requests) ||
      data.provider_requests !==
        full.provider_requests + advance.provider_requests
    )
      throw new CpiServiceFailure("m3_joint_contract_invalid");
    const fullValues = new Map(
      full.points.map((p) => [`${p.period}:${p.series_id}`, p.value]),
    );
    const advanceValues = new Map(
      advance.points.map((p) => [`${p.period}:${p.series_id}`, p.value]),
    );
    const keys = new Set<string>();
    for (const row of data.comparisons) {
      const key = `${row.month}:${row.series_id}`;
      if (
        row.m3_value !== (fullValues.get(key) ?? null) ||
        row.advm3_value !== (advanceValues.get(key) ?? null) ||
        !monthValid(row.month) ||
        row.month < scope.start ||
        row.month > scope.end ||
        !selected.includes(row.series_id) ||
        keys.has(key) ||
        row.comparison_basis !== data.comparison_basis ||
        row.official_event_revision !== null ||
        row.difference_direction !== "m3_minus_advm3" ||
        ![row.m3_value, row.advm3_value, row.difference].every(
          (v) => v === null || isDecimalString(v),
        ) ||
        ((row.m3_value === null || row.advm3_value === null) &&
          row.difference !== null)
      )
        throw new CpiServiceFailure("m3_joint_contract_invalid");
      keys.add(key);
    }
    if (
      keys.size !== expected ||
      data.coverage.expected !== expected * 2 ||
      data.coverage.available !== full.points.length + advance.points.length
    )
      throw new CpiServiceFailure("m3_joint_contract_invalid");
    return {
      ...full,
      m3_comparisons: data.comparisons,
      m3_coverage: data.coverage,
      complete: full.complete && advance.complete,
      coverage_complete: full.complete && advance.complete,
      provider_requests: data.provider_requests,
      provider_attempts: data.provider_attempts,
    };
  }
  const selected = scope.series?.split(",") ?? ids;
  const history = scope.query_mode === "history";
  const expected = monthCount(scope.start, scope.end) * selected.length;
  const keys = new Set<string>();
  if (
    !data ||
    data.dataset !== scope.dataset ||
    data.report_stage !== (scope.dataset === "m3" ? "full" : "advance") ||
    data.query_mode !== (history ? "history" : "current") ||
    data.as_of_basis !== "available" ||
    data.local_only !== history ||
    data.persisted !== false ||
    (history
      ? Date.parse(data.as_of ?? "") !== Date.parse(scope.as_of ?? "")
      : data.as_of !== null) ||
    !count(data.provider_requests) ||
    data.provider_requests > 30 ||
    !(data.provider_attempts === null || count(data.provider_attempts)) ||
    (history &&
      (data.provider_requests !== 0 || data.provider_attempts !== 0)) ||
    !Array.isArray(data.points) ||
    data.points.length !== expected ||
    !Array.isArray(data.missing) ||
    !data.coverage ||
    data.coverage.expected !== expected ||
    !count(data.coverage.available) ||
    !["complete", "partial", "unavailable"].includes(data.status)
  )
    throw new CpiServiceFailure("m3_result_contract_invalid");
  for (const p of data.points) {
    const key = `${p.month}:${p.series_id}`;
    if (
      !selected.includes(p.series_id) ||
      !monthValid(p.month) ||
      p.month < scope.start ||
      p.month > scope.end ||
      keys.has(key) ||
      p.dataset !== scope.dataset ||
      !["local", "upstream"].includes(p.origin) ||
      (history && p.origin !== "local") ||
      !(p.value === null || isDecimalString(p.value)) ||
      (p.value === null ? p.status !== "missing" : p.status !== "available") ||
      p.source?.unit !== "million_current_usd" ||
      p.source?.seasonal_adjustment !== "SA" ||
      p.source?.frequency !== "monthly" ||
      p.source?.report_stage !== data.report_stage
    )
      throw new CpiServiceFailure("m3_result_contract_invalid");
    keys.add(key);
  }
  const available = data.points.filter((p) => p.value !== null);
  const missing = data.points.filter((p) => p.value === null);
  const missingKeys = new Set(
    data.missing.map((p) => `${p.month}:${p.series_id}`),
  );
  if (
    available.length !== data.coverage.available ||
    missing.length !== data.missing.length ||
    missingKeys.size !== missing.length ||
    !missing.every((p) => missingKeys.has(`${p.month}:${p.series_id}`)) ||
    (data.source_failure !== null &&
      (typeof data.source_failure?.classification !== "string" ||
        !(
          data.source_failure.http_status === null ||
          count(data.source_failure.http_status)
        )))
  )
    throw new CpiServiceFailure("m3_result_contract_invalid");
  return {
    start_period: scope.start,
    end_period: scope.end,
    series_ids: selected,
    points: available.map((p) => ({
      period: p.month,
      series_id: p.series_id,
      value: p.value,
      value_unit: "million_current_usd",
      observed_at: p.observed_at ?? undefined,
      available_at: p.available_at,
      origin: p.origin,
      availability: p.origin === "upstream" ? "unpersisted" : "available",
    })),
    missing: missing.map((p) => ({
      period: p.month,
      series_id: p.series_id,
      reason: p.reason ?? "source_unavailable",
    })),
    calculation_missing: [],
    complete: data.status === "complete",
    coverage_complete: data.status === "complete",
    persisted: false,
    provider_requests: data.provider_requests,
    provider_attempts: data.provider_attempts,
    vintage_mode: "available",
    as_of: data.as_of,
    as_of_basis: data.as_of_basis,
    source_failure: data.source_failure && {
      ...data.source_failure,
      retry_at: null,
    },
    query_mode: history ? "history" : "current",
  };
}
export async function queryM3Range(
  transport: Transport,
  scope: Scope,
  signal: AbortSignal,
  onExchange: (value: CpiHttpExchange) => void,
) {
  const request = buildM3QueryRequest(scope);
  return normalizeM3Result(
    await requestCpiJson<Response>(
      transport,
      request.path,
      { signal },
      45000,
      onExchange,
    ),
    scope,
  );
}
