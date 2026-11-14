import "./offlineTestSetup";
import { afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { FINANCIAL, FinancialDailyWorkbench } from "./FinancialDailyWorkbench";
import type { Transport } from "./api";
import { financialResponse } from "./financialQueryTestFixture";
import type { Route } from "./workflowRoute";

afterEach(() => {
  cleanup();
  localStorage.removeItem("mdf.financial.timeseries.pending.v1");
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
      (screen.getByLabelText("采集开始日期") as HTMLInputElement).value,
    ).toBe("2026-10-23"),
  );
  // Initial defaults legitimately clear an older confirmation. Flush that
  // render/effect before expressing this user's new submit intent.
  await act(async () => {});
  expect(
    (screen.getByLabelText("采集结束日期") as HTMLInputElement).value,
  ).toBe("2026-11-05");
  fireEvent.click(
    screen.getByRole("checkbox", { name: "我确认将以上范围采集到开发测试库" }),
  );
  expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(true);
  expect(
    (
      screen.getByRole("button", {
        name: "提交 金融条件日频 采集",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(false);
  fireEvent.click(
    screen.getByRole("button", { name: "提交 金融条件日频 采集" }),
  );
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
      (screen.getByLabelText("采集开始日期") as HTMLInputElement).value,
    ).toBeTruthy(),
  );
  fireEvent.click(
    screen.getByRole("checkbox", { name: "我确认将以上范围采集到开发测试库" }),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "提交 金融条件日频 采集" }),
  );
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "核实原请求" })).toBeNull();
  expect(
    (
      screen.getByRole("button", {
        name: "提交 金融条件日频 采集",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true); // Rejected range requires a fresh explicit confirmation.
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
            job: {
              request_id: (body as { request_id: string }).request_id,
              state: "complete",
              created_at: "2026-11-05T12:00:00Z",
              steps: [],
              provider_requests: 0,
              command: body,
            },
          });
    if (url.endsWith("/status"))
      return Response.json({
        status: "OK",
        defaults: { start: "2006-11-05", end: "2026-11-05" },
        web_capabilities: { query: true, collect: true },
        series: [],
      });
    if (url.endsWith("/jobs")) return Response.json({ status: "OK", jobs: [] });
    if (url.includes("/jobs/"))
      return Response.json({
        status: "OK",
        job: {
          request_id: url.split("/").at(-1),
          state: "complete",
          created_at: "2026-11-05T12:00:00Z",
          steps: [],
          provider_requests: 0,
          command: JSON.parse(
            localStorage.getItem("mdf.financial.timeseries.pending.v1") ?? "{}",
          ).command ?? {
            action: "update",
            start: "2026-10-23",
            end: "2026-11-05",
          },
        },
      });
    if (url.includes("/query?")) return Response.json(financialResponse(url));
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
  fireEvent.change(screen.getByLabelText("采集方式"), {
    target: { value: "init" },
  });
  await waitFor(() =>
    expect(
      (screen.getByLabelText("采集开始日期") as HTMLInputElement).value,
    ).toBe("2006-11-05"),
  );
  expect(calls.every((c) => c.method === "GET")).toBe(true);
  // Date defaults render before their confirmation-reset effect is settled.
  // Express a new user intent only after that initial asynchronous load.
  await act(async () => {});
  fireEvent.click(
    screen.getByRole("checkbox", { name: "我确认将以上范围采集到开发测试库" }),
  );
  expect(
    (
      screen.getByRole("checkbox", {
        name: "我确认将以上范围采集到开发测试库",
      }) as HTMLInputElement
    ).checked,
  ).toBe(true);
  expect(
    (
      screen.getByRole("button", {
        name: "提交 金融条件日频 采集",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(false);
  fireEvent.click(
    screen.getByRole("button", { name: "提交 金融条件日频 采集" }),
  );
  await screen.findByRole("button", { name: "开始另一项采集" });
  expect(navigate).not.toHaveBeenCalled();
  const writes = calls.filter((c) => c.method === "POST");
  expect(writes).toHaveLength(1);
  expect(writes[0].body).toMatchObject({
    action: "init",
    start: "2006-11-05",
    end: "2026-11-05",
  });
});
it("does not repeat an uncertain POST and verifies the original identity by GET", async () => {
  const { transport: baseTransport, calls } = fixture(async () => {
    throw new TypeError("offline");
  });
  let readable = false;
  const transport: Transport = (u, i) =>
    String(u).includes("/jobs/") && !readable
      ? Promise.resolve(
          Response.json(
            {
              status: "UNKNOWN",
              classification: "financial_local_unavailable",
            },
            { status: 503 },
          ),
        )
      : baseTransport(u, i);
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
      (screen.getByLabelText("采集开始日期") as HTMLInputElement).value,
    ).toBeTruthy(),
  );
  fireEvent.click(
    screen.getByRole("checkbox", { name: "我确认将以上范围采集到开发测试库" }),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "提交 金融条件日频 采集" }),
  );
  await screen.findByRole("button", { name: "核实原请求" });
  expect(
    (
      screen.getByRole("button", {
        name: "提交 金融条件日频 采集",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  readable = true;
  fireEvent.click(screen.getByRole("button", { name: "核实原请求" }));
  await screen.findByRole("button", { name: "开始另一项采集" });
  expect(navigate).not.toHaveBeenCalled();
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
it("queries ordinary data then uses the common historical form for local GET", async () => {
  const { transport, calls } = fixture();
  render(
    <FinancialDailyWorkbench
      route={route("assets", "local")}
      navigate={vi.fn()}
      transport={transport}
      generation={0}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  await screen.findByRole("region", { name: "本次原始响应" });
  fireEvent.click(screen.getByRole("checkbox", { name: "历史截止查询" }));
  fireEvent.change(screen.getByLabelText("截止时间（北京时间）"), {
    target: { value: "2026-11-05T22:00:00" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  await waitFor(() =>
    expect(calls.filter((c) => c.url.includes("/query?")).length).toBe(2),
  );
  expect(calls.every((c) => c.method === "GET")).toBe(true);
  expect(calls.filter((c) => c.url.includes("/query?")).at(-1)!.url).toContain(
    "as_of=2026-11-05T14%3A00%3A00.000Z",
  );
});

it("shows the exact failed daily period, reason and recovery without converting a real HTTP failure to a calendar wait", async () => {
  const base = fixture();
  const request_id = "00000000-0000-4000-8000-000000000003";
  const transport: Transport = async (input, init) =>
    String(input).endsWith("/jobs/" + request_id)
      ? Response.json({
          status: "OK",
          job: {
            request_id,
            state: "partial",
            created_at: "2026-11-05T17:00:00Z",
            command: {
              action: "update",
              start: "2026-11-01",
              end: "2026-11-06",
            },
            provider_requests: 1,
            steps: [
              {
                series_id: "DGS2",
                start: "2026-11-01",
                end: "2026-11-06",
                state: "failed",
                classification: "financial_http_failure",
                retry_at: "2026-11-05T18:00:00Z",
              },
            ],
          },
        })
      : base.transport(input, init);
  const r = route("collection", "executions");
  r.fields.set("job", request_id);
  render(
    <FinancialDailyWorkbench
      route={r}
      navigate={vi.fn()}
      transport={transport}
      generation={0}
      embedded
      detailOnly
    />,
  );
  await screen.findByRole("heading", { name: "执行详情" });
  const alert = screen.getByRole("alert");
  expect(alert.textContent).toContain("2026-11-01 至 2026-11-06");
  expect(alert.textContent).toContain("具名来源暂未成功响应");
  expect(alert.textContent).not.toContain("尚未进入");
  expect(screen.getByRole("button", { name: "续跑此作业" })).toBeTruthy();
  expect(base.calls.every((c) => c.method === "GET")).toBe(true);
});
