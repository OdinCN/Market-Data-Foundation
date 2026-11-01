import { afterEach, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { RelatedReleaseLinks } from "./RelatedReleaseLinks";
import {
  PCE_PROFILE,
  RETAIL_PROFILE,
  CONSUMPTION_PROFILE,
  macroAssetLabel,
} from "./macroTimeSeriesProfile";
import { releaseReasonTexts } from "./macroReleaseLinks";
afterEach(cleanup);
const link = {
  event_id: "bea:2026-09-30",
  evidence_ids: ["e1"],
  association: "related_scope_only",
  reason: "continuous_version_not_bound_to_material",
  source_version: {
    value: "2026-09-30",
    precision: "date" as const,
    basis: "official_release_date",
    source: "BEA",
  },
  roles: ["actual"],
};
it("distinguishes a month's original publication from its later revision without inventing a time", () => {
  render(
    <RelatedReleaseLinks
      profile={PCE_PROFILE}
      links={[
        link,
        {
          ...link,
          event_id: "bea:2026-10-29",
          source_version: { ...link.source_version, value: "2026-10-29" },
          roles: ["prior_revised"],
        },
      ]}
    />,
  );
  expect(screen.getByText("发布记录（2）")).toBeTruthy();
  const first = screen.getByText("2026-09-30 · 当期公布");
  expect(first.getAttribute("href")).toContain(
    encodeURIComponent(PCE_PROFILE.asset.identity),
  );
  expect(first.getAttribute("href")).toContain(
    "release_event=bea%3A2026-09-30",
  );
  expect(screen.getByText("2026-10-29 · 后续修订")).toBeTruthy();
  expect(screen.getByText(/未与当前表格数值确认/)).toBeTruthy();
  expect(document.body.textContent).not.toContain("00:00");
  expect(document.body.textContent).not.toContain("当前数值已有版本证明");
});
it("keeps retail month-level relevance separate from proven values", () => {
  render(<RelatedReleaseLinks profile={RETAIL_PROFILE} links={[link]} />);
  expect(screen.getByText("相关月份发布（1）")).toBeTruthy();
});
it("only labels explicit version evidence as proven", () => {
  render(
    <RelatedReleaseLinks
      profile={PCE_PROFILE}
      links={[{ ...link, association: "explicit_version_proof", reason: null }]}
    />,
  );
  expect(screen.getByText(/当前数值已有版本证明/)).toBeTruthy();
  expect(screen.queryByText(/未与当前表格数值确认/)).toBeNull();
});
it("keeps legacy metadata readable and distinct missing explanations", () => {
  render(
    <RelatedReleaseLinks
      profile={PCE_PROFILE}
      links={[{ ...link, source_version: undefined, roles: undefined }]}
    />,
  );
  expect(screen.getByText("查看发布事件")).toBeTruthy();
  expect(macroAssetLabel(CONSUMPTION_PROFILE)).toBe("美国消费与收入");
  expect(
    releaseReasonTexts([
      "native_money_levels_not_in_release_html",
      "real_goods_services_not_in_release_html",
      "historical_comparisons_xlsx_is_not_vintage_revision_table",
    ]),
  ).toHaveLength(3);
  expect(releaseReasonTexts(["unknown_one", "unknown_two"])).toHaveLength(1);
});
