import { useState } from "react";
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
import { CpiStrategyWorkbench } from "./CpiStrategyWorkbench";
import { CollectionWorkbench } from "./CollectionWorkbench";
import {
  CPI_PROFILE,
  PPI_PROFILE,
  type MacroProfile,
} from "./macroTimeSeriesProfile";
import { CPI, PPI, keyOf, type Asset } from "./workflowModel";
import type { Navigate, Route } from "./workflowRoute";
import {
  buildCpiQueryRequest,
  queryCpiRange,
  shiftMonth,
  useCpiAvailability,
  type CpiAvailability,
  type CpiJob,
  type CpiResult,
  type CpiScope,
  type CpiStatus,
} from "./cpiTimeSeriesApi";

// Every request stays in this deterministic in-memory transport; no sockets or providers.
const response = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });
const flush = () => act(async () => {});
const scope: CpiScope = {
  start: "2026-09",
  end: "2026-09",
  basket: "all",
  adjustment: "all",
  measure: "index",
};
function status(profile = PPI_PROFILE): CpiStatus {
  return {
    latest_months: Object.fromEntries(
      profile.series.map(([id]) => [id, "2026-09"]),
    ),
    observation_count: 4,
    revision_count: 0,
    batch_count: 1,
    web_capabilities: { query: true, collect: true, reason: null },
    initialization: {
      preset_id: `${profile.id}_latest_20y_v1`,
      months: 240,
      start: "2006-10",
      end: "2026-09",
      available: true,
      reason: null,
      anchor_basis: "confirmed_local_available",
    },
  };
}
const availability = (profile = PPI_PROFILE): CpiAvailability => ({
  data: status(profile),
  legacy: false,
  busy: false,
  failure: null,
});
function result(profile: MacroProfile, query: CpiScope): CpiResult {
  const series = profile.series.filter(
    (s) =>
      (query.basket === "all" || query.basket.split(",").includes(s[2])) &&
      ["all", s[3]].includes(query.adjustment),
  );
  const points: CpiResult["points"] = [];
  for (
    let month = query.start;
    month <= query.end;
    month = shiftMonth(month, 1)
  ) {
    for (const [id] of series)
      points.push({
        series_id: id,
        month,
        index_value: profile.id === "ppi" ? "141.12345" : "320.54321",
        mom_pct: "0.2",
        yoy_pct: "2.4",
        change_1m_ratio: "0.002",
        change_3m_ratio: "0.006",
        change_6m_ratio: "0.012",
        change_12m_ratio: "0.024",
      });
  }
  const history = query.query_mode === "history";
  return {
    start_month: query.start,
    end_month: query.end,
    series_ids: series.map((s) => s[0]),
    points,
    missing: [],
    calculation_missing: [],
    complete: true,
    coverage_complete: true,
    persisted: false,
    provider_requests: 0,
    provider_attempts: 0,
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
    claims: false,
  };
  const transport: Transport = async (input, init) => {
    const url = new URL(String(input), "http://fixture.invalid");
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({
      path: url.pathname + url.search,
      method: init?.method ?? "GET",
      body,
    });
    const profile = url.pathname.startsWith(PPI_PROFILE.basePath)
      ? PPI_PROFILE
      : CPI_PROFILE;
    if (url.pathname.endsWith("/status")) return response(status(profile));
    if (url.pathname.endsWith("/query"))
      return response(
        result(profile, Object.fromEntries(url.searchParams) as CpiScope),
      );
    if (url.pathname.endsWith("/jobs")) {
      if (init?.method === "POST") {
        const job: CpiJob = {
          ...body,
          job_id: `${profile.id}-job`,
          state: "complete",
          created_at: "2026-10-01T01:00:00Z",
          started_at: null,
          finished_at: null,
          classification: null,
          result: { saved_points: 4 },
        };
        jobs.push(job);
        return response(job);
      }
      return response({ jobs });
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
      if (init?.method === "PUT") enabled[profile.id] = body.enabled;
      return response({
        exists: true,
        enabled: enabled[profile.id],
        history_start: profile.id === "ppi" ? "2009-11" : "1999-01",
        worker: { state: "idle", last_heartbeat_at: null },
        next_due: null,
        policy: {},
      });
    }
    if (url.pathname.endsWith("/schedule"))
      return response({
        calendar_status: "fresh",
        targets: [
          {
            reference_month: "2026-09",
            release_at: "2026-10-14T12:30:00Z",
            target: { state: "pending" },
            history: { state: "pending" },
          },
        ],
      });
    if (url.pathname === "/api/r5/packages")
      return response({
        status: "OK",
        packages: [
          {
            package_id: "macro",
            name: "宏观",
            history_completeness: "COMPLETE",
            capabilities: [{ title: "美国 PPI", definition_ref: PPI }],
            assets: [],
          },
        ],
      });
    if (
      url.pathname === "/api/r5/sources" ||
      url.pathname === "/api/r3/management"
    )
      return response({ status: "OK", entries: [], instances: [] });
    return response(
      { status: "NOT_AVAILABLE", classification: "not_found" },
      404,
    );
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
it("preserves the CPI default and orders the four PPI definitions with the exact core meaning", () => {
  expect(buildCpiQueryRequest(scope).path).toMatch(
    /^\/api\/r5\/cpi\/timeseries\/query\?/,
  );
  expect(buildCpiQueryRequest(scope, PPI_PROFILE).path).toMatch(
    /^\/api\/r5\/ppi\/timeseries\/query\?/,
  );
  expect(PPI_PROFILE.series.map((s) => s[0])).toEqual([
    "WPSFD4",
    "WPUFD4",
    "WPSFD49104",
    "WPUFD49104",
    "WPSFD49116",
    "WPUFD49116",
  ]);
  expect(PPI_PROFILE.series.map((s) => s.slice(2))).toEqual([
    ["headline", "sa"],
    ["headline", "nsa"],
    ["core", "sa"],
    ["core", "nsa"],
    ["core_ex_trade", "sa"],
    ["core_ex_trade", "nsa"],
  ]);
  expect(PPI_PROFILE.historyNotice).toContain(
    "核心指剔除食品和能源的最终需求，仍含贸易服务",
  );
});
it("validates PPI JSON against PPI series and refuses a CPI result", async () => {
  const h = fixture();
  expect(
    (
      await queryCpiRange(
        h.transport,
        scope,
        new AbortController().signal,
        undefined,
        PPI_PROFILE,
      )
    ).series_ids,
  ).toEqual(PPI_PROFILE.series.map((s) => s[0]));
  await expect(
    queryCpiRange(
      async () => response(result(CPI_PROFILE, scope)),
      scope,
      new AbortController().signal,
      undefined,
      PPI_PROFILE,
    ),
  ).rejects.toThrow("cpi_result_scope_unknown");
  expect(h.calls.every((c) => c.path.startsWith(PPI_PROFILE.basePath))).toBe(
    true,
  );
});
it("opens the PPI asset with copied actual URL/cURL, ordered rows and original JSON", async () => {
  const h = fixture();
  const writeText = vi.fn(async (_text: string) => {});
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  location.hash = `ui=r5&view=assets&asset=${PPI.identity}&asset_revision=${PPI.revision}`;
  render(<App transport={h.transport} />);
  await flush();
  const query = within(screen.getByRole("region", { name: "PPI 范围查询" }));
  expect(query.getAllByText(PPI_PROFILE.historyNotice!)).toHaveLength(2);
  fireEvent.change(query.getByLabelText("开始月份"), {
    target: { value: "2026-09" },
  });
  fireEvent.click(query.getByRole("button", { name: "复制请求预览URL" }));
  await flush();
  expect(writeText.mock.calls.at(-1)?.[0]).toContain(
    "/api/r5/ppi/timeseries/query?start=2026-09&end=2026-09",
  );
  fireEvent.click(
    query
      .getAllByText("cURL 示例（当前服务地址）")
      .find((element) => !element.closest("[hidden]"))!,
  );
  fireEvent.click(query.getByRole("button", { name: "复制cURL" }));
  await flush();
  expect(writeText.mock.calls.at(-1)?.[0]).toContain("curl --request GET");
  fireEvent.click(query.getByRole("button", { name: "查询数据" }));
  await flush();
  const rows = within(query.getByRole("table", { name: "PPI 范围数据" }))
    .getAllByRole("row")
    .slice(1);
  expect(rows.map((r) => r.textContent)).toEqual(
    PPI_PROFILE.series
      .filter(([id]) => PPI_PROFILE.defaultQuerySeries?.includes(id))
      .map(([, label]) => expect.stringContaining(label)),
  );
  fireEvent.click(query.getByRole("tab", { name: "原始响应" }));
  expect(query.getByText(/"index_value":"141.12345"/)).toBeTruthy();
  fireEvent.click(query.getByRole("tab", { name: "API说明" }));
  expect(query.getByText("GET /api/r5/ppi/timeseries/query")).toBeTruthy();
  expect(
    h.calls.filter(
      (c) => c.path.includes("/cpi/") || /\/pce\/(jobs|read)/.test(c.path),
    ),
  ).toEqual([]);
});
it("keeps Beijing history parameters and multiselect scopes on the PPI endpoint", async () => {
  const h = fixture();
  render(
    <CpiTimeSeries
      transport={h.transport}
      availability={availability()}
      profile={PPI_PROFILE}
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
    target: { value: "2026-10-15T20:30" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  await flush();
  const url = new URL(h.calls[0].path, "http://fixture.invalid");
  expect(url.pathname).toBe("/api/r5/ppi/timeseries/query");
  expect(Object.fromEntries(url.searchParams)).toMatchObject({
    basket: "core",
    adjustment: "all",
    query_mode: "history",
    local_only: "true",
    as_of_basis: "available",
    as_of: "2026-10-15T20:30:00+08:00",
  });
});
it("discards an aborted CPI query and raw exchange after switching to PPI", async () => {
  const h = fixture();
  let finish!: (value: Response) => void;
  const delayed = new Promise<Response>((resolve) => {
    finish = resolve;
  });
  const transport: Transport = (input, init) =>
    String(input).startsWith(`${CPI_PROFILE.basePath}/query`)
      ? delayed
      : h.transport(input, init);
  const view = render(
    <CpiTimeSeries
      transport={transport}
      availability={availability(CPI_PROFILE)}
    />,
  );
  fireEvent.change(screen.getByLabelText("开始月份"), {
    target: { value: "2026-09" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  view.rerender(
    <CpiTimeSeries
      transport={transport}
      availability={availability()}
      profile={PPI_PROFILE}
    />,
  );
  await act(async () => {
    finish(response(result(CPI_PROFILE, scope)));
  });
  expect(screen.queryByRole("table", { name: "CPI 范围数据" })).toBeNull();
  expect(screen.queryByText(/320.54321/)).toBeNull();
  fireEvent.change(screen.getByLabelText("开始月份"), {
    target: { value: "2026-09" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  await flush();
  expect(screen.getByRole("table", { name: "PPI 范围数据" })).toBeTruthy();
  expect(screen.queryByText(/320.54321/)).toBeNull();
});
function AvailabilityProbe({
  transport,
  profile,
}: {
  transport: Transport;
  profile: MacroProfile;
}) {
  const value = useCpiAvailability(transport, 0, true, false, profile);
  return <output>{JSON.stringify(value)}</output>;
}
it("isolates late status and treats a missing PPI host as unavailable without CPI fallback", async () => {
  let finish!: (value: Response) => void;
  const delayed = new Promise<Response>((resolve) => {
    finish = resolve;
  });
  const calls: string[] = [];
  const transport: Transport = async (input) => {
    const path = String(input);
    calls.push(path);
    return path.startsWith(CPI_PROFILE.basePath) ? delayed : response({}, 404);
  };
  const view = render(
    <AvailabilityProbe transport={transport} profile={CPI_PROFILE} />,
  );
  view.rerender(
    <AvailabilityProbe transport={transport} profile={PPI_PROFILE} />,
  );
  await flush();
  await act(async () => {
    finish(response(status(CPI_PROFILE)));
  });
  expect(screen.getByRole("status").textContent).toContain('"legacy":false');
  expect(screen.getByRole("status").textContent).toContain(
    '"failure":"read_failed"',
  );
  expect(screen.getByRole("status").textContent).toContain('"data":null');
  expect(calls).toEqual([
    `${CPI_PROFILE.basePath}/status`,
    `${PPI_PROFILE.basePath}/status`,
  ]);
});
it("keeps pending collection intents separate and takes the editable initialization range from the host", async () => {
  const h = fixture();
  localStorage.setItem(
    "mdf.cpi.timeseries.pending.v1",
    JSON.stringify({
      request_id: "00000000-0000-4000-8000-000000000001",
      action: "update",
      start: "2026-01",
      end: "2026-01",
    }),
  );
  render(
    <CpiTimeSeriesCollection
      profile={PPI_PROFILE}
      transport={h.transport}
      availability={availability()}
      tab="new"
    />,
  );
  await flush();
  fireEvent.change(screen.getByLabelText("采集方式"), {
    target: { value: "initialize" },
  });
  expect(
    (screen.getByLabelText("采集开始月份") as HTMLInputElement).value,
  ).toBe("2006-10");
  expect(screen.getByText(/服务预置月份：2006-10 至 2026-09/)).toBeTruthy();
  expect(
    screen.getByText(/初始化服务预置范围：240个月，可自行修改/),
  ).toBeTruthy();
  expect(screen.getByText(PPI_PROFILE.historyNotice!)).toBeTruthy();
  fireEvent.change(screen.getByLabelText("采集开始月份"), {
    target: { value: "2010-01" },
  });
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "提交 PPI 采集" }));
  await flush();
  expect(h.calls.find((c) => c.method === "POST")).toMatchObject({
    path: "/api/r5/ppi/timeseries/jobs",
    body: { action: "initialize", start: "2010-01", end: "2026-09" },
  });
  expect(localStorage.getItem("mdf.ppi.timeseries.pending.v1")).toContain(
    "2010-01",
  );
  expect(localStorage.getItem("mdf.cpi.timeseries.pending.v1")).toContain(
    "2026-01",
  );
});
const assets: Asset[] = [CPI, PPI].map((key) => ({
  key,
  label: key.identity === PPI.identity ? "美国 PPI" : "美国 CPI",
  interfaces: [],
  catalog: null,
  holding: "",
  versions: null,
  publication: "",
  readability: "",
  storage: "",
}));
function Workbench({
  transport,
  tab = "new",
  assetKey = PPI,
  job,
}: {
  transport: Transport;
  tab?: string;
  assetKey?: typeof PPI | null;
  job?: string;
}) {
  const [route, setRoute] = useState<Route>({
    view: "collection",
    invalid: false,
    fields: new URLSearchParams({
      tab,
      ...(assetKey
        ? { asset: assetKey.identity, asset_revision: assetKey.revision }
        : {}),
      ...(job ? { job } : {}),
    }),
  });
  const navigate: Navigate = (view, fields = {}, preserve) =>
    setRoute((old) => {
      const next = new URLSearchParams(preserve ? old.fields : undefined);
      for (const [key, value] of Object.entries(fields))
        value === null ? next.delete(key) : next.set(key, value);
      return { view, invalid: false, fields: next };
    });
  return (
    <CollectionWorkbench
      assets={assets}
      asset={assets.find((a) => a.key.identity === route.fields.get("asset"))}
      source={undefined}
      route={route}
      navigate={navigate}
      transport={transport}
      generation={0}
      invalidIdentity={false}
      cpiAvailability={availability(CPI_PROFILE)}
      ppiAvailability={availability()}
    />
  );
}
it("opens the exact PPI execution from its brief in the single existing list and detail", async () => {
  const h = fixture();
  render(<Workbench transport={h.transport} />);
  await flush();
  expect((screen.getByLabelText("采集目标") as HTMLSelectElement).value).toBe(
    keyOf(PPI),
  );
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "提交 PPI 采集" }));
  await flush();
  fireEvent.click(
    screen.getByRole("button", { name: /美国 PPI · 更新指定范围/ }),
  );
  await flush();
  expect(
    within(screen.getByRole("region", { name: "执行记录" })).getAllByRole(
      "table",
    ),
  ).toHaveLength(1);
  expect(screen.getAllByRole("heading", { name: "执行详情" })).toHaveLength(1);
  const detail = within(
    screen.getByRole("complementary", { name: "执行记录详情" }),
  );
  expect(detail.getByText("ppi-job")).toBeTruthy();
  expect(detail.getByText("美国 PPI")).toBeTruthy();
  expect(detail.getByText("手动采集")).toBeTruthy();
  expect(detail.getByText("2026-09 至 2026-09")).toBeTruthy();
});
it("shows CPI and PPI as two preset rows in one table with one toggle each and shared PPI calendar detail", async () => {
  const h = fixture();
  render(
    <CpiStrategyWorkbench
      transport={h.transport}
      availability={availability(CPI_PROFILE)}
      additionalProfile={PPI_PROFILE}
      additionalAvailability={availability()}
    />,
  );
  await flush();
  expect(screen.getAllByRole("table")).toHaveLength(1);
  const row = screen.getByText("美国 PPI").closest("tr")!;
  expect(
    within(row).getAllByRole("button", { name: /^(启动|暂停)$/ }),
  ).toHaveLength(1);
  fireEvent.click(within(row).getByRole("button", { name: "启动" }));
  await flush();
  expect(h.calls.filter((c) => c.method === "PUT")).toEqual([
    {
      path: "/api/r5/ppi/timeseries/strategy",
      method: "PUT",
      body: { enabled: true },
    },
  ]);
  fireEvent.click(within(row).getByRole("button", { name: /^选择/ }));
  await flush();
  expect(screen.getByRole("heading", { name: "PPI 策略详情" })).toBeTruthy();
  expect(screen.getByRole("list", { name: "PPI 月份计划" })).toBeTruthy();
  expect(
    within(
      screen.getByRole("complementary", { name: "周期任务详情" }),
    ).queryByRole("button"),
  ).toBeNull();
});
it("shows an unconfigured PPI host honestly and blocks query and collection without CPI fallback", async () => {
  const h = fixture();
  const missing: Transport = async (input, init) =>
    String(input) === `${PPI_PROFILE.basePath}/status`
      ? response({}, 404)
      : h.transport(input, init);
  location.hash = `ui=r5&view=assets&asset=${PPI.identity}&asset_revision=${PPI.revision}`;
  render(<App transport={missing} />);
  await flush();
  expect(screen.getByText(/PPI 服务状态未知/)).toBeTruthy();
  expect(
    (screen.getByRole("button", { name: "查询数据" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "采集数据" }));
  await flush();
  expect(screen.getByRole("heading", { name: "PPI 受控采集" })).toBeTruthy();
  expect(
    (screen.getByRole("button", { name: "提交 PPI 采集" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  expect(h.calls.some((c) => c.path.includes("/cpi/"))).toBe(false);
});
it("keeps a late CPI mutation receipt out of the newly selected PPI collection", async () => {
  const h = fixture();
  let finish!: (value: Response) => void;
  let submitted: Record<string, string> | undefined;
  const delayed = new Promise<Response>((resolve) => {
    finish = resolve;
  });
  const transport: Transport = (input, init) => {
    if (
      String(input) === `${CPI_PROFILE.basePath}/jobs` &&
      init?.method === "POST"
    ) {
      submitted = JSON.parse(String(init.body));
      return delayed;
    }
    return h.transport(input, init);
  };
  const view = render(
    <CpiTimeSeriesCollection
      transport={transport}
      availability={availability(CPI_PROFILE)}
      tab="new"
    />,
  );
  await flush();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "提交 CPI 采集" }));
  view.rerender(
    <CpiTimeSeriesCollection
      transport={transport}
      availability={availability()}
      profile={PPI_PROFILE}
      tab="new"
    />,
  );
  await flush();
  await act(async () => {
    finish(
      response({
        ...submitted,
        job_id: "late-cpi-receipt",
        state: "complete",
        created_at: "2026-10-01T01:00:00Z",
        started_at: null,
        finished_at: null,
        classification: null,
        result: null,
      }),
    );
  });
  expect(screen.queryByText(/late-cpi-receipt/)).toBeNull();
  expect(screen.getByText("暂无手动采集记录。")).toBeTruthy();
  expect(localStorage.getItem("mdf.cpi.timeseries.pending.v1")).toContain(
    submitted!.request_id,
  );
  expect(localStorage.getItem("mdf.ppi.timeseries.pending.v1")).toBeNull();
});
it("disambiguates identical execution IDs from CPI and PPI in the unified list", async () => {
  const h = fixture();
  const transport: Transport = async (input, init) => {
    if (String(input).endsWith("/executions")) {
      const ppi = String(input).startsWith(PPI_PROFILE.basePath);
      return response({
        executions: [
          {
            id: "same-native-id",
            origin: ppi ? "automatic" : "manual",
            action: "update",
            stage: ppi ? "history" : null,
            start: ppi ? "2010-01" : "2000-01",
            end: "2026-09",
            created_at: "2026-10-01T01:00:00Z",
            started_at: null,
            finished_at: null,
            state: "complete",
            classification: null,
            result: null,
          },
        ],
      });
    }
    return h.transport(input, init);
  };
  render(<Workbench transport={transport} tab="executions" />);
  await flush();
  const list = within(screen.getByRole("region", { name: "执行记录" }));
  expect(list.getAllByRole("row")).toHaveLength(3);
  fireEvent.click(
    list.getByRole("button", { name: /选择 same-native-id 美国 PPI/ }),
  );
  await flush();
  const detail = within(
    screen.getByRole("complementary", { name: "执行记录详情" }),
  );
  expect(detail.getByText("美国 PPI")).toBeTruthy();
  expect(detail.getByText("2010-01 至 2026-09")).toBeTruthy();
  expect(detail.getByText("自动策略 · 最近真实执行摘要")).toBeTruthy();
  fireEvent.click(
    list.getByRole("button", { name: /选择 same-native-id 美国 CPI/ }),
  );
  await flush();
  expect(detail.getByText("美国 CPI")).toBeTruthy();
  expect(detail.getByText("2000-01 至 2026-09")).toBeTruthy();
});
it("locates a late PPI execution beyond the first page after a brief jump", async () => {
  const h = fixture();
  let finish!: (value: Response) => void;
  const delayed = new Promise<Response>((resolve) => {
    finish = resolve;
  });
  const transport: Transport = (input, init) =>
    String(input) === `${PPI_PROFILE.basePath}/executions`
      ? delayed
      : h.transport(input, init);
  render(<Workbench transport={transport} />);
  await flush();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "提交 PPI 采集" }));
  await flush();
  fireEvent.click(
    screen.getByRole("button", { name: /美国 PPI · 更新指定范围/ }),
  );
  await flush();
  expect(screen.queryByRole("heading", { name: "执行详情" })).toBeNull();
  const record = {
    id: "ppi-job",
    origin: "manual",
    action: "update",
    start: "2026-09",
    end: "2026-09",
    created_at: "2026-10-01T01:00:00Z",
    started_at: null,
    finished_at: null,
    state: "complete",
    classification: null,
    result: null,
  };
  await act(async () => {
    finish(
      response({
        executions: [
          ...Array.from({ length: 25 }, (_, i) => ({
            ...record,
            id: `older-${i}`,
          })),
          record,
        ],
      }),
    );
  });
  expect(screen.getByText("第 2 页")).toBeTruthy();
  const row = screen
    .getByRole("button", { name: /选择 ppi-job 美国 PPI/ })
    .closest("tr")!;
  expect(row.getAttribute("aria-selected")).toBe("true");
  expect(
    within(
      screen.getByRole("complementary", { name: "执行记录详情" }),
    ).getByText("ppi-job"),
  ).toBeTruthy();
  expect(
    h.calls.some((c) => c.path === `${CPI_PROFILE.basePath}/executions`),
  ).toBe(false);
});

it("renders a complete PPI collection with source gaps in brief, row and detail", async () => {
  const h = fixture();
  const partial = {
    saved_points: 2,
    coverage_complete: false,
    completion: "source_coverage_partial",
    readback: {
      verified: true,
      point_count: 2,
      expected_points: 4,
      source_unavailable_count: 1,
      missing: [{ series_id: "WPSFD4", month: "2026-09" }],
      source_missing: [
        { series_id: "WPSFD4", month: "2026-09", reason: "not_provided" },
      ],
      source_unavailable: [{ series_id: "WPUFD4", month: "2026-09" }],
    },
  };
  const transport: Transport = async (input, init) => {
    const value = await h.transport(input, init);
    if (String(input).endsWith("/jobs") && init?.method === "POST") {
      h.jobs.at(-1)!.result = partial;
      return response(h.jobs.at(-1));
    }
    return value;
  };
  render(<Workbench transport={transport} />);
  await flush();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "提交 PPI 采集" }));
  await flush();
  expect(screen.getByText("已完成（来源有缺口）")).toBeTruthy();
  fireEvent.click(
    screen.getByRole("button", { name: /美国 PPI · 更新指定范围/ }),
  );
  await flush();
  const list = within(screen.getByRole("region", { name: "执行记录" }));
  expect(list.getByText("已完成（来源有缺口）")).toBeTruthy();
  const detail = within(
    screen.getByRole("complementary", { name: "执行记录详情" }),
  );
  expect(detail.getByText("已完成（来源有缺口）")).toBeTruthy();
  expect(
    detail.getByText("范围覆盖：3 / 4 条，来源未返回 1 条，来源空值 1 条。"),
  ).toBeTruthy();
  expect(detail.getByText("查看缺口明细").closest("details")?.open).toBe(false);
  expect(detail.getByText("请求范围：2 条")).toBeTruthy();
  expect(detail.queryByText(/官方 null/)).toBeNull();
  expect(detail.getAllByText("WPSFD4 · 2026-09")).toHaveLength(1);
  expect(detail.getByText("WPUFD4 · 2026-09")).toBeTruthy();
  expect(detail.queryByText("完整")).toBeNull();
});

for (const assetKey of [CPI, PPI, null]) {
  for (const onlyOther of [false, true]) {
    it(`isolates explicit ${assetKey?.identity ?? "all"} execution links, other-only=${onlyOther}`, async () => {
      const h = fixture();
      const id = "00000000-0000-4000-8000-000000000099";
      const transport: Transport = async (input, init) => {
        if (String(input).endsWith("/executions")) {
          const ppi = String(input).startsWith(PPI_PROFILE.basePath);
          const own = assetKey?.identity === PPI.identity ? ppi : !ppi;
          return response({
            executions:
              onlyOther && own
                ? []
                : [
                    {
                      id,
                      origin: "manual",
                      action: "update",
                      start: ppi ? "2010-01" : "2000-01",
                      end: "2026-09",
                      created_at: "2026-10-01T01:00:00Z",
                      started_at: null,
                      finished_at: null,
                      state: "complete",
                      classification: null,
                      result: null,
                    },
                  ],
          });
        }
        return h.transport(input, init);
      };
      render(
        <Workbench
          transport={transport}
          tab="executions"
          assetKey={assetKey}
          job={id}
        />,
      );
      await flush();
      if (assetKey && onlyOther) {
        expect(screen.queryByRole("heading", { name: "执行详情" })).toBeNull();
        expect(
          screen.getByText("请求的具名执行记录当前不可核实，未改选其他任务。"),
        ).toBeTruthy();
      } else {
        const detail = within(
          screen.getByRole("complementary", { name: "执行记录详情" }),
        );
        const ppi =
          assetKey?.identity === PPI.identity || (!assetKey && onlyOther);
        expect(detail.getByText(ppi ? "美国 PPI" : "美国 CPI")).toBeTruthy();
        expect(
          detail.getByText(ppi ? "2010-01 至 2026-09" : "2000-01 至 2026-09"),
        ).toBeTruthy();
      }
    });
  }
}

it.each([CPI_PROFILE, PPI_PROFILE])(
  "shows automatic $label history source gaps in the calendar and execution without readback",
  async (profile) => {
    const h = fixture();
    const partial = {
      coverage_complete: false,
      source_missing: [
        {
          series_id: profile.series[0][0],
          month: "2026-09",
          reason: "not_provided",
        },
        { series_id: profile.series[1][0], month: "2026-08" },
      ],
    };
    const transport: Transport = async (input, init) => {
      if (String(input) === `${profile.basePath}/schedule`)
        return response({
          calendar_status: "fresh",
          targets: [
            {
              reference_month: "2026-09",
              release_at: "2026-10-14T12:30:00Z",
              target: { state: "complete" },
              history: { state: "complete", ...partial },
            },
          ],
        });
      if (String(input) === `${profile.basePath}/executions`)
        return response({
          executions: [
            {
              id: "ppi-auto-partial",
              origin: "automatic",
              action: "update",
              stage: "history",
              reference_month: "2026-09",
              start: "2009-11",
              end: "2026-09",
              created_at: null,
              started_at: "2026-10-14T13:00:00Z",
              finished_at: "2026-10-14T13:01:00Z",
              state: "complete",
              classification: null,
              result: {
                summary_scope: "latest_stage_attempt",
                scheduler_attempts_total: 1,
                ...partial,
              },
            },
          ],
        });
      return h.transport(input, init);
    };
    const strategy = render(
      <CpiStrategyWorkbench
        transport={transport}
        availability={availability(CPI_PROFILE)}
        additionalProfile={PPI_PROFILE}
        additionalAvailability={availability()}
      />,
    );
    await flush();
    fireEvent.click(
      within(
        screen.getByText(`美国 ${profile.label}`).closest("tr")!,
      ).getByRole("button", {
        name: /^选择/,
      }),
    );
    await flush();
    const calendar = within(
      screen.getByRole("list", { name: `${profile.label} 月份计划` }),
    );
    expect(calendar.getByText("已完成（来源有缺口）")).toBeTruthy();
    expect(
      calendar.getByText("范围未记录，无法区分请求范围与计算缓冲期：2 条"),
    ).toBeTruthy();
    expect(calendar.getByText("范围覆盖：有缺口（数量未记录）。")).toBeTruthy();
    expect(
      calendar.getByText(`${profile.series[0][0]} · 2026-09`),
    ).toBeTruthy();
    expect(
      calendar.getByText(`${profile.series[1][0]} · 2026-08`),
    ).toBeTruthy();
    expect(calendar.queryByText("完整")).toBeNull();
    strategy.unmount();
    render(
      <Workbench
        transport={transport}
        tab="executions"
        assetKey={profile.asset}
      />,
    );
    await flush();
    const list = within(screen.getByRole("region", { name: "执行记录" }));
    expect(list.getByText("已完成（来源有缺口）")).toBeTruthy();
    fireEvent.click(
      list.getByRole("button", {
        name: new RegExp(`选择 ppi-auto-partial 美国 ${profile.label}`),
      }),
    );
    await flush();
    const detail = within(
      screen.getByRole("complementary", { name: "执行记录详情" }),
    );
    expect(detail.getByText("已完成（来源有缺口）")).toBeTruthy();
    expect(
      detail.getByText("范围覆盖：有缺口（数量未记录），来源未返回 2 条。"),
    ).toBeTruthy();
    expect(detail.getByText("请求范围：2 条")).toBeTruthy();
    expect(detail.getByText(`${profile.series[0][0]} · 2026-09`)).toBeTruthy();
    expect(detail.getByText(`${profile.series[1][0]} · 2026-08`)).toBeTruthy();
    expect(detail.queryByText("完整")).toBeNull();
    expect(detail.queryByText(/回读缺口/)).toBeNull();
    expect(detail.queryByText(/官方 null/)).toBeNull();
  },
);

it.each([CPI_PROFILE, PPI_PROFILE])(
  "uses known $label history scope for nested readback without inventing coverage",
  async (profile) => {
    const h = fixture();
    const gap = { series_id: profile.series[0][0], month: "2026-09" };
    const buffer = { series_id: profile.series[0][0], month: "2026-07" };
    const nullPoint = { series_id: profile.series[1][0], month: "2026-08" };
    const transport: Transport = (input, init) =>
      String(input) === `${profile.basePath}/schedule`
        ? Promise.resolve(
            response({
              calendar_status: "fresh",
              targets: [
                {
                  reference_month: "2026-09",
                  release_at: null,
                  history: {
                    state: "complete",
                    update_start: "2026-08",
                    update_end: "2026-09",
                    coverage_complete: false,
                    source_missing: [gap, buffer],
                    result: {
                      coverage_complete: false,
                      readback: {
                        expected_points: 8,
                        point_count: 55,
                        missing: [gap],
                        source_missing: [gap, buffer],
                        source_unavailable: [nullPoint],
                        source_unavailable_count: 1,
                      },
                    },
                  },
                },
              ],
            }),
          )
        : h.transport(input, init);
    render(
      <CpiStrategyWorkbench
        transport={transport}
        availability={availability(CPI_PROFILE)}
        additionalProfile={PPI_PROFILE}
        additionalAvailability={availability()}
      />,
    );
    await flush();
    fireEvent.click(
      within(
        screen.getByText(`美国 ${profile.label}`).closest("tr")!,
      ).getByRole("button", { name: /^选择/ }),
    );
    await flush();
    const calendar = within(
      screen.getByRole("list", { name: `${profile.label} 月份计划` }),
    );
    expect(
      calendar.getByText(
        "范围覆盖：7 / 8 条，来源未返回 1 条，来源空值 1 条。",
      ),
    ).toBeTruthy();
    expect(calendar.getByText("请求范围：2 条")).toBeTruthy();
    expect(
      calendar.getByText("计算缓冲期（请求前 12 个月，不计入范围覆盖）：1 条"),
    ).toBeTruthy();
    expect(
      calendar.getAllByText(`${gap.series_id} · ${gap.month}`),
    ).toHaveLength(1);
    expect(calendar.getByText("查看缺口明细").closest("details")?.open).toBe(
      false,
    );
    expect(calendar.queryByText(/官方 null/)).toBeNull();
    expect(h.calls.every((c) => c.method === "GET")).toBe(true);
  },
);

for (const tab of ["periodic", "executions"] as const) {
  it.each([404, 503, "transport"])(
    `preserves legacy CPI ${tab} evidence when PPI status fails with %s`,
    async (ppiFailure) => {
      for (const state of [
        "rows",
        "empty",
        "unconfigured",
        "unknown",
        "failure",
      ]) {
        const h = fixture();
        const calls: string[] = [];
        const transport: Transport = async (input, init) => {
          const path = new URL(String(input), "http://fixture.invalid")
            .pathname;
          calls.push(path);
          if (path === `${PPI_PROFILE.basePath}/status`) {
            if (typeof ppiFailure !== "number")
              throw new Error("private-status-detail");
            return response({ classification: "not_available" }, ppiFailure);
          }
          if (path === `${CPI_PROFILE.basePath}/status`)
            return response({ classification: "not_found" }, 404);
          if (/\/api\/r5\/cpi\/(jobs|plans)$/.test(path)) {
            if (state === "failure") throw new Error("private-record-detail");
            return response({
              status: state === "unknown" ? "UNKNOWN" : "OK",
              evidence_mode:
                state === "unconfigured" ? "UNCONFIGURED" : "OFFLINE_FIXTURE",
              asset_identity: CPI.identity,
              asset_revision: CPI.revision,
              jobs: [],
              plans:
                state === "rows"
                  ? [
                      {
                        identity: "legacy-plan",
                        state: "failed",
                        spec: { name: "legacy-plan", timezone: "UTC" },
                        batches: [
                          {
                            reference: "2024-12",
                            jobs: [
                              { job_identity: "legacy-job", state: "failed" },
                            ],
                          },
                        ],
                      },
                    ]
                  : [],
            });
          }
          return h.transport(input, init);
        };
        location.hash = `ui=r5&view=collection&tab=${tab}`;
        const view = render(<App transport={transport} />);
        await flush();
        const region = within(
          screen.getByRole("region", {
            name: tab === "periodic" ? "周期任务" : "执行记录",
          }),
        );
        if (state === "rows") {
          expect(
            region.getByRole("button", {
              name: tab === "periodic" ? /选择 legacy-plan/ : /选择 legacy-job/,
            }),
          ).toBeTruthy();
        } else {
          expect(
            region.getByText(
              state === "empty"
                ? tab === "periodic"
                  ? "当前已查询范围暂无周期任务。"
                  : "当前已查询范围没有执行记录。"
                : state === "unconfigured"
                  ? "读取范围未配置，记录是否存在尚未核实。"
                  : state === "unknown"
                    ? "读取范围或记录状态尚未核实。"
                    : "采集记录读取失败，记录是否存在尚未核实。",
            ),
          ).toBeTruthy();
        }
        expect(screen.queryByText(/private-.*-detail/)).toBeNull();
        expect(calls.some((path) => path === "/api/r5/cpi/plans")).toBe(true);
        expect(calls.some((path) => path === "/api/r5/cpi/jobs")).toBe(true);
        expect(
          calls.some(
            (path) =>
              path.startsWith(PPI_PROFILE.basePath) &&
              !path.endsWith("/status"),
          ),
        ).toBe(false);
        expect(h.calls.every((call) => call.method === "GET")).toBe(true);
        view.unmount();
      }
    },
  );

  it.each([404, 503, "transport"])(
    `keeps explicit unavailable PPI ${tab} unqueried without CPI fallback (%s)`,
    async (ppiFailure) => {
      const h = fixture();
      const calls: string[] = [];
      const transport: Transport = async (input, init) => {
        const path = new URL(String(input), "http://fixture.invalid").pathname;
        calls.push(path);
        if (path === `${PPI_PROFILE.basePath}/status`) {
          if (typeof ppiFailure !== "number")
            throw new Error("private-status-detail");
          return response({ classification: "not_available" }, ppiFailure);
        }
        return h.transport(input, init);
      };
      location.hash = `ui=r5&view=collection&tab=${tab}&asset=${PPI.identity}&asset_revision=${PPI.revision}`;
      render(<App transport={transport} />);
      await flush();
      expect(screen.getByText(/尚未查询/)).toBeTruthy();
      expect(
        screen.queryByText(/当前已查询范围暂无|当前已查询范围没有/),
      ).toBeNull();
      expect(calls.some((path) => /cpi\/(jobs|plans|read)/.test(path))).toBe(
        false,
      );
      expect(
        calls.some((path) => path === `${PPI_PROFILE.basePath}/executions`),
      ).toBe(false);
      expect(h.calls.every((call) => call.method === "GET")).toBe(true);
    },
  );
}
