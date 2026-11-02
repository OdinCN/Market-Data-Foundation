import { afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import App from "./App";
import type { Transport } from "./api";
import { CpiTimeSeries } from "./CpiTimeSeries";
import { CpiTimeSeriesCollection } from "./CpiTimeSeriesCollection";
import {
  PCE_PROFILE,
  CPI_PROFILE,
  PPI_PROFILE,
} from "./macroTimeSeriesProfile";
import { CPI, PCE, PCE_OPERATION, keyOf } from "./workflowModel";
import { pceLabels, type PceBinding } from "./pce";
import {
  buildCpiQueryRequest,
  queryCpiRange,
  useCpiAvailability,
  shiftMonth,
  type CpiAvailability,
  type CpiJob,
  type CpiResult,
  type CpiScope,
  type CpiStatus,
} from "./cpiTimeSeriesApi";

// Synthetic DTOs and an in-memory transport only. No sockets, DB, keys or real acceptance.
const response = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });
const flush = () => act(async () => {});
const status: CpiStatus = {
  latest_months: { DPCERG: "2026-09", DPCCRG: "2026-09" },
  observation_count: 2,
  revision_count: 0,
  batch_count: 1,
  web_capabilities: { query: true, collect: true, reason: null },
  initialization: {
    preset_id: "synthetic_pce_240",
    months: 240,
    start: "2006-10",
    end: "2026-09",
    available: true,
    reason: null,
    anchor_basis: "confirmed_local_available",
  },
};
const availability: CpiAvailability = {
  data: status,
  legacy: false,
  busy: false,
  failure: null,
};
const scope: CpiScope = {
  start: "2026-09",
  end: "2026-09",
  basket: "all",
  adjustment: "sa",
  measure: "index",
};
function result(query: CpiScope): CpiResult {
  const ids = PCE_PROFILE.series
    .filter(
      (s) => query.basket === "all" || query.basket.split(",").includes(s[2]),
    )
    .map((s) => s[0]);
  const points: CpiResult["points"] = [];
  for (
    let month = query.start;
    month <= query.end;
    month = shiftMonth(month, 1)
  ) {
    for (const id of ids)
      points.push({
        series_id: id,
        month,
        index_value: "123.456789",
        mom_pct: "0.2",
        yoy_pct: "3.12",
        change_1m_ratio: "0.002",
        change_3m_ratio: "0.006",
        change_6m_ratio: "0.012",
        change_12m_ratio: "0.0312",
        available_at: "2026-10-30T01:00:00Z",
      });
  }
  const history = query.query_mode === "history";
  return {
    start_month: query.start,
    end_month: query.end,
    series_ids: ids,
    points,
    missing: [],
    calculation_missing: [],
    complete: true,
    coverage_complete: true,
    persisted: false,
    provider_requests: history ? 0 : 1,
    provider_attempts: history ? 0 : 1,
    vintage_mode: history ? "available" : "observed",
    as_of: query.as_of ?? null,
    query_mode: history ? "history" : "current",
  };
}
function fixture() {
  const calls: {
    path: string;
    method: string;
    body?: Record<string, unknown>;
  }[] = [];
  const jobs: CpiJob[] = [];
  const enabled = {
    cpi: false,
    ppi: false,
    pce: false,
    employment: false,
    retail: false,
    consumption: false,
    gdp: false,
  };
  const transport: Transport = async (input, init) => {
    const url = new URL(String(input), "http://synthetic.invalid");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ path: url.pathname + url.search, method, body });
    const profile = [PCE_PROFILE, CPI_PROFILE, PPI_PROFILE].find((p) =>
      url.pathname.startsWith(p.basePath),
    );
    if (profile) {
      if (url.pathname.endsWith("/status"))
        return response({
          ...status,
          latest_months: Object.fromEntries(
            profile.series.map(([id]) => [id, "2026-09"]),
          ),
        });
      if (url.pathname.endsWith("/query"))
        return response(
          result(Object.fromEntries(url.searchParams) as CpiScope),
        );
      if (url.pathname.endsWith("/jobs")) {
        if (method === "POST")
          jobs.push({
            ...body,
            job_id: "synthetic-pce-job",
            state: "complete",
            created_at: "2026-10-30T01:00:00Z",
            started_at: null,
            finished_at: null,
            classification: null,
            result: {
              saved_points: 1,
              coverage_complete: false,
              readback: {
                verified: false,
                point_count: 1,
                expected_points: 2,
                source_unavailable_count: 0,
                missing: [{ series_id: "DPCCRG", month: "2026-09" }],
                source_missing: [
                  {
                    series_id: "DPCCRG",
                    month: "2026-09",
                    reason: "source_unavailable",
                  },
                ],
              },
            },
          });
        return response(method === "POST" ? jobs.at(-1) : { jobs });
      }
      if (url.pathname.endsWith("/executions"))
        return response({
          executions: jobs.map((j) => ({
            id: j.job_id,
            origin: "manual",
            action: j.action,
            start: j.start,
            end: j.end,
            created_at: j.created_at,
            started_at: null,
            finished_at: null,
            state: j.state,
            classification: null,
            result: j.result,
          })),
        });
      if (url.pathname.endsWith("/strategy")) {
        if (method === "PUT") enabled[profile.id] = body.enabled;
        return response({
          exists: true,
          enabled: enabled[profile.id],
          history_start: "2006-10",
          worker: { state: "idle", last_heartbeat_at: null },
          next_due: null,
          policy: {},
        });
      }
      if (url.pathname.endsWith("/schedule"))
        return response({
          calendar_status: "fresh",
          targets: ["2026-08", "2026-09"].map((reference_month) => ({
            reference_month,
            release_at: "2026-10-30T12:30:00Z",
            target: { state: "pending" },
            history: { state: "pending" },
          })),
        });
    }
    if (url.pathname === "/api/r5/packages")
      return response({
        status: "OK",
        packages: [
          {
            package_id: "macro",
            name: "宏观",
            history_completeness: "COMPLETE",
            capabilities: [{ title: "美国 PCE", definition_ref: PCE }],
            assets: [],
          },
        ],
      });
    if (["/api/r5/sources", "/api/r3/management"].includes(url.pathname))
      return response({ status: "OK", entries: [], instances: [] });
    return response({ classification: "not_found" }, 404);
  };
  return { transport, calls, jobs };
}
afterEach(() => {
  cleanup();
  localStorage.clear();
  sessionStorage.clear();
  location.hash = "";
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("defines precisely the BEA monthly SA indices and keeps CPI/PPI paths", () => {
  expect(PCE_PROFILE).toMatchObject({
    id: "pce",
    provider: "BEA",
    asset: PCE,
    basePath: "/api/r5/pce/timeseries",
  });
  expect(PCE_PROFILE.series).toEqual([
    ["DPCERG", "总体 · 季调", "headline", "sa"],
    ["DPCCRG", "核心 · 季调", "core", "sa"],
  ]);
  expect(buildCpiQueryRequest(scope, PCE_PROFILE).path).toContain(
    "/pce/timeseries/query?",
  );
  expect(buildCpiQueryRequest(scope).path).toContain("/cpi/timeseries/query?");
  expect(buildCpiQueryRequest(scope, PPI_PROFILE).path).toContain(
    "/ppi/timeseries/query?",
  );
});

it("opens PCE in the shared asset query with two rows, ratios and working exchange copy controls", async () => {
  const h = fixture();
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  location.hash = `ui=r5&view=assets&asset=${PCE.identity}&asset_revision=${PCE.revision}`;
  render(<App transport={h.transport} />);
  await flush();
  expect(
    screen.getByRole("heading", { name: "美国 PCE价格指数 · 范围查询" }),
  ).toBeTruthy();
  fireEvent.change(screen.getByLabelText("开始月份"), {
    target: { value: "2026-09" },
  });
  fireEvent.click(screen.getByRole("button", { name: "季节调整" }));
  expect(screen.queryByRole("checkbox", { name: "未季调" })).toBeNull();
  expect(screen.getByRole("checkbox", { name: "季调" })).toBeTruthy();
  fireEvent.keyDown(screen.getByRole("group", { name: "季节调整选项" }), {
    key: "Escape",
  });
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  await flush();
  const table = screen.getByRole("table", { name: "PCE价格指数 范围数据" });
  expect(within(table).getAllByRole("row")).toHaveLength(3);
  expect(within(table).getAllByText("3.12%")).toHaveLength(2);
  expect(within(table).getAllByText("123.456789")).toHaveLength(2);
  expect(table.textContent).not.toContain("已发布");
  const request = new URL(
    h.calls.find((c) => c.path.includes("/query?"))!.path,
    "http://synthetic.invalid",
  );
  expect(Object.fromEntries(request.searchParams)).toMatchObject({
    basket: "all",
    adjustment: "sa",
    query_mode: "current",
  });
  fireEvent.click(screen.getByRole("button", { name: "复制请求预览URL" }));
  await flush();
  expect(writeText.mock.calls.at(-1)?.[0]).toContain(
    "/api/r5/pce/timeseries/query?",
  );
  fireEvent.click(screen.getByRole("button", { name: "复制cURL" }));
  await flush();
  expect(writeText.mock.calls.at(-1)?.[0]).toMatch(/^curl --request GET '/);
  fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
  fireEvent.click(screen.getByRole("button", { name: "复制原始响应" }));
  await flush();
  expect(writeText.mock.calls.at(-1)?.[0]).toContain(
    '"index_value":"123.456789"',
  );
  expect(
    h.calls.some((c) =>
      /\/cpi\/|\/ppi\/|\/api\/pce\/(read|assets|jobs)/.test(c.path),
    ),
  ).toBe(false);
  fireEvent.click(screen.getByRole("tab", { name: "API说明" }));
  const help = screen.getByRole("region", { name: "PCE价格指数 API说明" });
  expect(help.textContent).toContain("2个统计序列");
  expect(help.textContent).toContain("DPCCRG");
  expect(help.textContent).not.toContain("BLS");
});

async function queryPceSeptember() {
  expect(
    screen.getByRole("heading", { name: "美国 PCE价格指数 · 范围查询" }),
  ).toBeTruthy();
  expect((screen.getByLabelText("结束月份") as HTMLInputElement).value).toBe(
    "2026-09",
  );
  expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
  fireEvent.change(screen.getByLabelText("开始月份"), {
    target: { value: "2026-09" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  await flush();
  const table = screen.getByRole("table", { name: "PCE价格指数 范围数据" });
  expect(table.textContent).toContain("2026-09");
  expect(within(table).getAllByText("123.456789")).toHaveLength(2);
}

function expectOnlyPceRange(calls: ReturnType<typeof fixture>["calls"]) {
  const query = calls.find((c) =>
    c.path.startsWith(`${PCE_PROFILE.basePath}/query?`),
  );
  expect(query?.method).toBe("GET");
  expect(
    Object.fromEntries(
      new URL(query!.path, "http://synthetic.invalid").searchParams,
    ),
  ).toMatchObject({ start: "2026-09", end: "2026-09" });
  expect(
    calls.some((c) =>
      /\/api\/pce\/(read|assets)|\/api\/r5\/(cpi|ppi)\/timeseries/.test(c.path),
    ),
  ).toBe(false);
}

it("uses PCE range queries after actual CPI asset navigation, refresh and history restoration", async () => {
  const h = fixture();
  const transport: Transport = async (input, init) => {
    const url = new URL(String(input), "http://synthetic.invalid");
    if (url.pathname === "/api/r5/sources")
      return response({
        status: "OK",
        entries: [CPI, PCE].map((asset) => ({
          identity: asset === PCE ? PCE_OPERATION : "synthetic-cpi",
          domain: "Macro",
          source_name: asset === PCE ? "BEA" : "BLS",
          component_identity: "synthetic-macro",
          capability_identity: "synthetic",
          operation: "synthetic",
          asset,
          code_delivery: "DELIVERED",
          source_contract: null,
          binding_contract: null,
        })),
      });
    if (url.pathname === "/api/r5/packages")
      return response({
        status: "OK",
        packages: [
          {
            package_id: "macro",
            name: "宏观",
            history_completeness: "COMPLETE",
            capabilities: [CPI, PCE].map((asset) => ({
              title: asset === PCE ? "美国 PCE" : "美国 CPI",
              definition_ref: asset,
            })),
            assets: [CPI, PCE].map((asset) => ({
              identity: asset.identity,
              revisions: [
                {
                  definition_ref: asset,
                  capability_enabled: true,
                  metadata_view_permission: "allowed",
                  local_holding: "PRESENT",
                  source_entries: [],
                  local_read: { allowed: true },
                  upstream_read: { allowed: true },
                  collect: { allowed: true },
                },
              ],
            })),
          },
        ],
      });
    return h.transport(input, init);
  };
  location.hash = new URLSearchParams({
    ui: "r5",
    view: "assets",
    asset: CPI.identity,
    asset_revision: CPI.revision,
    reference: "2026-08",
    as_of: "2026-10-30T01:00:00Z",
  }).toString();
  const cpiHash = location.hash;
  let view = render(<App transport={transport} />);
  await flush();
  expect(
    screen.getByRole("heading", { name: "美国 CPI · 范围查询" }),
  ).toBeTruthy();
  h.calls.length = 0;
  fireEvent.click(
    within(screen.getByRole("region", { name: "数据资产" })).getByRole(
      "button",
      { name: /^选择 美国 PCE / },
    ),
  );
  await flush();
  const pceHash = location.hash;
  const fields = new URLSearchParams(pceHash.slice(1));
  expect(fields.get("asset")).toBe(PCE.identity);
  expect(fields.get("reference")).toBe("2026-07");
  expect(fields.has("as_of")).toBe(true);
  expect(fields.has("version")).toBe(false);
  await queryPceSeptember();
  expectOnlyPceRange(h.calls);

  // A fresh App reads the same carried fields, as on browser refresh.
  view.unmount();
  h.calls.length = 0;
  view = render(<App transport={transport} />);
  await flush();
  await queryPceSeptember();
  expectOnlyPceRange(h.calls);

  // Replay recorded Back/Forward entries through App's browser event handlers.
  act(() => {
    history.replaceState(null, "", cpiHash);
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await flush();
  expect(
    screen.getByRole("heading", { name: "美国 CPI · 范围查询" }),
  ).toBeTruthy();
  h.calls.length = 0;
  act(() => {
    history.replaceState(null, "", pceHash);
    window.dispatchEvent(new PopStateEvent("popstate"));
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });
  await flush();
  await queryPceSeptember();
  expectOnlyPceRange(h.calls);
  view.unmount();
});

it("queries September from a direct PCE reference/as_of link without the legacy reader", async () => {
  const h = fixture();
  location.hash = new URLSearchParams({
    ui: "r5",
    view: "assets",
    asset: PCE.identity,
    asset_revision: PCE.revision,
    reference: "2026-09",
    as_of: "2026-10-30T01:00:00Z",
  }).toString();
  render(<App transport={h.transport} />);
  await flush();
  await queryPceSeptember();
  expectOnlyPceRange(h.calls);
});

it("uses basket multiselect and an available-time Beijing cutoff without upstream access", async () => {
  const h = fixture();
  render(
    <CpiTimeSeries
      transport={h.transport}
      profile={PCE_PROFILE}
      availability={availability}
    />,
  );
  fireEvent.change(screen.getByLabelText("开始月份"), {
    target: { value: "2026-09" },
  });
  fireEvent.click(screen.getByRole("button", { name: "统计篮子" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "总体" }));
  fireEvent.keyDown(screen.getByRole("group", { name: "统计篮子选项" }), {
    key: "Escape",
  });
  fireEvent.click(screen.getByRole("checkbox", { name: "历史截止查询" }));
  fireEvent.change(screen.getByLabelText("截止时间（北京时间）"), {
    target: { value: "2026-10-30T20:30" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  await flush();
  const url = new URL(h.calls[0].path, "http://synthetic.invalid");
  expect(Object.fromEntries(url.searchParams)).toMatchObject({
    basket: "core",
    adjustment: "sa",
    local_only: "true",
    vintage_mode: "available",
    as_of_basis: "available",
    as_of: "2026-10-30T20:30:00+08:00",
  });
  expect(
    within(
      screen.getByRole("table", { name: "PCE价格指数 范围数据" }),
    ).getAllByRole("row"),
  ).toHaveLength(2);
});

it("rejects foreign provider series and history results with provider attempts", async () => {
  const foreign: Transport = async () =>
    response({
      ...result(scope),
      series_ids: ["CUSR0000SA0", "CUSR0000SA0L1E"],
    });
  await expect(
    queryCpiRange(
      foreign,
      scope,
      new AbortController().signal,
      undefined,
      PCE_PROFILE,
    ),
  ).rejects.toThrow("cpi_result_scope_unknown");
  const history = {
    ...scope,
    query_mode: "history" as const,
    as_of: "2026-10-30T20:30:00+08:00",
  };
  const upstream: Transport = async () =>
    response({ ...result(history), provider_attempts: 1 });
  await expect(
    queryCpiRange(
      upstream,
      history,
      new AbortController().signal,
      undefined,
      PCE_PROFILE,
    ),
  ).rejects.toThrow("cpi_result_scope_unknown");
});

it("edits the 240-month preset and opens the exact PCE brief in the single shared execution detail", async () => {
  const h = fixture();
  location.hash = `ui=r5&view=collection&tab=new&asset=${PCE.identity}&asset_revision=${PCE.revision}`;
  render(<App transport={h.transport} />);
  await flush();
  expect(
    screen.getAllByRole("heading", { name: "PCE价格指数 受控采集" }),
  ).toHaveLength(1);
  expect(screen.getByText(/共2种口径/)).toBeTruthy();
  fireEvent.change(screen.getByLabelText("采集方式"), {
    target: { value: "initialize" },
  });
  expect(
    (screen.getByLabelText("采集开始月份") as HTMLInputElement).value,
  ).toBe("2006-10");
  expect(
    screen.getByText(/初始化服务预置范围：240个月，可自行修改/),
  ).toBeTruthy();
  fireEvent.change(screen.getByLabelText("采集开始月份"), {
    target: { value: "2026-09" },
  });
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(
    screen.getByRole("button", { name: "提交 PCE价格指数 采集" }),
  );
  await flush();
  expect(h.calls.find((c) => c.method === "POST")).toMatchObject({
    path: `${PCE_PROFILE.basePath}/jobs`,
    body: { action: "initialize", start: "2026-09", end: "2026-09" },
  });
  expect(localStorage.getItem("mdf.pce.timeseries.pending.v1")).toContain(
    "2026-09",
  );
  expect(localStorage.getItem("mdf.cpi.timeseries.pending.v1")).toBeNull();
  const brief = screen.getByRole("complementary", { name: "执行简报" });
  expect(brief.textContent).toContain("2026-10-30 09:00:00");
  fireEvent.click(within(brief).getByRole("button", { name: /美国 PCE/ }));
  await flush();
  const records = screen.getByRole("region", { name: "执行记录" });
  expect(within(records).getAllByRole("table")).toHaveLength(1);
  expect(screen.getAllByRole("heading", { name: "执行详情" })).toHaveLength(1);
  expect(screen.getByText("synthetic-pce-job")).toBeTruthy();
  expect(
    screen.getByText("范围覆盖：1 / 2 条，来源未返回 1 条。"),
  ).toBeTruthy();
  expect(
    h.calls.some((c) => /\/cpi\/|\/ppi\/|\/api\/pce\/jobs/.test(c.path)),
  ).toBe(false);
});

it("lists all three preset strategies in one table and uses explicit BEA calendar reference months", async () => {
  const h = fixture();
  location.hash = "ui=r5&view=collection&tab=periodic";
  render(<App transport={h.transport} />);
  await flush();
  const region = screen.getByRole("region", { name: "周期任务" });
  expect(within(region).getAllByRole("table")).toHaveLength(1);
  expect(within(region).getAllByRole("row")).toHaveLength(4);
  fireEvent.click(
    within(region).getByRole("button", { name: /选择 美国 PCE/ }),
  );
  await flush();
  expect(
    screen.getByRole("heading", { name: "PCE价格指数 策略详情" }),
  ).toBeTruthy();
  expect(screen.getByText(/共2种口径/)).toBeTruthy();
  const calendar = screen.getByRole("list", { name: "PCE价格指数 月份计划" });
  expect(within(calendar).getAllByRole("listitem")).toHaveLength(2);
  expect(calendar.textContent).toContain("2026-08");
  expect(calendar.textContent).toContain("2026-09");
  expect(calendar.textContent).toContain("2026-10-30 20:30:00");
  const row = within(region)
    .getByRole("button", { name: /选择 美国 PCE/ })
    .closest("tr")!;
  fireEvent.click(within(row).getByRole("button", { name: "启动" }));
  await flush();
  expect(h.calls.filter((c) => c.method === "PUT")).toEqual([
    {
      path: `${PCE_PROFILE.basePath}/strategy`,
      method: "PUT",
      body: { enabled: true },
    },
  ]);
});

it.each([503, "transport"])(
  "keeps unavailable PCE qualification isolated (%s)",
  async (failure) => {
    const h = fixture();
    const transport: Transport = async (input, init) => {
      if (String(input).startsWith(`${PCE_PROFILE.basePath}/status`)) {
        if (typeof failure !== "number")
          throw new Error("synthetic_private_error");
        return response({ classification: "capability_disabled" }, failure);
      }
      return h.transport(input, init);
    };
    location.hash = `ui=r5&view=collection&tab=new&asset=${PCE.identity}&asset_revision=${PCE.revision}`;
    render(<App transport={transport} />);
    await flush();
    expect(screen.getByText(/PCE价格指数 服务状态未知/)).toBeTruthy();
    expect(
      (
        screen.getByRole("button", {
          name: "提交 PCE价格指数 采集",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    expect(screen.queryByText(/synthetic_private_error/)).toBeNull();
    expect(
      h.calls.some((c) => /\/cpi\/|\/ppi\/|\/api\/pce\/jobs/.test(c.path)),
    ).toBe(false);
  },
);

it("reports a missing initialization preset without inventing a range", async () => {
  const h = fixture();
  render(
    <CpiTimeSeriesCollection
      transport={h.transport}
      profile={PCE_PROFILE}
      availability={{
        ...availability,
        data: { ...status, initialization: undefined },
      }}
      tab="new"
    />,
  );
  await flush();
  fireEvent.change(screen.getByLabelText("采集方式"), {
    target: { value: "initialize" },
  });
  expect(
    (screen.getByLabelText("采集开始月份") as HTMLInputElement).value,
  ).toBe("");
  expect(screen.getByText(/服务暂无默认范围/)).toBeTruthy();
  expect(
    (
      screen.getByRole("button", {
        name: "提交 PCE价格指数 采集",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
});

it("can filter execution records to PCE without changing the selected provider", async () => {
  const h = fixture();
  location.hash = `ui=r5&view=collection&tab=executions&execution_asset=${encodeURIComponent(keyOf(PCE))}`;
  render(<App transport={h.transport} />);
  await flush();
  expect(
    (screen.getByLabelText("执行资产筛选") as HTMLSelectElement).value,
  ).toBe(keyOf(PCE));
  expect(
    h.calls.some((c) => c.path === `${PCE_PROFILE.basePath}/executions`),
  ).toBe(true);
  expect(
    h.calls.some((c) =>
      /\/(cpi|ppi)\/(timeseries\/)?(jobs|plans|executions)/.test(c.path),
    ),
  ).toBe(false);
});

function AvailabilityProbe({ transport }: { transport: Transport }) {
  const value = useCpiAvailability(transport, 0, true, false, PCE_PROFILE);
  return <output>{JSON.stringify(value)}</output>;
}
it("uses legacy PCE only for a missing timeseries host and rejects foreign status series", async () => {
  const paths: string[] = [];
  const missing: Transport = async (input) => {
    paths.push(String(input));
    return response({}, 404);
  };
  const view = render(<AvailabilityProbe transport={missing} />);
  await flush();
  expect(screen.getByRole("status").textContent).toContain('"legacy":true');
  expect(paths).toEqual([`${PCE_PROFILE.basePath}/status`]);
  const foreign: Transport = async () =>
    response({ ...status, latest_months: { CUSR0000SA0: "2026-09" } });
  view.rerender(<AvailabilityProbe transport={foreign} />);
  await flush();
  expect(screen.getByRole("status").textContent).toContain('"legacy":false');
  expect(screen.getByRole("status").textContent).toContain('"data":null');
});

it.each<[boolean, number, string?]>([
  [false, 404],
  [true, 404],
  [false, 503],
  [true, 503],
  [false, 200],
  [true, 200],
  [false, 403],
  [false, 503, "capability_disabled"],
  [false, 503, ""],
])(
  "routes PCE reference/as_of links by availability, retaining explicit versions (exact=%s, status=%s, classification=%s)",
  async (exact, statusCode, failureClass = "pce_timeseries_not_enabled") => {
    const h = fixture();
    // This constant matches the retained DTO binding; all requests remain in this function.
    const binding: PceBinding = {
      operator: "ao-dev-bea-pce",
      definition: PCE.identity,
      definition_revision: PCE.revision,
      source: "mdf.source-candidate.bea.nipa.pce",
      source_revision: "t04-retained-candidate-r1",
      operation: "GetData:NIPA:T20807:T20804:M",
      binding: "mdf.binding-candidate.bea.nipa.pce",
      binding_revision: "t04-retained-candidate-r1",
      reference: "2026-07",
      source_qualified: false,
      target: {
        host: "192.168.10.4",
        database: "market_data_foundation_dev",
        schema: "mdf_macro_bea_pce_vertical_r1",
      },
    };
    const at = "2026-10-30T01:00:00Z";
    const reads: URL[] = [];
    const legacyPaths: string[] = [];
    const transport: Transport = async (input, init) => {
      const url = new URL(String(input), "http://synthetic.invalid");
      if (/^\/api\/pce\/(read|assets)$/.test(url.pathname))
        legacyPaths.push(url.pathname);
      if (url.pathname === `${PCE_PROFILE.basePath}/status`)
        return statusCode === 200
          ? response(status)
          : response(
              failureClass ? { classification: failureClass } : {},
              statusCode,
            );
      if (url.pathname === "/api/r5/sources")
        return response({
          status: "OK",
          entries: [
            {
              identity: PCE_OPERATION,
              domain: "Macro",
              source_name: "BEA",
              component_identity: "synthetic-pce",
              capability_identity: "synthetic",
              operation: binding.operation,
              asset: PCE,
              code_delivery: "DELIVERED",
              source_contract: null,
              binding_contract: null,
            },
          ],
        });
      if (url.pathname === "/api/pce/catalog")
        return response({
          binding,
          parameters: [],
          measurements: Object.keys(pceLabels),
          source_qualified: false,
          write_eligible: false,
          submission: {
            binding,
            confirmation: "synthetic only",
            job_identity: "synthetic-legacy",
          },
        });
      if (url.pathname === "/api/pce/assets")
        return response({
          binding,
          as_of: at,
          versions: [
            {
              version_identity: "synthetic-legacy:version",
              frozen_at: at,
              selection_state: "stored_candidate_read_unverified",
              publications: [],
            },
          ],
        });
      if (url.pathname === "/api/pce/read") {
        reads.push(url);
        return response({
          binding,
          reference: "2026-07",
          as_of: url.searchParams.get("as_of"),
          version_identity:
            url.searchParams.get("version") ?? "synthetic-legacy:version",
          publication_identity: "synthetic-legacy:publication",
          acquisition_identity: "synthetic-legacy:acquisition",
          known_at: at,
          usable_at: at,
          source_qualified: false,
          measurements: Object.fromEntries(
            Object.keys(pceLabels).map((k) => [
              k,
              { value: "0.25", classification: "synthetic" },
            ]),
          ),
        });
      }
      return h.transport(input, init);
    };
    location.hash = new URLSearchParams({
      ui: "r5",
      view: "assets",
      asset: PCE.identity,
      asset_revision: PCE.revision,
      reference: "2026-07",
      as_of: at,
      ...(exact ? { version: "synthetic-legacy:version" } : {}),
    }).toString();
    render(<App transport={transport} />);
    await flush();
    if (
      !exact &&
      (statusCode === 403 ||
        (statusCode === 503 && failureClass !== "pce_timeseries_not_enabled"))
    ) {
      expect(
        screen.getByRole("heading", { name: "美国 PCE价格指数 · 范围查询" }),
      ).toBeTruthy();
      expect(
        (screen.getByRole("button", { name: "查询数据" }) as HTMLButtonElement)
          .disabled,
      ).toBe(true);
      expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
      expect(legacyPaths).toHaveLength(0);
      expect(h.calls.some((c) => /\/(cpi|ppi)\//.test(c.path))).toBe(false);
      return;
    }
    if (!exact && statusCode === 200) {
      expect(
        screen.getByRole("heading", { name: "美国 PCE价格指数 · 范围查询" }),
      ).toBeTruthy();
      expect(reads).toHaveLength(0);
      expect(legacyPaths).toHaveLength(0);
      expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
      fireEvent.change(screen.getByLabelText("开始月份"), {
        target: { value: "2026-09" },
      });
      fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
      await flush();
      expect(
        screen.getByRole("table", { name: "PCE价格指数 范围数据" }).textContent,
      ).toContain("2026-09");
      expect(reads).toHaveLength(0);
      expect(h.calls.some((c) => /\/(cpi|ppi)\//.test(c.path))).toBe(false);
      return;
    }
    expect(
      screen.queryByRole("heading", { name: "美国 PCE价格指数 · 范围查询" }),
    ).toBeNull();
    const read = screen.getByRole("button", {
      name: "读取本地数据",
    }) as HTMLButtonElement;
    expect(read.disabled).toBe(false);
    fireEvent.click(read);
    await flush();
    expect(
      screen.getByRole("region", { name: "读取结果" }).textContent,
    ).toContain("读取成功");
    expect(reads.at(-1)?.searchParams.get("version")).toBe(
      exact ? "synthetic-legacy:version" : null,
    );
    expect(reads.at(-1)?.searchParams.get("reference")).toBe("2026-07");
    expect(h.calls.some((c) => /\/(cpi|ppi)\//.test(c.path))).toBe(false);
  },
);

// Only the explicit unassembled-capability signal permits the compatibility reader.
it.each([
  [503, { classification: "pce_timeseries_not_enabled" }, true],
  [503, { classification: "capability_disabled" }, false],
  [503, {}, false],
  [401, { classification: "pce_timeseries_not_enabled" }, false],
  [403, { classification: "pce_timeseries_not_enabled" }, false],
  [200, {}, false],
  ["transport", null, false],
] as const)(
  "gates PCE legacy availability precisely (%s, %j)",
  async (code, body, legacy) => {
    const transport: Transport = async () => {
      if (code === "transport") throw new Error("synthetic_private_error");
      return response(body, code);
    };
    render(<AvailabilityProbe transport={transport} />);
    await flush();
    const value = JSON.parse(screen.getByRole("status").textContent!);
    expect(value.legacy).toBe(legacy);
    expect(value.data).toBeNull();
    expect(value.failure === null).toBe(legacy);
  },
);
