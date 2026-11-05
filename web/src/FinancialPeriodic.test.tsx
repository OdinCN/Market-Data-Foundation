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
