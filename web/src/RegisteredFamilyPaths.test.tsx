import "./offlineTestSetup";
import { useState } from "react";
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
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import App from "./App";
import type { Transport } from "./api";
import type { RuntimeFamily } from "./RuntimeFamily";
import { TDX_FAMILY } from "./TdxDailyFamily";
import { TDX_ROSTER_FAMILY } from "./TdxRosterFamily";
import { TDX_DAILY } from "./TdxDailyWorkbench";
import { asgi, closeBridges } from "./workflowTestBridge";
import { keyOf } from "./workflowModel";
const id = "2679c123-8a3e-4ae9-8aa9-fce60c5f94e1";
const future: RuntimeFamily = {
  ...TDX_FAMILY,
  collection: { protocol: "tdx", path: "/fixture/future/jobs" },
  asset: { identity: "mdf.test.registered.daily", revision: "r1" },
  title: "未来登记族",
  Query: () => <p>未来登记族本地查询</p>,
  ApiHelp: () => <p>未来登记族 API 合同</p>,
  Collection: function FutureCollection({ transport, navigate }) {
    const [created, setCreated] = useState(false);
    return (
      <>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void transport("/fixture/future/jobs", { method: "POST" }).then(
              () => setCreated(true),
            );
          }}
        >
          <button>提交未来族采集</button>
        </form>
        <aside aria-label="执行简报">
          {created && (
            <button
              aria-current="true"
              onClick={() =>
                navigate("collection", {
                  asset: future.asset.identity,
                  asset_revision: "r1",
                  tab: "executions",
                  execution_asset: keyOf(future.asset),
                  job: id,
                })
              }
            >
              {id}
            </button>
          )}
        </aside>
      </>
    );
  },
  ExecutionDetail: () => <p>未来登记族执行详情</p>,
  readExecutions: async () => [
    {
      id,
      title: "未来登记族采集",
      createdAt: "2026-11-12T06:00:00Z",
      search: `未来登记族 ${id}`,
      result: "领域结果",
    },
  ],
  readPeriodic: async () => ({}),
  periodic: (_data, _props, refresh) => ({
    row: {
      id: "future-auto",
      search: "未来登记族周期策略",
      cells: ["未来登记族", "已暂停", "领域证据"],
    },
    detail: <p>未来登记族周期详情</p>,
    refresh,
  }),
};
let root = "";
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  closeBridges();
  if (root) rmSync(root, { recursive: true, force: true });
  location.hash = "";
  sessionStorage.clear();
});
function nav(name: string) {
  fireEvent.click(
    within(screen.getByRole("navigation")).getByRole("button", { name }),
  );
}
async function setup(family: RuntimeFamily = TDX_FAMILY) {
  if (family !== future) vi.spyOn(crypto, "randomUUID").mockReturnValue(id);
  root = mkdtempSync(join(tmpdir(), "mdf-d01-family-path-"));
  const b = asgi("financial_manual", "d01_ui_exchange.py", { store_dir: root });
  for (const [name, capabilities] of [
    ["tdx", [family === TDX_ROSTER_FAMILY ? "stock_roster" : "daily_native"]],
    ["macro", ["cpi"]],
  ] as const) {
    const saved = await b.exchange({
      method: "PUT",
      path: `/api/r5/packages/${name}`,
      body: {
        expected_revision: null,
        enabled_capability_ids: capabilities,
        source_changes: [],
      },
    });
    expect(saved.status).toBe(200);
  }

  const calls: { url: string; method: string; body?: unknown }[] = [];
  const job = {
    request_id: id,
    state: "INTERRUPTED",
    created_at: "2026-11-12T06:00:00Z",
    provider_requests: 2,
    step_count: 1,
    complete_steps: 0,
    command: {
      action: family === TDX_ROSTER_FAMILY ? "universe" : "init",
      markets: [0, 1, 2],
      start: "1990-01-01",
      end: "2026-11-11",
    },
    steps: [],
  };
  const transport: Transport = async (input, init) => {
    const url = String(input),
      method = init?.method ?? "GET";
    calls.push({
      url,
      method,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
    if (url.startsWith("/fixture/")) return Response.json({});
    if (
      url.startsWith("/api/r5/tdx/daily") ||
      url.startsWith("/api/r5/tdx/roster")
    ) {
      if (method === "POST") {
        return Response.json(job);
      }
      if (url.endsWith("/status"))
        return Response.json({
          enabled: true,
          collect: true,
          defaults: { end: "2026-11-11" },
        });
      if (url.endsWith("/summary")) return Response.json({ markets: [] });
      if (url.endsWith("/help"))
        return Response.json({
          examples: {
            history: "history",
            cross_section: "day",
            coverage: "coverage",
          },
          notes: ["native_projection"],
        });
      if (url.endsWith("/strategy"))
        return Response.json({
          enabled: false,
          worker_online: true,
          heartbeat_at: "2026-11-12T06:00:00Z",
        });
      if (url.includes("/jobs/")) return Response.json(job);
      if (url.endsWith("/jobs")) return Response.json({ jobs: [job] });
    }
    if (url.startsWith("/api/r5/financial/timeseries"))
      return Response.json(
        url.endsWith("/jobs")
          ? { jobs: [] }
          : { defaults: { end: "2026-11-11" }, series: [] },
      );
    const response = await b.transport(input, init);
    if (
      family === future &&
      ["/api/r5/sources", "/api/r5/packages"].includes(url.split("?")[0])
    ) {
      return Response.json(
        JSON.parse(
          JSON.stringify(await response.json()).replaceAll(
            TDX_DAILY.identity,
            future.asset.identity,
          ),
        ),
      );
    }
    return response;
  };
  const app = render(<App transport={transport} runtimeFamilies={[family]} />);
  return { transport, calls, app, b };
}
// This same path must work for a new registration without another App route.
it.each([TDX_FAMILY, future, TDX_ROSTER_FAMILY])(
  "keeps $title in the catalog, standard form, global execution table and periodic details",
  async (family) => {
    const h = await setup(family);
    const catalogTitle =
      family === TDX_ROSTER_FAMILY
        ? "沪深北 A 股基础名册"
        : "沪深北 A 股原生日线";
    nav("数据资产");
    await screen.findByRole("button", {
      name: new RegExp(`选择 ${catalogTitle}`),
    });
    fireEvent.change(screen.getByLabelText("接入包筛选"), {
      target: { value: "tdx" },
    });
    fireEvent.click(
      await screen.findByRole("button", {
        name: new RegExp(`选择 ${catalogTitle}`),
      }),
    );
    expect(screen.getByLabelText("搜索数据资产")).toBeTruthy();
    expect(screen.getByLabelText("接入包筛选")).toBeTruthy();
    const scope = new URLSearchParams(location.hash.slice(1));
    expect(scope.has("as_of")).toBe(false);
    expect(scope.has("reference")).toBe(false);
    if (family === TDX_FAMILY)
      expect(
        (
          screen.getByRole("checkbox", {
            name: "历史截止查询",
          }) as HTMLInputElement
        ).checked,
      ).toBe(false);
    expect(
      screen.queryByRole("navigation", { name: "TDX 日线操作" }),
    ).toBeNull();
    fireEvent.click(await screen.findByRole("tab", { name: "API说明" }));
    if (family === TDX_FAMILY)
      await screen.findByRole("heading", {
        name: "GET /api/r5/tdx/daily/query",
      });
    else if (family === TDX_ROSTER_FAMILY)
      await screen.findByRole("heading", {
        name: "GET /api/r5/tdx/roster/query",
      });
    else await screen.findByText("未来登记族 API 合同");
    fireEvent.click(screen.getByRole("tab", { name: "查询数据" }));
    fireEvent.click(screen.getByRole("button", { name: "采集数据" }));
    await screen.findByLabelText("采集目标");
    expect((screen.getByLabelText("采集目标") as HTMLSelectElement).value).toBe(
      keyOf(family.asset),
    );
    expect(screen.getAllByRole("tab").map((x) => x.textContent)).toEqual([
      "新建采集",
      "周期任务",
      "执行记录",
    ]);
    if (family === TDX_FAMILY) {
      await waitFor(() =>
        expect(
          (screen.getByLabelText("采集结束日期") as HTMLInputElement).value,
        ).toBe("2026-11-11"),
      );
      fireEvent.click(
        screen.getByRole("checkbox", {
          name: "我确认将以上范围采集到开发测试库",
        }),
      );
      fireEvent.click(
        screen.getByRole("button", { name: "提交 TDX 日线 采集" }),
      );
    } else if (family === TDX_ROSTER_FAMILY) {
      fireEvent.click(
        screen.getByRole("checkbox", {
          name: "我确认刷新三市名册到开发测试库",
        }),
      );
      fireEvent.click(screen.getByRole("button", { name: "提交采集任务" }));
    } else
      fireEvent.click(screen.getByRole("button", { name: "提交未来族采集" }));
    {
      const briefs = screen.getByRole("complementary", { name: "执行简报" });
      const card = await within(briefs).findByRole("button", {
        name: new RegExp(id),
        current: true,
      });
      expect(new URLSearchParams(location.hash.slice(1)).get("tab")).toBe(
        "new",
      );
      expect(h.calls.filter((c) => c.method === "POST")).toHaveLength(1);
      fireEvent.click(card);
    }
    const table = await screen.findByRole("region", { name: "执行记录" });
    await screen.findByRole("button", { name: new RegExp(id) });
    expect(screen.getByLabelText("执行资产筛选")).toBeTruthy();
    const detail = screen.getByRole("complementary", { name: "执行记录详情" });
    await within(detail).findByText(
      family === TDX_FAMILY
        ? /原生价基（native_projection）/
        : family === TDX_ROSTER_FAMILY
          ? /深市、沪市、北交所 A 股名册/
          : "未来登记族执行详情",
    );
    expect(h.calls.filter((c) => c.method === "POST")).toHaveLength(1);
    fireEvent.change(screen.getByLabelText("执行资产筛选"), {
      target: { value: "all" },
    });
    const search = screen.getByLabelText("搜索执行记录");
    fireEvent.change(search, { target: { value: id } });
    fireEvent.click(
      within(table).getByRole("button", { name: new RegExp(id) }),
    );
    expect(
      (screen.getByLabelText("搜索执行记录") as HTMLInputElement).value,
    ).toBe(id);
    expect(
      (screen.getByLabelText("执行资产筛选") as HTMLSelectElement).value,
    ).toBe("all");
    const deepLink = location.hash;
    h.app.unmount();
    render(<App transport={h.transport} runtimeFamilies={[family]} />);
    await within(
      screen.getByRole("complementary", { name: "执行记录详情" }),
    ).findByText(
      family === TDX_FAMILY
        ? /原生价基（native_projection）/
        : family === TDX_ROSTER_FAMILY
          ? /深市、沪市、北交所 A 股名册/
          : "未来登记族执行详情",
    );
    expect(location.hash).toBe(deepLink);
    fireEvent.click(screen.getByRole("tab", { name: "周期任务" }));
    const periodic = await screen.findByRole("region", { name: "周期任务" });
    fireEvent.change(screen.getByLabelText("周期资产筛选"), {
      target: { value: keyOf(family.asset) },
    });
    fireEvent.click(
      await within(periodic).findByRole("button", {
        name: new RegExp(`选择 ${family.title}`),
      }),
    );
    await within(
      screen.getByRole("complementary", { name: "周期任务详情" }),
    ).findByText(
      family === TDX_FAMILY
        ? /交易日证据/
        : family === TDX_ROSTER_FAMILY
          ? /09:00 Asia/
          : "未来登记族周期详情",
    );
    expect(screen.getAllByLabelText("搜索周期任务")).toHaveLength(1);
  },
);
it("resolves a registered source deep link and rejects a conflicting revision", async () => {
  const h = await setup();
  const directory = (
    await h.b.exchange({ method: "GET", path: "/api/r5/sources" })
  ).body;
  const entry = directory.entries.find(
    (e: { asset?: { identity: string } }) =>
      e.asset?.identity === TDX_DAILY.identity,
  );
  await act(async () => {
    location.hash = new URLSearchParams({
      ui: "r5",
      view: "assets",
      asset: TDX_DAILY.identity,
      asset_revision: "r1",
      operation: entry.identity,
    }).toString();
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });
  await screen.findByLabelText("查询方向");
  expect(screen.getByLabelText("搜索数据资产")).toBeTruthy();
  await act(async () => {
    location.hash = new URLSearchParams({
      ui: "r5",
      view: "assets",
      asset: TDX_DAILY.identity,
      asset_revision: "wrong",
      operation: entry.identity,
    }).toString();
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });
  await screen.findByText(/关联对象缺失或身份冲突/);
  expect(screen.queryByLabelText("查询方向")).toBeNull();
});
