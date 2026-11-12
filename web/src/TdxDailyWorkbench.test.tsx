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
import type { Route } from "./workflowRoute";
import { TDX_DAILY, TdxDailyWorkbench } from "./TdxDailyWorkbench";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
const identity = "2679c123-8a3e-4ae9-8aa9-fce60c5f94e1";
function route(
  view: "assets" | "collection",
  tab: string,
  job?: string,
): Route {
  return {
    view,
    invalid: false,
    fields: new URLSearchParams({
      asset: TDX_DAILY.identity,
      asset_revision: TDX_DAILY.revision,
      tab,
      ...(job ? { job } : {}),
    }),
  };
}
function fixture() {
  const calls: { url: string; method: string; body: unknown }[] = [];
  const job = {
    request_id: identity,
    state: "INTERRUPTED",
    command: { action: "init", start: "1990-01-01", end: "2026-11-11" },
    created_at: "2026-11-12T06:00:00Z",
    provider_requests: 2,
    steps: [],
  };
  const transport: Transport = vi.fn(async (input, init) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, method, body });
    if (method === "POST") return Response.json(job);
    if (url.endsWith("/status"))
      return Response.json({
        enabled: true,
        collect: true,
        defaults: { end: "2026-11-11" },
      });
    if (url.endsWith("/help"))
      return Response.json({
        examples: {
          history: "history-example",
          cross_section: "day-example",
          coverage: "coverage-example",
        },
        notes: ["native_projection"],
      });
    if (url.endsWith("/summary")) return Response.json({ markets: [] });
    if (url.endsWith("/jobs")) return Response.json({ jobs: [job] });
    if (url.includes("/jobs/")) return Response.json(job);
    if (url.endsWith("/strategy"))
      return Response.json({ enabled: false, worker_online: true });
    if (url.includes("/coverage?"))
      return Response.json({ markets: [], missing: [], missing_total: 0 });
    if (url.includes("/query?"))
      return Response.json({
        rows: [
          {
            market: 2,
            instrument: "920002",
            trading_day: "2026-11-11",
            open: 1001,
            high: 2000,
            low: 900,
            close: 1100,
            volume: 12,
            amount: 123400,
            available_at: "2026-11-12T06:00:00Z",
          },
        ],
        total: 1,
        next_offset: null,
        provider_requests: 0,
        as_of: "2026-11-12T07:00:00Z",
      });
    throw new Error("unexpected test route " + url);
  });
  return { transport, calls };
}
it("queries BJ history and a three-market day locally with an explicit availability cutoff", async () => {
  const { transport, calls } = fixture();
  render(
    <TdxDailyWorkbench
      transport={transport}
      route={route("assets", "local")}
      navigate={vi.fn()}
    />,
  );
  await waitFor(() =>
    expect((screen.getByLabelText("结束日期") as HTMLInputElement).value).toBe(
      "2026-11-11",
    ),
  );
  fireEvent.change(screen.getByLabelText("交易所"), { target: { value: "2" } });
  fireEvent.change(screen.getByLabelText("证券代码"), {
    target: { value: "920002" },
  });
  fireEvent.change(screen.getByLabelText("可用截止（含时区，可留空）"), {
    target: { value: "2026-11-12T07:00:00Z" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查询本地日线" }));
  await screen.findByText("1.001");
  let query = new URL(
    calls.find((c) => c.url.includes("/query?"))!.url,
    "http://localhost",
  ).searchParams;
  expect(query.get("market")).toBe("2");
  expect(query.get("instrument")).toBe("920002");
  expect(query.get("local_only")).toBe("true");
  expect(query.get("as_of")).toBe("2026-11-12T07:00:00Z");
  fireEvent.change(screen.getByLabelText("查询方向"), {
    target: { value: "day" },
  });
  fireEvent.change(screen.getByLabelText("交易所"), {
    target: { value: "all" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查询本地日线" }));
  await waitFor(() =>
    expect(calls.filter((c) => c.url.includes("/query?"))).toHaveLength(2),
  );
  query = new URL(
    calls.filter((c) => c.url.includes("/query?")).at(-1)!.url,
    "http://localhost",
  ).searchParams;
  expect(query.get("day")).toBe("2026-11-11");
  expect(query.has("market")).toBe(false);
  expect(query.has("instrument")).toBe(false);
  expect(calls.every((c) => c.method === "GET")).toBe(true);
});
it("submits on the actual HTTP LAN origin using secure random values and the after-close default", async () => {
  const random = vi.fn(crypto.getRandomValues.bind(crypto));
  vi.stubGlobal("crypto", { getRandomValues: random });
  const { transport, calls } = fixture();
  const navigate = vi.fn();
  render(
    <TdxDailyWorkbench
      transport={transport}
      route={route("collection", "new")}
      navigate={navigate}
    />,
  );
  await waitFor(() =>
    expect((screen.getByLabelText("结束日期") as HTMLInputElement).value).toBe(
      "2026-11-11",
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: /提交.*采集/ }));
  await waitFor(() =>
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1),
  );
  const body = calls.find((c) => c.method === "POST")!.body as {
    request_id: string;
    end: string;
    start: string;
  };
  expect(body.request_id).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  );
  expect(body.end).toBe("2026-11-11");
  expect(body.start).toBe("1990-01-01");
  expect(random).toHaveBeenCalledOnce();
  await waitFor(() =>
    expect(navigate).toHaveBeenCalledWith(
      "collection",
      expect.objectContaining({ job: identity, tab: "executions" }),
    ),
  );
});
it("resumes the existing interrupted identity from execution detail", async () => {
  const { transport, calls } = fixture();
  render(
    <TdxDailyWorkbench
      transport={transport}
      route={route("collection", "executions", identity)}
      navigate={vi.fn()}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "续跑未完成分片" }),
  );
  await waitFor(() =>
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1),
  );
  expect(calls.find((c) => c.method === "POST")).toMatchObject({
    url: "/api/r5/tdx/daily/jobs/resume",
    body: { request_id: identity },
  });
});
