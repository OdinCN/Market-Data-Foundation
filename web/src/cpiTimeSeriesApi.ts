import { CPI_PROFILE, type MacroProfile } from "./macroTimeSeriesProfile";
import type { Transport } from "./api";
import { useVisiblePolling } from "./useVisiblePolling";

export const CPI_TIMESERIES = CPI_PROFILE.basePath;
export const cpiSeries = CPI_PROFILE.series;
export const monthValid = (value: unknown): value is string =>
  typeof value === "string" && /^\d{4}-(0[1-9]|1[0-2])$/.test(value);
export const uuidValid = (value: unknown): value is string =>
  typeof value === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
export const rangeValid = (start: string, end: string) => {
  const number = (m: string) => Number(m.slice(0, 4)) * 12 + Number(m.slice(5));
  return (
    monthValid(start) &&
    monthValid(end) &&
    start >= "1901-01" &&
    start <= end &&
    number(end) - number(start) < 2400
  );
};
export const monthCount = (start: string, end: string) =>
  (Number(end.slice(0, 4)) - Number(start.slice(0, 4))) * 12 +
  Number(end.slice(5)) -
  Number(start.slice(5)) +
  1;
export function shiftMonth(month: string, by: number) {
  const n = Number(month.slice(0, 4)) * 12 + Number(month.slice(5)) - 1 + by;
  return `${Math.floor(n / 12)}-${String((n % 12) + 1).padStart(2, "0")}`;
}
export type CpiStatus = {
  initialization?: {
    preset_id: string;
    months: number;
    start: string | null;
    end: string | null;
    available: boolean;
    reason: string | null;
    anchor_basis: string;
  };
  latest_months: Record<string, string | null>;
  observation_count: number;
  revision_count: number;
  batch_count: number;
  web_capabilities: { query: boolean; collect: boolean; reason: string | null };
  collection_scope?: { max_months?: number | null; series: string };
};
export type CpiAvailability = {
  refresh?: () => void;
  data: CpiStatus | null;
  legacy: boolean;
  busy: boolean;
  failure: string | null;
};
export class CpiServiceFailure extends Error {
  constructor(
    readonly classification: string,
    readonly status?: number,
  ) {
    super(classification);
  }
}
const classification = (value: unknown) =>
  typeof value === "string" && /^[a-z0-9_]{1,100}$/.test(value)
    ? value
    : "cpi_response_unknown";
export type CpiHttpExchange = {
  method: "GET";
  url: string;
  status: number;
  bodyText: string;
  elapsedMs: number;
  completedAt: string;
};
export async function cpiRequest<T>(
  transport: Transport,
  path: string,
  init: RequestInit = {},
  timeoutMs = 20000,
  profile: MacroProfile = CPI_PROFILE,
): Promise<T> {
  return requestCpiJson<T>(
    transport,
    `${profile.basePath}${path}`,
    init,
    timeoutMs,
  );
}
async function requestCpiJson<T>(
  transport: Transport,
  path: string,
  init: RequestInit,
  timeoutMs: number,
  onExchange?: (exchange: CpiHttpExchange) => void,
): Promise<T> {
  try {
    const startedAt = performance.now();
    let url = path;
    if (onExchange && typeof window !== "undefined") {
      try {
        url = new URL(path, window.location.origin).href;
      } catch {
        // Non-browser hosts may have no usable origin.
      }
    }
    const timeout = AbortSignal.timeout(timeoutMs);
    const signal = init.signal
      ? AbortSignal.any([init.signal, timeout])
      : timeout;
    const response = await transport(path, {
      ...init,
      method: init.method ?? "GET",
      cache: "no-store",
      credentials: "same-origin",
      signal,
    });
    let bodyText: string | undefined;
    if (onExchange) {
      bodyText = await response.text();
      if (!signal.aborted) {
        try {
          // Observers must not replace HTTP, parse or schema outcomes.
          void Promise.resolve(
            onExchange({
              method: "GET",
              url,
              status: response.status,
              bodyText,
              elapsedMs: Math.max(0, performance.now() - startedAt),
              completedAt: new Date().toISOString(),
            }),
          ).catch(() => {});
        } catch {
          // A synchronous observer failure is isolated too.
        }
      }
    }
    if (response.status === 404)
      throw new CpiServiceFailure("cpi_host_not_found", 404);
    const body =
      bodyText === undefined ? await response.json() : JSON.parse(bodyText);
    if (!response.ok)
      throw new CpiServiceFailure(
        classification(body?.classification),
        response.status,
      );
    return body as T;
  } catch (error) {
    if (error instanceof CpiServiceFailure) throw error;
    throw new CpiServiceFailure("cpi_response_unknown");
  }
}
export function useCpiAvailability(
  transport: Transport,
  generation: number,
  enabled = true,
  polling = true,
  profile: MacroProfile = CPI_PROFILE,
): CpiAvailability {
  const resource = useVisiblePolling<CpiStatus | "legacy">(
    async (signal) => {
      let value: CpiStatus;
      try {
        value = await cpiRequest<CpiStatus>(
          transport,
          "/status",
          { signal },
          20000,
          profile,
        );
      } catch (error) {
        if (
          (profile.id === "cpi" || profile.id === "pce") &&
          error instanceof CpiServiceFailure &&
          (error.status === 404 ||
            (profile.id === "pce" &&
              error.status === 503 &&
              error.classification === "pce_timeseries_not_enabled"))
        )
          return "legacy";
        throw error;
      }
      if (
        !value ||
        typeof value.web_capabilities?.query !== "boolean" ||
        typeof value.web_capabilities?.collect !== "boolean" ||
        !value.latest_months ||
        typeof value.latest_months !== "object" ||
        !Object.keys(value.latest_months).every((id) =>
          profile.series.some((s) => s[0] === id),
        ) ||
        !Object.values(value.latest_months).every(
          (m) => m === null || monthValid(m),
        ) ||
        ![
          value.observation_count,
          value.revision_count,
          value.batch_count,
        ].every((n) => Number.isInteger(n) && n >= 0) ||
        (value.collection_scope !== undefined &&
          (!value.collection_scope ||
            typeof value.collection_scope !== "object" ||
            Array.isArray(value.collection_scope) ||
            (value.collection_scope.max_months != null &&
              (!Number.isInteger(value.collection_scope.max_months) ||
                value.collection_scope.max_months < 1))))
      )
        throw new CpiServiceFailure("cpi_status_unknown");
      return value;
    },
    [transport, generation, enabled, profile.id],
    enabled,
    undefined,
    polling,
  );
  return {
    ...resource,
    legacy: resource.data === "legacy",
    data: resource.data === "legacy" ? null : resource.data,
  };
}
export function latestMonth(status: CpiStatus | null) {
  return (
    Object.values(status?.latest_months ?? {})
      .filter(monthValid)
      .sort()
      .at(-1) ?? ""
  );
}
export type CpiScope = {
  start: string;
  end: string;
  series?: string;
  basket: string;
  adjustment: string;
  measure: string;
  vintage_mode?: string;
  query_mode?: "current" | "history";
  as_of_basis?: "available";
  as_of?: string;
};
export type CpiPublicationEvent = {
  reference_month?: string;
  time?: string | null;
  publication_time?: string | null;
  status?: string;
  state?: string;
  evidence?: unknown;
};
export type CpiPoint = {
  series_id: string;
  month: string;
  value?: string | number | null;
  net_change_1m_thousand_jobs?: string | number | null;
  change_1m_percentage_points?: string | number | null;
  change_1m_hours?: string | number | null;
  value_unit?: string;
  // Employment omits index_value; only AHE carries ratios and percent changes.
  // Inflation response validation below still requires its complete field set.
  index_value?: string | number | null;
  mom_pct?: string | number | null;
  yoy_pct?: string | number | null;
  change_1m_ratio?: string | number | null;
  change_3m_ratio?: string | number | null;
  change_6m_ratio?: string | number | null;
  change_12m_ratio?: string | number | null;
  observed_at?: string;
  available_at?: string | null;
  origin?: string;
  availability?: string;
  publication_event?: CpiPublicationEvent | null;
};
export type CpiGap = { series_id: string; month: string; reason?: string };
export type CpiResult = {
  start_month: string;
  end_month: string;
  series_ids: string[];
  points: CpiPoint[];
  missing: CpiGap[];
  calculation_missing: (CpiGap & { metric: string; required_month: string })[];
  complete: boolean;
  coverage_complete: boolean;
  persisted: boolean;
  provider_requests: number;
  provider_attempts: number;
  vintage_mode: string;
  as_of: string | null;
  measure?: string;
  query_mode?: "current" | "history";
  as_of_basis?: string;
  publication_events?: CpiPublicationEvent[];
};
export function buildCpiQueryRequest(
  scope: CpiScope,
  profile: MacroProfile = CPI_PROFILE,
): {
  method: "GET";
  path: string;
} {
  const history = scope.query_mode === "history" || !!scope.as_of;
  const parameters = new URLSearchParams({
    start: scope.start,
    end: scope.end,
    ...(profile.id === "employment"
      ? { series: scope.series ?? profile.series.map((s) => s[0]).join(",") }
      : { basket: scope.basket, adjustment: scope.adjustment }),
    measure: scope.measure,
    query_mode: history ? "history" : "current",
    local_only: history ? "true" : "false",
    ...(history
      ? {
          vintage_mode: "available",
          as_of_basis: "available",
          ...(scope.as_of ? { as_of: scope.as_of } : {}),
        }
      : {}),
  });
  return { method: "GET", path: `${profile.basePath}/query?${parameters}` };
}
export async function queryCpiRange(
  transport: Transport,
  scope: CpiScope,
  signal: AbortSignal,
  onExchange?: (exchange: CpiHttpExchange) => void,
  profile: MacroProfile = CPI_PROFILE,
) {
  const history = scope.query_mode === "history" || !!scope.as_of;
  const basis = history ? "available" : "observed";
  const request = buildCpiQueryRequest(scope, profile);
  const result = await requestCpiJson<CpiResult>(
    transport,
    request.path,
    { method: request.method, signal },
    history ? 20000 : 180000,
    onExchange,
  );
  const selected = profile.series
    .filter((s) =>
      profile.id === "employment"
        ? (
            scope.series?.split(",") ?? profile.series.map((item) => item[0])
          ).includes(s[0])
        : ["all", s[2]].includes(scope.basket) &&
          ["all", s[3]].includes(scope.adjustment),
    )
    .map((s) => s[0] as string);
  const inScope = (p: CpiGap) =>
    p &&
    selected.includes(p.series_id) &&
    monthValid(p.month) &&
    p.month >= scope.start &&
    p.month <= scope.end;
  const numeric = (v: unknown) =>
    v === null ||
    (typeof v === "number" && Number.isFinite(v)) ||
    (typeof v === "string" && /^-?\d+(\.\d+)?$/.test(v));
  if (
    !result ||
    result.start_month !== scope.start ||
    result.end_month !== scope.end ||
    result.persisted !== false ||
    ![result.provider_requests, result.provider_attempts].every(
      (n) => Number.isInteger(n) && n >= 0,
    ) ||
    (history &&
      (result.provider_requests !== 0 || result.provider_attempts !== 0)) ||
    (result.query_mode !== undefined &&
      result.query_mode !== (history ? "history" : "current")) ||
    (result.as_of_basis !== undefined && result.as_of_basis !== basis) ||
    typeof result.complete !== "boolean" ||
    typeof result.coverage_complete !== "boolean" ||
    result.vintage_mode !== basis ||
    (result.measure !== undefined && result.measure !== scope.measure) ||
    (scope.as_of
      ? Date.parse(result.as_of ?? "") !== Date.parse(scope.as_of)
      : result.as_of !== null) ||
    !Array.isArray(result.series_ids) ||
    result.series_ids.length !== selected.length ||
    !selected.every((s) => result.series_ids.includes(s)) ||
    !Array.isArray(result.points) ||
    !Array.isArray(result.missing) ||
    !Array.isArray(result.calculation_missing) ||
    result.points.some(
      (p) =>
        p.availability === "unpersisted" && (history || p.available_at != null),
    ) ||
    ![...result.points, ...result.missing].every(inScope) ||
    !result.calculation_missing.every(
      (gap) =>
        inScope(gap) &&
        typeof gap.metric === "string" &&
        monthValid(gap.required_month),
    ) ||
    !result.points.every((p) =>
      (profile.id === "employment"
        ? [
            p.value,
            p.net_change_1m_thousand_jobs ?? null,
            p.change_1m_percentage_points ?? null,
            p.change_1m_hours ?? null,
            p.mom_pct ?? null,
            p.yoy_pct ?? null,
          ]
        : [
            p.index_value,
            p.mom_pct,
            p.yoy_pct,
            p.change_1m_ratio,
            p.change_3m_ratio,
            p.change_6m_ratio,
            p.change_12m_ratio,
          ]
      ).every(numeric),
    )
  )
    throw new CpiServiceFailure("cpi_result_scope_unknown");
  const keys = [...result.points, ...result.missing].map(
    (p) => `${p.month}:${p.series_id}`,
  );
  if (
    new Set(keys).size !== keys.length ||
    keys.length !== monthCount(scope.start, scope.end) * selected.length
  )
    throw new CpiServiceFailure("cpi_result_scope_unknown");
  return result;
}
export type CpiJobIntent = {
  series?: string;
  request_id: string;
  action: "init" | "initialize" | "update";
  start: string;
  end: string;
};
export type CpiJob = CpiJobIntent & {
  job_id: string;
  state: "running" | "complete" | "failed" | "unknown";
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  classification: string | null;
  result: {
    provider_requests?: number;
    provider_attempts?: number;
    saved_points?: number;
    saved_revisions?: number;
    coverage_complete?: boolean;
    completion?: string;
    readback?: {
      verified: boolean;
      point_count: number;
      expected_points: number;
      source_unavailable_count: number;
      missing?: { series_id: string; month: string }[];
      source_missing?: { series_id: string; month: string; reason?: string }[];
      source_unavailable?: { series_id: string; month: string }[];
    };
  } | null;
};
export function validJob(job: CpiJob): boolean {
  return (
    !!job &&
    typeof job.job_id === "string" &&
    !!job.job_id &&
    uuidValid(job.request_id) &&
    ["init", "initialize", "update"].includes(job.action) &&
    rangeValid(job.start, job.end) &&
    ["running", "complete", "failed", "unknown"].includes(job.state) &&
    Number.isFinite(Date.parse(job.created_at)) &&
    [job.started_at, job.finished_at].every(
      (v) =>
        v === null || (typeof v === "string" && Number.isFinite(Date.parse(v))),
    ) &&
    (job.classification === null ||
      (typeof job.classification === "string" &&
        /^[a-z0-9_]{1,100}$/.test(job.classification))) &&
    (job.result === null ||
      (typeof job.result === "object" &&
        [
          job.result.provider_requests,
          job.result.provider_attempts,
          job.result.saved_points,
          job.result.saved_revisions,
        ].every((v) => v === undefined || (Number.isInteger(v) && v! >= 0)) &&
        (job.result.coverage_complete === undefined ||
          typeof job.result.coverage_complete === "boolean") &&
        (job.result.completion === undefined ||
          typeof job.result.completion === "string") &&
        (job.result.readback === undefined ||
          (!!job.result.readback &&
            [
              job.result.readback.missing,
              job.result.readback.source_missing,
              job.result.readback.source_unavailable,
            ].every(
              (points) =>
                points === undefined ||
                (Array.isArray(points) &&
                  points.every(
                    (point) =>
                      !!point &&
                      typeof point.series_id === "string" &&
                      monthValid(point.month) &&
                      (!("reason" in point) ||
                        point.reason === undefined ||
                        typeof point.reason === "string"),
                  )),
            ) &&
            typeof job.result.readback.verified === "boolean" &&
            [
              job.result.readback.point_count,
              job.result.readback.expected_points,
              job.result.readback.source_unavailable_count,
            ].every((v) => Number.isInteger(v) && v >= 0)))))
  );
}
export async function readCpiJobs(
  transport: Transport,
  signal?: AbortSignal,
  profile: MacroProfile = CPI_PROFILE,
) {
  const result = await cpiRequest<{ jobs: CpiJob[] }>(
    transport,
    "/jobs",
    {
      signal,
    },
    20000,
    profile,
  );
  if (!Array.isArray(result?.jobs) || !result.jobs.every(validJob))
    throw new CpiServiceFailure("cpi_jobs_unknown");
  return result;
}

export type CpiStrategy = {
  exists: boolean;
  enabled: boolean;
  history_start: string;
  history_start_locked?: boolean;
  worker: { state: string; last_heartbeat_at: string | null };
  next_due: string | null;
  next_release_at?: string | null;
  state?: string;
  execution_state?: string;
  last_result?: Record<string, unknown> | null;
  policy: Record<string, unknown>;
};
export function validStrategy(value: CpiStrategy): boolean {
  const instant = (v: unknown) =>
    v === null || (typeof v === "string" && Number.isFinite(Date.parse(v)));
  return (
    !!value &&
    typeof value.exists === "boolean" &&
    typeof value.enabled === "boolean" &&
    (value.history_start_locked === undefined ||
      typeof value.history_start_locked === "boolean") &&
    (!value.enabled || value.exists) &&
    monthValid(value.history_start) &&
    value.history_start >= "1901-01" &&
    !!value.worker &&
    typeof value.worker.state === "string" &&
    !!value.worker.state &&
    instant(value.worker.last_heartbeat_at) &&
    instant(value.next_due) &&
    !!value.policy &&
    typeof value.policy === "object" &&
    !Array.isArray(value.policy)
  );
}
export async function readCpiStrategy(
  transport: Transport,
  signal?: AbortSignal,
  profile: MacroProfile = CPI_PROFILE,
) {
  const value = await cpiRequest<CpiStrategy>(
    transport,
    "/strategy",
    {
      signal,
    },
    20000,
    profile,
  );
  if (!validStrategy(value))
    throw new CpiServiceFailure("cpi_strategy_unknown");
  return value;
}
export async function saveCpiStrategy(
  transport: Transport,
  enabled: boolean,
  signal?: AbortSignal,
  profile: MacroProfile = CPI_PROFILE,
) {
  const value = await cpiRequest<CpiStrategy>(
    transport,
    "/strategy",
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
      signal,
    },
    20000,
    profile,
  );
  if (!validStrategy(value) || !value.exists || value.enabled !== enabled)
    throw new CpiServiceFailure("cpi_strategy_unknown");
  return value;
}

export type CpiExecution = {
  id: string;
  origin: "manual" | "automatic";
  stage?: string | null;
  action: string;
  start: string | null;
  end: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  state: string;
  classification: string | null;
  result: Record<string, unknown> | null;
};
export async function readCpiExecutions(
  transport: Transport,
  signal?: AbortSignal,
  profile: MacroProfile = CPI_PROFILE,
) {
  const value = await cpiRequest<{ executions: CpiExecution[] }>(
    transport,
    "/executions",
    { signal },
    20000,
    profile,
  );
  const instant = (v: unknown) =>
    v === null || (typeof v === "string" && Number.isFinite(Date.parse(v)));
  if (
    !Array.isArray(value?.executions) ||
    !value.executions.every(
      (e) =>
        !!e &&
        typeof e.id === "string" &&
        !!e.id &&
        ["manual", "automatic"].includes(e.origin) &&
        typeof e.action === "string" &&
        typeof e.state === "string" &&
        [e.start, e.end].every((v) => v === null || monthValid(v)) &&
        [e.created_at, e.started_at, e.finished_at].every(instant) &&
        (e.classification === null ||
          (typeof e.classification === "string" &&
            /^[a-z0-9_]{1,100}$/.test(e.classification))) &&
        (e.result === null ||
          (typeof e.result === "object" && !Array.isArray(e.result))),
    ) ||
    new Set(value.executions.map((e) => e.id)).size !== value.executions.length
  )
    throw new CpiServiceFailure("cpi_executions_unknown");
  return value;
}
