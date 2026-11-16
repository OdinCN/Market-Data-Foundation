import "./offlineTestSetup";
import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { Transport } from "./api";
import { FINANCIAL } from "./FinancialDailyWorkbench";
import { useFinancialPeriodic } from "./FinancialPeriodic";
afterEach(cleanup);
const base = {
  status: "OK",
  enabled: false,
  next_check_at: null,
  last_successful_collection_at: null,
  max_attempts: 3,
  tasks: [],
  worker: { state: "paused" },
  control: {
    online: false,
    registered: true,
    reason: "worker_service_not_started",
  },
  maintenance_times: { policy: ["09:10", "14:45"] },
  schedule_semantics: "维护检查，非官方公布承诺",
};
function fixture(post?: (body: unknown) => Promise<Response>) {
  let state = structuredClone(base);
  const calls: { method: string; body?: unknown }[] = [];
  const transport: Transport = vi.fn(async (_url, init) => {
    const method = init?.method ?? "GET",
      body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, body });
    if (method === "POST") {
      if (post) return post(body);
      state = {
        ...state,
        enabled: body.enabled,
        control: { ...state.control, online: true, reason: "worker_online" },
      };
    }
    return Response.json(state);
  });
  return {
    transport,
    calls,
    set: (v: typeof base) => {
      state = v;
    },
  };
}
function Page({ transport }: { transport: Transport }) {
  const p = useFinancialPeriodic(transport, true, vi.fn());
  return (
    <>
      <div>
        {p.row.cells.map((c, i) => (
          <div key={i}>{c}</div>
        ))}
      </div>
      {p.detail}
    </>
  );
}
it("read never starts; exact enable/pause leaves manual work independent", async () => {
  const f = fixture();
  render(<Page transport={f.transport} />);
  const toggle = await screen.findByRole("switch");
  await waitFor(() =>
    expect((toggle as HTMLButtonElement).disabled).toBe(false),
  );
  expect(f.calls.every((c) => c.method === "GET")).toBe(true);
  fireEvent.click(toggle);
  await waitFor(() => expect(toggle.getAttribute("aria-checked")).toBe("true"));
  expect(f.calls.filter((c) => c.method === "POST").map((c) => c.body)).toEqual(
    [{ enabled: true }],
  );
  fireEvent.click(toggle);
  await waitFor(() =>
    expect(toggle.getAttribute("aria-checked")).toBe("false"),
  );
  expect(f.calls.filter((c) => c.method === "POST").map((c) => c.body)).toEqual(
    [{ enabled: true }, { enabled: false }],
  );
  expect(screen.getAllByText(/在途作业正常收尾/).length).toBeGreaterThan(0);
});
it("unknown result is verified without automatically repeating POST", async () => {
  const f = fixture(async () => {
    throw Error("transport interrupted");
  });
  render(<Page transport={f.transport} />);
  const t = await screen.findByRole("switch");
  await waitFor(() => expect((t as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(t);
  await screen.findByRole("button", { name: "核实操作结果" });
  expect((t as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "核实操作结果" }));
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "核实操作结果" })).toBeNull(),
  );
  expect(f.calls.filter((c) => c.method === "POST")).toHaveLength(1);
  expect(screen.getAllByText(/已核实：策略暂停/).length).toBeGreaterThan(0);
});
it("enabled but stopped exposes one recovery, with real source failure reason", async () => {
  const f = fixture(async () =>
    Response.json(
      {
        status: "UNKNOWN",
        classification: "financial_bound_credential_missing",
      },
      { status: 403 },
    ),
  );
  f.set({ ...base, enabled: true });
  render(<Page transport={f.transport} />);
  const recover = await screen.findByRole("button", { name: "恢复自动采集" });
  fireEvent.click(recover);
  await screen.findAllByText(/现有 FRED 凭据绑定缺失/);
  expect(f.calls.filter((c) => c.method === "POST").map((c) => c.body)).toEqual(
    [{ enabled: true, recover: true }],
  );
  expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("true");
});
it("request in progress prevents duplicate actions and never claims collection success", async () => {
  let done: (r: Response) => void = () => {};
  const f = fixture(
    () =>
      new Promise((r) => {
        done = r;
      }),
  );
  render(<Page transport={f.transport} />);
  const t = await screen.findByRole("switch");
  await waitFor(() => expect((t as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(t);
  fireEvent.click(t);
  expect(f.calls.filter((c) => c.method === "POST")).toHaveLength(1);
  expect(t.getAttribute("aria-busy")).toBe("true");
  done(
    Response.json({
      ...base,
      enabled: true,
      control: { ...base.control, online: true },
    }),
  );
  await screen.findAllByText(/采集成功以实际作业回读为准/);
});

it("separates summary loading from no observations, retains known dates through refresh and labels source basis", async () => {
  let complete: (r: Response) => void = () => {};
  let summary: unknown = null;
  const transport: Transport = async (u) =>
    String(u).endsWith("/summary")
      ? summary
        ? Response.json({ status: "OK", series: summary })
        : new Promise((r) => {
            complete = r;
          })
      : Response.json(base);
  render(<Page transport={transport} />);
  await screen.findByText(/最新数据期：正在读取本地覆盖/);
  summary = [
    { series_id: "DFEDTARL", last: "2026-11-05" },
    { series_id: "DGS10", last: "2026-11-04" },
  ];
  complete(Response.json({ status: "OK", series: summary }));
  await screen.findByText(/最新数据期：2026-11-04 至 2026-11-05/);
  fireEvent.click(screen.getByText("各指标最新本地数据期"));
  expect(
    screen.getByText("DFEDTARL · 联储理事会（政策投影）：2026-11-05"),
  ).toBeTruthy();
  expect(
    screen.getByText("DGS10 · 美国财政部（原始）：2026-11-04"),
  ).toBeTruthy();
  expect(screen.queryByText(/本地尚无已确认的有效观测/)).toBeNull();
});
it("an observed empty summary is different from a failed summary", async () => {
  const empty: Transport = async (u) =>
    Response.json(
      String(u).endsWith("/summary")
        ? { status: "OK", series: [{ series_id: "DGS2", last: null }] }
        : base,
    );
  const view = render(<Page transport={empty} />);
  await screen.findByText(/最新数据期：本地尚无已确认的有效观测/);
  view.unmount();
  const failed: Transport = async (u) =>
    String(u).endsWith("/summary")
      ? Response.json(
          { status: "UNKNOWN", classification: "financial_local_unavailable" },
          { status: 503 },
        )
      : Response.json(base);
  render(<Page transport={failed} />);
  await screen.findByText(/最新数据期：读取失败，待核实/);
  expect(screen.queryByText(/本地尚无已确认的有效观测/)).toBeNull();
});

it("does not claim paused or absent execution before strategy facts have loaded", async () => {
  let complete: (r: Response) => void = () => {};
  const transport: Transport = async (u) =>
    String(u).endsWith("/summary")
      ? Response.json({ status: "OK", series: [] })
      : String(u).endsWith("/jobs")
        ? Response.json({ status: "OK", jobs: [] })
        : new Promise((r) => {
            complete = r;
          });
  render(<Page transport={transport} />);
  await screen.findByText("自动采集状态：正在读取…");
  expect(screen.queryByText(/^自动采集已暂停/)).toBeNull();
  expect(screen.getAllByText("正在读取执行记录…").length).toBeGreaterThan(0);
  complete(Response.json(base));
  await screen.findByText("自动采集已暂停 · 已暂停");
  expect(screen.getAllByText("尚无成功自动作业").length).toBeGreaterThan(0);
});

it("shows dated Beijing maintenance slots, persisted automatic job identity/stats and a GET-only detail jump", async () => {
  const id = "00000000-0000-4000-8000-000000000009";
  const nav = vi.fn(),
    requests: string[] = [];
  const transport: Transport = async (u, i) => {
    requests.push(i?.method ?? "GET");
    return Response.json(
      String(u).endsWith("/jobs")
        ? {
            status: "OK",
            jobs: [
              {
                request_id: id,
                origin: "periodic",
                state: "complete",
                created_at: "2026-11-05T14:00:00Z",
                started_at: "2026-11-05T14:00:01Z",
                finished_at: "2026-11-05T14:00:04Z",
                command: {
                  action: "update",
                  start: "2026-10-23",
                  end: "2026-11-05",
                },
                provider_requests: 2,
                steps: [
                  {
                    state: "complete",
                    result: { saved_points: 3, saved_revisions: 1 },
                  },
                ],
              },
            ],
          }
        : String(u).endsWith("/summary")
          ? { status: "OK", series: [] }
          : {
              ...base,
              next_group_checks: [
                { group: "policy", at: "2026-03-08T13:10:00Z" },
                { group: "treasury", at: "2026-11-02T23:30:00Z" },
              ],
            },
    );
  };
  function Test() {
    const p = useFinancialPeriodic(transport, true, nav);
    return p.detail;
  }
  render(<Test />);
  await screen.findByText(id);
  expect(
    screen.getByRole("table", { name: "下一次分组维护计划" }).textContent,
  ).toContain("2026-03-08 21:10:00 北京时间");
  expect(
    screen.getByRole("table", { name: "下一次分组维护计划" }).textContent,
  ).toContain("2026-11-03 07:30:00 北京时间");
  expect(screen.getByText("新增修订").closest(".fact")?.textContent).toContain(
    "1",
  );
  fireEvent.click(screen.getByRole("button", { name: "打开对应执行详情" }));
  expect(nav).toHaveBeenCalledWith(
    "collection",
    expect.objectContaining({ job: id, tab: "executions" }),
  );
  expect(requests.every((m) => m === "GET")).toBe(true);
});

it("execution loading, failed read and an observed empty result remain distinct", async () => {
  let done!: (r: Response) => void;
  const transport: Transport = async (u) =>
    String(u).endsWith("/jobs")
      ? new Promise((r) => {
          done = r;
        })
      : Response.json(
          String(u).endsWith("/summary") ? { status: "OK", series: [] } : base,
        );
  render(<Page transport={transport} />);
  await screen.findByText("正在读取真实执行记录…");
  expect(screen.queryByText("尚无真实执行记录。")).toBeNull();
  done(
    Response.json(
      { status: "UNKNOWN", classification: "financial_local_unavailable" },
      { status: 503 },
    ),
  );
  await screen.findByText("执行记录读取失败，最近执行待核实。");
  expect(screen.queryByText("尚无真实执行记录。")).toBeNull();
});

it.each(["complete", "failed"])(
  "uses latest group %s for current state and retains older exhausted failure",
  async (latestState) => {
    const old = {
      group: "treasury",
      slot: "2026-11-11T23:30:00Z",
      state: "failed",
      request_id: "old-treasury",
      attempts: 3,
      classification: "financial_periodic_partial_failure",
      retry_at: "2026-11-12T00:05:00Z",
      failures: [
        {
          series_id: "DGS10",
          state: "failed",
          classification: "financial_timeout",
        },
      ],
    };
    const current = {
      ...old,
      request_id: "current-treasury",
      slot: "2026-11-12T23:30:00Z",
      state: latestState,
    };
    const data = {
      ...base,
      enabled: true,
      control: { ...base.control, online: true },
      tasks: [old, current],
    };
    const transport: Transport = async (url) =>
      Response.json(
        String(url).endsWith("/jobs")
          ? { jobs: [] }
          : String(url).endsWith("/summary")
            ? { series: [] }
            : data,
      );
    render(<Page transport={transport} />);
    await screen.findByText(
      `运行情况：${latestState === "complete" ? "等待下次检查" : "需处理"}`,
    );
    expect(screen.getAllByText("old-treasury")).toHaveLength(1);
    expect(screen.getAllByText(/旧维护时点；不将旧 retry_at/)).toHaveLength(1);
    expect(
      screen.getAllByText(/financial_timeout|请求超时/).length,
    ).toBeGreaterThan(0);
  },
);

it.each(["manual", null])(
  "latest financial summary uses the actual newest %s record, not an older automatic result",
  async (origin) => {
    const nav = vi.fn();
    const make = (
      request_id: string,
      created_at: string,
      value: string | null,
    ) => ({
      request_id,
      origin: value,
      state: "complete",
      created_at,
      started_at: created_at,
      finished_at: created_at,
      command: { action: "update", start: "2026-11-01", end: "2026-11-02" },
      steps: [],
      provider_requests: 0,
    });
    const transport: Transport = async (url) =>
      Response.json(
        String(url).endsWith("/jobs")
          ? {
              status: "OK",
              jobs: [
                make("old-automatic", "2026-11-15T01:00:00Z", "periodic"),
                make("newest-record", "2026-11-16T01:00:00Z", origin),
              ],
            }
          : String(url).endsWith("/summary")
            ? { status: "OK", series: [] }
            : base,
      );
    function Test() {
      return useFinancialPeriodic(transport, true, nav).detail;
    }
    render(<Test />);
    await screen.findByText("newest-record");
    expect(
      screen.getByText(
        new RegExp(
          `本资产最近执行来源：${origin === "manual" ? "手动采集" : "触发来源待核实"}`,
        ),
      ),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "打开对应执行详情" }));
    expect(nav).toHaveBeenCalledWith(
      "collection",
      expect.objectContaining({
        asset: FINANCIAL.identity,
        job: "newest-record",
      }),
    );
  },
);
