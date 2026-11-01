import { afterEach, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { releaseLink } from "./macroReleaseLinks";
import { route } from "./workflowRoute";
import { EMPLOYMENT_PROFILE } from "./macroTimeSeriesProfile";
import { CpiExecutionDetail } from "./CpiStrategyWorkbench";

afterEach(() => {
  cleanup();
  location.hash = "";
});
it("opens an evidence link inside its existing asset pane and preserves the event identity", () => {
  location.hash = releaseLink(
    EMPLOYMENT_PROFILE,
    "bls:employment-situation:2026-10-02",
    "evidence-1",
  );
  const parsed = route();
  expect(parsed.invalid).toBe(false);
  expect(parsed.view).toBe("assets");
  expect(parsed.fields.get("asset")).toBe(EMPLOYMENT_PROFILE.asset.identity);
  expect(parsed.fields.get("release_event")).toBe(
    "bls:employment-situation:2026-10-02",
  );
  expect(parsed.fields.get("release_evidence")).toBe("evidence-1");
});
it("keeps numeric completion and qualified material absence separate in execution details", () => {
  render(
    <CpiExecutionDetail
      profile={EMPLOYMENT_PROFILE}
      execution={{
        id: "synthetic-job",
        origin: "automatic",
        action: "update",
        start: "2026-09",
        end: "2026-09",
        created_at: null,
        started_at: null,
        finished_at: null,
        state: "complete",
        classification: null,
        result: {
          coverage_complete: true,
          release_material: {
            state: "partial",
            reasons: ["http_403"],
            event_ids: ["bls:employment-situation:2026-10-02"],
            evidence_ids: ["retained-proof"],
          },
        },
      }}
    />,
  );
  expect(screen.getByText("已完成")).toBeTruthy();
  const material = screen.getByRole("region", { name: "发布材料执行结果" });
  expect(material.textContent).toContain("连续数值结果另列");
  expect(material.textContent).toContain("官方档案拒绝访问");
  expect(material.textContent).not.toContain("http_403");
  expect(
    screen
      .getByRole("link", { name: "打开发布事件与证据" })
      .getAttribute("href"),
  ).toContain("release_evidence=retained-proof");
});
