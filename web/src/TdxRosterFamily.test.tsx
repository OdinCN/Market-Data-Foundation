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
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import App from "./App";
import type { FamilyProps } from "./RuntimeFamily";
import type { Transport } from "./api";
import { TdxRosterQuery, TDX_ROSTER_FAMILY } from "./TdxRosterFamily";
import { TdxApiDocumentation } from "./TdxApiDocumentation";
import { keyOf } from "./workflowModel";
import { asgi, closeBridges } from "./workflowTestBridge";
const props: Omit<FamilyProps, "transport"> = {
  route: { view: "assets", invalid: false, fields: new URLSearchParams() },
  navigate: vi.fn(),
  generation: 0,
};
let root = "";
afterEach(() => {
  cleanup();
  closeBridges();
  if (root) rmSync(root, { recursive: true, force: true });
  root = "";
  location.hash = "";
  sessionStorage.clear();
  vi.restoreAllMocks();
});
const status = {
  state: "DATED",
  snapshot_id: "official-20261112",
  valid_through: "2026-11-12",
  available_at: "2026-11-12T09:00:00Z",
  collect: true,
};
function fixture(fail = false) {
  const calls: { path: string; method: string }[] = [];
  const transport: Transport = async (path, init) => {
    const url = String(path);
    calls.push({ path: url, method: init?.method ?? "GET" });
    if (url.endsWith("/status")) return Response.json(status);
    if (url.endsWith("/help"))
      return Response.json({
        notes: ["GetBJDirectory；北交所官方分页、旧代码和历史区间独立留证"],
      });
    const offset = Number(
      new URL(url, "http://fixture").searchParams.get("offset"),
    );
    if (fail)
      return Response.json(
        { classification: "tdx_storage_unavailable" },
        { status: 503 },
      );
    return Response.json({
      ...status,
      rows: [
        {
          market: 2,
          instrument: offset ? "920003" : "920002",
          name: "fixture",
          membership_state: "ACTIVE",
          evidence: { listing_date: "2020-07-27", old_code: "430002" },
        },
      ],
      total: 101,
      offset,
      next_offset: offset ? null : 100,
      provider_requests: 0,
      sources: [],
    });
  };
  return { calls, transport };
}
it("uses explicit roster query, shared microsecond cutoff, live preview, one server pager and real raw HTTP", async () => {
  const f = fixture();
  render(<TdxRosterQuery {...props} transport={f.transport} />);
  await screen.findByText("有据日期名册");
  expect(f.calls.filter((c) => c.path.includes("/query"))).toHaveLength(0);
  fireEvent.change(screen.getByLabelText("名册日期"), {
    target: { value: "2026-11-12" },
  });
  fireEvent.change(screen.getByLabelText("交易所"), { target: { value: "2" } });
  fireEvent.change(screen.getByLabelText("代码或名称"), {
    target: { value: "920" },
  });
  fireEvent.click(screen.getByRole("checkbox", { name: "历史截止查询" }));
  fireEvent.change(screen.getByLabelText("截止时间（北京时间）"), {
    target: { value: "2026-11-13T00:00:00.123456" },
  });
  const preview = screen.getByLabelText("请求 URL");
  expect(preview.textContent).toContain(
    "as_of=2026-11-13T00%3A00%3A00.123456%2B08%3A00",
  );
  const query = screen.getByRole("button", { name: "查询数据" });
  fireEvent.pointerDown(query);
  expect(query.closest("form")!.checkValidity()).toBe(true);
  fireEvent.click(query);
  await screen.findByRole("region", { name: "名册结果" });
  expect(f.calls.filter((c) => c.path.includes("/query"))).toHaveLength(1);
  fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
  expect(screen.getByLabelText("原始 HTTP 正文").textContent).toContain(
    '"old_code":"430002"',
  );
  fireEvent.click(screen.getByRole("tab", { name: "表格" }));
  fireEvent.click(
    within(screen.getByLabelText("TDX 名册 结果分页")).getByRole("button", {
      name: "下一页",
    }),
  );
  await screen.findByText("920003");
  expect(screen.getByLabelText("请求 URL").textContent).toContain("offset=100");
  expect(screen.getByLabelText("请求 URL").textContent).toContain(
    "snapshot_id=official-20261112",
  );
  const paged = f.calls.filter((c) => c.path.includes("/query"))[1];
  expect(
    new URL(paged.path, "http://fixture").searchParams.get("snapshot_id"),
  ).toBe("official-20261112");
  fireEvent.click(
    within(screen.getByLabelText("TDX 名册 结果分页")).getByRole("button", {
      name: "上一页",
    }),
  );
  await screen.findByText("920002");
  expect(f.calls.filter((c) => c.path.includes("/query"))).toHaveLength(3);
  expect(f.calls.every((c) => c.method === "GET")).toBe(true);
});
it("does not present failed expected data as an empty roster", async () => {
  const f = fixture(true);
  render(<TdxRosterQuery {...props} transport={f.transport} />);
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("region", { name: "名册结果" })).toBeNull();
  expect(f.calls.filter((c) => c.path.includes("/query"))).toHaveLength(1);
});
it("documents actual daily and base roster contracts with the Macro tables and copy controls", async () => {
  const f = fixture();
  const view = render(<TdxApiDocumentation transport={f.transport} />);
  await screen.findByRole("table", { name: "查询参数" });
  expect(screen.getByRole("table", { name: "返回字段" }).textContent).toContain(
    "0.001 CNY",
  );
  fireEvent.click(screen.getByText("查询与采集示例"));
  expect(screen.getByRole("button", { name: "复制采集命令示例" })).toBeTruthy();
  expect(
    screen.getByRole("table", { name: "错误与恢复" }).textContent,
  ).toContain("tdx_roster_stale");
  view.rerender(<TdxApiDocumentation transport={f.transport} roster />);
  await screen.findByRole("heading", { name: "GET /api/r5/tdx/roster/query" });
  expect(screen.getByRole("table", { name: "查询参数" }).textContent).toContain(
    "snapshot_id",
  );
});
it("recalls the original roster UUID after a lost write and prevents duplicate dispatch", async () => {
  const id = "2679c123-8a3e-4ae9-8aa9-fce60c5f94e1";
  vi.spyOn(crypto, "randomUUID").mockReturnValue(id);
  const calls: { path: string; method: string }[] = [];
  const transport: Transport = async (path, init) => {
    const method = init?.method ?? "GET";
    calls.push({ path: String(path), method });
    if (String(path).endsWith("/status")) return Response.json(status);
    if (String(path).endsWith("/jobs") && method === "GET")
      return Response.json({ jobs: [] });
    throw Error("lost");
  };
  render(<TDX_ROSTER_FAMILY.Collection {...props} transport={transport} />);
  fireEvent.click(
    screen.getByRole("checkbox", { name: "我确认刷新三市名册到开发测试库" }),
  );
  const submit = screen.getByRole("button", { name: "提交采集任务" });
  await waitFor(() =>
    expect((submit as HTMLButtonElement).disabled).toBe(false),
  );
  fireEvent.click(submit);
  await screen.findByText(/刷新回执未核实/);
  expect((submit as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(submit);
  expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
  expect(calls.some((c) => c.path.endsWith("/jobs/" + id))).toBe(true);
});

it("opens roster refresh from the shared workbench periodic tab without writing or losing the asset revision", async () => {
  // Provision only isolated package configuration before rendering. Every request
  // made by the actual App navigation below must remain a read.
  root = mkdtempSync(join(tmpdir(), "mdf-d01-roster-entry-"));
  const directory = asgi("financial_manual", "d01_ui_exchange.py", {
    store_dir: root,
  });
  const configured = await directory.exchange({
    method: "PUT",
    path: "/api/r5/packages/tdx",
    body: {
      expected_revision: null,
      enabled_capability_ids: ["stock_roster"],
      source_changes: [],
    },
  });
  expect(configured.status).toBe(200);
  const backend = asgi("roster", "collection_submit_fixture.py");
  const calls: { path: string; method: string }[] = [];
  const transport: Transport = async (input, init) => {
    const path = String(input),
      method = init?.method ?? "GET";
    calls.push({ path, method });
    if (method !== "GET") throw Error("navigation_must_not_write");
    if (/^\/api\/r5\/(packages|sources)(?:\/|\?|$)/.test(path))
      return directory.transport(input, init);
    if (
      path.startsWith("/api/r5/tdx/roster/") ||
      path === "/api/r5/tdx/daily/strategy"
    )
      return backend.transport(input, init);
    return Response.json({ classification: "not_enabled" }, { status: 404 });
  };
  location.hash = new URLSearchParams({
    ui: "r5",
    view: "collection",
    tab: "periodic",
    periodic_asset: keyOf(TDX_ROSTER_FAMILY.asset),
  }).toString();
  render(<App transport={transport} />);
  fireEvent.click(
    await screen.findByRole("button", {
      name: /选择 沪深北 A 股基础名册/,
    }),
  );
  const detail = await screen.findByRole("region", {
    name: "TDX 名册 周期详情",
  });
  expect(within(detail).queryByRole("button", { name: /新建/ })).toBeNull();
  expect(
    screen.getByLabelText("周期操作").getAttribute("data-periodic-id"),
  ).toBe("tdx-roster-auto");
  fireEvent.click(screen.getByRole("tab", { name: "新建采集" }));

  const form = await screen.findByRole("region", {
    name: "TDX 基础名册 刷新",
  });
  const route = new URLSearchParams(location.hash.slice(1));
  expect(route.get("view")).toBe("collection");
  expect(route.get("tab")).toBe("new");
  expect(route.get("asset")).toBe(TDX_ROSTER_FAMILY.asset.identity);
  expect(route.get("asset_revision")).toBe(TDX_ROSTER_FAMILY.asset.revision);
  expect(
    screen.getByRole("tab", { name: "新建采集" }).getAttribute("aria-selected"),
  ).toBe("true");
  expect((screen.getByLabelText("采集目标") as HTMLSelectElement).value).toBe(
    keyOf(TDX_ROSTER_FAMILY.asset),
  );
  expect(
    (within(form).getByLabelText("采集方式") as HTMLSelectElement).value,
  ).toBe("refresh");
  expect(screen.queryByLabelText("采集开始日期")).toBeNull();
  const confirm = within(form).getByRole("checkbox", {
    name: "我确认刷新三市名册到开发测试库",
  }) as HTMLInputElement;
  const submit = within(form).getByRole("button", {
    name: "提交采集任务",
  }) as HTMLButtonElement;
  expect(confirm.checked).toBe(false);
  expect(submit.disabled).toBe(true);
  fireEvent.click(confirm);
  await waitFor(() => expect(submit.disabled).toBe(false));
  expect(calls.some((c) => c.path === "/api/r5/tdx/roster/status")).toBe(true);
  expect(calls.some((c) => c.path === "/api/r5/tdx/roster/jobs")).toBe(true);
  expect(calls.every((c) => c.method === "GET")).toBe(true);
  expect(
    (await backend.exchange({ path: "/api/r5/tdx/roster/jobs" })).body.jobs,
  ).toEqual([]);
});

it("keeps the roster periodic domain detail free of duplicate collection navigation", () => {
  const navigate = vi.fn();
  const entry = TDX_ROSTER_FAMILY.periodic(
    { enabled: true, worker_online: true },
    { ...props, navigate, transport: fixture().transport },
    vi.fn(),
  );
  render(<>{entry.detail}</>);
  const detail = screen.getByRole("region", { name: "TDX 名册 周期详情" });
  expect(within(detail).queryByRole("button", { name: /新建/ })).toBeNull();
  expect(within(detail).queryByRole("tab", { name: "新建采集" })).toBeNull();
  expect(navigate).not.toHaveBeenCalled();
});

it("uses recorded markets for historical roster jobs instead of the shared daily date window", async () => {
  const jobs = [
    {
      request_id: "r",
      state: "COMPLETE",
      origin: "periodic",
      created_at: "2026-11-13T07:10:00Z",
      provider_requests: 2,
      command: {
        action: "universe",
        markets: [0, 2],
        start: "2026-10-30",
        end: "2026-11-13",
      },
    },
  ];
  const transport: Transport = async () => Response.json({ jobs });
  const [row] = await TDX_ROSTER_FAMILY.readExecutions(
    transport,
    new AbortController().signal,
  );
  expect(row.scope).toBe("深市、北交所 A 股名册（来源目录快照）");
  expect(row.schedule).toBe("tdx-roster-auto");
});
