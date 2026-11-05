import "./offlineTestSetup";
import { afterEach, expect, it } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import App from "./App";
import { FINANCIAL } from "./FinancialDailyWorkbench";
import { CPI, keyOf } from "./workflowModel";
import { asgi, closeBridges } from "./workflowTestBridge";
import type { Transport } from "./api";
let root = "";
afterEach(() => {
  cleanup();
  closeBridges();
  if (root) rmSync(root, { recursive: true, force: true });
  location.hash = "";
});
const id = "00000000-0000-4000-8000-000000000004";
const job = {
  request_id: id,
  state: "complete",
  created_at: "2026-11-05T12:00:00Z",
  provider_requests: 0,
  command: { action: "init", start: "2006-11-05", end: "2026-11-05" },
  steps: [],
  coverage: { series: [], as_of: "2026-11-05T12:00:00Z" },
};
async function setup(financialJobs = [job]) {
  root = mkdtempSync(join(tmpdir(), "mdf-d01-financial-entry-"));
  const b = asgi("financial_manual", "d01_ui_exchange.py", { store_dir: root });
  await b.exchange({
    method: "PUT",
    path: "/api/r5/packages/macro",
    body: {
      expected_revision: null,
      enabled_capability_ids: ["cpi"],
      source_changes: [],
    },
  });
  const requests: { url: string; method: string }[] = [];
  const transport: Transport = async (input, init) => {
    const url = String(input);
    requests.push({ url, method: init?.method ?? "GET" });
    if (url.startsWith("/api/r5/financial/timeseries")) {
      const data = url.endsWith("/status")
        ? { defaults: { start: "2006-11-05", end: "2026-11-05" }, series: [] }
        : url.endsWith("/jobs")
          ? { jobs: financialJobs }
          : url.includes("/jobs/")
            ? { job }
            : url.includes("/query?")
              ? {
                  rows: [],
                  as_of: "2026-11-05T12:00:00Z",
                  total: 0,
                  next_offset: null,
                  provider_requests: 0,
                  coverage: { series: [] },
                }
              : { endpoints: { query: "/api/r5/financial/timeseries/query" } };
      return Response.json({ status: "OK", ...data });
    }
    return b.transport(input, init);
  };
  render(<App transport={transport} />);
  return requests;
}
function nav(name: string) {
  fireEvent.click(
    within(screen.getByRole("navigation")).getByRole("button", { name }),
  );
}
function pickFinancial() {
  fireEvent.click(
    within(screen.getByRole("region", { name: "数据资产" })).getByRole(
      "button",
      { name: /选择 金融条件日频/ },
    ),
  );
}
it("keeps the real macro catalog/filter/search while alternating financial daily and CPI month readers", async () => {
  const requests = await setup();
  nav("数据资产");
  await screen.findByRole("button", { name: /选择 金融条件日频/ });
  fireEvent.change(screen.getByLabelText("接入包筛选"), {
    target: { value: "macro" },
  });
  fireEvent.change(screen.getByLabelText("搜索数据资产"), {
    target: { value: "金融" },
  });
  pickFinancial();
  const daily = await screen.findByLabelText("开始日期");
  expect(daily.getAttribute("placeholder")).toBe("YYYY-MM-DD");
  expect(screen.getByLabelText("搜索数据资产")).toBeTruthy();
  expect(screen.getByLabelText("接入包筛选")).toBeTruthy();
  expect(
    screen.queryByRole("button", { name: "金融条件日频（十项）" }),
  ).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "查询本地数据" }));
  await screen.findByRole("button", { name: "固定此截止" });
  fireEvent.click(screen.getByRole("tab", { name: "API说明" }));
  await screen.findByRole("heading", {
    name: "GET /api/r5/financial/timeseries/query",
  });
  fireEvent.change(screen.getByLabelText("搜索数据资产"), {
    target: { value: "CPI" },
  });
  fireEvent.click(
    within(screen.getByRole("region", { name: "数据资产" })).getByRole(
      "button",
      { name: /选择 美国 CPI/ },
    ),
  );
  const month = await screen.findByLabelText("查询月份");
  expect((month as HTMLInputElement).value).toMatch(/^\d{4}-\d{2}$/);
  expect(screen.queryByLabelText("开始日期")).toBeNull();
  fireEvent.change(screen.getByLabelText("搜索数据资产"), {
    target: { value: "金融" },
  });
  pickFinancial();
  expect(
    (await screen.findByLabelText("开始日期")).getAttribute("placeholder"),
  ).toBe("YYYY-MM-DD");
  expect(requests.every((r) => r.method === "GET")).toBe(true);
});
it("uses the existing target, tabs and unified execution detail; retains the original job deep link", async () => {
  const requests = await setup();
  nav("采集任务");
  const select = await screen.findByLabelText("采集目标");
  await screen.findByRole("option", { name: "金融条件日频（十项）" });
  fireEvent.change(select, { target: { value: keyOf(FINANCIAL) } });
  expect(
    ((await screen.findByLabelText("采集开始日期")) as HTMLInputElement).value,
  ).toBe("2006-11-05");
  expect(screen.getByLabelText("采集目标").tagName).toBe("SELECT");
  expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual([
    "新建采集",
    "周期任务",
    "执行记录",
  ]);
  fireEvent.change(screen.getByLabelText("采集目标"), {
    target: { value: keyOf(CPI) },
  });
  await screen.findByLabelText("开始参考月");
  fireEvent.change(screen.getByLabelText("采集目标"), {
    target: { value: keyOf(FINANCIAL) },
  });
  await screen.findByLabelText("采集开始日期");
  fireEvent.click(screen.getByRole("tab", { name: "执行记录" }));
  const row = await screen.findByRole("button", {
    name: new RegExp(`选择 金融条件日频 ${id}`),
  });
  fireEvent.click(row);
  await screen.findByRole("heading", { name: "执行详情" });
  expect(
    screen
      .getByRole("complementary", { name: "执行记录详情" })
      .contains(screen.getByRole("heading", { name: "执行详情" })),
  ).toBe(true);
  expect(screen.getByLabelText("执行资产筛选")).toBeTruthy();
  await act(async () => {
    location.hash = `ui=r5&view=collection&asset=${FINANCIAL.identity}&asset_revision=r1&tab=executions&job=${id}`;
    window.dispatchEvent(new Event("hashchange"));
  });
  await screen.findByRole("heading", { name: "执行详情" });
  expect(screen.getByRole("tab", { name: "新建采集" })).toBeTruthy();
  expect(requests.every((r) => r.method === "GET")).toBe(true);
});

it("distinguishes a confirmed empty financial execution scope from an unbound legacy reader", async () => {
  const requests = await setup([]);
  nav("采集任务");
  await screen.findByRole("option", { name: "金融条件日频（十项）" });
  fireEvent.change(screen.getByLabelText("采集目标"), {
    target: { value: keyOf(FINANCIAL) },
  });
  await screen.findByLabelText("采集开始日期");
  fireEvent.click(screen.getByRole("tab", { name: "执行记录" }));
  fireEvent.change(screen.getByLabelText("执行资产筛选"), {
    target: { value: keyOf(FINANCIAL) },
  });
  await screen.findByText("当前已查询范围没有金融日频执行记录。");
  expect(
    screen.queryByText("未选择读取范围或此资产尚未绑定读取能力 / 尚未查询。"),
  ).toBeNull();
  expect(requests.every((r) => r.method === "GET")).toBe(true);
});
