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
import { financialResponse } from "./financialQueryTestFixture";

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
function setup(queryExtra: Record<string, unknown> = {}, asOf = "") {
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
    return Response.json(financialResponse(url, queryExtra));
  };
  render(
    <FinancialDailyWorkbench
      route={{
        view: "assets",
        invalid: false,
        fields: new URLSearchParams({
          asset: FINANCIAL.identity,
          asset_revision: "r1",
          ...(asOf ? { as_of: asOf } : {}),
        }),
      }}
      navigate={vi.fn()}
      transport={transport}
      generation={0}
    />,
  );
  return calls;
}
function chooseSeries(ids: string[]) {
  fireEvent.click(screen.getByRole("button", { name: "金融指标" }));
  const group = screen.getByRole("group", { name: "金融指标选项" });
  fireEvent.click(within(group).getByRole("button", { name: "清空" }));
  for (const id of ids)
    fireEvent.click(
      within(group).getByRole("checkbox", { name: new RegExp(` · ${id}$`) }),
    );
  fireEvent.keyDown(group, { key: "Escape" });
}
const tab = (name: string) =>
  fireEvent.click(screen.getByRole("tab", { name }));
function url() {
  return within(screen.getByRole("region", { name: "请求预览" }))
    .getByLabelText("请求 URL")
    .textContent!.replace(/^GET /, "");
}

it("shares the exact date/series/cutoff/page request with the query and copies without submitting", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  const calls = setup();
  await screen.findByLabelText("开始日期");
  expect(screen.queryByLabelText("截止时间（北京时间）")).toBeNull();
  fireEvent.click(screen.getByLabelText("历史截止查询"));
  chooseSeries(["SOFR"]);
  fireEvent.change(screen.getByLabelText("开始日期"), {
    target: { value: "2026-09-01" },
  });
  fireEvent.change(screen.getByLabelText("结束日期"), {
    target: { value: "2026-11-03" },
  });
  fireEvent.change(screen.getByLabelText("截止时间（北京时间）"), {
    target: { value: "2026-11-05T16:00:03" },
  });
  tab("API说明");
  await screen.findByRole("region", { name: "请求预览" });
  const preview = url(),
    parsed = new URL(preview);
  expect(Object.fromEntries(parsed.searchParams)).toEqual({
    start: "2026-09-01",
    end: "2026-11-03",
    series: "SOFR",
    offset: "0",
    limit: "40",
    local_only: "true",
    order: "desc",
    as_of: "2026-11-05T08:00:03.000Z",
  });
  fireEvent.click(screen.getByRole("button", { name: "复制请求预览URL" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(preview));
  fireEvent.click(
    within(screen.getByRole("region", { name: "请求预览" })).getByText(
      "cURL 示例（当前服务地址）",
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "复制cURL" }));
  await waitFor(() =>
    expect(writeText).toHaveBeenCalledWith(`curl --request GET '${preview}'`),
  );
  expect(calls.filter((c) => c.url.includes("/query?"))).toHaveLength(0);
  tab("查询数据");
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  await screen.findByRole("button", { name: "下一页" });
  expect(calls.find((c) => c.url.includes("/query?"))!.url).toBe(
    parsed.pathname + parsed.search,
  );
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  await waitFor(() =>
    expect(calls.filter((c) => c.url.includes("/query?"))).toHaveLength(2),
  );
  tab("API说明");
  expect(new URL(url()).searchParams.get("offset")).toBe("40");
  tab("查询数据");
  chooseSeries(["DGS2"]);
  fireEvent.click(screen.getByLabelText("历史截止查询"));
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

it("keeps ordinary mode across responses and requires explicit historical cutoff", async () => {
  const calls = setup();
  await screen.findByLabelText("开始日期");
  expect(screen.queryByLabelText("截止时间（北京时间）")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  await screen.findByRole("region", { name: "本次原始响应" });
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  await waitFor(() =>
    expect(calls.filter((c) => c.url.includes("/query?")).length).toBe(2),
  );
  expect(
    calls
      .filter((c) => c.url.includes("/query?"))
      .every(
        (c) => !c.url.includes("as_of") && c.url.includes("local_only=false"),
      ),
  ).toBe(true);
  fireEvent.click(screen.getByLabelText("历史截止查询"));
  expect(
    (screen.getByLabelText("截止时间（北京时间）") as HTMLInputElement)
      .required,
  ).toBe(true);
  expect(
    (
      screen.getByRole("button", {
        name: "查询数据",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  tab("API说明");
  expect(screen.queryByRole("region", { name: "请求预览" })).toBeNull();
  tab("查询数据");
  fireEvent.click(screen.getByLabelText("历史截止查询"));
  expect(screen.queryByLabelText("仅查询本地")).toBeNull();
  expect(
    financialQuerySuffix({
      start: "2026-10-30",
      end: "2026-10-30",
      series: "DGS10",
      cutoff: "",
      localOnly: true,
    }),
  ).toContain("local_only=true");
});

it.each([false, true])(
  "shows unpersisted upstream and specific partial failure=%s without creating jobs",
  async (failed) => {
    const calls = setup({
      query_mode: "current",
      rows: [
        {
          series_id: "DGS10",
          observation_date: "2026-10-30",
          value: "0",
          basis_points: null,
          state: "observed",
          origin: "upstream",
          persisted: false,
          revision: null,
          available_at: null,
          observed_at: null,
          raw_locator: null,
          provider_extra: { source_origin: "us_treasury" },
        },
      ],
      upstream: {
        cache_hits: 0,
        issues: failed
          ? [
              {
                series_id: "DGS10",
                start: "2026-10-29",
                end: "2026-10-29",
                classification: "financial_http_failure",
              },
            ]
          : [],
      },
    });
    chooseSeries(["DGS10"]);
    fireEvent.change(screen.getByLabelText("开始日期"), {
      target: { value: "2026-10-30" },
    });
    fireEvent.change(screen.getByLabelText("结束日期"), {
      target: { value: "2026-10-30" },
    });
    fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
    await screen.findByText("临时来源值 · 未保存");
    const table = screen.getByRole("table", { name: "金融条件日频 范围数据" });
    expect(table.textContent).toContain("临时来源值 · 未保存");
    expect(table.textContent).toContain("0");
    if (failed) {
      expect(screen.getByLabelText("查询补源缺口").textContent).toContain(
        "具名来源HTTP请求暂未成功",
      );
      expect(screen.getByLabelText("查询补源缺口").textContent).toContain(
        "2026-10-29",
      );
    }
    expect(calls.every((c) => c.method === "GET")).toBe(true);
  },
);

it("preserves explicit old financial as_of deep links as historical reads", async () => {
  const calls = setup({}, "2026-10-01T00:00:00Z");
  await screen.findByLabelText("开始日期");
  expect(
    (screen.getByRole("checkbox", { name: "历史截止查询" }) as HTMLInputElement)
      .checked,
  ).toBe(true);
  expect(
    (screen.getByLabelText("截止时间（北京时间）") as HTMLInputElement).value,
  ).toBe("2026-10-01T08:00:00");
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  await screen.findByRole("region", { name: "本次原始响应" });
  const request = new URL(
    calls.find((c) => c.url.includes("/query?"))!.url,
    "http://local",
  );
  expect(request.searchParams.get("as_of")).toBe("2026-10-01T00:00:00.000Z");
  expect(request.searchParams.get("local_only")).toBe("true");
});
