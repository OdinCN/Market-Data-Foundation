import type { Transport } from "./api";
import { CLAIMS_PROFILE } from "./macroTimeSeriesProfile";
import {
  CpiServiceFailure,
  requestCpiJson,
  uuidValid,
  type CpiScope,
  type CpiStatus,
  type CpiHttpExchange,
  type CpiJob,
} from "./cpiTimeSeriesApi";
import { isDecimalString } from "./decimalValue";
import type { RangePoint, RangeResult } from "./rangeQueryModel";
export const dayValid = (v: unknown): v is string =>
  typeof v === "string" &&
  /^\d{4}-\d{2}-\d{2}$/.test(v) &&
  Number.isFinite(Date.parse(v)) &&
  new Date(v).toISOString().slice(0, 10) === v;
export const dayRangeValid = (a: string, b: string) =>
  dayValid(a) &&
  dayValid(b) &&
  a >= "1901-01-01" &&
  a <= b &&
  Date.parse(b) - Date.parse(a) <= 100 * 366 * 86400000;
export const shiftDay = (v: string, n: number) =>
  new Date(Date.parse(v) + n * 86400000).toISOString().slice(0, 10);
export const weekValid = (v: unknown): v is string =>
  dayValid(v) && new Date(v).getUTCDay() === 6;
export const weekCount = (a: string, b: string) => {
  if (!dayRangeValid(a, b)) return 0;
  const first =
    Date.parse(a) + ((6 - new Date(a).getUTCDay() + 7) % 7) * 86400000;
  return Math.max(0, Math.floor((Date.parse(b) - first) / (7 * 86400000)) + 1);
};
export const claimsLatest = (v?: CpiStatus | null) =>
  Object.values(v?.latest_weeks ?? {})
    .filter((w): w is string => w !== null)
    .sort()
    .at(-1) ?? new Date().toISOString().slice(0, 10);
const ids = CLAIMS_PROFILE.series.map(([id]) => id);
const count = (v: unknown) =>
  typeof v === "number" && Number.isInteger(v) && v >= 0;
const decimal = (v: unknown) => v === null || isDecimalString(v);
export type ClaimsRow = {
  series_id: string;
  week_ending: string;
  value: string | null;
  unit: string;
  statistic: string;
  missing_reason: string | null;
  provider: string;
  original_agency: string;
  provenance: {
    origin: string;
    observed_at: string;
    available_at: string | null;
    revision: number | null;
  } | null;
  weekly_change: { value: string | null; missing_reason: string | null };
  weekly_change_ratio: { value: string | null; missing_reason: string | null };
  local_four_week_average?: {
    value: string | null;
    missing_reason: string | null;
  };
  related_releases: {
    event_id: string;
    evidence_id: string;
    reference_week: string;
    roles: string[];
    source_version: {
      value: string;
      precision: "date";
      basis: string;
      source?: string;
    };
    scheduled_publication_at: string | null;
    schedule_basis: string;
    association: "scope_only";
    continuous_version_bound: false;
  }[];
};
export type ClaimsResult = {
  asset_id: string;
  start_date: string;
  end_date: string;
  rows: ClaimsRow[];
  coverage: {
    expected: number;
    available: number;
    missing_rows: number;
    provider_missing_values: number;
  };
  as_of: string | null;
  as_of_basis: string;
  query_mode: string;
  local_only: boolean;
  provider_requests: number;
  provider_attempts: number;
  source_failure: string | null;
  status: string;
};
export function buildClaimsQueryRequest(
  scope: CpiScope & { local_only?: boolean },
) {
  const history = scope.query_mode === "history" || !!scope.as_of;
  const params = new URLSearchParams({
    start: scope.start,
    end: scope.end,
    series: scope.series ?? ids.join(","),
    query_mode: history ? "history" : "current",
    local_only: String(history || scope.local_only !== false),
    as_of_basis: "available",
    ...(history ? { as_of: scope.as_of ?? "" } : {}),
  });
  return {
    method: "GET" as const,
    path: `${CLAIMS_PROFILE.basePath}/query?${params}`,
  };
}
export async function readClaimsStatus(
  transport: Transport,
  signal: AbortSignal,
): Promise<CpiStatus> {
  const v = await requestCpiJson<
    CpiStatus & {
      initialization: {
        start: string;
        end: string;
        years: number;
        available: boolean;
        preset_id: string;
      };
    }
  >(transport, `${CLAIMS_PROFILE.basePath}/status`, { signal }, 20000);
  if (
    !v ||
    !v.latest_weeks ||
    Object.keys(v.latest_weeks).length !== 6 ||
    !ids.every(
      (id) =>
        id in v.latest_weeks! &&
        (v.latest_weeks![id] === null || weekValid(v.latest_weeks![id])),
    ) ||
    ![v.observation_count, v.revision_count, v.batch_count].every(count) ||
    !dayRangeValid(v.initialization?.start, v.initialization?.end) ||
    v.initialization.years !== 20 ||
    typeof v.web_capabilities?.query !== "boolean" ||
    typeof v.web_capabilities?.collect !== "boolean" ||
    typeof v.source_allowed !== "boolean"
  )
    throw new CpiServiceFailure("claims_status_contract_invalid");
  return {
    ...v,
    latest_months: {},
    week_initialization: {
      ...v.initialization,
      anchor_basis: "twenty_calendar_years",
      years: 20,
    },
    initialization: undefined,
  };
}
export function normalizeClaimsResult(
  v: ClaimsResult,
  scope: CpiScope & { local_only?: boolean },
): RangeResult {
  const selected = scope.series?.split(",") ?? ids,
    history = scope.query_mode === "history" || !!scope.as_of,
    expected = weekCount(scope.start, scope.end) * selected.length;
  const invalid = () => {
    throw new CpiServiceFailure("claims_result_contract_invalid");
  };
  if (
    !v ||
    v.asset_id !== CLAIMS_PROFILE.asset.identity ||
    v.start_date !== scope.start ||
    v.end_date !== scope.end ||
    v.query_mode !== (history ? "history" : "current") ||
    v.local_only !== (history || scope.local_only !== false) ||
    v.as_of_basis !== "available" ||
    (history
      ? Date.parse(v.as_of ?? "") !== Date.parse(scope.as_of ?? "")
      : v.as_of !== null) ||
    !["complete", "partial", "unavailable"].includes(v.status) ||
    !(v.source_failure === null || typeof v.source_failure === "string") ||
    !Array.isArray(v.rows) ||
    v.rows.length !== expected ||
    !v.coverage ||
    v.coverage.expected !== expected ||
    ![
      v.coverage.available,
      v.coverage.missing_rows,
      v.coverage.provider_missing_values,
      v.provider_requests,
      v.provider_attempts,
    ].every(count) ||
    v.coverage.available +
      v.coverage.missing_rows +
      v.coverage.provider_missing_values !==
      expected ||
    ((history || scope.local_only !== false) &&
      (v.provider_requests !== 0 || v.provider_attempts !== 0))
  )
    invalid();
  const keys = new Set<string>();
  for (const r of v.rows) {
    const key = `${r.series_id}:${r.week_ending}`;
    if (
      !selected.includes(r.series_id) ||
      !weekValid(r.week_ending) ||
      r.week_ending < scope.start ||
      r.week_ending > scope.end ||
      keys.has(key) ||
      r.unit !== "Number" ||
      r.statistic !==
        (r.series_id === "IC4WSA" || r.series_id === "CC4WSA"
          ? "official_four_week_average"
          : "weekly_level") ||
      (r.value === null
        ? !["local_not_provided", "provider_missing"].includes(
            r.missing_reason ?? "",
          )
        : r.missing_reason !== null) ||
      (r.series_id === "IC4WSA" || r.series_id === "CC4WSA"
        ? r.local_four_week_average !== undefined
        : !r.local_four_week_average) ||
      (r.provenance?.origin === "upstream" &&
        (history || scope.local_only !== false)) ||
      r.provider !== "FRED" ||
      r.original_agency !== "DOL-ETA" ||
      ![
        r.value,
        r.weekly_change?.value,
        r.weekly_change_ratio?.value,
        ...(r.local_four_week_average ? [r.local_four_week_average.value] : []),
      ].every(decimal) ||
      !Array.isArray(r.related_releases) ||
      !r.related_releases.every(
        (l) =>
          l.event_id.startsWith("dol-claims-") &&
          l.reference_week === r.week_ending &&
          l.association === "scope_only" &&
          l.continuous_version_bound === false &&
          Array.isArray(l.roles) &&
          (l.scheduled_publication_at === null ||
            (l.roles.includes("actual") &&
              l.schedule_basis === "embargo_header" &&
              Number.isFinite(Date.parse(l.scheduled_publication_at)))),
      )
    )
      invalid();
    keys.add(key);
  }
  const points: RangePoint[] = v.rows.map((r) => ({
    ...r,
    period: r.week_ending,
    availability: r.value === null ? "missing" : "available",
    origin: r.provenance?.origin,
    weekly_change: r.weekly_change.value,
    weekly_change_ratio: r.weekly_change_ratio.value,
    local_four_week_average: r.local_four_week_average?.value ?? null,
    related_release_events: r.related_releases.map((l) => ({
      ...l,
      source_version: { ...l.source_version, source: "DOL-ETA" },
      evidence_ids: [l.evidence_id],
      reason: "continuous_version_unbound",
    })),
    claims_plans: r.related_releases
      .filter((l) => l.scheduled_publication_at)
      .map((l) => l.scheduled_publication_at!),
  }));
  return {
    start_period: v.start_date,
    end_period: v.end_date,
    series_ids: selected,
    points,
    missing: [],
    calculation_missing: v.rows.flatMap((r) =>
      (
        [
          "weekly_change",
          "weekly_change_ratio",
          "local_four_week_average",
        ] as const
      ).flatMap((metric) => {
        const d = r[metric];
        return d?.missing_reason
          ? [
              {
                series_id: r.series_id,
                period: r.week_ending,
                metric,
                reason: d.missing_reason,
                required_period: shiftDay(
                  r.week_ending,
                  metric === "local_four_week_average" ? -21 : -7,
                ),
              },
            ]
          : [];
      }),
    ),
    complete: v.coverage.available === expected,
    coverage_complete: v.coverage.missing_rows === 0,
    persisted: false,
    provider_requests: v.provider_requests,
    provider_attempts: v.provider_attempts,
    vintage_mode: "available",
    query_mode: history ? "history" : "current",
    as_of: v.as_of,
    source_failure: v.source_failure
      ? { classification: v.source_failure, http_status: null, retry_at: null }
      : null,
  };
}
export async function queryClaimsRange(
  transport: Transport,
  scope: CpiScope & { local_only?: boolean },
  signal: AbortSignal,
  onExchange?: (v: CpiHttpExchange) => void,
) {
  return normalizeClaimsResult(
    await requestCpiJson<ClaimsResult>(
      transport,
      buildClaimsQueryRequest(scope).path,
      { signal },
      20000,
      onExchange,
    ),
    scope,
  );
}
export function validClaimsJob(j: CpiJob): boolean {
  const instant = (v: unknown) =>
    typeof v === "string" && Number.isFinite(Date.parse(v));
  const r = j?.result as unknown as {
    coverage?: ClaimsResult["coverage"];
    series_coverage?: Record<
      string,
      {
        latest_week: string | null;
        available: number;
        expected: number;
        missing_weeks: string[];
        missing_weeks_total: number;
        missing_weeks_truncated: boolean;
        trailing_missing_weeks: string[];
        trailing_missing_weeks_total: number;
        trailing_missing_weeks_truncated: boolean;
      }
    >;
    readback?: { verified: boolean; as_of_basis: string };
    provider_requests?: number;
    provider_attempts?: number;
    saved_points?: number;
    saved_revisions?: number;
  } | null;
  const c = r?.coverage;
  return (
    !!j &&
    "asset" in j &&
    j.asset === "claims" &&
    "origin" in j &&
    ["manual", "schedule"].includes(String(j.origin)) &&
    uuidValid(j.request_id) &&
    j.job_id === j.request_id &&
    ["init", "update"].includes(j.action) &&
    dayRangeValid(j.start, j.end) &&
    ["running", "complete", "failed", "unknown"].includes(j.state) &&
    instant(j.created_at) &&
    [j.started_at, j.finished_at].every((v) => v === null || instant(v)) &&
    (j.classification === null || typeof j.classification === "string") &&
    (r === null ||
      (!!r &&
        [
          r.provider_requests,
          r.provider_attempts,
          r.saved_points,
          r.saved_revisions,
        ].every((v) => v === undefined || count(v)))) &&
    (j.state !== "complete" ||
      (!!c &&
        r?.readback?.verified === true &&
        r.readback.as_of_basis === "available" &&
        j.finished_at !== null &&
        [
          c.expected,
          c.available,
          c.missing_rows,
          c.provider_missing_values,
        ].every(count) &&
        c.expected === weekCount(j.start, j.end) * 6 &&
        c.expected ===
          c.available + c.missing_rows + c.provider_missing_values &&
        !!r.series_coverage &&
        Object.keys(r.series_coverage).length === 6 &&
        ids.every((id) => {
          const v = r.series_coverage![id];
          return (
            !!v &&
            (v.latest_week === null || weekValid(v.latest_week)) &&
            [
              v.available,
              v.expected,
              v.missing_weeks_total,
              v.trailing_missing_weeks_total,
            ].every(count) &&
            [v.missing_weeks, v.trailing_missing_weeks].every(
              (ws) =>
                Array.isArray(ws) && ws.length <= 16 && ws.every(weekValid),
            ) &&
            typeof v.missing_weeks_truncated === "boolean" &&
            typeof v.trailing_missing_weeks_truncated === "boolean"
          );
        })))
  );
}
