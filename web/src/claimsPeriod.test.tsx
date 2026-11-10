import "./offlineTestSetup";
import { expect, it, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import {
  claimsPlanPeriod,
  claimsExecutionPeriod,
  claimsActualsVerified,
  CLAIMS_IDS,
  claimsLatestDataText,
} from "./claimsPeriod";
import { planPeriod, autoCollectionDiagnosis } from "./autoCollectionDiagnosis";
import { cpiExecutionStateText } from "./CpiStrategyWorkbench";
import {
  CLAIMS_PROFILE,
  GDP_PROFILE,
  CPI_PROFILE,
} from "./macroTimeSeriesProfile";
import { ClaimsCollectionDetails } from "./ClaimsCollectionDetails";
afterEach(cleanup);
const weeks = Object.fromEntries(
  CLAIMS_IDS.map((s) => [s, s.startsWith("IC") ? "2026-10-31" : "2026-10-24"]),
);
const result = {
  status: "partial",
  material: {
    status: "available",
    readback_verified: true,
    event_id: "dol-claims-2026-11-05",
    actual_weeks: weeks,
  },
  readback: {
    verified: true,
    as_of_basis: "available",
    actuals_verified: false,
    actual_weeks: {},
  },
  coverage: { expected: 48, available: 39, missing_rows: 9 },
};
const target = {
  key: "dol-claims-week-2026-11-05",
  event_id: "dol-claims-2026-11-05",
  source_date: "2026-11-05",
  release_at: "2026-11-05T13:30:00Z",
  target: {
    state: "awaiting_provider",
    attempts: 4,
    next_attempt_at: "2026-11-06T13:30:00Z",
    result,
  },
  history: { state: "pending", attempts: 0 },
};
it("uses the release event date and independently proved initial/continued weeks everywhere", () => {
  const text = "发布 2026-11-05 · 初请参考周 2026-10-31；续请参考周 2026-10-24";
  expect(claimsPlanPeriod(target)).toBe(text);
  expect(planPeriod(target)).toBe(text);
  expect(claimsExecutionPeriod(result)).toBe(text);
  const future = {
    ...target,
    event_id: "dol-claims-2026-11-12",
    source_date: "2026-11-12",
    release_at: "2026-11-12T13:30:00Z",
    target: { state: "pending", attempts: 0 },
  };
  expect(claimsPlanPeriod(future)).toBe(
    "发布 2026-11-12 · 初请参考周 待报告确认；续请参考周 待报告确认",
  );
  expect(planPeriod({ reference_month: "2026-09" })).toBe("2026-09");
  expect(
    planPeriod({ reference_quarter: "2026-Q3", estimate_kind: "second" }),
  ).toBe("2026-Q3 · second");
});
it("a complete range readback cannot impersonate current actuals, while the normal one-week lag is not a missing release", () => {
  expect(cpiExecutionStateText("complete", result, CLAIMS_PROFILE)).toBe(
    "执行完成（本次发布仍有缺项）",
  );
  expect(claimsActualsVerified(result)).toBe(false);
  const verified = {
    ...result,
    readback: {
      ...result.readback,
      actuals_verified: true,
      actual_weeks: weeks,
    },
  };
  expect(claimsActualsVerified(verified)).toBe(true);
  expect(cpiExecutionStateText("complete", verified, CLAIMS_PROFILE)).toBe(
    "执行完成（本次发布参考周已核实）",
  );
  expect(cpiExecutionStateText("running", verified, CLAIMS_PROFILE)).toBe(
    "执行中",
  );
  expect(
    cpiExecutionStateText(
      "complete",
      { readback: { verified: true } },
      CPI_PROFILE,
    ),
  ).toBe("已完成");
  expect(cpiExecutionStateText("running", {}, GDP_PROFILE)).toBeTruthy();
});
it("diagnoses and locates the exact release and missing actuals rather than an unrelated success", () => {
  const entry: any = {
    saving: false,
    uncertain: false,
    resource: { stale: false },
    data: {
      enabled: true,
      control: { online: true },
      schedule: { calendar_status: "fresh", targets: [target] },
    },
  };
  const d = autoCollectionDiagnosis(entry, Date.parse("2026-11-06T09:00:00Z"));
  expect(d.issues[0].summary).toContain("发布 2026-11-05");
  expect(d.issues[0].reason).toContain("初请/续请参考周");
  expect(d.issues[0].targetKey).toBe(target.key);
  expect(d.issues[0].summary).not.toContain("期间未知");
});
it("labels calendar-range trailing slots beyond each report week independently from actual missing keys", () => {
  render(
    <ClaimsCollectionDetails
      result={{
        ...result,
        series_coverage: {
          CCSA: {
            latest_week: "2026-10-24",
            available: 7,
            expected: 8,
            missing_weeks: ["2026-10-31"],
            missing_weeks_total: 1,
            trailing_missing_weeks: ["2026-10-31"],
            trailing_missing_weeks_total: 1,
          },
        },
      }}
    />,
  );
  expect(screen.getByText(/发布日期与参考周分别列示/).textContent).toContain(
    "续请参考周 2026-10-24",
  );
  expect(screen.getByText(/不作为本次发布漏采/)).toBeTruthy();
});

it("shows both locally held weeks without assuming the continued series has caught up", () => {
  expect(claimsLatestDataText(weeks)).toBe("初请 2026-10-31；续请 2026-10-24");
  expect(claimsLatestDataText(undefined)).toBe("初请 待核实；续请 待核实");
});
