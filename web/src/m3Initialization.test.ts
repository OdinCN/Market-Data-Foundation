import { expect, it } from "vitest";
import { validJob, type CpiJob } from "./cpiTimeSeriesApi";
import { CPI_PROFILE, M3_PROFILE } from "./macroTimeSeriesProfile";
import { cpiExecutionStateText } from "./CpiStrategyWorkbench";

it("keeps M3 source waiting distinct without widening old family job contracts", () => {
  const job = {
    request_id: "0403a273-3f4b-48fd-9666-c97ce8ef5b09",
    job_id: "0403a273-3f4b-48fd-9666-c97ce8ef5b09",
    action: "initialize",
    start: "2006-11",
    end: "2026-10",
    state: "waiting",
    created_at: "2026-11-03T17:00:00Z",
    started_at: "2026-11-03T17:00:00Z",
    finished_at: "2026-11-03T17:01:00Z",
    classification: "m3_source_wait",
    result: { provider_requests: 0, provider_attempts: 0, saved_points: 0 },
  } as CpiJob;
  expect(validJob(job, M3_PROFILE)).toBe(true);
  expect(validJob(job, CPI_PROFILE)).toBe(false);
  expect(cpiExecutionStateText(job.state, job.result, M3_PROFILE)).toBe(
    "等待来源准入",
  );
});

it("reports a fully requested initialization with source gaps as complete with gaps", () => {
  const result = {
    coverage_complete: false,
    completion: "source_coverage_partial",
    remaining_requests: 0,
    requested_points: 720,
    readback: {
      source_missing: [
        { series_id: "durable_goods_new_orders", month: "2026-10" },
      ],
      missing: [{ series_id: "durable_goods_new_orders", month: "2026-10" }],
      verified: true,
    },
  };
  expect(
    cpiExecutionStateText("complete", result, M3_PROFILE, {
      start: "2006-11",
      end: "2026-10",
    }),
  ).toBe("已完成（来源有缺口）");
});
