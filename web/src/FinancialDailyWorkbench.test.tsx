import "./offlineTestSetup";
import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { FINANCIAL, FinancialDailyWorkbench } from "./FinancialDailyWorkbench";
import type { Transport } from "./api";
import type { Route } from "./workflowRoute";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
it("submits from HTTP LAN using the existing secure-random fallback", async () => {
  const random = vi.fn(crypto.getRandomValues.bind(crypto));
  vi.stubGlobal("crypto", { getRandomValues: random });
  const { transport, calls } = fixture();
  render(
    <FinancialDailyWorkbench
      route={route("collection", "new")}
      navigate={vi.fn()}
      transport={transport}
      generation={0}
    />,
  );
  await waitFor(() =>
    expect(
      (screen.getByLabelText("起始观测日") as HTMLInputElement).value,
    ).toBeTruthy(),
  );
  fireEvent.click(screen.getByRole("button", { name: "提交日频作业" }));
  await waitFor(() =>
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1),
  );
  expect(random).toHaveBeenCalledOnce();
  expect(
    (calls.find((c) => c.method === "POST")!.body as { request_id: string })
      .request_id,
  ).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  );
});
it("keeps an explicit admission rejection distinct from an unknown network result", async () => {
  const { transport } = fixture(async () =>
    Response.json(
      { status: "UNKNOWN", classification: "financial_job_range_invalid" },
      { status: 400 },
    ),
  );
  render(
    <FinancialDailyWorkbench
      route={route("collection", "new")}
      navigate={vi.fn()}
      transport={transport}
      generation={0}
    />,
  );
  await waitFor(() =>
    expect(
      (screen.getByLabelText("起始观测日") as HTMLInputElement).value,
    ).toBeTruthy(),
  );
  fireEvent.click(screen.getByRole("button", { name: "提交日频作业" }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "核实原请求" })).toBeNull();
  expect(
    (screen.getByRole("button", { name: "提交日频作业" }) as HTMLButtonElement)
      .disabled,
  ).toBe(false);
});
const route = (view: "assets" | "collection", tab: string): Route => ({
  view,
  invalid: false,
  fields: new URLSearchParams({
    asset: FINANCIAL.identity,
    asset_revision: "r1",
    tab,
  }),
});
function fixture(post?: (body: unknown) => Promise<Response>) {
  const calls: { url: string; method: string; body?: unknown }[] = [];
  const transport: Transport = vi.fn(async (input, init) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, method, body });
    if (method === "POST")
      return post
        ? post(body)
        : Response.json({
            status: "OK",
            job: { request_id: (body as { request_id: string }).request_id },
          });
    if (url.endsWith("/status"))
      return Response.json({
        status: "OK",
        defaults: { start: "2006-11-05", end: "2026-11-05" },
        series: [],
      });
    if (url.endsWith("/jobs")) return Response.json({ status: "OK", jobs: [] });
    if (url.includes("/jobs/"))
      return Response.json({
        status: "OK",
        job: { request_id: url.split("/").at(-1) },
      });
    return Response.json({
      status: "OK",
      rows: [],
      as_of: "2026-11-05T14:00:00+00:00",
      total: 0,
      next_offset: null,
      provider_requests: 0,
      coverage: { series: [] },
    });
  });
  return { transport, calls };
}
it("reads defaults without writes and submits exactly one editable twenty-year job", async () => {
  const { transport, calls } = fixture();
  const navigate = vi.fn();
  render(
    <FinancialDailyWorkbench
      route={route("collection", "new")}
      navigate={navigate}
      transport={transport}
      generation={0}
    />,
  );
  await waitFor(() =>
    expect(
      (screen.getByLabelText("起始观测日") as HTMLInputElement).value,
    ).toBe("2006-11-05"),
  );
  expect(calls.every((c) => c.method === "GET")).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "提交日频作业" }));
  await waitFor(() => expect(navigate).toHaveBeenCalled());
  const writes = calls.filter((c) => c.method === "POST");
  expect(writes).toHaveLength(1);
  expect(writes[0].body).toMatchObject({
    action: "init",
    start: "2006-11-05",
    end: "2026-11-05",
  });
});
it("does not repeat an uncertain POST and verifies the original identity by GET", async () => {
  const { transport, calls } = fixture(async () => {
    throw new TypeError("offline");
  });
  const navigate = vi.fn();
  render(
    <FinancialDailyWorkbench
      route={route("collection", "new")}
      navigate={navigate}
      transport={transport}
      generation={0}
    />,
  );
  await waitFor(() =>
    expect(
      (screen.getByLabelText("起始观测日") as HTMLInputElement).value,
    ).toBeTruthy(),
  );
  fireEvent.click(screen.getByRole("button", { name: "提交日频作业" }));
  await screen.findByRole("button", { name: "核实原请求" });
  expect(
    (screen.getByRole("button", { name: "提交日频作业" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "核实原请求" }));
  await waitFor(() => expect(navigate).toHaveBeenCalled());
  expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
  const requestId = (
    calls.find((c) => c.method === "POST")!.body as { request_id: string }
  ).request_id;
  expect(
    calls.some(
      (c) => c.method === "GET" && c.url.endsWith("/jobs/" + requestId),
    ),
  ).toBe(true);
});
it("query and refresh use only local GET with a fixed cutoff", async () => {
  const { transport, calls } = fixture();
  render(
    <FinancialDailyWorkbench
      route={route("assets", "local")}
      navigate={vi.fn()}
      transport={transport}
      generation={0}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "查询本地数据" }));
  await screen.findByRole("button", { name: "固定此截止" });
  fireEvent.click(screen.getByRole("button", { name: "固定此截止" }));
  fireEvent.click(screen.getByRole("button", { name: "刷新本地结果" }));
  await waitFor(() =>
    expect(calls.filter((c) => c.url.includes("/query?")).length).toBe(2),
  );
  expect(calls.every((c) => c.method === "GET")).toBe(true);
  expect(calls.filter((c) => c.url.includes("/query?")).at(-1)!.url).toContain(
    "as_of=2026-11-05T14%3A00%3A00.000Z",
  );
});
