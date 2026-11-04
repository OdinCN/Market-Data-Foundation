import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { M3ComparisonTable, m3MomText } from "./M3ComparisonTable";
import { normalizeM3Result, buildM3QueryRequest } from "./m3TimeSeriesAdapter";
const id = "durable_goods_new_orders";
const scope = {
  start: "2026-09",
  end: "2026-09",
  series: id,
  dataset: "both" as const,
  basket: "all",
  adjustment: "sa",
  measure: "native",
  query_mode: "current" as const,
};
function report(dataset: string, value: string | null) {
  return {
    dataset,
    report_stage: dataset === "m3" ? "full" : "advance",
    status: value === null ? "unavailable" : "complete",
    query_mode: "current",
    as_of: null,
    as_of_basis: "available",
    local_only: false,
    persisted: false,
    provider_requests: 0,
    provider_attempts: 0,
    coverage: { expected: 1, available: value === null ? 0 : 1 },
    source_failure: null,
    missing:
      value === null
        ? [{ series_id: id, month: "2026-09", reason: "source_row_absent" }]
        : [],
    points: [
      {
        dataset,
        series_id: id,
        month: "2026-09",
        value,
        status: value === null ? "missing" : "available",
        reason: value === null ? "source_row_absent" : null,
        origin: "local",
        observed_at: "2026-11-03T00:00:00Z",
        available_at: "2026-11-03T00:01:00Z",
        source: {
          unit: "million_current_usd",
          seasonal_adjustment: "SA",
          frequency: "monthly",
          report_stage: dataset === "m3" ? "full" : "advance",
        },
      },
    ],
  };
}
function joint(
  full: string | null,
  advance: string | null,
  difference: string | null,
) {
  return {
    dataset: "both",
    reports: { m3: report("m3", full), advm3: report("advm3", advance) },
    comparison_basis: "current_source_snapshots_not_official_event_revisions",
    provider_requests: 0,
    provider_attempts: 0,
    persisted: false,
    coverage: {
      expected: 2,
      available: Number(full !== null) + Number(advance !== null),
    },
    comparisons: [
      {
        month: "2026-09",
        series_id: id,
        m3_value: full,
        advm3_value: advance,
        difference,
        reason:
          full === null || advance === null
            ? "one_or_both_reports_missing"
            : null,
        comparison_basis:
          "current_source_snapshots_not_official_event_revisions",
        difference_direction: "m3_minus_advm3",
        official_event_revision: null,
      },
    ],
  };
}
afterEach(cleanup);
it("compares Decimal values and actual zero with explicit non-event semantics", () => {
  const result = normalizeM3Result(
    joint("0", "1.000000001", "-1.000000001") as never,
    scope,
  );
  expect(buildM3QueryRequest(scope).path).toContain("dataset=both");
  expect(result.m3_coverage?.available).toBe(2);
  render(<M3ComparisonTable rows={result.m3_comparisons!} />);
  expect(screen.getByText("-1.000000001")).toBeTruthy();
  expect(screen.getByText(/不代表官方当次修订/)).toBeTruthy();
});
it("retains one report when the other is absent and rejects a fabricated zero difference", () => {
  const result = normalizeM3Result(joint(null, "100", null) as never, scope);
  expect(result.m3_comparisons![0].advm3_value).toBe("100");
  expect(result.m3_comparisons![0].difference).toBeNull();
  expect(() =>
    normalizeM3Result(joint(null, "100", "0") as never, scope),
  ).toThrow();
});

it("formats both report MoM as two-digit percent with Chinese gaps and obeys selected columns", () => {
  const rows = [
    {
      ...joint("10", "12", "-2").comparisons[0],
      m3_mom_percent: "1.234567",
      advm3_mom_percent: null,
      advm3_mom_reason: "zero_denominator",
    },
  ];
  const { rerender } = render(
    <M3ComparisonTable rows={rows} columns={["mom_percent"]} />,
  );
  expect(screen.getByText("1.23%")).toBeTruthy();
  expect(screen.getByText("上月值为零，无法计算环比")).toBeTruthy();
  expect(
    screen.queryByRole("columnheader", { name: "来源快照差额" }),
  ).toBeNull();
  rerender(<M3ComparisonTable rows={rows} columns={["value"]} />);
  expect(screen.queryByText("1.23%")).toBeNull();
  expect(
    screen.getByRole("columnheader", { name: "来源快照差额" }),
  ).toBeTruthy();
});

it("rounds the shared single/joint MoM display using Decimal digits", () => {
  expect(m3MomText("1.005000")).toBe("1.00%");
  expect(m3MomText("1.015000")).toBe("1.02%");
  expect(m3MomText("-0.000001")).toBe("0.00%");
  expect(m3MomText(null, "missing_denominator")).toBe("缺少同报告上月值");
});
