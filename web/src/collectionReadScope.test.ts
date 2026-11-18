import { expect, it } from "vitest";
import { collectionReadScope } from "./collectionReadScope";
import { CPI, PPI, keyOf } from "./workflowModel";
import type { Route } from "./workflowRoute";

const scope = (query: string, invalid = false) =>
  collectionReadScope(
    {
      view: "collection",
      invalid: false,
      fields: new URLSearchParams(query),
    } as Route,
    invalid,
  );

it.each(["executions", "periodic"])(
  "keeps an explicit all-assets %s filter while selecting a named record",
  (tab) => {
    const field = tab === "periodic" ? "periodic_asset" : "execution_asset";
    const value = scope(
      `tab=${tab}&${field}=all&asset=${PPI.identity}&asset_revision=${PPI.revision}&job=original`,
    );
    expect(value.filter).toBe("all");
    expect([CPI, PPI].filter(value.reads)).toEqual([CPI, PPI]);
  },
);

it("a direct asset/version link cannot read another family or another revision", () => {
  const value = scope(
    `tab=executions&asset=${PPI.identity}&asset_revision=${PPI.revision}`,
  );
  expect(value.filter).toBe(keyOf(PPI));
  expect([CPI, PPI, { ...PPI, revision: "old" }].filter(value.reads)).toEqual([
    PPI,
  ]);
  expect(
    scope(`tab=executions&asset=${PPI.identity}&asset_revision=old`).reads(PPI),
  ).toBe(false);
});

it("source-only links use the verified DTO version; invalid scope never reads", () => {
  const route: Route = {
    view: "collection",
    invalid: false,
    fields: new URLSearchParams("tab=periodic&operation=bound"),
  };
  const value = collectionReadScope(route, false, PPI);
  expect(value.filter).toBe(keyOf(PPI));
  expect(value.reads(CPI)).toBe(false);
  expect(collectionReadScope(route, true, PPI).reads(PPI)).toBe(false);
});
