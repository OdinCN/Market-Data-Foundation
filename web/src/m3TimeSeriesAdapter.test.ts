import { describe, it, expect } from "vitest";
import { normalizeM3Result, buildM3QueryRequest } from "./m3TimeSeriesAdapter";
const id = "durable_goods_new_orders";
const scope = {
  start: "2025-01",
  end: "2025-02",
  series: id,
  dataset: "m3" as const,
  basket: "all",
  adjustment: "sa",
  measure: "native",
  query_mode: "current" as const,
};
const data = {
  dataset: "m3",
  report_stage: "full",
  status: "partial",
  query_mode: "current",
  as_of: null,
  as_of_basis: "available",
  local_only: false,
  persisted: false,
  provider_requests: 1,
  provider_attempts: null,
  coverage: { expected: 2, available: 1 },
  source_failure: {
    classification: "census_m3_source_failure",
    http_status: null,
  },
  missing: [
    { series_id: id, month: "2025-02", reason: "local_record_missing" },
  ],
  points: [
    {
      series_id: id,
      month: "2025-01",
      dataset: "m3",
      value: "0",
      status: "available" as const,
      reason: null,
      origin: "local" as const,
      observed_at: "2026-01-01T00:00:00Z",
      available_at: "2026-01-01T00:00:01Z",
      source: {
        unit: "million_current_usd",
        seasonal_adjustment: "SA",
        frequency: "monthly",
        report_stage: "full",
      },
    },
    {
      series_id: id,
      month: "2025-02",
      dataset: "m3",
      value: null,
      status: "missing" as const,
      reason: "local_record_missing",
      origin: "local" as const,
      observed_at: null,
      available_at: null,
      source: {
        unit: "million_current_usd",
        seasonal_adjustment: "SA",
        frequency: "monthly",
        report_stage: "full",
      },
    },
  ],
};
describe("M3 page contract", () => {
  it("preserves zero, missing and unknown attempt count", () => {
    const parsed = normalizeM3Result(data, scope);
    expect(parsed.points[0].value).toBe("0");
    expect(parsed.missing).toHaveLength(1);
    expect(parsed.provider_attempts).toBeNull();
    expect(parsed.source_failure?.classification).toBe(
      "census_m3_source_failure",
    );
  });
  it("rejects wrong stage and previews historical local query", () => {
    expect(() =>
      normalizeM3Result({ ...data, report_stage: "advance" }, scope),
    ).toThrow();
    const path = buildM3QueryRequest({
      ...scope,
      query_mode: "history",
      as_of: "2026-01-01T00:00:00Z",
    }).path;
    expect(path).toContain("local_only=true");
    expect(path).toContain("as_of_basis=available");
  });
});
