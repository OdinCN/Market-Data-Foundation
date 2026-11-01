import { shiftMonth } from "./cpiTimeSeriesApi";
import type { CpiPoint, CpiResult, CpiScope } from "./cpiTimeSeriesApi";
import { isDecimalString } from "./decimalValue";
type Calculation = { value: string | null; reason: string | null };
type RetailMonth = {
  month: string;
  publication_event?: CpiPoint["publication_event"];
  headline_level: Calculation;
  control_level: Calculation;
  ex_auto_level: Calculation;
  headline_rates: Record<string, Calculation>;
  control_rates: Record<string, Calculation>;
  ex_auto_rates: Record<string, Calculation>;
  official_headline_mpcsm: { value: string | null; unit: string };
  source_cells: { value: string | null; status: string }[];
  input_versions?: Record<
    string,
    { origin?: string; available_at?: string | null }
  >;
};
export function normalizeRetailResult(
  value: unknown,
  scope: CpiScope,
): CpiResult {
  const raw = value as {
    status?: "OK" | "PARTIAL";
    source_failure?: CpiResult["source_failure"];
    source_no_content?: CpiResult["source_no_content"];
    start_month: string;
    end_month: string;
    persisted: boolean;
    provider_requests: number;
    provider_attempts: number;
    as_of: string | null;
    as_of_basis: string;
    query_mode: "current" | "history";
    units: { levels: string; rates: string; official_headline_mpcsm: string };
    points: RetailMonth[];
  };
  const numeric = (v: unknown) => v === null || isDecimalString(v);
  if (
    !raw ||
    raw.start_month !== scope.start ||
    raw.end_month !== scope.end ||
    raw.persisted !== false ||
    raw.units?.levels !== "million_current_usd" ||
    raw.units.rates !== "ratio" ||
    raw.units.official_headline_mpcsm !== "percent" ||
    (raw.status !== undefined && !["OK", "PARTIAL"].includes(raw.status)) ||
    (raw.source_failure != null && raw.status !== "PARTIAL") ||
    (raw.source_no_content !== undefined &&
      (!Array.isArray(raw.source_no_content) ||
        raw.source_no_content.some(
          (event) =>
            !event ||
            event.classification !== "census_retail_no_content" ||
            event.http_status !== 204 ||
            event.response_bytes !== 0 ||
            !/^\d{4}-\d{2}$/.test(event.start_month) ||
            !/^\d{4}-\d{2}$/.test(event.end_month),
        ))) ||
    (raw.status === "PARTIAL" &&
      !raw.source_failure &&
      !raw.source_no_content?.length) ||
    (raw.source_failure != null &&
      (typeof raw.source_failure.classification !== "string" ||
        !/^census_retail_(http_failure|timeout|transport_failure)$/.test(
          raw.source_failure.classification,
        ) ||
        !(
          raw.source_failure.http_status === null ||
          (Number.isInteger(raw.source_failure.http_status) &&
            raw.source_failure.http_status >= 100 &&
            raw.source_failure.http_status <= 599)
        ) ||
        !(
          raw.source_failure.retry_at === null ||
          (typeof raw.source_failure.retry_at === "string" &&
            Number.isFinite(Date.parse(raw.source_failure.retry_at)))
        ))) ||
    !Array.isArray(raw.points)
  )
    throw new Error("retail_result_unknown");
  const selected = ["headline", "control", "ex_auto"].filter(
    (id) =>
      scope.basket === "all" ||
      scope.basket.split(",").includes(id === "control" ? "core" : id),
  );
  const points: CpiPoint[] = [];
  const missing: CpiResult["missing"] = [];
  const gaps: CpiResult["calculation_missing"] = [];
  for (const month of raw.points) {
    if (
      !month ||
      month.month < scope.start ||
      month.month > scope.end ||
      !Array.isArray(month.source_cells) ||
      month.source_cells.length !== 6 ||
      ((!scope.columns || scope.columns.includes("official_mpcsm_percent")) &&
        (!numeric(month.official_headline_mpcsm?.value) ||
          month.official_headline_mpcsm.unit !== "percent"))
    )
      throw new Error("retail_month_unknown");
    for (const id of selected) {
      const level =
        id === "headline"
          ? month.headline_level
          : id === "control"
            ? month.control_level
            : month.ex_auto_level;
      const rates =
        id === "headline"
          ? month.headline_rates
          : id === "control"
            ? month.control_rates
            : month.ex_auto_rates;
      if (
        ((!scope.columns || scope.columns.includes("index_value")) &&
          (!level || !numeric(level.value))) ||
        !rates ||
        ![1, 3, 6, 12].every(
          (h) =>
            (scope.columns && !scope.columns.includes(`change_${h}m_ratio`)) ||
            (rates[h] && numeric(rates[h].value)),
        )
      )
        throw new Error("retail_calculation_unknown");
      if (level?.value === null) {
        missing.push({
          series_id: id,
          month: month.month,
          reason: level.reason ?? "source_unavailable",
        });
        continue;
      }
      const upstream = Object.values(month.input_versions ?? {}).some(
        (v) => v.origin === "upstream",
      );
      const point: CpiPoint = {
        origin: upstream ? "upstream" : "local",
        availability: upstream ? "unpersisted" : "available",
        available_at: null,
        series_id: id,
        month: month.month,
        publication_event: month.publication_event,
        index_value: level?.value ?? null,
        mom_pct: null,
        yoy_pct: null,
        official_mpcsm_percent:
          id === "headline"
            ? (month.official_headline_mpcsm?.value ?? null)
            : null,
      };
      for (const h of [1, 3, 6, 12] as const) {
        const metric = `change_${h}m_ratio` as const;
        point[metric] = rates[h]?.value ?? null;
        if (rates[h]?.value === null)
          gaps.push({
            series_id: id,
            month: month.month,
            metric,
            required_month: shiftMonth(month.month, -h),
            reason: rates[h].reason ?? "source_unavailable",
          });
      }
      points.push(point);
    }
  }
  return {
    ...raw,
    series_ids: selected,
    points,
    missing,
    calculation_missing: gaps,
    complete: raw.status !== "PARTIAL" && !missing.length && !gaps.length,
    coverage_complete: raw.points.every((p) =>
      p.source_cells.every(
        (c) => c.status !== "missing" && c.status !== "not_provided",
      ),
    ),
    vintage_mode: raw.as_of_basis,
    measure: scope.measure,
  };
}
