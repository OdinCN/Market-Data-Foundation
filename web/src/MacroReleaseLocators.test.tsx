import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MacroReleaseRecords } from "./MacroReleaseRecords";
import { PPI_PROFILE } from "./macroTimeSeriesProfile";
import type { Transport } from "./api";

afterEach(cleanup);
const root = resolve(process.cwd(), "..");
// Expected wire projected from the real retained government material fixture.
const wire = readFileSync(
  resolve(root, "tests/fixtures/macro_release/ppi_10152026_event.json"),
  "utf8",
);

it("renders real PPI table locators and Chinese labels without undefined spans, preserving raw wire", async () => {
  const transport: Transport = async () => new Response(wire, { status: 200 });
  render(
    <MacroReleaseRecords
      profile={PPI_PROFILE}
      transport={transport}
      initialEventId="bls:ppi:2026-10-15"
    />,
  );
  const table = await screen.findByRole("table", { name: "事件公布与修订值" });
  expect(table.textContent).toContain("总量 · 季调 / 环比");
  expect(table.textContent).toContain("PPI 表A · Sept.");
  expect(table.textContent).toContain("1.3 %");
  expect(table.textContent).not.toContain("undefined");
  expect(table.textContent).not.toContain("材料段落");
  expect(table.textContent).toContain("WPSFD4");
  fireEvent.click(screen.getByRole("button", { name: "实际响应" }));
  expect(screen.getByLabelText("发布记录实际HTTP正文").textContent).toBe(wire);
});

it("preserves unknown source-version basis", async () => {
  const data = JSON.parse(wire);
  data.events[0].source_version.basis = "unconfirmed_import_basis";
  const transport: Transport = async () =>
    new Response(JSON.stringify(data), { status: 200 });
  render(
    <MacroReleaseRecords
      profile={PPI_PROFILE}
      transport={transport}
      initialEventId="bls:ppi:2026-10-15"
    />,
  );
  const detail = await screen.findByRole("region", { name: "发布事件详情" });
  expect(detail.textContent).toContain("未知（来源依据未确认）");
  expect(detail.textContent).not.toContain("材料声明发布日期");
});
