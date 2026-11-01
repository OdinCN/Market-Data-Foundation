import { shiftMonth } from "./cpiTimeSeriesApi";
import type { CpiPoint, CpiResult, CpiScope } from "./cpiTimeSeriesApi";
type Calculation = { value: string | null; reason: string | null };
type RetailMonth = {
  month: string;
  headline_level: Calculation;
  control_level: Calculation;
  headline_rates: Record<string, Calculation>;
  control_rates: Record<string, Calculation>;
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
  const numeric = (v: unknown) =>
    v === null || (typeof v === "string" && /^-?\d+(\.\d+)?$/.test(v));
  if (
    !raw ||
    raw.start_month !== scope.start ||
    raw.end_month !== scope.end ||
    raw.persisted !== false ||
    raw.units?.levels !== "million_current_usd" ||
    raw.units.rates !== "ratio" ||
    raw.units.official_headline_mpcsm !== "percent" ||
    !Array.isArray(raw.points)
  )
    throw new Error("retail_result_unknown");
  const selected = ["headline", "control"].filter(
    (id) =>
      scope.basket === "all" ||
      (id === "control" ? "core" : id) === scope.basket,
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
      !numeric(month.official_headline_mpcsm?.value) ||
      month.official_headline_mpcsm.unit !== "percent"
    )
      throw new Error("retail_month_unknown");
    for (const id of selected) {
      const level =
        id === "headline" ? month.headline_level : month.control_level;
      const rates =
        id === "headline" ? month.headline_rates : month.control_rates;
      if (
        !level ||
        !numeric(level.value) ||
        !rates ||
        ![1, 3, 6, 12].every((h) => rates[h] && numeric(rates[h].value))
      )
        throw new Error("retail_calculation_unknown");
      if (level.value === null) {
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
        index_value: level.value,
        mom_pct: null,
        yoy_pct: null,
        official_mpcsm_percent:
          id === "headline" ? month.official_headline_mpcsm.value : null,
      };
      for (const h of [1, 3, 6, 12] as const) {
        const metric = `change_${h}m_ratio` as const;
        point[metric] = rates[h].value;
        if (rates[h].value === null)
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
    complete: !missing.length && !gaps.length,
    coverage_complete: raw.points.every((p) =>
      p.source_cells.every(
        (c) => c.status !== "missing" && c.status !== "not_provided",
      ),
    ),
    vintage_mode: raw.as_of_basis,
    measure: scope.measure,
  };
}
