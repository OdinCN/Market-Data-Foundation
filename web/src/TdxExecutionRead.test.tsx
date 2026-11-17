import "./offlineTestSetup";
import { afterEach, expect, it, vi } from "vitest";
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
import { TDX_FAMILY, TdxExecutionDetail } from "./TdxDailyFamily";
import { TDX_DAILY, type TdxJob } from "./TdxDailyWorkbench";
import type { Transport } from "./api";
import { keyOf } from "./workflowModel";
import type { FamilyProps, RuntimeFamily } from "./RuntimeFamily";
import { useFamilyRecords } from "./RuntimeFamily";
import { asgi, closeBridges } from "./workflowTestBridge";
const ids = [
  "a097ee8a-bdab-4edf-9bfe-504a69093f18",
  "83a0ed6a-f841-52a8-8e1f-ac404ea19c68",
];
const job = (id = ids[0]): TdxJob => ({
  request_id: id,
  state: "COMPLETE",
  origin: id === ids[0] ? "manual" : "periodic",
  created_at: "2026-11-16T11:32:18Z",
  provider_requests: 5582,
  step_count: 5582,
  complete_steps: 5582,
  steps_paging: true,
  command: { action: "update", start: "2026-11-02", end: "2026-11-16" },
});
const fields = (id = ids[0]) =>
  new URLSearchParams({
    tab: "executions",
    asset: TDX_DAILY.identity,
    asset_revision: TDX_DAILY.revision,
    execution_asset: "all",
    job: id,
  });
const props = (id = ids[0]): Omit<FamilyProps, "transport"> => ({
  route: { view: "collection", invalid: false, fields: fields(id) },
  navigate: vi.fn(),
  generation: 0,
});
const pending = () => new Promise<Response>(() => {});
let root = "";
afterEach(() => {
  cleanup();
  closeBridges();
  if (root) rmSync(root, { recursive: true, force: true });
  root = "";
  location.hash = "";
  sessionStorage.clear();
  localStorage.clear();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

it.each(["all", keyOf(TDX_DAILY)])(
  "shows original facts, switches and returns through policy with delayed shards and unrelated statuses (%s)",
  async (filter) => {
    root = mkdtempSync(join(tmpdir(), "mdf-d01-detail-"));
    const directory = asgi("financial_manual", "d01_ui_exchange.py", {
      store_dir: root,
    });
    expect(
      (
        await directory.exchange({
          method: "PUT",
          path: "/api/r5/packages/tdx",
          body: {
            expected_revision: null,
            enabled_capability_ids: ["daily_native", "stock_roster"],
            source_changes: [],
          },
        })
      ).status,
    ).toBe(200);
    const sources = (
      await directory.exchange({ method: "GET", path: "/api/r5/sources" })
    ).body;
    const packages = (
      await directory.exchange({ method: "GET", path: "/api/r5/packages" })
    ).body;
    const calls: { path: string; method: string }[] = [];
    const transport: Transport = async (input, init) => {
      const path = String(input);
      calls.push({ path, method: init?.method ?? "GET" });
      if (path === "/api/r5/sources") return Response.json(sources);
      if (path === "/api/r5/packages") return Response.json(packages);
      if (path.includes("/timeseries/status"))
        return path.includes("consumption")
          ? Response.json(
              { classification: "cpi_timeseries_storage_read_failed" },
              { status: 503 },
            )
          : pending();
      if (path === "/api/r5/tdx/daily/jobs")
        return Response.json({ jobs: ids.map((id) => job(id)) });
      if (path === "/api/r5/tdx/roster/jobs") return pending();
      if (path === "/api/r5/tdx/daily/strategy")
        return Response.json({ enabled: true, worker_online: true });
      if (path === "/api/r5/tdx/daily/status")
        return Response.json({ collect: true });
      if (path.includes("/tdx/daily/jobs/")) return pending();
      return Response.json({ classification: "not_enabled" }, { status: 404 });
    };
    vi.useFakeTimers();
    location.hash = new URLSearchParams({
      ui: "r5",
      view: "collection",
      ...Object.fromEntries(fields()),
      execution_asset: filter,
    }).toString();
    render(<App transport={transport} />);
    await act(async () => {});
    let shell = screen.getByRole("region", { name: "执行详情主体" });
    expect(shell.dataset.executionId).toBe(ids[0]);
    expect(shell.dataset.executionAsset).toBe(keyOf(TDX_DAILY));
    fireEvent.click(
      within(shell).getByRole("button", { name: "查看周期任务" }),
    );
    await act(async () => {});
    expect(
      screen.getByLabelText("周期操作").getAttribute("data-periodic-id"),
    ).toBe("tdx-daily-auto");
    fireEvent.click(screen.getByRole("button", { name: "返回此执行记录" }));
    await act(async () => {});
    expect(new URLSearchParams(location.hash.slice(1)).get("job")).toBe(ids[0]);
    const rows = screen.getByRole("region", { name: "执行记录" });
    const second = rows.querySelector<HTMLTableRowElement>(
      `tr[data-execution-id="${ids[1]}"]`,
    )!;
    fireEvent.click(within(second).getByRole("button"));
    await act(async () => {});
    shell = screen.getByRole("region", { name: "执行详情主体" });
    expect(shell.dataset.executionId).toBe(ids[1]);
    expect(within(shell).getByText("自动策略", { exact: true })).toBeTruthy();
    const before = calls.filter((c) =>
      c.path.includes("/timeseries/status"),
    ).length;
    expect(before).toBe(0);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30000);
    });
    expect(
      calls.filter((c) => c.path.includes("/timeseries/status")),
    ).toHaveLength(before);
    expect(
      screen.getByRole("region", { name: "执行详情主体" }).dataset.executionId,
    ).toBe(ids[1]);
    const refresh = screen.getByRole("button", {
      name: "刷新",
    }) as HTMLButtonElement;
    expect(refresh.disabled).toBe(false);
    const reads = calls.filter(
      (c) => c.path === "/api/r5/tdx/daily/jobs",
    ).length;
    fireEvent.click(refresh);
    await act(async () => {});
    expect(
      calls.filter((c) => c.path === "/api/r5/tdx/daily/jobs"),
    ).toHaveLength(reads + 1);
    expect(
      screen.getByRole("region", { name: "执行详情主体" }).dataset.executionId,
    ).toBe(ids[1]);
    expect(calls.every((c) => c.method === "GET")).toBe(true);
  },
);

it("keeps fast summary and actions usable while a shard page waits, and rejects a false empty page", async () => {
  let release!: (r: Response) => void;
  const transport: Transport = async (input) =>
    String(input).includes("view=summary")
      ? Response.json(job())
      : new Promise((r) => {
          release = r;
        });
  render(<TdxExecutionDetail {...props()} transport={transport} />);
  await act(async () => {});
  expect(
    screen.getByRole("region", { name: "执行详情主体" }).dataset.executionId,
  ).toBe(ids[0]);
  expect(screen.getByRole("button", { name: "查看资产数据" })).toBeTruthy();
  expect(screen.getByText(/正在读取采集分片/)).toBeTruthy();
  await act(async () =>
    release(
      Response.json({
        request_id: ids[0],
        steps: [],
        total: 5582,
        offset: 0,
        next_offset: 25,
      }),
    ),
  );
  expect(screen.getByRole("alert").textContent).toContain("采集分片读取失败");
  expect(screen.queryByText("暂无可用条目")).toBeNull();
});

it("uses actual server search/pages and ignores late job A after switching to B", async () => {
  const calls: string[] = [];
  let late!: (r: Response) => void;
  const transport: Transport = async (input) => {
    const u = new URL(String(input), "http://fixture");
    calls.push(u.pathname + u.search);
    const id = u.pathname.includes(ids[1]) ? ids[1] : ids[0];
    if (u.searchParams.get("view") === "summary")
      return id === ids[0]
        ? new Promise((r) => {
            late = r;
          })
        : Response.json(job(id));
    const offset = Number(u.searchParams.get("offset")),
      search = u.searchParams.get("search");
    return Response.json({
      request_id: id,
      steps: Array.from({ length: search ? 1 : 25 }, (_, i) => ({
        market: 0,
        instrument: search || String(offset + i).padStart(6, "0"),
        state: "COMPLETE",
      })),
      total: search ? 1 : 5582,
      offset,
      next_offset: search ? null : offset + 25,
    });
  };
  const view = render(
    <TdxExecutionDetail {...props()} transport={transport} />,
  );
  view.rerender(
    <TdxExecutionDetail {...props(ids[1])} transport={transport} />,
  );
  await act(async () => {});
  await act(async () => late(Response.json(job())));
  expect(
    screen.getByRole("region", { name: "执行详情主体" }).dataset.executionId,
  ).toBe(ids[1]);
  fireEvent.click(screen.getByRole("button", { name: "后一页分片" }));
  await act(async () => {});
  expect(screen.getByText("000025")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("搜索采集分片"), {
    target: { value: "920002" },
  });
  fireEvent.click(screen.getByRole("button", { name: "搜索分片" }));
  await act(async () => {});
  expect(screen.getByText("920002")).toBeTruthy();
  expect(calls.at(-1)).toContain("search=920002&offset=0&limit=25");
  const count = calls.length;
  fireEvent.click(screen.getByRole("button", { name: "搜索分片" }));
  await act(async () => {});
  expect(calls).toHaveLength(count + 1);
});

it.each(["missing", "wrong-id", "legacy"])(
  "handles %s named records without manufacturing successful shards",
  async (mode) => {
    const transport: Transport = async () =>
      mode === "missing"
        ? Response.json({ classification: "job_not_found" }, { status: 404 })
        : Response.json({
            ...job(mode === "wrong-id" ? ids[1] : ids[0]),
            steps_paging: undefined,
            steps:
              mode === "legacy"
                ? [
                    {
                      market: 2,
                      instrument: "920002",
                      state: "FAILED",
                      classification: "real_failure",
                    },
                  ]
                : undefined,
          });
    render(<TdxExecutionDetail {...props()} transport={transport} />);
    await act(async () => {});
    if (mode === "legacy")
      expect(screen.getByText("real_failure")).toBeTruthy();
    else {
      expect(screen.getByRole("alert")).toBeTruthy();
      expect(screen.queryByRole("region", { name: "执行详情主体" })).toBeNull();
    }
  },
);

it("tracks running metadata and only permits confirmed recovery with a fresh grant", async () => {
  vi.useFakeTimers();
  let state = "RUNNING",
    collect = false;
  const calls: { path: string; method: string }[] = [];
  const transport: Transport = async (input, init) => {
    const path = String(input);
    calls.push({ path, method: init?.method ?? "GET" });
    if (path.endsWith("/status")) return Response.json({ collect });
    if (path.includes("view=summary"))
      return Response.json({ ...job(), state });
    return Response.json({
      request_id: ids[0],
      steps: Array.from({ length: 25 }, (_, i) => ({
        market: 0,
        instrument: String(i).padStart(6, "0"),
        state: "RUNNING",
      })),
      total: 5582,
      offset: 0,
      next_offset: 25,
    });
  };
  render(<TdxExecutionDetail {...props()} transport={transport} />);
  await act(async () => {});
  expect(screen.queryByRole("button", { name: "续跑未完成分片" })).toBeNull();
  state = "PARTIAL";
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  const resume = screen.getByRole("button", {
    name: "续跑未完成分片",
  }) as HTMLButtonElement;
  fireEvent.click(
    screen.getByRole("checkbox", { name: "我确认按原请求恢复采集" }),
  );
  expect(resume.disabled).toBe(true);
  fireEvent.click(resume);
  collect = true;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(resume.disabled).toBe(false);
  state = "COMPLETE";
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(screen.queryByRole("button", { name: "续跑未完成分片" })).toBeNull();
  expect(calls.every((c) => c.method === "GET")).toBe(true);
});

it.each([false, true])(
  "a newly registered family publishes independently of a failed=%s or delayed peer",
  async (fail) => {
    const future: RuntimeFamily = {
      ...TDX_FAMILY,
      asset: { identity: "mdf.test.future", revision: "v2" },
      collection: {
        protocol: "macro",
        path: "/fixture/future/jobs",
        strategy: null,
      },
      readExecutions: async () => [
        {
          id: "future-job",
          createdAt: "t",
          title: "future",
          search: "future",
          result: "verified",
        },
      ],
    };
    const slow = {
      ...TDX_FAMILY,
      readExecutions: async () => {
        if (fail) throw Error("peer_failed");
        return new Promise<never>(() => {});
      },
    };
    const transport: Transport = async () => Response.json({});
    function Page() {
      const data = useFamilyRecords(
        [slow, future],
        { ...props(), transport },
        false,
      );
      return (
        <>
          <p>{data.executions.data?.map((r) => r.id).join(" ")}</p>
          {data.executions.failure && <p role="alert">登记资产读取失败</p>}
        </>
      );
    }
    render(<Page />);
    await act(async () => {});
    expect(screen.getByText("future-job")).toBeTruthy();
    expect(screen.queryByRole("alert") !== null).toBe(fail);
  },
);

it.each([true, false])(
  "reads the recorded roster identities without inferring historical morning provenance (bound=%s)",
  async (bound) => {
    const rosterId = "b38ebc04-8e26-534a-b9ba-282bb0ce4e48";
    const snapshot = "58710b412e0b4f86be81009e3f36121a";
    const calls: string[] = [];
    const record: TdxJob = {
      ...job(ids[1]),
      snapshot_id: snapshot,
      ...(bound
        ? {
            roster_binding: {
              day: "2026-11-16",
              request_id: rosterId,
              snapshot_id: snapshot,
              source_binding: "mdf.binding.tdx-v100.stock.roster",
            },
          }
        : {}),
    };
    const transport: Transport = async (input, init) => {
      expect(init?.method ?? "GET").toBe("GET");
      calls.push(String(input));
      return String(input).includes("view=summary")
        ? Response.json(record)
        : pending();
    };
    render(<TdxExecutionDetail {...props(ids[1])} transport={transport} />);
    await screen.findByText(snapshot);
    expect(
      screen.getByRole("region", { name: "执行详情主体" }).dataset.executionId,
    ).toBe(ids[1]);
    if (bound) {
      expect(
        screen.getByText("晨间名册作业").closest(".fact")?.textContent,
      ).toContain(rosterId);
      expect(
        screen.getByText("名册目标交易日").closest(".fact")?.textContent,
      ).toContain("2026-11-16");
    } else {
      expect(screen.queryByText("晨间名册作业")).toBeNull();
      expect(screen.queryByText(rosterId)).toBeNull();
    }
    expect(screen.queryByRole("button", { name: /回查/ })).toBeNull();
    expect(calls.every((path) => path.includes("/jobs/" + ids[1]))).toBe(true);
  },
);

it("labels an unresolved morning target as unverified rather than an available roster", async () => {
  const rosterId = "b38ebc04-8e26-534a-b9ba-282bb0ce4e48";
  const record: TdxJob = {
    ...job(ids[1]),
    state: "PARTIAL",
    provider_requests: 0,
    step_count: 0,
    complete_steps: 0,
    classification: "tdx_periodic_roster_job_missing",
    roster_binding: {
      day: "2026-11-16",
      request_id: rosterId,
      snapshot_id: null,
      source_binding: "mdf.binding.tdx-v100.stock.roster",
    },
  };
  const transport: Transport = async (input, init) => {
    expect(init?.method ?? "GET").toBe("GET");
    return String(input).includes("view=summary")
      ? Response.json(record)
      : String(input).endsWith("/status")
        ? Response.json({ enabled: true, collect: false })
        : pending();
  };
  render(<TdxExecutionDetail {...props(ids[1])} transport={transport} />);
  await screen.findByText(rosterId);
  expect(
    screen.getByText("待核实名册作业").closest(".fact")?.textContent,
  ).toContain(rosterId);
  expect(screen.queryByText("晨间名册作业")).toBeNull();
  expect(screen.getByText("发布名册").closest(".fact")?.textContent).toContain(
    "尚未发布或未记录",
  );
  expect(screen.queryByRole("button", { name: /回查/ })).toBeNull();
});
