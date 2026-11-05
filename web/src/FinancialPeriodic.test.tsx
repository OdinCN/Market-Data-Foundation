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
