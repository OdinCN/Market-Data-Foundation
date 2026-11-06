import "./offlineTestSetup";
import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { FinancialDailyWorkbench, FINANCIAL } from "./FinancialDailyWorkbench";
import { FinancialApiHelp } from "./FinancialApiHelp";
import {
  financialQuerySuffix,
  FINANCIAL_PREFIX as prefix,
} from "./financialQuery";
import example from "./financial-query-example.json";
import type { Transport } from "./api";

const originalClipboard = Object.getOwnPropertyDescriptor(
  navigator,
  "clipboard",
);
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  if (originalClipboard)
    Object.defineProperty(navigator, "clipboard", originalClipboard);
  else Reflect.deleteProperty(navigator, "clipboard");
});
function setup() {
  const calls: { url: string; method: string }[] = [];
  const transport: Transport = async (input, init) => {
    const url = String(input);
    calls.push({ url, method: init?.method ?? "GET" });
    if (url.endsWith("/status"))
      return Response.json({
        defaults: { start: "2006-11-05", end: "2026-11-05" },
        series: [],
      });
    if (url.endsWith("/summary"))
      return Response.json({ series: [], observation_count: 0 });
    if (url.endsWith("/jobs")) return Response.json({ jobs: [] });
    if (url.endsWith("/api-help"))
      return Response.json({
        endpoints: { query: prefix + "/query" },
        notes: ["GET 本地读取零上游请求。"],
      });
    const offset = Number(
      new URL(url, location.origin).searchParams.get("offset") ?? 0,
    );
    return Response.json({
      status: "OK",
      rows: [],
      as_of: "2026-11-05T08:00:03+00:00",
      total: 201,
      offset,
      next_offset: offset === 0 ? 200 : null,
      provider_requests: 0,
      coverage: { series: [] },
    });
  };
  render(
    <FinancialDailyWorkbench
      route={{
        view: "assets",
        invalid: false,
        fields: new URLSearchParams({
          asset: FINANCIAL.identity,
          asset_revision: "r1",
        }),
      }}
      navigate={vi.fn()}
      transport={transport}
      generation={0}
    />,
  );
  return calls;
}
const tab = (name: string) =>
  fireEvent.click(screen.getByRole("tab", { name }));
function url() {
  return screen.getByLabelText("请求 URL").textContent!.replace(/^GET /, "");
}

it("shares the exact date/series/cutoff/page request with the query and copies without submitting", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  const calls = setup();
  await screen.findByLabelText("开始日期");
  fireEvent.change(screen.getByLabelText("显示指标"), {
    target: { value: "SOFR" },
  });
  fireEvent.change(screen.getByLabelText("开始日期"), {
    target: { value: "2026-10-30" },
  });
  fireEvent.change(screen.getByLabelText("结束日期"), {
    target: { value: "2026-11-03" },
  });
  fireEvent.change(screen.getByLabelText("截止时间（北京时间，可空）"), {
    target: { value: "2026-11-05T16:00:03" },
  });
  tab("API说明");
  await screen.findByRole("region", { name: "请求预览" });
  const preview = url(),
    parsed = new URL(preview);
  expect(Object.fromEntries(parsed.searchParams)).toEqual({
    start: "2026-10-30",
    end: "2026-11-03",
    series: "SOFR",
    offset: "0",
    limit: "200",
    as_of: "2026-11-05T08:00:03.000Z",
  });
  fireEvent.click(screen.getByRole("button", { name: "复制请求预览URL" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(preview));
  fireEvent.click(screen.getByText("cURL 示例（当前服务地址）"));
  fireEvent.click(screen.getByRole("button", { name: "复制cURL" }));
  await waitFor(() =>
    expect(writeText).toHaveBeenCalledWith(`curl --request GET '${preview}'`),
  );
  expect(calls.filter((c) => c.url.includes("/query?"))).toHaveLength(0);
  tab("查询数据");
  fireEvent.click(screen.getByRole("button", { name: "查询本地数据" }));
  await screen.findByRole("button", { name: "下一页" });
  expect(calls.find((c) => c.url.includes("/query?"))!.url).toBe(
    parsed.pathname + parsed.search,
  );
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  await waitFor(() =>
    expect(calls.filter((c) => c.url.includes("/query?"))).toHaveLength(2),
  );
  tab("API说明");
  expect(new URL(url()).searchParams.get("offset")).toBe("200");
  tab("查询数据");
  fireEvent.change(screen.getByLabelText("显示指标"), {
    target: { value: "DGS2" },
  });
  fireEvent.change(screen.getByLabelText("截止时间（北京时间，可空）"), {
    target: { value: "" },
  });
  tab("API说明");
  expect(new URL(url()).searchParams.get("offset")).toBe("0");
  expect(new URL(url()).searchParams.has("as_of")).toBe(false);
  expect(calls.every((c) => c.method === "GET")).toBe(true);
});

it("documents the actual daily fields, nullable values, ten series and POST strategy; example copies are inert", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  const calls = setup();
  tab("API说明");
  await screen.findByRole("table", { name: "查询参数" });
  for (const heading of [
    "十条序列、类型与单位",
    "返回字段、类型与单位",
    "缺值、状态与历史截止",
    "来源与MDF计算",
    "成功示例（合成合同样本）",
    "失败示例与诊断",
    "采集、恢复与周期接口",
  ])
    fireEvent.click(screen.getByText(heading));
  expect(
    within(screen.getByRole("table", { name: "指标与单位" })).getAllByRole(
      "row",
    ),
  ).toHaveLength(11);
  const fields = screen.getByRole("table", { name: "返回字段" }).textContent!;
  for (const key of [
    "observation_date",
    "basis_points",
    "available_at",
    "raw_locator",
    "source_available_at",
    "last_confirmed",
    "provider_requests",
    "coverage",
  ])
    expect(fields).toContain(key);
  expect(screen.getByText(/两条财政部输入/)).toBeTruthy();
  expect(
    screen.getByRole("table", { name: "采集与周期接口" }).textContent,
  ).toContain(`POST ${prefix}/strategy`);
  expect(
    screen.getByRole("table", { name: "采集与周期接口" }).textContent,
  ).not.toContain(`PUT ${prefix}/strategy`);
  fireEvent.click(screen.getByRole("button", { name: "复制成功示例JSON" }));
  await waitFor(() =>
    expect(writeText).toHaveBeenCalledWith(JSON.stringify(example, null, 2)),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "复制周期恢复请求体示例" }),
  );
  await waitFor(() =>
    expect(writeText).toHaveBeenCalledWith(
      JSON.stringify({ enabled: true, recover: true }, null, 2),
    ),
  );
  expect(calls.every((c) => c.method === "GET")).toBe(true);
  expect(calls.filter((c) => c.url.includes("/query?"))).toHaveLength(0);
});

it("does not render a fabricated request for an invalid cutoff draft", () => {
  render(
    <FinancialApiHelp
      help={null}
      start="2026-10-30"
      end="2026-11-03"
      series="DGS10"
      cutoff="invalid"
      offset={0}
    />,
  );
  expect(screen.getByRole("status").textContent).toContain("格式无效");
  expect(screen.queryByRole("region", { name: "请求预览" })).toBeNull();
  expect(
    financialQuerySuffix({
      start: "2026-10-30",
      end: "2026-11-03",
      series: "DGS10",
      cutoff: "",
    }),
  ).not.toContain("as_of");
});
