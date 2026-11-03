import type { CpiPoint, CpiResult } from "./cpiTimeSeriesApi";
export type RangePoint = Omit<CpiPoint, "month"> & {
  period: string;
  weekly_change?: string | null;
  weekly_change_ratio?: string | null;
  local_four_week_average?: string | null;
  claims_plans?: string[];

  missing_reason?: string | null;
  change_1q_ratio?: string | null;
  change_4q_ratio?: string | null;
  calendar_plans?: {
    estimate_kind: string;
    release_at: string;
    status: string;
  }[];
};
export type RangeGap = { series_id: string; period: string; reason?: string };
export type RangeResult = Omit<
  CpiResult,
  | "points"
  | "missing"
  | "calculation_missing"
  | "start_month"
  | "end_month"
  | "provider_attempts"
> & {
  provider_attempts: number | null;
  start_period: string;
  end_period: string;
  points: RangePoint[];
  missing: RangeGap[];
  calculation_missing: (RangeGap & {
    metric: string;
    required_period: string;
  })[];
};
export function monthlyRangeResult(result: CpiResult): RangeResult {
  return {
    ...result,
    start_period: result.start_month,
    end_period: result.end_month,
    points: result.points.map((point) => ({ ...point, period: point.month })),
    missing: result.missing.map((gap) => ({ ...gap, period: gap.month })),
    calculation_missing: result.calculation_missing.map((gap) => ({
      ...gap,
      period: gap.month,
      required_period: gap.required_month,
    })),
  };
}
