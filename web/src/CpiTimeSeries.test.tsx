import { afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import App from "./App";
import {
  CpiAvailabilityNotice,
  CpiTimeSeries,
  publicationText,
} from "./CpiTimeSeries";
import { CpiTimeSeriesCollection } from "./CpiTimeSeriesCollection";
import { CPI } from "./workflowModel";
import type { Transport } from "./api";
import {
  CPI_TIMESERIES,
  cpiSeries,
  queryCpiRange,
  shiftMonth,
  useCpiAvailability,
  type CpiAvailability,
  type CpiJob,
  type CpiResult,
  type CpiStatus,
} from "./cpiTimeSeriesApi";

// Explicit synthetic transport: no socket, Python bridge, real store or provider.
const status: CpiStatus = {
  initialization: {
    preset_id: "cpi_latest_20y_v1",
    months: 240,
    start: "2007-03",
    end: "2027-02",
    available: true,
    reason: null,
    anchor_basis: "confirmed_local_available",
  },
  latest_months: Object.fromEntries(cpiSeries.map(([id]) => [id, "2027-02"])),
  observation_count: 2000,
  revision_count: 4,
  batch_count: 8,
  web_capabilities: { query: true, collect: true, reason: null },
};
const availability: CpiAvailability = {
  data: status,
  legacy: false,
  busy: false,
  failure: null,
};
const response = (value: unknown, code = 200) =>
  new Response(JSON.stringify(value), {
    status: code,
    headers: { "Content-Type": "application/json" },
  });
const btn = (name: string) =>
  screen.getByRole("button", { name }) as HTMLButtonElement;
const change = (name: string, value: string) => {
  const field = screen.getByLabelText(name);
  if (field instanceof HTMLButtonElement) {
    const labels: Record<string, string[]> = {
      core: ["核心"],
      headline: ["总体"],
      sa: ["季调"],
      nsa: ["未季调"],
      index: ["指数"],
    };
    fireEvent.click(field);
    const group = screen.getByRole("group", { name: `${name}选项` });
    fireEvent.click(within(group).getByRole("button", { name: "清空" }));
    for (const label of labels[value])
      fireEvent.click(within(group).getByRole("checkbox", { name: label }));
    fireEvent.keyDown(group, { key: "Escape" });
  } else fireEvent.change(field, { target: { value } });
};
afterEach(() => {
  cleanup();
  localStorage.clear();
  location.hash = "";
  vi.restoreAllMocks();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
async function reread() {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "hidden",
  });
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "visible",
  });
  await act(async () => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
}
function fixture(statusValue: CpiStatus = status) {
  const calls: {
    path: string;
    method: string;
    body?: Record<string, string>;
  }[] = [];
  let statusCode = 200,
    queryCode = 200,
    lose = false,
    mismatch = false;
  let jobs: CpiJob[] = [];
  let gap: string | null = null;
  let postFailure: { classification: string; code: number } | null = null;
  const transport: Transport = async (input, init) => {
    const url = new URL(String(input), "http://fixture.invalid");
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({
      path: url.pathname + url.search,
      method: init?.method ?? "GET",
      body,
    });
    if (url.pathname === `${CPI_TIMESERIES}/status`)
      return response(statusValue, statusCode);
    if (url.pathname === `${CPI_TIMESERIES}/query`) {
      if (queryCode !== 200)
        return response({ classification: "synthetic_failure" }, queryCode);
      const params = url.searchParams,
        start = params.get("start")!,
        end = params.get("end")!;
      const selected = cpiSeries.filter(
        (s) =>
          (params.get("basket") === "all" ||
            params.get("basket")!.split(",").includes(s[2])) &&
          ["all", s[3]].includes(params.get("adjustment")!),
      );
      const points = [],
        missing = [];
      for (let month = start; month <= end; month = shiftMonth(month, 1)) {
        for (const [series_id] of selected) {
          if (gap && series_id === selected[0][0]) {
            missing.push({ series_id, month, reason: gap });
            continue;
          }
          points.push({
            series_id,
            month,
            index_value: "320.125",
            mom_pct: "0.24",
            yoy_pct: "2.82",
            change_1m_ratio: "0.0024",
            change_3m_ratio: "0.0083",
            change_6m_ratio: "0.0145",
            change_12m_ratio: "0.0282",
            observed_at: "2027-03-01T00:00:00Z",
            available_at: "2027-03-01T00:00:01Z",
          });
        }
      }
      const result: CpiResult = {
        start_month: mismatch ? "1901-01" : start,
        end_month: end,
        series_ids: selected.map((s) => s[0]),
        points,
        missing,
        calculation_missing: [],
        complete: missing.length === 0,
        coverage_complete: true,
        persisted: false,
        provider_requests: 0,
        provider_attempts: 0,
        vintage_mode: params.get("vintage_mode") ?? "observed",
        query_mode: (params.get("query_mode") ?? "current") as
          | "current"
          | "history",
        as_of_basis: params.get("as_of_basis") ?? "observed",
        as_of: params.get("as_of"),
        measure: params.get("measure")!,
      };
      return response(result);
    }
    if (url.pathname === `${CPI_TIMESERIES}/jobs`) {
      if (init?.method === "POST") {
        if (postFailure)
          return response(
            { classification: postFailure.classification },
            postFailure.code,
          );
        const job = {
          ...body,
          job_id: body.request_id,
          state: "complete",
          created_at: "2027-03-01T00:00:00Z",
          started_at: "2027-03-01T00:00:00Z",
          finished_at: "2027-03-01T00:00:02Z",
          classification: null,
          result: {
            provider_requests: 1,
            provider_attempts: 1,
            saved_points: 4,
            saved_revisions: 0,
            coverage_complete: true,
          },
        } as CpiJob;
        if (lose) throw new Error("synthetic_lost_receipt");
        jobs.push(job);
        return response(job);
      }
      return response({ jobs });
    }
    if (url.pathname === `${CPI_TIMESERIES}/strategy`)
      return response({
        exists: false,
        enabled: false,
        history_start: "1999-01",
        worker: { state: "not_started", last_heartbeat_at: null },
        next_due: null,
        policy: {},
      });
    if (url.pathname === `${CPI_TIMESERIES}/schedule`)
      return response({
        calendar_status: "fresh",
        calendar_coverage: "future_dates_available",
        next_due: "2027-03-10T12:30:00Z",
        checked_at: "2027-03-01T00:00:00Z",
        history_start: "1999-01",
        worker: { enabled: false },
        targets: [
          {
            reference_month: "2027-02",
            release_at: "2027-03-10T12:30:00Z",
            target: { state: "pending" },
            history: { state: "complete" },
          },
        ],
        signals: { recovery: { state: "awaiting_provider" } },
      });
    if (url.pathname === "/api/r5/sources")
      return response({
        status: "OK",
        entries: [
          {
            identity: "fixture.cpi",
            asset: CPI,
            domain: "Macro",
            source_name: "BLS",
            component_identity: "fixture.cpi_history",
            source_contract: true,
            binding_contract: true,
            code_delivery: "DELIVERED",
            operation: "fixture",
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
            capabilities: [{ title: "美国 CPI", definition_ref: CPI }],
            assets: [
              {
                identity: CPI.identity,
                revisions: [
                  {
                    definition_ref: CPI,
                    capability_enabled: true,
                    source_entries: [],
                    local_read: { allowed: true },
                    upstream_read: { allowed: false },
                    collect: { allowed: false },
                  },
                ],
              },
            ],
          },
        ],
      });
    if (url.pathname === "/api/r3/management")
      return response({ status: "OK", entries: [], instances: [] });
    return response(
      { status: "NOT_AVAILABLE", classification: "not_found" },
      404,
    );
  };
  return {
    transport,
    calls,
    setStatus: (n: number) => {
      statusCode = n;
    },
    setQuery: (n: number) => {
      queryCode = n;
    },
    gap: (s: string) => {
      gap = s;
    },
    lose: () => {
      lose = true;
    },
    mismatch: () => {
      mismatch = true;
    },
    jobs: (value: CpiJob[]) => {
      jobs = value;
    },
    rejectJobs: (classification: string | null, code = 409) => {
      postFailure = classification ? { classification, code } : null;
    },
  };
}
function openApp(transport: Transport, view = "assets", tab?: string) {
  location.hash = new URLSearchParams({
    ui: "r5",
    view,
    asset: CPI.identity,
    asset_revision: CPI.revision,
    ...(tab ? { tab } : {}),
  }).toString();
  return render(<App transport={transport} />);
}

it("uses real App asset selection, four series, status-derived dates and paginated local results", async () => {
  const h = fixture();
  openApp(h.transport);
  await waitFor(() => expect(btn("查询数据").disabled).toBe(false));
  expect((screen.getByLabelText("结束月份") as HTMLInputElement).value).toBe(
    "2027-02",
  );
  expect((screen.getByLabelText("开始月份") as HTMLInputElement).value).toBe(
    "2026-03",
  );
  expect(
    within(screen.getByRole("navigation")).getAllByRole("button"),
  ).toHaveLength(5);
  expect(screen.queryByLabelText("数据定义")).toBeNull();
  expect(screen.queryByRole("region", { name: "普通查询" })).toBeNull();
  fireEvent.click(btn("查询数据"));
  const table = await screen.findByRole("table", { name: "CPI 范围数据" });
  expect(within(table).getAllByRole("row")).toHaveLength(41);
  expect(screen.getByText(/共 48 条/)).toBeTruthy();
  for (const title of cpiSeries
    .filter((s) => ["headline", "core"].includes(s[2]))
    .map((s) => s[1]))
    expect(within(table).getAllByText(title).length).toBe(10);
  fireEvent.click(btn("下一页"));
  expect(within(table).getAllByRole("row")).toHaveLength(9);
  const query = new URL(
    h.calls.find((c) => c.path.includes("/query?"))!.path,
    "http://fixture.invalid",
  );
  expect(query.searchParams.get("local_only")).toBe("false");
  expect(query.searchParams.get("query_mode")).toBe("current");
  expect(query.searchParams.get("as_of_basis")).toBeNull();
  expect(query.searchParams.get("vintage_mode")).toBeNull();
  expect(h.calls.every((c) => c.method === "GET")).toBe(true);
  expect(h.calls.some((c) => /^\/api\/r5\/cpi\?/.test(c.path))).toBe(false);
  const collect = btn("采集数据");
  expect(collect.closest(".cpi-asset-title")).toBeTruthy();
  expect(collect.closest(".cpi-timeseries")).toBeNull();
  fireEvent.click(btn("展开窗体"));
  expect(
    collect.closest(".workbench")?.classList.contains("cpi-asset-expanded"),
  ).toBe(true);
  fireEvent.click(btn("收起窗体"));
  expect(
    collect.closest(".workbench")?.classList.contains("cpi-asset-expanded"),
  ).toBe(false);
  fireEvent.click(collect);
  await screen.findByRole("region", { name: "CPI 受控采集" });
  expect(screen.getByLabelText("采集目标")).toBeTruthy();
});

it("renders May 2026 core SA ratios and reuses the response for metric edits", async () => {
  const h = fixture();
  render(<CpiTimeSeries transport={h.transport} availability={availability} />);
  change("开始月份", "2026-05");
  change("结束月份", "2026-05");
  change("统计篮子", "core");
  change("季节调整", "sa");
  fireEvent.click(btn("查询数据"));
  const table = await screen.findByRole("table", { name: "CPI 范围数据" });
  expect(within(table).getAllByRole("row")).toHaveLength(2);
  for (const value of ["0.24%", "0.83%", "1.45%", "2.82%"])
    expect(within(table).getByText(value)).toBeTruthy();
  change("显示指标", "index");
  expect(screen.getByRole("table", { name: "CPI 范围数据" })).toBeTruthy();
  expect(screen.getByText("320.125")).toBeTruthy();
  expect(h.calls.filter((call) => call.path.includes("/query?"))).toHaveLength(
    1,
  );
});

it.each<Record<string, string>>([
  { asset_revision: "nonexistent-revision" },
  { operation: "nonexistent.operation" },
  { package: "unrelated" },
])("App rejects conflicting CPI query context %j", async (fields) => {
  const h = fixture();
  const transport: Transport = async (input, init) => {
    const result = await h.transport(input, init);
    if (String(input) === "/api/r5/packages") {
      const body = await result.json();
      // A real visible package that does not own CPI, not just a missing package.
      body.packages.push({
        package_id: "unrelated",
        name: "其他",
        history_completeness: "COMPLETE",
        capabilities: [],
        assets: [],
      });
      return response(body);
    }
    return result;
  };
  location.hash = new URLSearchParams({
    ui: "r5",
    view: "assets",
    asset: CPI.identity,
    asset_revision: CPI.revision,
    ...fields,
  }).toString();
  render(<App transport={transport} />);
  await waitFor(() => expect(btn("刷新").disabled).toBe(false));
  if (!fields.package)
    expect(screen.getByText(/关联对象缺失或身份冲突/)).toBeTruthy();
  expect(screen.queryByRole("region", { name: "CPI 范围查询" })).toBeNull();
  expect(screen.queryByRole("button", { name: "查询数据" })).toBeNull();
  expect(screen.queryByRole("button", { name: "采集数据" })).toBeNull();
  expect(h.calls.some((c) => c.path.includes(`${CPI_TIMESERIES}/query`))).toBe(
    false,
  );
});

it.each([false, true])(
  "keeps a valid CPI entry without historical assets (package filter: %s)",
  async (filtered) => {
    const h = fixture();
    const transport: Transport = async (input, init) => {
      const result = await h.transport(input, init);
      if (String(input) === "/api/r5/packages") {
        const body = await result.json();
        body.packages[0].assets = [];
        return response(body);
      }
      return result;
    };
    location.hash = new URLSearchParams({
      ui: "r5",
      view: "assets",
      asset: CPI.identity,
      ...(filtered ? { package: "macro" } : {}),
    }).toString();
    render(<App transport={transport} />);
    await waitFor(() => expect(btn("查询数据").disabled).toBe(false));
    fireEvent.click(btn("查询数据"));
    expect(
      await screen.findByRole("table", { name: "CPI 范围数据" }),
    ).toBeTruthy();
    expect(
      h.calls.filter((c) => c.path.includes(`${CPI_TIMESERIES}/query`)),
    ).toHaveLength(1);
  },
);

it("shows official missing values and available-time evidence without zero filling", async () => {
  const h = fixture();
  h.gap("source_unavailable");
  render(<CpiTimeSeries transport={h.transport} availability={availability} />);
  change("开始月份", "2026-05");
  change("结束月份", "2026-05");
  fireEvent.click(screen.getByRole("checkbox", { name: "历史截止查询" }));
  change("截止时间（北京时间）", "2026-06-01T12:00");
  fireEvent.click(btn("查询数据"));
  expect(await screen.findByText("未公布 / 来源缺值")).toBeTruthy();
  const url = new URL(h.calls[0].path, "http://fixture.invalid");
  expect(url.searchParams.get("start")).toBe("2026-05");
  expect(url.searchParams.get("end")).toBe("2026-05");
  expect(url.searchParams.get("vintage_mode")).toBe("available");
  expect(url.searchParams.get("as_of")).toBe("2026-06-01T12:00:00+08:00");
  const missingRow = screen.getByText("未公布 / 来源缺值").closest("tr")!;
  expect(within(missingRow).getAllByText("—")).toHaveLength(5);
  expect(within(missingRow).getByText("无对应月份记录")).toBeTruthy();
  expect(url.searchParams.get("query_mode")).toBe("history");
  expect(url.searchParams.get("local_only")).toBe("true");
  expect(url.searchParams.get("as_of_basis")).toBe("available");
  expect(screen.queryByLabelText("时间依据")).toBeNull();
});

it.each([500, 404])(
  "query HTTP %s stays explicit unknown and never invokes legacy query",
  async (code) => {
    const h = fixture();
    h.setQuery(code);
    render(
      <CpiTimeSeries transport={h.transport} availability={availability} />,
    );
    fireEvent.click(btn("查询数据"));
    expect(await screen.findByText(/本次查询结果未知/)).toBeTruthy();
    expect(h.calls).toHaveLength(1);
  },
);
it("rejects mismatched returned ranges", async () => {
  const h = fixture();
  h.mismatch();
  render(<CpiTimeSeries transport={h.transport} availability={availability} />);
  fireEvent.click(btn("查询数据"));
  expect(await screen.findByText(/本次查询结果未知/)).toBeTruthy();
});
it.each([500, 403])(
  "App status HTTP %s hides legacy data and disables action",
  async (code) => {
    const h = fixture();
    h.setStatus(code);
    openApp(h.transport);
    expect(await screen.findByText(/CPI 服务状态未知/)).toBeTruthy();
    expect(btn("查询数据").disabled).toBe(true);
    expect(screen.queryByRole("region", { name: "普通查询" })).toBeNull();
    expect(h.calls.some((c) => c.path.startsWith("/api/r5/cpi?"))).toBe(false);
  },
);
it("only status 404 restores the existing ordinary query in App", async () => {
  const h = fixture();
  h.setStatus(404);
  openApp(h.transport);
  expect(await screen.findByRole("region", { name: "普通查询" })).toBeTruthy();
  expect(screen.queryByRole("region", { name: "CPI 范围查询" })).toBeNull();
});
it("malformed status cannot grant query or collection permission", async () => {
  // Stable transport avoids intentionally changing the effect on every render.
  const transport: Transport = async () =>
    response({ ...status, web_capabilities: { query: "true", collect: true } });
  function StableProbe() {
    const a = useCpiAvailability(transport, 0);
    return <CpiTimeSeries transport={transport} availability={a} />;
  }
  render(<StableProbe />);
  expect(await screen.findByText(/CPI 服务状态未知/)).toBeTruthy();
  expect(btn("查询数据").disabled).toBe(true);
});

it.each([false, true])(
  "submits one server-preset initialize request with a saved UUID (HTTP LAN fallback: %s)",
  async (httpLan) => {
    const h = fixture();
    const randomValues = vi.fn(crypto.getRandomValues.bind(crypto));
    if (httpLan) vi.stubGlobal("crypto", { getRandomValues: randomValues });
    render(
      <CpiTimeSeriesCollection
        transport={h.transport}
        availability={availability}
        tab="new"
      />,
    );
    change("采集方式", "initialize");
    expect(
      (screen.getByLabelText("采集开始月份") as HTMLInputElement).readOnly,
    ).toBe(false);
    expect(
      (screen.getByLabelText("采集结束月份") as HTMLInputElement).value,
    ).toBe(status.initialization!.end);
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(btn("提交 CPI 采集"));
    expect((await screen.findAllByText("已完成"))[0]).toBeTruthy();
    const posts = h.calls.filter((c) => c.method === "POST");
    expect(posts).toHaveLength(1);
    expect(Object.keys(posts[0].body!).sort()).toEqual([
      "action",
      "end",
      "request_id",
      "start",
    ]);
    expect(posts[0].body!.action).toBe("initialize");
    expect(posts[0].body!.start).toBe(status.initialization!.start);
    expect(posts[0].body!.end).toBe(status.initialization!.end);
    expect(posts[0].body!.request_id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
    if (httpLan) expect(randomValues).toHaveBeenCalledOnce();
    expect(localStorage.getItem("mdf.cpi.timeseries.pending.v1")).toContain(
      posts[0].body!.request_id,
    );
    expect(btn("提交 CPI 采集").disabled).toBe(true);
    fireEvent.click(btn("开始另一项采集"));
    expect(screen.queryByText(/已保留请求编号/)).toBeNull();
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(
      false,
    );
  },
);
it("accepts a 202 running receipt and checks the same task without reposting", async () => {
  const h = fixture();
  let completed: CpiJob | null = null;
  const transport: Transport = async (input, init) => {
    const result = await h.transport(input, init);
    if (init?.method !== "POST") return result;
    completed = (await result.json()) as CpiJob;
    const running: CpiJob = {
      ...completed,
      state: "running",
      finished_at: null,
      result: null,
    };
    h.jobs([running]);
    return response(running, 202);
  };
  render(
    <CpiTimeSeriesCollection
      transport={transport}
      availability={availability}
      tab="new"
    />,
  );
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(btn("提交 CPI 采集"));
  expect((await screen.findAllByText("执行中"))[0]).toBeTruthy();
  expect(btn("提交 CPI 采集").disabled).toBe(true);
  expect(screen.queryByRole("button", { name: "开始另一项采集" })).toBeNull();
  const reserved = localStorage.getItem("mdf.cpi.timeseries.pending.v1");
  await act(async () => {});
  expect(completed).not.toBeNull();
  h.jobs([completed!]);
  await reread();
  (await screen.findAllByText("已完成"))[0];
  expect(screen.getByRole("button", { name: "开始另一项采集" })).toBeTruthy();
  expect(h.calls.filter((c) => c.method === "POST")).toHaveLength(1);
  expect(localStorage.getItem("mdf.cpi.timeseries.pending.v1")).toBe(reserved);
});
it("lost POST receipt survives remount and never blindly resubmits an unknown request", async () => {
  const h = fixture();
  h.lose();
  const view = render(
    <CpiTimeSeriesCollection
      transport={h.transport}
      availability={availability}
      tab="new"
    />,
  );
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(btn("提交 CPI 采集"));
  await screen.findByText(/采集回执不明/);
  const saved = localStorage.getItem("mdf.cpi.timeseries.pending.v1")!;
  view.unmount();
  render(
    <CpiTimeSeriesCollection
      transport={h.transport}
      availability={availability}
      tab="new"
    />,
  );
  expect(await screen.findByText(/结果未知，保留请求/)).toBeTruthy();
  expect(btn("提交 CPI 采集").disabled).toBe(true);
  await act(async () => {});
  await reread();
  await act(async () => {});
  expect(h.calls.filter((c) => c.method === "POST")).toHaveLength(1);
  expect(localStorage.getItem("mdf.cpi.timeseries.pending.v1")).toBe(saved);
  expect(screen.queryByText("开始另一项采集")).toBeNull();
});
it("storage failure prevents any POST", async () => {
  const h = fixture();
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("synthetic_storage_failure");
  });
  render(
    <CpiTimeSeriesCollection
      transport={h.transport}
      availability={availability}
      tab="new"
    />,
  );
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(btn("提交 CPI 采集"));
  expect(await screen.findByText(/无法可靠保存请求编号/)).toBeTruthy();
  expect(h.calls.filter((c) => c.method === "POST")).toHaveLength(0);
});
it("collection disabled is truthful and calendar reads do not trigger scheduling", async () => {
  const h = fixture(),
    blocked = {
      ...availability,
      data: {
        ...status,
        web_capabilities: {
          query: true,
          collect: false,
          reason: "collection_not_enabled",
        },
      },
    };
  const view = render(
    <CpiTimeSeriesCollection
      transport={h.transport}
      availability={blocked}
      tab="new"
    />,
  );
  fireEvent.click(screen.getByRole("checkbox"));
  expect(btn("提交 CPI 采集").disabled).toBe(true);
  view.rerender(
    <CpiTimeSeriesCollection
      transport={h.transport}
      availability={blocked}
      tab="periodic"
    />,
  );
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: /选择 美国 CPI/ }));
  expect(screen.getByText("CPI 策略详情")).toBeTruthy();
  expect(screen.queryByText("当前未启用")).toBeNull();
  expect(screen.getByRole("list", { name: "CPI 月份计划" })).toBeTruthy();
  expect(h.calls.every((c) => c.method === "GET")).toBe(true);
});

it("ignores a late query result after the user changes the requested month", async () => {
  const h = fixture();
  let release!: () => void;
  const transport: Transport = async (input, init) => {
    const result = await h.transport(input, init);
    await new Promise<void>((resolve) => {
      release = resolve;
    });
    return result;
  };
  render(<CpiTimeSeries transport={transport} availability={availability} />);
  fireEvent.click(btn("查询数据"));
  await waitFor(() => expect(release).toBeTypeOf("function"));
  change("开始月份", "2026-05");
  await act(async () => {
    release();
  });
  expect(screen.queryByRole("region", { name: "CPI 范围结果" })).toBeNull();
});

it("honors the current host collection limit and resets confirmation when scope changes", async () => {
  const h = fixture();
  render(
    <CpiTimeSeriesCollection
      transport={h.transport}
      availability={{
        ...availability,
        data: { ...status, collection_scope: { max_months: 2, series: "all" } },
      }}
      tab="new"
    />,
  );
  fireEvent.click(screen.getByRole("checkbox"));
  expect(btn("提交 CPI 采集").disabled).toBe(false);
  change("采集开始月份", "2026-01");
  expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(
    false,
  );
  fireEvent.click(screen.getByRole("checkbox"));
  expect(btn("提交 CPI 采集").disabled).toBe(true);
  expect(screen.getByText(/每次允许最多 2 个月/)).toBeTruthy();
  expect(h.calls.every((c) => c.method === "GET")).toBe(true);
});

it.each([
  {
    name: "null max_months",
    collection_scope: { max_months: null, series: "all" },
    start: "1999-01",
    end: "2027-02",
  },
  {
    name: "omitted max_months",
    collection_scope: { series: "all" },
    start: "1999-01",
    end: "2027-02",
  },
  {
    name: "omitted collection_scope",
    collection_scope: undefined,
    start: "1999-01",
    end: "2027-02",
  },
])(
  "accepts $name status and submits one long range with its reserved UUID",
  async ({ collection_scope, start, end }) => {
    const h = fixture({ ...status, collection_scope });
    const request_id = "9fb1bfd8-067f-42f4-a174-82b1ff77f042";
    vi.spyOn(crypto, "randomUUID").mockReturnValue(request_id);
    let reservedAtPost: unknown;
    const transport: Transport = (input, init) => {
      if (init?.method === "POST")
        reservedAtPost = JSON.parse(
          localStorage.getItem("mdf.cpi.timeseries.pending.v1")!,
        );
      return h.transport(input, init);
    };
    function CollectionProbe() {
      const available = useCpiAvailability(transport, 0);
      return (
        <CpiTimeSeriesCollection
          transport={transport}
          availability={available}
          tab="new"
        />
      );
    }
    const view = render(<CollectionProbe />);
    await waitFor(() =>
      expect(
        (screen.getByLabelText("采集结束月份") as HTMLInputElement).value,
      ).toBe("2027-02"),
    );
    expect(screen.queryByText(/CPI 服务状态未知/)).toBeNull();
    expect(screen.getByText(/长范围由后端自动分批，无需手动拆分/)).toBeTruthy();
    expect(screen.queryByText(/每次允许最多/)).toBeNull();
    change("采集方式", "update");
    change("采集开始月份", start);
    change("采集结束月份", end);
    expect(btn("提交 CPI 采集").disabled).toBe(true);
    fireEvent.click(screen.getByText("我确认将以上范围采集到开发测试库"));
    expect(
      (
        screen.getByRole("checkbox", {
          name: "我确认将以上范围采集到开发测试库",
        }) as HTMLInputElement
      ).checked,
    ).toBe(true);
    expect(btn("提交 CPI 采集").disabled).toBe(false);
    fireEvent.click(btn("提交 CPI 采集"));
    (await screen.findAllByText("已完成"))[0];
    const expected = { request_id, action: "update", start, end };
    const posts = h.calls.filter((c) => c.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0].path).toBe(`${CPI_TIMESERIES}/jobs`);
    expect(posts[0].body).toEqual(expected);
    expect(reservedAtPost).toEqual(expected);
    expect(screen.getAllByText(request_id)[0]).toBeTruthy();

    expect(btn("提交 CPI 采集").disabled).toBe(true);
    view.unmount();
    render(<CollectionProbe />);
    await screen.findByRole("button", { name: "开始另一项采集" });
    expect(screen.queryByText(/本机请求记录无法核实/)).toBeNull();
    expect(h.calls.filter((c) => c.method === "POST")).toHaveLength(1);
    expect(
      JSON.parse(localStorage.getItem("mdf.cpi.timeseries.pending.v1")!),
    ).toEqual(expected);
  },
);

it.each([0, -1, 1.5, "24", false])(
  "rejects an invalid explicit host limit %s when decoding status",
  async (max_months) => {
    const transport: Transport = async () =>
      response({ ...status, collection_scope: { max_months, series: "all" } });
    function CollectionProbe() {
      const available = useCpiAvailability(transport, 0);
      return (
        <CpiTimeSeriesCollection
          transport={transport}
          availability={available}
          tab="new"
        />
      );
    }
    render(<CollectionProbe />);
    await screen.findByText(/CPI 服务状态未知/);
    expect(btn("提交 CPI 采集").disabled).toBe(true);
  },
);

it.each([
  ["2027-03", "2027-02"],
  ["2027-13", "2027-02"],
  ["2027-1", "2027-02"],
  ["2027-01", ""],
  ["1900-12", "2027-02"],
  ["1901-01", "2101-01"],
])(
  "rejects invalid collection dates %s to %s without a host limit",
  (start, end) => {
    const h = fixture();
    render(
      <CpiTimeSeriesCollection
        transport={h.transport}
        availability={{
          ...availability,
          data: {
            ...status,
            collection_scope: { max_months: null, series: "all" },
          },
        }}
        tab="new"
      />,
    );
    change("采集开始月份", start);
    change("采集结束月份", end);
    fireEvent.click(screen.getByRole("checkbox"));
    if (["2027-13", "2027-1"].includes(start)) {
      expect(
        (
          screen.getByLabelText("采集开始月份") as HTMLInputElement
        ).checkValidity(),
      ).toBe(false);
    }
    expect(btn("提交 CPI 采集").disabled).toBe(true);
    fireEvent.submit(btn("提交 CPI 采集").closest("form")!);
    expect(h.calls.every((c) => c.method === "GET")).toBe(true);
    expect(localStorage.getItem("mdf.cpi.timeseries.pending.v1")).toBeNull();
  },
);

it("an explicit pre-execution rejection releases the reservation but requires fresh confirmation", async () => {
  const h = fixture();
  const transport: Transport = (input, init) =>
    init?.method === "POST"
      ? Promise.resolve(
          response({ classification: "collection_not_enabled" }, 403),
        )
      : h.transport(input, init);
  render(
    <CpiTimeSeriesCollection
      transport={transport}
      availability={availability}
      tab="new"
    />,
  );
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(btn("提交 CPI 采集"));
  expect(await screen.findByText(/服务明确拒绝本次采集/)).toBeTruthy();
  expect(localStorage.getItem("mdf.cpi.timeseries.pending.v1")).toBeNull();
  expect(btn("提交 CPI 采集").disabled).toBe(true);
});

it("busy before journal creation preserves the original form and submits after the other job releases", async () => {
  const h = fixture();
  const other: CpiJob = {
    request_id: "9fb1bfd8-067f-42f4-a174-82b1ff77f042",
    job_id: "9fb1bfd8-067f-42f4-a174-82b1ff77f042",
    action: "update",
    start: "2026-01",
    end: "2026-01",
    state: "running",
    created_at: "2027-03-01T00:00:00Z",
    started_at: "2027-03-01T00:00:00Z",
    finished_at: null,
    classification: null,
    result: null,
  };
  h.jobs([other]);
  h.rejectJobs("cpi_jobs_busy");
  render(
    <CpiTimeSeriesCollection
      transport={h.transport}
      availability={availability}
      tab="new"
    />,
  );
  change("采集方式", "update");
  change("采集开始月份", "2026-05");
  change("采集结束月份", "2026-06");
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(btn("提交 CPI 采集"));
  await screen.findByText(/另一项 CPI 采集正在执行，本次请求未创建任务/);
  expect(localStorage.getItem("mdf.cpi.timeseries.pending.v1")).toBeNull();
  expect((screen.getByLabelText("采集方式") as HTMLSelectElement).value).toBe(
    "update",
  );
  expect(
    (screen.getByLabelText("采集开始月份") as HTMLInputElement).value,
  ).toBe("2026-05");
  expect(
    (screen.getByLabelText("采集结束月份") as HTMLInputElement).value,
  ).toBe("2026-06");
  expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(
    false,
  );
  expect(btn("提交 CPI 采集").disabled).toBe(true);
  h.jobs([
    { ...other, state: "complete", finished_at: "2027-03-01T00:00:02Z" },
  ]);
  h.rejectJobs(null);
  await act(async () => {});
  await reread();
  await act(async () => {});
  expect(h.calls.filter((c) => c.method === "POST")).toHaveLength(1);
  fireEvent.click(screen.getByRole("checkbox"));
  expect(btn("提交 CPI 采集").disabled).toBe(false);
  fireEvent.click(btn("提交 CPI 采集"));
  (await screen.findAllByText("已完成"))[0];
  const posts = h.calls.filter((c) => c.method === "POST");
  expect(posts).toHaveLength(2);
  expect(posts[1].body).toEqual({
    ...posts[0].body,
    request_id: posts[1].body!.request_id,
  });
  expect(posts[1].body!.request_id).not.toBe(posts[0].body!.request_id);
});

it.each([
  [409, "request_id_conflict"],
  [409, "package_configuration_changed"],
  [409, "unknown_conflict"],
  [503, "cpi_jobs_busy"],
])("retains the reservation on HTTP %s / %s", async (code, classification) => {
  const h = fixture();
  h.rejectJobs(String(classification), Number(code));
  render(
    <CpiTimeSeriesCollection
      transport={h.transport}
      availability={availability}
      tab="new"
    />,
  );
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(btn("提交 CPI 采集"));
  await screen.findByText(/采集回执不明/);
  expect(localStorage.getItem("mdf.cpi.timeseries.pending.v1")).toContain(
    h.calls.find((c) => c.method === "POST")!.body!.request_id,
  );
  expect(btn("提交 CPI 采集").disabled).toBe(true);
  expect(screen.queryByRole("button", { name: "开始另一项采集" })).toBeNull();
  fireEvent.click(btn("提交 CPI 采集"));
  expect(h.calls.filter((c) => c.method === "POST")).toHaveLength(1);
});

it.each(["unknown", "running"] as const)(
  "a persisted %s job remains locked until the same request has a terminal record",
  async (state) => {
    const h = fixture();
    const intent = {
      request_id: "9fb1bfd8-067f-42f4-a174-82b1ff77f042",
      action: "update" as const,
      start: "2026-05",
      end: "2026-05",
    };
    localStorage.setItem(
      "mdf.cpi.timeseries.pending.v1",
      JSON.stringify(intent),
    );
    const job: CpiJob = {
      ...intent,
      job_id: intent.request_id,
      state,
      created_at: "2026-06-01T00:00:00Z",
      started_at: null,
      finished_at: null,
      classification: "execution_interrupted",
      result: null,
    };
    h.jobs([job]);
    render(
      <CpiTimeSeriesCollection
        transport={h.transport}
        availability={availability}
        tab="new"
      />,
    );
    await act(async () => {});
    expect(btn("提交 CPI 采集").disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "开始另一项采集" })).toBeNull();
    h.jobs([{ ...job, state: "complete", classification: null }]);
    await reread();
    expect(
      await screen.findByRole("button", { name: "开始另一项采集" }),
    ).toBeTruthy();
    expect(h.calls.every((c) => c.method === "GET")).toBe(true);
  },
);

it("available-time gaps are not displayed as source zero values", async () => {
  const h = fixture();
  h.gap("availability_evidence_insufficient");
  render(<CpiTimeSeries transport={h.transport} availability={availability} />);
  change("开始月份", "2026-05");
  change("结束月份", "2026-05");
  fireEvent.click(screen.getByRole("checkbox", { name: "历史截止查询" }));
  change("截止时间（北京时间）", "2026-06-01T12:00");
  fireEvent.click(btn("查询数据"));
  expect(await screen.findByText("当时可用证据不足")).toBeTruthy();
});

it("a directory outage on a canonical CPI collection link keeps the controlled service visible", async () => {
  const h = fixture();
  const transport: Transport = (input, init) =>
    String(input).startsWith("/api/r5/sources")
      ? Promise.resolve(
          response(
            { status: "UNKNOWN", classification: "directory_unavailable" },
            503,
          ),
        )
      : h.transport(input, init);
  openApp(transport, "collection", "new");
  expect(
    await screen.findByRole("region", { name: "CPI 受控采集" }),
  ).toBeTruthy();
  expect(screen.queryByLabelText("关联数据源 / Operation")).toBeNull();
  expect(
    h.calls.some((call) => call.path.startsWith("/api/r5/cpi/jobs?")),
  ).toBe(false);
});

it("a query denial in status cannot be bypassed by editing a valid range", async () => {
  const h = fixture();
  render(
    <CpiTimeSeries
      transport={h.transport}
      availability={{
        ...availability,
        data: {
          ...status,
          web_capabilities: {
            query: false,
            collect: false,
            reason: "local_read_not_authorized",
          },
        },
      }}
    />,
  );
  change("开始月份", "2026-05");
  change("结束月份", "2026-05");
  expect(btn("查询数据").disabled).toBe(true);
  expect(screen.getByText(/未开放 CPI 查询/)).toBeTruthy();
  expect(h.calls).toHaveLength(0);
});

it("renders calendar instants across the UTC day boundary in Beijing while preserving reference months", async () => {
  const h = fixture();
  const transport: Transport = async (input, init) => {
    const result = await h.transport(input, init);
    if (!String(input).includes("/schedule")) return result;
    const value = await result.json();
    value.next_due = "2027-03-10T20:30:00Z";
    value.checked_at = "2027-02-28T23:59:59Z";
    value.targets[0].release_at = "2027-03-10T20:30:00Z";
    return response(value);
  };
  render(
    <CpiTimeSeriesCollection
      transport={transport}
      availability={availability}
      tab="periodic"
    />,
  );
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: /选择 美国 CPI/ }));
  expect(
    within(screen.getByRole("list", { name: "CPI 月份计划" })).getByText(
      /2027-03-11 04:30:00/,
    ),
  ).toBeTruthy();
  expect(
    within(screen.getByRole("list", { name: "CPI 月份计划" })).getByText(
      "2027-02",
    ),
  ).toBeTruthy();
  expect(h.calls.every((call) => call.method === "GET")).toBe(true);
});

it("uses the Beijing month and current instant for App defaults across a UTC month boundary", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-31T20:30:05Z"));
  const h = fixture();
  h.setStatus(404);
  openApp(h.transport);
  const field = await screen.findByLabelText("查询月份");
  expect((field as HTMLInputElement).value).toBe("2026-11");
  await waitFor(() =>
    expect(h.calls.some((call) => call.path.startsWith("/api/r5/cpi?"))).toBe(
      true,
    ),
  );
  const request = h.calls.find((call) => call.path.startsWith("/api/r5/cpi?"))!;
  const params = new URL(request.path, "http://fixture.invalid").searchParams;
  expect(params.get("reference")).toBe("2026-11");
  expect(params.get("as_of")).toBe("2026-11-01T04:30:05.000+08:00");
});

it.each(["统计篮子", "季节调整", "显示指标"])(
  "clearing %s blocks querying and select all restores the dimension",
  async (dimension) => {
    const h = fixture();
    render(
      <CpiTimeSeries transport={h.transport} availability={availability} />,
    );
    fireEvent.click(btn(dimension));
    const options = screen.getByRole("group", { name: `${dimension}选项` });
    fireEvent.click(within(options).getByRole("button", { name: "清空" }));
    expect(btn("查询数据").disabled).toBe(true);
    expect(within(btn(dimension)).getByText("未选择")).toBeTruthy();
    fireEvent.submit(btn("查询数据").closest("form")!);
    expect(h.calls).toHaveLength(0);
    fireEvent.click(within(options).getByRole("button", { name: "全选" }));
    fireEvent.keyDown(options, { key: "Escape" });
    expect(btn("查询数据").disabled).toBe(false);
    fireEvent.click(btn("查询数据"));
    await screen.findByRole("table", { name: "CPI 范围数据" });
    expect(h.calls).toHaveLength(1);
  },
);

it.each([
  ["headline", "sa", 1],
  ["headline", "nsa", 1],
  ["core", "sa", 1],
  ["core", "nsa", 1],
  ["headline", "all", 2],
  ["all", "sa", 2],
  ["all", "all", 4],
])(
  "combines basket %s and adjustment %s in one request",
  async (basket, adjustment, count) => {
    const h = fixture();
    render(
      <CpiTimeSeries transport={h.transport} availability={availability} />,
    );
    change("开始月份", "2026-05");
    change("结束月份", "2026-05");
    if (basket !== "all") change("统计篮子", String(basket));
    if (adjustment !== "all") change("季节调整", String(adjustment));
    fireEvent.click(btn("查询数据"));
    const table = await screen.findByRole("table", { name: "CPI 范围数据" });
    expect(within(table).getAllByRole("row")).toHaveLength(Number(count) + 1);
    expect(h.calls).toHaveLength(1);
    const url = new URL(h.calls[0].path, "http://fixture.invalid");
    expect(url.searchParams.get("basket")).toBe(
      basket === "all" ? "headline,core" : basket,
    );
    expect(url.searchParams.get("adjustment")).toBe(adjustment);
  },
);

it("changes metric combinations using existing fields without more requests", async () => {
  const h = fixture();
  render(<CpiTimeSeries transport={h.transport} availability={availability} />);
  fireEvent.click(btn("查询数据"));
  const table = await screen.findByRole("table", { name: "CPI 范围数据" });
  fireEvent.click(btn("显示指标"));
  const options = screen.getByRole("group", { name: "显示指标选项" });
  expect(within(options).getAllByRole("checkbox")).toHaveLength(5);
  fireEvent.click(within(options).getByRole("button", { name: "清空" }));
  expect(btn("查询数据").disabled).toBe(true);
  fireEvent.click(within(options).getByRole("checkbox", { name: "指数" }));
  fireEvent.click(within(options).getByRole("checkbox", { name: "6月变化" }));
  const updated = screen.getByRole("table", { name: "CPI 范围数据" });
  expect(
    within(updated).getByRole("columnheader", { name: "指数" }),
  ).toBeTruthy();
  expect(
    within(updated).getByRole("columnheader", { name: "6月变化" }),
  ).toBeTruthy();
  expect(
    within(updated).queryByRole("columnheader", { name: "3月变化" }),
  ).toBeNull();
  expect(h.calls).toHaveLength(1);
  expect(table).toBeTruthy();
});

it("permits current passthrough counts but rejects persistence and history provider access", async () => {
  const h = fixture();
  let persisted = false;
  const transport: Transport = async (input, init) => {
    const result = await h.transport(input, init);
    const body = await result.json();
    return response({
      ...body,
      provider_requests: 2,
      provider_attempts: 3,
      persisted,
    });
  };
  render(<CpiTimeSeries transport={transport} availability={availability} />);
  fireEvent.click(btn("查询数据"));
  await screen.findByRole("table", { name: "CPI 范围数据" });
  change("开始月份", "2026-05");
  persisted = true;
  fireEvent.click(btn("查询数据"));
  await screen.findByText(/本次查询结果未知/);
  persisted = false;
  fireEvent.click(screen.getByRole("checkbox", { name: "历史截止查询" }));
  expect(btn("查询数据").disabled).toBe(true);
  change("截止时间（北京时间）", "2026-06-01T12:00");
  fireEvent.click(btn("查询数据"));
  await screen.findByText(/本次查询结果未知/);
});

it.each(["time", "publication_time"])(
  "shows evidenced official events via %s without binding the revision",
  async (timeField) => {
    const h = fixture();
    const transport: Transport = async (input, init) => {
      const result = await h.transport(input, init);
      const body = await result.json();
      body.points = body.points.map(
        (point: Record<string, unknown>, index: number) => ({
          ...point,
          publication_event:
            index < 2
              ? {
                  reference_month: point.month,
                  [timeField]: "2026-06-10T12:30:00Z",
                  status: index === 0 ? "published" : "scheduled",
                  evidence: {
                    source: "https://www.bls.gov/schedule/news_release/cpi.htm",
                  },
                }
              : {
                  reference_month: point.month,
                  [timeField]: "2026-06-10T12:30:00Z",
                  status: "published",
                  evidence: {},
                },
        }),
      );
      return response(body);
    };
    render(<CpiTimeSeries transport={transport} availability={availability} />);
    change("开始月份", "2026-05");
    change("结束月份", "2026-05");
    fireEvent.click(btn("查询数据"));
    const table = await screen.findByRole("table", { name: "CPI 范围数据" });
    expect(
      within(table).getByText("已发布：2026-06-10 20:30:00 北京时间"),
    ).toBeTruthy();
    expect(
      within(table).getByText("计划发布：2026-06-10 20:30:00 北京时间"),
    ).toBeTruthy();
    expect(
      within(table).getByRole("columnheader", { name: "官方日历时间" }),
    ).toBeTruthy();
    expect(within(table).getAllByText("无对应月份记录").length).toBeGreaterThan(
      0,
    );
    expect(within(table).queryByText(/2027-03-01/)).toBeNull();
    expect(screen.getByText(/不表示当前修订值在该时已知/)).toBeTruthy();
  },
);

it.each([
  {
    reference_month: "2026-05",
    time: "2026-06-10T12:30:00",
    status: "published",
    evidence: "official",
  },
  {
    reference_month: "2026-05",
    time: "2026-02-30T12:30:00Z",
    status: "published",
    evidence: "official",
  },
  {
    reference_month: "2026-05",
    time: "2026-06-10T12:30:00Z",
    status: "published",
  },
  {
    reference_month: "2026-04",
    time: "2026-06-10T12:30:00Z",
    status: "published",
    evidence: "https://www.bls.gov/schedule/news_release/cpi.htm",
  },
  {
    reference_month: "2026-05",
    time: "2026-06-10T12:30:00Z",
    evidence: { source: "https://www.bls.gov/schedule/news_release/cpi.htm" },
  },
  {
    reference_month: "2026-05",
    time: "bad-time",
    status: "published",
    evidence: "official",
  },
  {
    reference_month: "2026-05",
    time: "2026-06-10T12:30:00Z",
    status: "published",
    evidence: {},
  },
])(
  "withholds unproven or ambiguous publication metadata %j",
  (publication_event) => {
    expect(publicationText({ month: "2026-05", publication_event })).toBeNull();
  },
);

it("shows a scheduled reference-month event on a missing row without inventing a data value", async () => {
  const h = fixture();
  h.gap("source_unavailable");
  const transport: Transport = async (input, init) => {
    const result = await h.transport(input, init);
    const value = await result.json();
    value.publication_events = [
      null,
      {
        reference_month: "2026-05",
        publication_time: "2026-06-10T12:30:00Z",
        status: "scheduled",
        evidence: {
          source: "https://www.bls.gov/schedule/news_release/cpi.htm",
        },
      },
    ];
    return response(value);
  };
  render(<CpiTimeSeries transport={transport} availability={availability} />);
  change("开始月份", "2026-05");
  change("结束月份", "2026-05");
  fireEvent.click(btn("查询数据"));
  const missing = await screen.findByText("未公布 / 来源缺值");
  expect(
    within(missing.closest("tr")!).getByText(
      "计划发布：2026-06-10 20:30:00 北京时间",
    ),
  ).toBeTruthy();
  expect(within(missing.closest("tr")!).getAllByText("—")).toHaveLength(5);
});

it("forces an as_of request into local available history even when the caller says current", async () => {
  const h = fixture();
  await queryCpiRange(
    h.transport,
    {
      start: "2026-05",
      end: "2026-05",
      basket: "all",
      adjustment: "all",
      measure: "index",
      query_mode: "current",
      as_of: "2026-06-01T12:00:00+08:00",
    },
    new AbortController().signal,
  );
  const params = new URL(h.calls[0].path, "http://fixture.invalid")
    .searchParams;
  expect(params.get("query_mode")).toBe("history");
  expect(params.get("local_only")).toBe("true");
  expect(params.get("as_of_basis")).toBe("available");
  expect(params.get("vintage_mode")).toBe("available");
});

it("gives current range queries a bounded passthrough deadline and keeps local history short", async () => {
  const h = fixture();
  const timeout = vi.spyOn(AbortSignal, "timeout");
  const scope = {
    start: "2026-05",
    end: "2026-05",
    basket: "all",
    adjustment: "all",
    measure: "index",
  };
  await queryCpiRange(h.transport, scope, new AbortController().signal);
  await queryCpiRange(
    h.transport,
    { ...scope, query_mode: "history" },
    new AbortController().signal,
  );
  expect(timeout.mock.calls.map((call) => call[0])).toEqual([180000, 20000]);
});

it("marks only explicitly unpersisted current points as temporary source values", async () => {
  const h = fixture();
  const transport: Transport = async (input, init) => {
    const result = await h.transport(input, init);
    const value = await result.json();
    value.points = value.points.map(
      (point: CpiResult["points"][number], i: number) => ({
        ...point,
        ...(i === 0
          ? {
              origin: "upstream",
              availability: "unpersisted",
              available_at: null,
              raw_locator: null,
            }
          : i === 1
            ? { origin: "local", availability: "available" }
            : { origin: "upstream" }),
      }),
    );
    return response(value);
  };
  render(<CpiTimeSeries transport={transport} availability={availability} />);
  change("开始月份", "2026-05");
  change("结束月份", "2026-05");
  fireEvent.click(btn("查询数据"));
  const table = await screen.findByRole("table", { name: "CPI 范围数据" });
  expect(within(table).getAllByText("临时来源值")).toHaveLength(1);
  const rows = within(table).getAllByRole("row");
  expect(within(rows[1]).getByText("临时来源值")).toBeTruthy();
  for (const row of rows.slice(2))
    expect(within(row).queryByText("临时来源值")).toBeNull();
});

it.each(["history", "contradictory_available_time"])(
  "rejects unpersisted values in %s responses",
  async (kind) => {
    const h = fixture();
    const transport: Transport = async (input, init) => {
      const result = await h.transport(input, init);
      const value = await result.json();
      value.points[0] = {
        ...value.points[0],
        origin: "upstream",
        availability: "unpersisted",
        available_at: kind === "history" ? null : "2026-06-01T00:00:00Z",
      };
      return response(value);
    };
    render(<CpiTimeSeries transport={transport} availability={availability} />);
    if (kind === "history") {
      fireEvent.click(screen.getByRole("checkbox", { name: "历史截止查询" }));
      change("截止时间（北京时间）", "2026-06-01T12:00");
    }
    fireEvent.click(btn("查询数据"));
    await screen.findByText(/本次查询结果未知/);
    expect(screen.queryByRole("table", { name: "CPI 范围数据" })).toBeNull();
    expect(screen.queryByText("临时来源值")).toBeNull();
  },
);

it("status polling preserves an inflight current query and its completed result", async () => {
  vi.useFakeTimers();
  const h = fixture();
  let release!: () => void;
  let querySignal: AbortSignal | null | undefined;
  const transport: Transport = async (input, init) => {
    if (String(input).includes("/query?")) {
      querySignal = init?.signal;
      await new Promise<void>((resolve) => {
        release = resolve;
      });
    }
    return h.transport(input, init);
  };
  function Probe() {
    const a = useCpiAvailability(transport, 0);
    return <CpiTimeSeries transport={transport} availability={a} />;
  }
  render(<Probe />);
  await act(async () => {});
  change("开始月份", "2026-05");
  change("结束月份", "2026-05");
  fireEvent.click(btn("查询数据"));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(15000);
  });
  expect(querySignal?.aborted).toBe(false);
  expect(h.calls.filter((c) => c.path.endsWith("/status"))).toHaveLength(4);
  await act(async () => release());
  expect(screen.getByRole("region", { name: "CPI 范围结果" })).toBeTruthy();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(screen.getByRole("region", { name: "CPI 范围结果" })).toBeTruthy();
  expect(h.calls.filter((c) => c.path.includes("/query?"))).toHaveLength(1);
});

it("previews the exact builder URL and copies URL/cURL with encoded Beijing cutoff", async () => {
  const h = fixture();
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  render(<CpiTimeSeries transport={h.transport} availability={availability} />);
  change("开始月份", "2026-05");
  change("结束月份", "2026-05");
  change("统计篮子", "core");
  change("季节调整", "sa");
  fireEvent.click(screen.getByRole("checkbox", { name: "历史截止查询" }));
  change("截止时间（北京时间）", "2026-06-01T12:00");
  const preview = screen.getByRole("region", { name: "请求预览" });
  const shown = within(preview).getByLabelText("请求 URL").textContent!;
  expect(shown).toContain("%2B08%3A00");
  fireEvent.click(
    within(preview).getByRole("button", { name: "复制请求预览URL" }),
  );
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(shown.slice(4)));
  fireEvent.click(within(preview).getByText("cURL 示例（当前服务地址）"));
  fireEvent.click(within(preview).getByRole("button", { name: "复制cURL" }));
  await waitFor(() =>
    expect(writeText).toHaveBeenCalledWith(
      `curl --request GET '${shown.slice(4)}'`,
    ),
  );
  fireEvent.click(btn("查询数据"));
  await screen.findByRole("table", { name: "CPI 范围数据" });
  const wire = h.calls.find((c) => c.path.includes("/query?"))!;
  expect(shown).toBe(`GET ${new URL(wire.path, location.origin).href}`);
  const url = new URL(wire.path, location.origin);
  expect(url.searchParams.get("basket")).toBe("core");
  expect(url.searchParams.get("adjustment")).toBe("sa");
  expect(url.searchParams.get("measure")).toBe("index");
  expect(url.searchParams.get("as_of")).toBe("2026-06-01T12:00:00+08:00");
  expect([...url.searchParams.keys()]).not.toContain("columns");
});

it("retains exact raw precision and unknown fields without a query when tabs or status change", async () => {
  const h = fixture();
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  let bodyText = "";
  const transport: Transport = async (input, init) => {
    const res = await h.transport(input, init);
    bodyText = (await res.text()).replace(
      /}$/,
      ',"unknown_integer":9007199254740993,"unknown_decimal":0.12345678901234567890123456789}',
    );
    return new Response(bodyText, { status: 200 });
  };
  const view = render(
    <CpiTimeSeries transport={transport} availability={availability} />,
  );
  fireEvent.click(btn("查询数据"));
  await screen.findByRole("table", { name: "CPI 范围数据" });
  fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
  const raw = screen.getByRole("region", { name: "本次原始响应" });
  expect(within(raw).getByLabelText("原始 HTTP 正文").textContent).toBe(
    bodyText,
  );
  expect(raw.textContent).toContain("HTTP 状态200");
  expect(raw.textContent).toContain("耗时");
  expect(raw.textContent).toContain("完成时间");
  const originalUrl = within(raw).getByLabelText("本次请求 URL").textContent!;
  fireEvent.click(within(raw).getByRole("button", { name: "复制本次请求URL" }));
  await waitFor(() =>
    expect(writeText).toHaveBeenCalledWith(originalUrl.slice(4)),
  );
  fireEvent.click(within(raw).getByRole("button", { name: "复制原始响应" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(bodyText));
  expect(h.calls).toHaveLength(1);
  fireEvent.click(screen.getByRole("tab", { name: "API说明" }));
  expect(screen.getByRole("table", { name: "查询参数" })).toBeTruthy();
  fireEvent.click(screen.getByRole("tab", { name: "查询数据" }));
  fireEvent.click(screen.getByRole("tab", { name: "表格" }));
  change("显示指标", "index");
  view.rerender(
    <CpiTimeSeries
      transport={transport}
      availability={{ ...availability, busy: true }}
    />,
  );
  view.rerender(
    <CpiTimeSeries
      transport={transport}
      availability={{ ...availability, failure: "read_failed" }}
    />,
  );
  expect(screen.getByRole("table", { name: "CPI 范围数据" })).toBeTruthy();
  fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
  expect(screen.getByLabelText("原始 HTTP 正文").textContent).toBe(bodyText);
  expect(
    within(screen.getByRole("region", { name: "本次原始响应" })).getByLabelText(
      "本次请求 URL",
    ).textContent,
  ).toBe(originalUrl);
  expect(h.calls).toHaveLength(1);
  change("开始月份", "2026-05");
  expect(screen.queryByRole("region", { name: "本次原始响应" })).toBeNull();
});

it.each([
  [
    503,
    '{"status":"ERROR","classification":"fixture_failure","unknown":9007199254740993}',
  ],
  [502, "<html>fixture upstream unavailable</html>"],
  [404, "fixture route absent"],
])(
  "preserves the actual HTTP %s exchange after query failure",
  async (code, bodyText) => {
    const transport: Transport = async () =>
      new Response(bodyText, { status: code });
    render(<CpiTimeSeries transport={transport} availability={availability} />);
    fireEvent.click(btn("查询数据"));
    await screen.findByText(/本次查询结果未知/);
    fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
    const raw = screen.getByRole("region", { name: "本次原始响应" });
    expect(within(raw).getByLabelText("原始 HTTP 正文").textContent).toBe(
      bodyText,
    );
    expect(within(raw).getByText(String(code))).toBeTruthy();
    expect(screen.queryByRole("table", { name: "CPI 范围数据" })).toBeNull();
  },
);

it("does not invent an HTTP status or body for a network failure", async () => {
  const transport: Transport = async () => {
    throw new TypeError("fixture network");
  };
  render(<CpiTimeSeries transport={transport} availability={availability} />);
  fireEvent.click(btn("查询数据"));
  await screen.findByText(/未收到可读取的 HTTP 响应/);
  expect(screen.queryByRole("tab", { name: "原始响应" })).toBeNull();
  expect(screen.queryByLabelText("原始 HTTP 正文")).toBeNull();
});

it("keeps status failures distinct and clears them after a manual successful retry without polling", async () => {
  const h = fixture();
  h.setStatus(503);
  function Probe() {
    const a = useCpiAvailability(h.transport, 0, true, false);
    return <CpiTimeSeries transport={h.transport} availability={a} />;
  }
  render(<Probe />);
  expect(await screen.findByText(/首次读取失败/)).toBeTruthy();
  expect(screen.queryByText(/自动核实/)).toBeNull();
  expect(btn("查询数据").disabled).toBe(true);
  h.setStatus(200);
  fireEvent.click(btn("重新核实"));
  await waitFor(() => expect(btn("查询数据").disabled).toBe(false));
  expect(screen.queryByText(/首次读取失败/)).toBeNull();
  cleanup();
  const refresh = vi.fn();
  const view = render(
    <CpiAvailabilityNotice
      availability={{ ...availability, failure: "read_failed", refresh }}
    />,
  );
  expect(screen.getByText(/不代表已保存数据过期/)).toBeTruthy();
  fireEvent.click(btn("重新核实"));
  expect(refresh).toHaveBeenCalledTimes(1);
  view.rerender(
    <CpiAvailabilityNotice availability={{ ...availability, refresh }} />,
  );
  expect(screen.queryByRole("alert")).toBeNull();
  view.rerender(
    <CpiAvailabilityNotice
      availability={{ ...availability, data: null, busy: true }}
    />,
  );
  expect(screen.queryByRole("alert")).toBeNull();
  view.rerender(
    <CpiAvailabilityNotice availability={{ ...availability, data: null }} />,
  );
  expect(screen.queryByRole("alert")).toBeNull();
});

it("ignores late aborted exchanges and keeps the active query through status refresh and tab switches", async () => {
  const h = fixture();
  const releases: (() => void)[] = [];
  const signals: (AbortSignal | null | undefined)[] = [];
  const transport: Transport = (input, init) =>
    new Promise((resolve) => {
      signals.push(init?.signal);
      releases.push(() => {
        void h.transport(input, init).then(resolve);
      });
    });
  const view = render(
    <CpiTimeSeries transport={transport} availability={availability} />,
  );
  change("开始月份", "2026-04");
  change("结束月份", "2026-05");
  fireEvent.click(btn("查询数据"));
  change("开始月份", "2026-05");
  expect(signals[0]?.aborted).toBe(true);
  fireEvent.click(btn("查询数据"));
  view.rerender(
    <CpiTimeSeries
      transport={transport}
      availability={{ ...availability, busy: true }}
    />,
  );
  fireEvent.click(screen.getByRole("tab", { name: "API说明" }));
  fireEvent.click(screen.getByRole("tab", { name: "查询数据" }));
  expect(signals[1]?.aborted).toBe(false);
  await act(async () => releases[1]());
  await screen.findByRole("table", { name: "CPI 范围数据" });
  fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
  const raw = screen.getByLabelText("原始 HTTP 正文").textContent;
  await act(async () => releases[0]());
  expect(screen.getByLabelText("原始 HTTP 正文").textContent).toBe(raw);
  expect(
    screen.getByRole("region", { name: "本次原始响应" }).textContent,
  ).toContain("start=2026-05");
  expect(releases).toHaveLength(2);
});

it.each([false, null])(
  "revoking query permission to %s aborts inflight and blocks late exchange refill",
  async (permission) => {
    const h = fixture();
    const releases: (() => void)[] = [];
    const signals: (AbortSignal | null | undefined)[] = [];
    const transport: Transport = (input, init) =>
      new Promise((resolve) => {
        signals.push(init?.signal);
        releases.push(() => {
          void h.transport(input, init).then(resolve);
        });
      });
    const view = render(
      <CpiTimeSeries transport={transport} availability={availability} />,
    );
    fireEvent.click(btn("查询数据"));
    const revoked: CpiAvailability = {
      ...availability,
      data:
        permission === null
          ? null
          : {
              ...status,
              web_capabilities: { ...status.web_capabilities, query: false },
            },
    };
    view.rerender(
      <CpiTimeSeries transport={transport} availability={revoked} />,
    );
    expect(signals[0]?.aborted).toBe(true);
    expect(btn("查询数据").disabled).toBe(true);
    expect(screen.queryByText("正在查询 CPI…")).toBeNull();
    await act(async () => releases[0]());
    expect(screen.queryByRole("region", { name: "本次原始响应" })).toBeNull();
    expect(screen.queryByRole("table", { name: "CPI 范围数据" })).toBeNull();
    view.rerender(
      <CpiTimeSeries transport={transport} availability={availability} />,
    );
    fireEvent.click(btn("查询数据"));
    await act(async () => releases[1]());
    await screen.findByRole("table", { name: "CPI 范围数据" });
    view.rerender(
      <CpiTimeSeries transport={transport} availability={revoked} />,
    );
    expect(screen.queryByRole("table", { name: "CPI 范围数据" })).toBeNull();
    expect(screen.queryByRole("region", { name: "本次原始响应" })).toBeNull();
    expect(releases).toHaveLength(2);
  },
);

it("shows cancellation without retaining its obsolete planned time", () => {
  expect(
    publicationText({
      month: "2026-05",
      publication_event: {
        reference_month: "2026-05",
        status: "cancelled",
        time: "2026-06-10T12:30:00Z",
        evidence: {
          source: "https://www.bls.gov/schedule/news_release/cpi.htm",
        },
      },
    }),
  ).toBe("已取消");
  expect(
    publicationText({
      month: "2026-05",
      publication_event: {
        reference_month: "2026-05",
        status: "unknown",
        time: null,
        evidence: {
          source: "https://www.bls.gov/schedule/news_release/cpi.htm",
        },
      },
    }),
  ).toBe("计划时间待定（TBA / 未知）");
});
