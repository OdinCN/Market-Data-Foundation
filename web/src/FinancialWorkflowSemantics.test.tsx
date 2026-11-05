import "./offlineTestSetup";
import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
  waitFor,
} from "@testing-library/react";
import type { Transport } from "./api";
import {
  FINANCIAL,
  FinancialDailyWorkbench,
  type FinancialJob,
} from "./FinancialDailyWorkbench";
import { CollectionWorkbench } from "./CollectionWorkbench";
import { executionCreatedOrder } from "./executionOrder";
import { financialStateText } from "./financialJobPresentation";
import type { Route } from "./workflowRoute";
afterEach(cleanup);
const old = "00000000-0000-4000-8000-000000000001",
  recent = "00000000-0000-4000-8000-000000000002";
const job = (changes: Partial<FinancialJob> = {}): FinancialJob => ({
  request_id: old,
  state: "complete",
  created_at: "2026-11-05T09:00:00Z",
  provider_requests: 0,
  command: { action: "update", start: "2026-11-01", end: "2026-11-05" },
  steps: [],
  ...changes,
});
const route: Route = {
  view: "collection",
  invalid: false,
  fields: new URLSearchParams({ tab: "executions", execution_asset: "all" }),
};
const step = (state: string, series_id = "DGS2") => ({
  state,
  series_id,
  start: "2026-11-01",
  end: "2026-11-05",
  source: "origin",
});
const availability = {
  data: {
    latest_months: {},
    observation_count: 0,
    revision_count: 0,
    batch_count: 0,
    web_capabilities: { query: true, collect: true, reason: null },
  },
  busy: false,
  failure: null,
  legacy: false,
};
it("interleaves actual creation times across assets, distinguishes triggers and supports source search", async () => {
  const records = [
    job({
      request_id: recent,
      origin: "periodic",
      created_at: "2026-11-05T12:00:00Z",
      steps: [step("complete", "EFFR")],
    }),
    job({ steps: [{ ...step("complete", "BAMLC0A0CM"), source: "fred" }] }),
  ];
  const cpi = {
    id: "cpi-between",
    origin: "manual",
    action: "update",
    state: "complete",
    start: "2026-09",
    end: "2026-09",
    created_at: "2026-11-05T11:00:00Z",
    started_at: null,
    finished_at: null,
    classification: null,
    result: null,
  };
  const calls: string[] = [];
  const transport: Transport = async (input, init) => {
    calls.push(init?.method ?? "GET");
    const url = String(input);
    return Response.json({
      status: "OK",
      ...(url.includes("financial")
        ? { jobs: records }
        : url.endsWith("/executions")
          ? { executions: [cpi] }
          : { jobs: [], plans: [] }),
    });
  };
  const nav = vi.fn();
  render(
    <CollectionWorkbench
      assets={[]}
      asset={undefined}
      source={undefined}
      route={route}
      navigate={nav}
      transport={transport}
      generation={0}
      invalidIdentity={false}
      financialRegistered
      cpiAvailability={availability}
    />,
  );
  await screen.findByRole("button", { name: /选择 cpi-between/ });
  await screen.findByRole("button", {
    name: new RegExp("选择 金融条件日频 " + old),
  });
  const table = screen.getByRole("region", { name: "执行记录" });
  const buttons = within(table)
    .getAllByRole("button")
    .filter((b) => b.classList.contains("row-link"));
  expect(
    buttons.map(
      (b) =>
        b
          .getAttribute("aria-label")
          ?.match(/(?:00000000-[\da-f-]+|cpi-between)/)?.[0],
    ),
  ).toEqual([recent, "cpi-between", old]);
  expect(buttons[0].textContent).toContain("自动策略");
  expect(buttons[2].textContent).toContain("手动采集");
  const search = screen.getByLabelText("搜索执行记录");
  fireEvent.change(search, { target: { value: "FRED" } });
  expect(
    within(table)
      .getAllByRole("button")
      .filter((b) => b.classList.contains("row-link")),
  ).toHaveLength(1);
  fireEvent.click(
    screen.getByRole("button", {
      name: new RegExp("选择 金融条件日频 " + old),
    }),
  );
  expect(nav).toHaveBeenCalledWith(
    "collection",
    expect.objectContaining({ asset: FINANCIAL.identity, job: old }),
    true,
  );
  fireEvent.change(search, { target: { value: "自动策略" } });
  expect(
    within(table).getByRole("button", {
      name: new RegExp("选择 金融条件日频 " + recent),
    }),
  ).toBeTruthy();
  expect(calls.every((m) => m === "GET")).toBe(true);
});
it("sorts unknown creation last without borrowing updated or current timestamps", () => {
  const rows = [
    { id: "unknown" },
    { id: "invalid", createdAt: "bad" },
    { id: "early", createdAt: "2026-11-05T18:00:00+08:00" },
    { id: "late", createdAt: "2026-11-05T11:00:00Z" },
  ].sort(executionCreatedOrder);
  expect(rows.map((r) => r.id)).toEqual([
    "late",
    "early",
    "unknown",
    "invalid",
  ]);
  expect(financialStateText("some_new_machine_state")).toBe("状态待核实");
});
function detail(record: FinancialJob) {
  const requests: string[] = [];
  const transport: Transport = async (_u, i) => {
    requests.push(i?.method ?? "GET");
    return Response.json({ status: "OK", job: record });
  };
  render(
    <FinancialDailyWorkbench
      route={{
        ...route,
        fields: new URLSearchParams({
          tab: "executions",
          asset: FINANCIAL.identity,
          asset_revision: "r1",
          job: record.request_id,
        }),
      }}
      navigate={vi.fn()}
      transport={transport}
      generation={0}
      embedded
      detailOnly
    />,
  );
  return requests;
}
it("running and pending steps are progress, with no recovery or stale finish time", async () => {
  const calls = detail(
    job({
      state: "running",
      origin: "periodic",
      finished_at: "2026-11-05T08:00:00Z",
      steps: [step("pending"), step("running", "EFFR")],
    }),
  );
  await screen.findByRole("heading", { name: "执行详情" });
  expect(screen.queryByRole("region", { name: "失败与恢复" })).toBeNull();
  expect(screen.queryByRole("button", { name: "续跑此作业" })).toBeNull();
  expect(screen.getByLabelText("执行进度").textContent).toContain("等待执行 1");
  expect(
    screen.getByText("结束时间（北京时间）").closest(".fact")?.textContent,
  ).toContain("未记录");
  expect(calls.every((m) => m === "GET")).toBe(true);
});
it("partial failures identify only failed steps, leaving pending and unpublished steps separate", async () => {
  detail(
    job({
      state: "partial",
      steps: [
        { ...step("failed"), classification: "financial_http_failure" },
        step("pending", "EFFR"),
        step("waiting_publication", "SOFR"),
      ],
    }),
  );
  await screen.findByRole("heading", { name: "执行详情" });
  const alerts = screen.getAllByRole("alert");
  expect(alerts).toHaveLength(1);
  expect(alerts[0].textContent).toContain("2 年名义收益率");
  expect(alerts[0].textContent).not.toContain("有效联邦基金利率");
  expect(screen.getByLabelText("等待来源公布").textContent).toContain(
    "担保隔夜融资利率",
  );
  expect(
    (screen.getByRole("button", { name: "续跑此作业" }) as HTMLButtonElement)
      .disabled,
  ).toBe(false);
});
it("complete legacy record retains unknown start instead of manufacturing it", async () => {
  detail(
    job({ steps: [step("complete")], updated_at: "2026-11-05T10:00:00Z" }),
  );
  await screen.findByRole("heading", { name: "执行详情" });
  expect(
    screen.getByText("开始时间（北京时间）").closest(".fact")?.textContent,
  ).toContain("未记录");
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByText("执行结果").closest(".fact")?.textContent).toContain(
    "已完成",
  );
});

it("new-collection briefs distinguish persisted automatic and manual triggers without a submission", async () => {
  const records = [job({ origin: "periodic", request_id: recent }), job()];
  const calls: string[] = [];
  const transport: Transport = async (u, i) => {
    calls.push(i?.method ?? "GET");
    return Response.json({
      status: "OK",
      ...(String(u).endsWith("/status")
        ? { defaults: { start: "2006-11-05", end: "2026-11-05" }, series: [] }
        : String(u).endsWith("/jobs")
          ? { jobs: records }
          : {}),
    });
  };
  render(
    <FinancialDailyWorkbench
      route={{
        ...route,
        fields: new URLSearchParams({
          tab: "new",
          asset: FINANCIAL.identity,
          asset_revision: "r1",
        }),
      }}
      navigate={vi.fn()}
      transport={transport}
      generation={0}
      embedded
    />,
  );
  const briefs = await screen.findByRole("complementary", { name: "执行简报" });
  await within(briefs).findByText(/金融日频 · 自动策略 · 更新指定范围/);
  expect(
    within(briefs).getByText(/金融日频 · 手动采集 · 更新指定范围/),
  ).toBeTruthy();
  expect(calls.every((m) => m === "GET")).toBe(true);
});
