import "./offlineTestSetup";
import { afterEach, expect, it } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import App from "./App";
import { TdxRosterQuery } from "./TdxRosterFamily";
import type { Transport } from "./api";
import { asgi, closeBridges } from "./workflowTestBridge";

let root = "";
afterEach(() => {
  cleanup();
  closeBridges();
  if (root) rmSync(root, { recursive: true, force: true });
  root = "";
  location.hash = "";
  sessionStorage.clear();
});
const props = {
  route: {
    view: "assets" as const,
    invalid: false,
    fields: new URLSearchParams(),
  },
  navigate: () => {},
  generation: 0,
};
function fixture(scenario = "normal") {
  const backend = asgi(scenario, "tdx_roster_refresh_fixture.py");
  const transport: Transport = async (path, init) => {
    const response = await backend.transport(path, init);
    return Response.json(await response.json(), { status: response.status });
  };
  return { ...backend, transport };
}
const query = () =>
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
async function ready() {
  await waitFor(() =>
    expect((screen.getByLabelText("名册日期") as HTMLInputElement).value).toBe(
      "2026-11-18",
    ),
  );
}
it("shows real per-security refresh receipts through the catalog while retaining official delistings and raw lifecycle", async () => {
  root = mkdtempSync(join(tmpdir(), "mdf-d01-roster-refresh-"));
  const directory = asgi("financial_manual", "d01_ui_exchange.py", {
    store_dir: root,
  });
  expect(
    (
      await directory.exchange({
        method: "PUT",
        path: "/api/r5/packages/tdx",
        body: {
          expected_revision: null,
          enabled_capability_ids: ["stock_roster"],
          source_changes: [],
        },
      })
    ).status,
  ).toBe(200);
  const backend = fixture();
  const transport: Transport = (path, init) =>
    String(path).startsWith("/api/r5/tdx/roster/")
      ? backend.transport(path, init)
      : directory.transport(path, init);
  location.hash = "ui=r5&view=assets";
  render(<App transport={transport} />);
  fireEvent.click(
    await screen.findByRole("button", { name: /选择 沪深北 A 股基础名册/ }),
  );
  await ready();
  query();
  const table = await screen.findByRole("region", { name: "名册结果" });
  for (const label of ["官方证据已过期", "tdx_roster_stale"])
    for (const element of screen.getAllByText(label)) {
      const details = element.closest("details")!;
      expect(details.querySelector("summary")?.textContent).toBe(
        "交易所上市/退市资料",
      );
      expect(details.hasAttribute("open")).toBe(false);
    }
  for (const code of ["000001", "000002", "000006", "000007", "000008"]) {
    const row = within(table).getByRole("row", { name: new RegExp(code) });
    expect(within(row).getByText("本次名册在列")).toBeTruthy();
    expect(within(row).getByText("2026-11-18 09:00:50 北京时间")).toBeTruthy();
  }
  for (const code of ["000003", "000004", "000005"])
    expect(
      within(
        within(table).getByRole("row", { name: new RegExp(code) }),
      ).getByText("已退市或终止上市"),
    ).toBeTruthy();
  expect(
    within(within(table).getByRole("row", { name: /000009/ })).getByText(
      "本次名册未列入",
    ),
  ).toBeTruthy();
  expect(
    within(within(table).getByRole("row", { name: /000010/ })).getByText(
      "本次名册在列",
    ),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
  const raw = JSON.parse(screen.getByLabelText("原始 HTTP 正文").textContent!);
  expect(
    raw.rows.find((r: { instrument: string }) => r.instrument === "000001")
      .membership_state,
  ).toBe("UNKNOWN");
  expect(raw.valid_through).toBe("2026-11-13");
  expect(raw.directory_verification.method).toBe("published_receipts");
  expect(backend.requests.every((r) => r.method === "GET")).toBe(true);
});
it("shows a failed refresh without fabricating a later verification time", async () => {
  const backend = fixture();
  render(<TdxRosterQuery {...props} transport={backend.transport} />);
  await ready();
  query();
  await screen.findByRole("region", { name: "名册结果" });
  const failure = (await backend.exchange({ control: "fail" })).body;
  expect(failure.state).toBe("PARTIAL");
  query();
  const table = await screen.findByRole("region", { name: "名册结果" });
  const row = within(table).getByRole("row", { name: /000001/ });
  expect(within(row).getByText("2026-11-18 09:00:50 北京时间")).toBeTruthy();
  expect(screen.queryByText("新版平安银行")).toBeNull();
  expect(
    screen
      .getAllByRole("alert")
      .some(
        (a) =>
          a.textContent?.includes(failure.request_id) &&
          a.textContent?.includes("tdx_roster_refresh_incomplete"),
      ),
  ).toBe(true);
  expect(
    (await backend.exchange({ control: "stats" })).body.get_source_requests,
  ).toBe(0);
});
it("keeps exchange lifecycle dates and status inside collapsed reference details", async () => {
  const backend = fixture();
  render(<TdxRosterQuery {...props} transport={backend.transport} />);
  await ready();
  query();
  await screen.findByRole("region", { name: "名册结果" });
  const label = screen.getAllByText("交易所上市/退市证据截至日").at(-1)!;
  const details = label.closest("details")!;
  expect(details).not.toBeNull();
  expect(details.querySelector("summary")?.textContent).toBe(
    "交易所上市/退市资料",
  );
  expect(details.hasAttribute("open")).toBe(false);
  expect(within(details).getByText("2026-11-13")).toBeTruthy();
  expect(within(details).getByText("官方证据已过期")).toBeTruthy();
  expect(within(details).getByText("tdx_roster_stale")).toBeTruthy();
  fireEvent.click(details.querySelector("summary")!);
  await waitFor(() => expect(details.hasAttribute("open")).toBe(true));
  expect(screen.getAllByText("目录已逐只核验").length).toBeGreaterThan(0);
});
it("reads a new verification time for an unchanged name after the next completed manual refresh", async () => {
  const backend = fixture();
  render(<TdxRosterQuery {...props} transport={backend.transport} />);
  await ready();
  fireEvent.change(screen.getByLabelText("代码或名称"), {
    target: { value: "000002" },
  });
  query();
  let table = await screen.findByRole("region", { name: "名册结果" });
  expect(within(table).getByText("万科Ａ")).toBeTruthy();
  expect(within(table).getByText("2026-11-18 09:00:50 北京时间")).toBeTruthy();
  expect((await backend.exchange({ control: "manual" })).body.state).toBe(
    "COMPLETE",
  );
  query();
  table = await screen.findByRole("region", { name: "名册结果" });
  expect(within(table).getByText("万科Ａ")).toBeTruthy();
  expect(within(table).getByText("本次名册在列")).toBeTruthy();
  expect(within(table).getByText("2026-11-18 14:00:50 北京时间")).toBeTruthy();
  expect(
    backend.requests.filter((r) =>
      r.path.startsWith("/api/r5/tdx/roster/query?"),
    ),
  ).toHaveLength(2);
  expect(backend.requests.every((r) => r.method === "GET")).toBe(true);
});
it("pins both snapshot identities and cutoff when paging across a real new refresh", async () => {
  const backend = fixture("paging");
  render(<TdxRosterQuery {...props} transport={backend.transport} />);
  await ready();
  query();
  await screen.findByRole("region", { name: "名册结果" });
  fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
  const first = JSON.parse(
    screen.getByLabelText("原始 HTTP 正文").textContent!,
  );
  fireEvent.click(screen.getByRole("tab", { name: "表格" }));
  await backend.exchange({ control: "manual" });
  fireEvent.click(
    within(screen.getByLabelText("TDX 名册 结果分页")).getByRole("button", {
      name: "下一页",
    }),
  );
  await screen.findByText("分页证券219");
  const request = backend.requests.filter((r) =>
    r.path.startsWith("/api/r5/tdx/roster/query?"),
  )[1];
  const q = new URL(request.path, "http://fixture").searchParams;
  expect(q.get("snapshot_id")).toBe(first.snapshot_id);
  expect(q.get("directory_snapshot_id")).toBe(first.directory_snapshot_id);
  expect(q.get("as_of")).toBe(first.search_as_of);
  expect(q.get("day")).toBe(first.requested_day);
  fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
  const page = JSON.parse(screen.getByLabelText("原始 HTTP 正文").textContent!);
  expect(page.snapshot_id).toBe(first.snapshot_id);
  expect(page.directory_snapshot_id).toBe(first.directory_snapshot_id);
  expect(
    page.rows.filter((r: { roster_verified_at: string }) =>
      r.roster_verified_at.startsWith("2026-11-18T14:"),
    ),
  ).toHaveLength(0);
});
it("queries a manually selected legacy day after the next publication without borrowing its name or observation time", async () => {
  const backend = fixture("legacy_history");
  render(<TdxRosterQuery {...props} transport={backend.transport} />);
  await ready();
  fireEvent.change(screen.getByLabelText("名册日期"), {
    target: { value: "2026-11-17" },
  });
  fireEvent.change(screen.getByLabelText("代码或名称"), {
    target: { value: "000001" },
  });
  query();
  const table = await screen.findByRole("region", { name: "名册结果" });
  expect(within(table).getByText("平安银行")).toBeTruthy();
  expect(within(table).getByText("本次名册在列")).toBeTruthy();
  expect(within(table).getByText("2026-11-17 09:00:50 北京时间")).toBeTruthy();
  expect(within(table).queryByText("十八日名称")).toBeNull();
  fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
  const body = JSON.parse(screen.getByLabelText("原始 HTTP 正文").textContent!);
  expect(body.requested_day).toBe("2026-11-17");
  expect(body.directory_observed_day).toBe("2026-11-17");
  expect(body.directory_verification.method).toBe("retained_job_steps");
  expect(body.snapshot_id).not.toBe(body.directory_snapshot_id);
  expect(body.rows[0].membership_state).toBe("UNKNOWN");
  expect(body.rows[0].roster_verification.request_id).toBe(
    body.directory_verification.request_id,
  );
  expect(backend.requests.every((r) => r.method === "GET")).toBe(true);
});
it("identifies ambiguous legacy evidence without claiming a new member verification or hiding delisting facts", async () => {
  const backend = fixture("legacy_ambiguous");
  render(<TdxRosterQuery {...props} transport={backend.transport} />);
  await ready();
  query();
  const table = await screen.findByRole("region", { name: "名册结果" });
  const row = within(table).getByRole("row", { name: /000001/ });
  expect(within(row).getByText("未取得该日逐只核验")).toBeTruthy();
  expect(within(row).getByText("未核实")).toBeTruthy();
  expect(within(table).queryByText("本次名册在列")).toBeNull();
  expect(
    screen.getAllByText("tdx_directory_job_ambiguous").length,
  ).toBeGreaterThan(0);
  expect(
    within(within(table).getByRole("row", { name: /000003/ })).getByText(
      "已退市或终止上市",
    ),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
  const body = JSON.parse(screen.getByLabelText("原始 HTTP 正文").textContent!);
  expect(body.directory_verification.state).toBe("UNVERIFIED");
  expect(body.directory_verification.request_id).toBeNull();
  expect(
    body.rows.find((r: { instrument: string }) => r.instrument === "000001")
      .roster_verified_at,
  ).toBeNull();
  expect(backend.requests.every((r) => r.method === "GET")).toBe(true);
});
