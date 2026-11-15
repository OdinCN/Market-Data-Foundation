import "./offlineTestSetup";
import { afterEach, expect, it } from "vitest";
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
import { asgi, closeBridges } from "./workflowTestBridge";
import type { Transport } from "./api";
let root = "";
afterEach(() => {
  cleanup();
  closeBridges();
  localStorage.clear();
  sessionStorage.clear();
  if (root) rmSync(root, { recursive: true, force: true });
  root = "";
  location.hash = "";
});
async function mount(scenario = "intraday") {
  const source = asgi(scenario, "tdx_query_ui_exchange.py");
  root = mkdtempSync(join(tmpdir(), "mdf-d01-query-"));
  const directory = asgi("default", "d01_ui_exchange.py", { store_dir: root });
  const configured = await directory.exchange({
    method: "PUT",
    path: "/api/r5/packages/tdx",
    body: {
      expected_revision: null,
      enabled_capability_ids: ["daily_native"],
      source_changes: [],
    },
  });
  expect(configured.status).toBe(200);
  const calls: { path: string; method: string }[] = [];
  const transport: Transport = async (url, init) => {
    const path = String(url),
      method = init?.method ?? "GET";
    calls.push({ path, method });
    if (/^\/api\/r5\/(packages|sources)/.test(path))
      return directory.transport(url, init);
    if (path.startsWith("/api/r5/tdx/daily/")) {
      const response = await source.transport(url, init);
      return Response.json(await response.json(), { status: response.status });
    }
    return Response.json({ classification: "not_enabled" }, { status: 404 });
  };
  location.hash = new URLSearchParams({
    ui: "r5",
    view: "assets",
    package: "tdx",
  }).toString();
  render(<App transport={transport} />);
  fireEvent.click(
    await screen.findByRole("button", { name: /选择 沪深北 A 股原生日线/ }),
  );
  await screen.findByRole("button", { name: "查询数据" });
  await waitFor(() =>
    expect((screen.getByLabelText("结束日期") as HTMLInputElement).value).toBe(
      "2026-11-16",
    ),
  );
  fireEvent.change(screen.getByLabelText("开始日期"), {
    target: { value: "2026-11-13" },
  });
  await act(async () => {});
  return { source, calls };
}
const submit = () =>
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
it.each(["history", "day"])(
  "normal App %s reads current unfinished native bars, refreshes, and never collects",
  async (mode) => {
    const h = await mount();
    if (mode === "day") {
      fireEvent.change(screen.getByLabelText("查询方向"), {
        target: { value: "day" },
      });
      fireEvent.change(screen.getByLabelText("交易所"), {
        target: { value: "all" },
      });
    }
    submit();
    const result = await screen.findByRole("region", { name: "日线结果" });
    await within(result).findAllByText(/临时 TDX（未入库）.*未完成日K/);
    expect(within(result).getAllByText("未入库，无本地可用时间").length).toBe(
      mode === "day" ? 3 : 1,
    );
    const first = h.calls.filter((c) => c.path.includes("/query?"));
    expect(first).toHaveLength(1);
    expect(h.calls.filter((c) => c.path.includes("/coverage?"))).toEqual([]);
    expect(screen.getByText(/尚未到 15:35 盘后采集时间/)).toBeTruthy();
    const params = new URL(first[0].path, "http://fixture").searchParams;
    expect(params.get("local_only")).toBe("false");
    expect(params.has("day")).toBe(mode === "day");
    expect(params.get("basis")).toBe("native_projection");
    const stats1 = (await h.source.exchange({ control: "stats" })).body;
    expect(stats1.source_requests).toBe(mode === "day" ? 3 : 1);
    expect(stats1.job_files).toBe(0);
    expect(stats1.store_unchanged).toBe(true);
    const currentRow = () =>
      within(screen.getByRole("region", { name: "日线结果" })).getByRole(
        "row",
        { name: /2026-11-16.*600519/ },
      );
    expect(within(currentRow()).getAllByRole("cell")[6].textContent).toBe(
      "1.100",
    );
    submit();
    await waitFor(() =>
      expect(within(currentRow()).getAllByRole("cell")[6].textContent).toBe(
        "1.150",
      ),
    );
    expect(h.calls.filter((c) => c.path.includes("/query?"))).toHaveLength(2);
    fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
    await screen.findByText(/"bar_state"\s*:\s*"intraday_incomplete"/, {
      exact: false,
    });
    expect(h.calls.filter((c) => c.method !== "GET")).toEqual([]);
    const stats = (await h.source.exchange({ control: "stats" })).body;
    expect(stats.source_requests).toBe(mode === "day" ? 6 : 2);
    expect(stats.job_files).toBe(0);
    expect(stats.store_unchanged).toBe(true);
    expect(stats.all_sources_closed).toBe(true);
    expect(screen.getByText(/收益口径待核验/)).toBeTruthy();
  },
  30000,
);
it("explicit local/history switches issue no source requests, including microsecond cutoff", async () => {
  const h = await mount();
  fireEvent.click(
    screen.getByRole("checkbox", { name: "仅本地已保存数据（不补源）" }),
  );
  submit();
  const result = await screen.findByRole("region", { name: "日线结果" });
  await within(result).findByText("2026-11-13");
  expect(
    (await h.source.exchange({ control: "stats" })).body.source_requests,
  ).toBe(0);
  fireEvent.click(screen.getByRole("checkbox", { name: /历史截止/ }));
  const cutoff = screen.getByLabelText("截止时间（北京时间）");
  fireEvent.change(cutoff, { target: { value: "2026-11-16T10:00:00.123456" } });
  submit();
  await waitFor(() =>
    expect(h.calls.filter((c) => c.path.includes("/query?")).length).toBe(2),
  );
  const q = new URL(
    h.calls.filter((c) => c.path.includes("/query?")).at(-1)!.path,
    "http://fixture",
  ).searchParams;
  expect(q.get("as_of")).toContain("123456");
  expect(q.get("local_only")).toBe("true");
  expect(
    (await h.source.exchange({ control: "stats" })).body.source_requests,
  ).toBe(0);
}, 30000);
it("preopen does not invent today's bar from yesterday", async () => {
  const h = await mount("preopen");
  fireEvent.change(screen.getByLabelText("查询方向"), {
    target: { value: "day" },
  });
  submit();
  await screen.findByRole("region", { name: "日线结果" });
  expect(screen.queryByText(/临时 TDX（未入库）/)).toBeNull();
  expect(
    (await h.source.exchange({ control: "stats" })).body.source_requests,
  ).toBe(0);
  await screen.findByText(/当前阶段 盘前/);
}, 30000);

it("cross-section pagination follows consumed candidates and pins the roster across next/previous", async () => {
  const h = await mount("paging");
  fireEvent.change(screen.getByLabelText("查询方向"), {
    target: { value: "day" },
  });
  fireEvent.change(screen.getByLabelText("交易所"), {
    target: { value: "all" },
  });
  submit();
  let table = await screen.findByRole("region", { name: "日线结果" });
  await within(table).findByText("000002");
  expect(within(table).queryByText("000001")).toBeNull();
  const pager = () => within(screen.getByLabelText("TDX 日线 结果分页"));
  fireEvent.click(pager().getByRole("button", { name: "下一页" }));
  await waitFor(() =>
    expect(
      within(screen.getByRole("region", { name: "日线结果" })).getByText(
        "000004",
      ),
    ).toBeTruthy(),
  );
  let q = new URL(
    h.calls.filter((c) => c.path.includes("/query?")).at(-1)!.path,
    "http://fixture",
  ).searchParams;
  expect(q.get("offset")).toBe("2");
  expect(q.get("snapshot_id")).toBe("published-roster");
  expect(
    (pager().getByRole("button", { name: "下一页" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  fireEvent.click(pager().getByRole("button", { name: "上一页" }));
  await waitFor(() =>
    expect(
      within(screen.getByRole("region", { name: "日线结果" })).getByText(
        "000002",
      ),
    ).toBeTruthy(),
  );
  q = new URL(
    h.calls.filter((c) => c.path.includes("/query?")).at(-1)!.path,
    "http://fixture",
  ).searchParams;
  expect(q.get("offset")).toBe("0");
  expect(q.get("snapshot_id")).toBe("published-roster");
  const stats = (await h.source.exchange({ control: "stats" })).body;
  expect(stats.source_requests).toBe(6);
  expect(stats.job_files).toBe(0);
  expect(stats.store_unchanged).toBe(true);
}, 30000);
