import { afterEach, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { releaseEvidenceLinks, releaseLink } from "./macroReleaseLinks";
import { route } from "./workflowRoute";
import { EMPLOYMENT_PROFILE, M3_PROFILE } from "./macroTimeSeriesProfile";
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

it("pairs legacy M3 joint proofs by report rather than independently sorted aggregate arrays", () => {
  const material = {
    state: "retained",
    event_ids: ["census:m3:advance:2026-10-27", "census:m3:full:2026-11-03"],
    evidence_ids: ["full-proof", "advance-proof"],
    reports: {
      advm3: {
        event_ids: ["census:m3:advance:2026-10-27"],
        evidence_ids: ["advance-proof"],
      },
      m3: {
        event_ids: ["census:m3:full:2026-11-03"],
        evidence_ids: ["full-proof"],
      },
    },
  };
  render(
    <CpiExecutionDetail
      profile={M3_PROFILE}
      execution={{
        id: "legacy-joint-job",
        origin: "manual",
        action: "update",
        start: "2026-09",
        end: "2026-09",
        created_at: null,
        started_at: null,
        finished_at: null,
        state: "complete",
        classification: null,
        result: { release_material: material },
      }}
    />,
  );
  const advance = screen.getByRole("link", {
    name: "打开耐用品先行报告事件与证据",
  });
  const full = screen.getByRole("link", {
    name: "打开制造业完整报告事件与证据",
  });
  for (const [link, expectedEvent, expectedProof] of [
    [advance, material.event_ids[0], "advance-proof"],
    [full, material.event_ids[1], "full-proof"],
  ] as const) {
    const fields = new URLSearchParams(link.getAttribute("href")!.slice(1));
    expect(fields.get("release_event")).toBe(expectedEvent);
    expect(fields.get("release_evidence")).toBe(expectedProof);
  }
});

it("does not invent proof pairing for unqualified multi-event aggregates", () => {
  expect(
    releaseEvidenceLinks({
      event_ids: ["advance", "full"],
      evidence_ids: ["b", "a"],
    }),
  ).toEqual([
    { eventId: "advance", evidenceId: undefined, report: undefined },
    { eventId: "full", evidenceId: undefined, report: undefined },
  ]);
});

it("uses explicit per-material event identities even when aggregate arrays disagree in order", () => {
  expect(
    releaseEvidenceLinks({
      event_ids: ["advance", "full"],
      evidence_ids: ["f", "a"],
      material_outcomes: [
        { event_id: "full", evidence_id: "f" },
        { event_id: "advance", evidence_ids: ["a"] },
      ],
    }),
  ).toEqual([
    { eventId: "advance", evidenceId: "a", report: undefined },
    { eventId: "full", evidenceId: "f", report: undefined },
  ]);
});
