import type { Transport } from "./api";
import { requestCpiJson, type CpiHttpExchange } from "./cpiTimeSeriesApi";

export const FINANCIAL_PREFIX = "/api/r5/financial/timeseries";
export const FINANCIAL_PAGE_SIZE = 40;
export const FINANCIAL_NAMES: Record<string, string> = {
  DFEDTARL: "政策目标下限",
  DFEDTARU: "政策目标上限",
  EFFR: "有效联邦基金利率",
  SOFR: "担保隔夜融资利率",
  DGS2: "2 年名义收益率",
  DGS10: "10 年名义收益率",
  DFII10: "10 年实际收益率",
  T10YIE: "MDF 通胀补偿",
  BAMLC0A0CM: "投资级 OAS",
  BAMLH0A0HYM2: "高收益 OAS",
};
export const FINANCIAL_FIELDS = [
  { value: "value", label: "原生值（%）" },
  { value: "basis_points", label: "信用利差（bp）" },
] as const;
export type FinancialScope = {
  start: string;
  end: string;
  series: string;
  cutoff: string;
  history?: boolean;
  localOnly?: boolean;
  offset?: number;
};
export type FinancialPoint = {
  series_id: string;
  observation_date: string;
  value: string | null;
  basis_points: string | null;
  state: string;
  origin: string | null;
  revision: number | null;
  observed_at: string | null;
  available_at: string | null;
  source_publication_at: string | null;
  source_available_at: string | null;
  policy_effective_date: string | null;
  raw_locator: string | null;
  metadata_locator: string | null;
  identity: string | null;
  attribution: string;
  original_agency: string;
  distributor: string;
  source_url: string;
  persisted?: boolean;
  query_classification?: string;
  [key: string]: unknown;
};
export type FinancialQueryResult = {
  status: "OK";
  rows: FinancialPoint[];
  as_of: string;
  total: number;
  offset: number;
  next_offset: number | null;
  provider_requests: number;
  query_mode: string;
  upstream: {
    cache_hits: number;
    issues: {
      series_id: string;
      start: string;
      end: string;
      classification: string;
    }[];
  };
  coverage: unknown;
};
export function financialQuerySuffix({
  start,
  end,
  series,
  cutoff,
  history = Boolean(cutoff),
  localOnly = false,
  offset = 0,
}: FinancialScope) {
  if (
    !series ||
    series.split(",").some((s) => !FINANCIAL_NAMES[s]) ||
    new Set(series.split(",")).size !== series.split(",").length
  )
    throw new Error("financial_series_invalid");
  const params = new URLSearchParams({
    start,
    end,
    series,
    offset: String(offset),
    limit: String(FINANCIAL_PAGE_SIZE),
    local_only: String(localOnly || history),
    order: "desc",
  });
  if (history) {
    if (!cutoff) throw new Error("financial_history_cutoff_required");
    params.set("as_of", new Date(cutoff).toISOString());
  }
  return `/query?${params}`;
}
export async function readFinancialQuery(
  transport: Transport,
  scope: FinancialScope,
  signal: AbortSignal,
  onExchange: (value: CpiHttpExchange) => void,
) {
  const result = await requestCpiJson<FinancialQueryResult>(
    transport,
    FINANCIAL_PREFIX + financialQuerySuffix(scope),
    { signal },
    20000,
    onExchange,
  );
  const history = scope.history ?? Boolean(scope.cutoff);
  const selected = scope.series.split(","),
    offset = scope.offset ?? 0;
  const end = Date.parse(scope.end + "T00:00:00Z"),
    start = Date.parse(scope.start + "T00:00:00Z");
  const total = ((end - start) / 86400000 + 1) * selected.length;
  const expected = Math.max(0, Math.min(FINANCIAL_PAGE_SIZE, total - offset));
  if (
    result.status !== "OK" ||
    result.total !== total ||
    result.offset !== offset ||
    result.rows?.length !== expected ||
    result.next_offset !==
      (offset + FINANCIAL_PAGE_SIZE < total
        ? offset + FINANCIAL_PAGE_SIZE
        : null) ||
    result.query_mode !==
      (history ? "history" : scope.localOnly ? "local" : "current") ||
    !Number.isFinite(Date.parse(result.as_of)) ||
    !Number.isInteger(result.provider_requests) ||
    result.provider_requests < 0 ||
    (history &&
      (result.provider_requests !== 0 ||
        Date.parse(result.as_of) !== Date.parse(scope.cutoff))) ||
    result.rows.some((r, i) => {
      const index = offset + i;
      const at = new Date(end - Math.floor(index / selected.length) * 86400000)
        .toISOString()
        .slice(0, 10);
      return (
        r.series_id !== selected[index % selected.length] ||
        r.observation_date !== at ||
        typeof r.state !== "string" ||
        (r.value !== null &&
          (typeof r.value !== "string" ||
            !/^-?\d+(?:\.\d+)?$/.test(r.value))) ||
        (history &&
          (r.origin === "upstream" ||
            (r.value !== null &&
              (!r.available_at ||
                Date.parse(r.available_at) > Date.parse(result.as_of)))))
      );
    })
  )
    throw new Error("financial_query_scope_mismatch");
  return result;
}
