import "./offlineTestSetup";
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
import { FinancialCollection } from "./FinancialCollection";
import {
  FinancialDailyWorkbench,
  FINANCIAL,
  type FinancialJob,
  type FinancialStatus,
} from "./FinancialDailyWorkbench";
import { financialIntentKey } from "./useFinancialSubmission";
import type { Transport } from "./api";
const status: FinancialStatus = {
  defaults: { start: "2006-11-05", end: "2026-11-05" },
  update_defaults: { start: "2026-10-23", end: "2026-11-05" },
  series: [],
  cutoff_semantics: "MDF held",
};
const id = "00000000-0000-4000-8000-000000000099";
const make = (
  rid: string,
  state = "running",
  updated_at = "2026-11-05T12:01:00Z",
): FinancialJob => ({
  request_id: rid,
  state,
  created_at: "2026-11-05T12:00:00Z",
  updated_at,
  command: { action: "update", ...status.update_defaults! },
  resume_count: 0,
  provider_requests: 0,
  steps: [],
});
afterEach(() => {
  cleanup();
  localStorage.removeItem(financialIntentKey);
  vi.useRealTimers();
  vi.restoreAllMocks();
});
function api(initial = "running") {
  let record: FinancialJob | null = null,
    mode = "ok";
  const calls: { url: string; method: string; body: any }[] = [];
  const transport: Transport = async (u, i) => {
    const url = String(u),
      method = i?.method ?? "GET",
      body = i?.body ? JSON.parse(String(i.body)) : null;
    calls.push({ url, method, body });
    if (method === "POST") {
      record = make(body.request_id, initial);
      if (url.endsWith("/resume"))
        record = {
          ...record,
          resume_count: 1,
          updated_at: "2026-11-05T12:03:00Z",
        };
      if (mode === "timeout") throw new TypeError("controlled_timeout");
      return Response.json({ status: "OK", job: record });
    }
    if (url.endsWith("/jobs"))
      return Response.json({ status: "OK", jobs: record ? [record] : [] });
    if (mode === "404" || mode === "timeout" || !record)
      return Response.json(
        { status: "UNKNOWN", classification: "financial_job_not_found" },
        { status: 404 },
      );
    if (mode === "wrong")
      return Response.json({
        status: "OK",
        job: { ...record, request_id: id },
      });
    return Response.json({ status: "OK", job: record });
  };
  return {
    transport,
    calls,
    get: () => record,
    set: (r: FinancialJob | null) => {
      record = r;
    },
    mode: (m: string) => {
      mode = m;
    },
  };
}
const submit = () => {
  fireEvent.click(
    screen.getByRole("checkbox", { name: "我确认将以上范围采集到开发测试库" }),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "提交 金融条件日频 采集" }),
  );
};
const postCount = (a: ReturnType<typeof api>) =>
  a.calls.filter((c) => c.method === "POST").length;
it.each(["complete", "failed"])(
  "%s receipts stay on the form, retain ID until newIntent, and only an explicit card click navigates",
  async (state) => {
    const a = api(state),
      nav = vi.fn();
    render(
      <FinancialCollection
        status={status}
        transport={a.transport}
        generation={0}
        onExecution={nav}
      />,
    );
    submit();
    const next = await screen.findByRole("button", { name: "开始另一项采集" });
    expect(nav).not.toHaveBeenCalled();
    expect(postCount(a)).toBe(1);
    const rid = a.get()!.request_id;
    expect(
      JSON.parse(localStorage.getItem(financialIntentKey)!).request_id,
    ).toBe(rid);
    const brief = screen.getByRole("complementary", { name: "执行简报" });
    const card = within(brief).getByRole("button", { name: new RegExp(rid) });
    expect(card.getAttribute("aria-current")).toBe("true");
    fireEvent.click(next);
    expect(localStorage.getItem(financialIntentKey)).toBeNull();
    expect(postCount(a)).toBe(1);
    expect(nav).not.toHaveBeenCalled();
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(
      false,
    );
    await waitFor(() =>
      expect(
        within(brief).getByRole("button", { name: new RegExp(rid) }),
      ).toBeTruthy(),
    );
    fireEvent.click(
      within(brief).getByRole("button", { name: new RegExp(rid) }),
    );
    expect(nav).toHaveBeenCalledWith(rid);
  },
);
it("accepted running becomes complete through automatic GET polling, without navigation or a second POST", async () => {
  vi.useFakeTimers();
  const a = api(),
    nav = vi.fn();
  render(
    <FinancialCollection
      status={status}
      transport={a.transport}
      generation={0}
      onExecution={nav}
    />,
  );
  await act(async () => submit());
  const running = a.get()!;
  expect(screen.queryByRole("button", { name: "开始另一项采集" })).toBeNull();
  a.set({ ...running, state: "complete", updated_at: "2026-11-05T12:02:00Z" });
  await act(async () => vi.advanceTimersByTimeAsync(5100));
  expect(screen.getByRole("button", { name: "开始另一项采集" })).toBeTruthy();
  expect(nav).not.toHaveBeenCalled();
  expect(postCount(a)).toBe(1);
  expect(
    a.calls.some(
      (c) =>
        c.method === "GET" && c.url.endsWith("/jobs/" + running.request_id),
    ),
  ).toBe(true);
});
it.each(["404", "wrong"])(
  "unknown and %s readbacks protect the original intent through reload and a later matching receipt",
  async (mode) => {
    const a = api();
    a.mode("timeout");
    const nav = vi.fn();
    const view = render(
      <FinancialCollection
        status={status}
        transport={a.transport}
        generation={0}
        onExecution={nav}
      />,
    );
    submit();
    await screen.findByRole("button", { name: "核实原请求" });
    const original = a.get()!;
    a.mode(mode);
    view.unmount();
    render(
      <FinancialCollection
        status={status}
        transport={a.transport}
        generation={0}
        onExecution={nav}
      />,
    );
    await screen.findByRole("button", { name: "核实原请求" });
    expect(screen.queryByRole("button", { name: "开始另一项采集" })).toBeNull();
    expect(postCount(a)).toBe(1);
    a.mode("ok");
    a.set({
      ...original,
      state: "complete",
      updated_at: "2026-11-05T12:02:00Z",
    });
    fireEvent.click(screen.getByRole("button", { name: "核实原请求" }));
    await screen.findByRole("button", { name: "开始另一项采集" });
    expect(nav).not.toHaveBeenCalled();
    expect(postCount(a)).toBe(1);
    expect(
      JSON.parse(localStorage.getItem(financialIntentKey)!).request_id,
    ).toBe(original.request_id);
  },
);
it("stale regressing receipts retain the last known state but cannot release the request", async () => {
  vi.useFakeTimers();
  const a = api("complete"),
    nav = vi.fn();
  render(
    <FinancialCollection
      status={status}
      transport={a.transport}
      generation={0}
      onExecution={nav}
    />,
  );
  await act(async () => submit());
  const complete = a.get()!;
  expect(screen.getByRole("button", { name: "开始另一项采集" })).toBeTruthy();
  a.set({ ...complete, state: "running", updated_at: "2026-11-05T12:00:30Z" });
  await act(async () => vi.advanceTimersByTimeAsync(5100));
  expect(screen.queryByRole("button", { name: "开始另一项采集" })).toBeNull();
  expect(screen.getByRole("button", { name: "核实原请求" })).toBeTruthy();
  expect(JSON.parse(localStorage.getItem(financialIntentKey)!).request_id).toBe(
    complete.request_id,
  );
  expect(nav).not.toHaveBeenCalled();
  expect(postCount(a)).toBe(1);
  a.set({ ...complete, updated_at: "2026-11-05T12:02:00Z" });
  await act(async () => vi.advanceTimersByTimeAsync(5100));
  expect(screen.getByRole("button", { name: "开始另一项采集" })).toBeTruthy();
});
it("resume keeps the original execution route and posts only its original request ID", async () => {
  const a = api();
  const failed = make(id, "failed");
  failed.steps = [
    {
      series_id: "DGS10",
      start: "2026-10-23",
      end: "2026-11-05",
      source: "origin",
      state: "failed",
      classification: "financial_http_failure",
    },
  ];
  a.set(failed);
  const nav = vi.fn();
  render(
    <FinancialDailyWorkbench
      detailOnly
      route={{
        view: "collection",
        invalid: false,
        fields: new URLSearchParams({
          asset: FINANCIAL.identity,
          tab: "executions",
          job: id,
        }),
      }}
      navigate={nav}
      transport={a.transport}
      generation={0}
    />,
  );
  fireEvent.click(await screen.findByRole("button", { name: "续跑此作业" }));
  await waitFor(() => expect(postCount(a)).toBe(1));
  await screen.findByText("采集中");
  expect(nav).not.toHaveBeenCalled();
  expect(a.calls.find((c) => c.method === "POST")!.body).toEqual({
    request_id: id,
  });
  expect(JSON.parse(localStorage.getItem(financialIntentKey)!).request_id).toBe(
    id,
  );
});

it.each([408, 429, 404, 400])(
  "HTTP %s without an explicit admission/validation rejection cannot release the request",
  async (statusCode) => {
    const calls: { method: string; body: any }[] = [];
    const transport: Transport = async (_u, i) => {
      const method = i?.method ?? "GET";
      calls.push({ method, body: i?.body ? JSON.parse(String(i.body)) : null });
      return method === "POST"
        ? Response.json(
            {
              status: "UNKNOWN",
              classification: "financial_request_outcome_unknown",
            },
            { status: statusCode },
          )
        : Response.json(
            { status: "UNKNOWN", classification: "financial_job_not_found" },
            { status: 404 },
          );
    };
    render(
      <FinancialCollection
        transport={transport}
        generation={0}
        status={status}
        onExecution={vi.fn()}
      />,
    );
    submit();
    await screen.findByRole("button", { name: "核实原请求" });
    expect(screen.queryByRole("button", { name: "开始另一项采集" })).toBeNull();
    expect(
      JSON.parse(localStorage.getItem(financialIntentKey)!).request_id,
    ).toBe(calls.find((c) => c.method === "POST")!.body.request_id);
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
  },
);
