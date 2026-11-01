import { afterEach, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  cleanup,
  render,
  screen,
  fireEvent,
  within,
  act,
  waitFor,
} from "@testing-library/react";
import App from "./App";
import { asgi, closeBridges } from "./workflowTestBridge";
import { CPI, RETAIL, keyOf } from "./workflowModel";
import { RETAIL_PROFILE as profile } from "./macroTimeSeriesProfile";
import {
  buildCpiQueryRequest,
  queryCpiRange,
  type CpiScope,
  type CpiAvailability,
  type CpiJob,
} from "./cpiTimeSeriesApi";
import type { Transport } from "./api";
import { CollectionWorkbench } from "./CollectionWorkbench";
function decimalWire() {
  const root = resolve(process.cwd(), "..");
  return readFileSync(
    resolve(root, "tests/fixtures/retail_decimal_response.json"),
    "utf8",
  );
}
const decimalScope: CpiScope = {
  start: "2025-10",
  end: "2026-12",
  basket: "all",
  adjustment: "sa",
  measure: "index",
  query_mode: "current",
};
const flush = () => act(async () => {});
const response = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status });
function fixture(partial = false, noContent = false, queryBody?: string) {
  const calls: {
    path: string;
    method: string;
    body?: Record<string, string>;
  }[] = [];
  const jobs: CpiJob[] = [];
  let original = "";
  let loseReceipt = false;
  const transport: Transport = async (path, init) => {
    const url = new URL(String(path), "http://fixture");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ path: String(path), method, body });
    if (url.pathname === "/api/r5/packages")
      return response({
        status: "OK",
        packages: [
          {
            package_id: "macro",
            name: "宏观",
            history_completeness: "COMPLETE",
            capabilities: [{ title: "美国零售销售", definition_ref: RETAIL }],
            assets: [
              {
                identity: RETAIL.identity,
                revisions: [
                  {
                    definition_ref: RETAIL,
                    capability_enabled: true,
                    local_holding: "PRESENT",
                    source_entries: [],
                    metadata_view_permission: "allowed",
                    upstream_read: { allowed: true },
                    local_read: { allowed: true },
                    collect: { allowed: true },
                  },
                ],
              },
            ],
          },
        ],
      });
    if (
      url.pathname === "/api/r5/sources" ||
      url.pathname === "/api/r3/management"
    )
      return response({ status: "OK", entries: [], instances: [] });
    if (!url.pathname.startsWith(profile.basePath))
      return response({ classification: "not_enabled" }, 503);
    if (url.pathname.endsWith("/status"))
      return response({
        latest_months: { headline: "2026-09", control: "2026-09" },
        observation_count: 78,
        revision_count: 0,
        batch_count: 1,
        web_capabilities: { query: true, collect: true, reason: null },
        initialization: {
          preset_id: "retail_240",
          months: 240,
          start: "2006-10",
          end: "2026-09",
          available: true,
          reason: null,
          anchor_basis: "previous_month",
        },
      });
    if (url.pathname.endsWith("/query")) {
      if (queryBody !== undefined) {
        original = queryBody;
        return new Response(original);
      }
      const calc = (value: string | null) => ({ value, reason: null });
      const rates = Object.fromEntries(
        [1, 3, 6, 12].map((h) => [h, calc("0.0123456789012345678901234567")]),
      );
      original =
        " \n" +
        JSON.stringify({
          status: partial ? "PARTIAL" : "OK",
          source_no_content: noContent
            ? [
                {
                  classification: "census_retail_no_content",
                  http_status: 204,
                  response_bytes: 0,
                  start_month: "2026-10",
                  end_month: "2026-10",
                },
              ]
            : [],
          source_failure:
            partial && !noContent
              ? {
                  classification: "census_retail_http_failure",
                  http_status: 429,
                  retry_at: "2026-11-01T00:10:00Z",
                }
              : null,
          start_month: url.searchParams.get("start"),
          end_month: url.searchParams.get("end"),
          persisted: false,
          provider_requests: partial ? 1 : 0,
          provider_attempts: partial ? 1 : 0,
          missing_input_count: partial ? 18 : 0,
          last_completed_month: "2026-10",
          source_skipped_months: partial ? ["2026-11", "2026-12"] : [],
          as_of: url.searchParams.get("as_of"),
          as_of_basis: url.searchParams.get("as_of") ? "available" : "observed",
          query_mode: url.searchParams.get("query_mode"),
          units: {
            levels: "million_current_usd",
            rates: "ratio",
            official_headline_mpcsm: "percent",
          },
          points: (partial
            ? [
                "2025-10",
                "2025-11",
                "2025-12",
                "2026-01",
                "2026-02",
                "2026-03",
                "2026-04",
                "2026-05",
                "2026-06",
                "2026-07",
                "2026-08",
                "2026-09",
                "2026-10",
                "2026-11",
                "2026-12",
              ]
            : ["2026-08", "2026-09"]
          ).map((month) => ({
            month,
            headline_level: calc(
              partial && month > "2026-09" ? null : "123456.123456789",
            ),
            control_level: calc(
              partial && month > "2026-09" ? null : "100001.000000001",
            ),
            headline_rates: rates,
            control_rates: rates,
            official_headline_mpcsm: {
              value: partial && month > "2026-09" ? null : "1.3",
              unit: "percent",
            },
            source_cells: Array.from({ length: 6 }, () => ({
              value: partial && month > "2026-09" ? null : "1",
              status:
                partial && month > "2026-09" ? "not_provided" : "available",
            })),
          })),
          unknown: "retain raw",
        }) +
        "\n";
      return new Response(original);
    }
    if (url.pathname.endsWith("/jobs/resume")) {
      const job = jobs.find((j) => j.request_id === body.request_id)!;
      job.state = "running";
      return response(job, 202);
    }
    if (url.pathname.endsWith("/jobs")) {
      if (method === "POST")
        jobs.push({
          ...body,
          job_id: body.request_id,
          state: "running",
          created_at: "2026-11-01T00:00:00Z",
          started_at: "2026-11-01T00:00:00Z",
          finished_at: null,
          classification: null,
          result: null,
        });
      if (method === "POST" && loseReceipt) {
        loseReceipt = false;
        throw new Error("synthetic_lost_receipt");
      }
      return response(
        method === "POST" ? jobs.at(-1) : { jobs },
        method === "POST" ? 202 : 200,
      );
    }
    if (url.pathname.endsWith("/executions"))
      return response({
        executions: jobs.map((j) => ({ ...j, id: j.job_id, origin: "manual" })),
      });
    return response({ classification: "not_found" }, 404);
  };
  return {
    transport,
    calls,
    jobs,
    raw: () => original,
    loseNext: () => {
      loseReceipt = true;
    },
  };
}
afterEach(() => {
  closeBridges();
  cleanup();
  localStorage.clear();
  sessionStorage.clear();
  location.hash = "";
  vi.restoreAllMocks();
  vi.useRealTimers();
});
it.each(["missing", "disabled", "loading", "failed"])(
  "keeps legacy CPI record refresh independent of %s retail status",
  async (state) => {
    const calls: string[] = [];
    const availability: CpiAvailability | undefined =
      state === "missing"
        ? undefined
        : {
            data: null,
            legacy: false,
            busy: state === "loading",
            failure: state === "failed" ? "read_failed" : null,
          };
    render(
      <CollectionWorkbench
        assets={[]}
        asset={undefined}
        source={undefined}
        route={{
          view: "collection",
          invalid: false,
          fields: new URLSearchParams("tab=executions&execution_asset=all"),
        }}
        navigate={() => {}}
        transport={async (path) => {
          calls.push(String(path));
          return response({
            status: "OK",
            asset_identity: CPI.identity,
            asset_revision: CPI.revision,
            jobs: [],
            plans: [],
          });
        }}
        generation={0}
        invalidIdentity={false}
        retailAvailability={availability}
      />,
    );
    await flush();
    expect(calls).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "回查执行记录" }));
    await flush();
    expect(calls).toHaveLength(4);
    expect(
      calls.every((path) => /^\/api\/r5\/cpi\/(jobs|plans)/.test(path)),
    ).toBe(true);
  },
);
it.each(["loading", "failed", "invalid"])(
  "retains selected retail collection identity while status is %s",
  async (state) => {
    const h = fixture();
    const calls: string[] = [];
    const transport: Transport = async (path, init) => {
      calls.push(String(path));
      if (String(path).startsWith(profile.basePath + "/status")) {
        if (state === "loading") return new Promise<Response>(() => {});
        return state === "failed"
          ? response({ classification: "not_enabled" }, 503)
          : response({});
      }
      return h.transport(path, init);
    };
    location.hash = `ui=r5&view=collection&tab=new&asset=${RETAIL.identity}&asset_revision=${RETAIL.revision}`;
    render(<App transport={transport} />);
    await flush();
    expect((screen.getByLabelText("采集目标") as HTMLSelectElement).value).toBe(
      keyOf(RETAIL),
    );
    if (state === "loading")
      expect(screen.getByText("正在核实 零售销售 采集服务…")).toBeTruthy();
    else {
      const form = within(
        screen.getByRole("region", { name: "零售销售 受控采集" }),
      );
      expect(form.getByRole("alert").textContent).toContain(
        "零售销售 服务状态未知",
      );
      expect(
        (
          form.getByRole("button", {
            name: "提交 零售销售 采集",
          }) as HTMLButtonElement
        ).disabled,
      ).toBe(true);
    }
    expect(calls.some((path) => /\/cpi\//.test(path))).toBe(false);
    expect(calls.some((path) => path.endsWith("/executions"))).toBe(false);
  },
);
it("automatically refreshes confirmed retail records without the legacy refresh button", async () => {
  vi.useFakeTimers();
  const h = fixture();
  location.hash = `ui=r5&view=collection&tab=executions&execution_asset=${encodeURIComponent(keyOf(RETAIL))}`;
  render(<App transport={h.transport} />);
  await flush();
  const reads = () =>
    h.calls.filter((call) => call.path === profile.basePath + "/executions");
  expect(reads()).toHaveLength(1);
  expect(screen.queryByRole("button", { name: "回查执行记录" })).toBeNull();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(reads()).toHaveLength(2);
  expect(h.calls.every((call) => call.method === "GET")).toBe(true);
});
it("selects retail in the real shared App, preserves amount/raw precision, actual URL and descending months", async () => {
  const h = fixture();
  location.hash = "ui=r5&view=assets";
  render(<App transport={h.transport} />);
  fireEvent.click(await screen.findByRole("row", { name: /美国零售销售/ }));
  const query = within(
    await screen.findByRole("region", { name: "零售销售 范围查询" }),
  );
  fireEvent.change(query.getByLabelText("开始月份"), {
    target: { value: "2026-08" },
  });
  fireEvent.click(query.getByRole("button", { name: "查询数据" }));
  await flush();
  const table = query.getByRole("table", { name: "零售销售 范围数据" });
  expect(
    within(table)
      .getAllByRole("row")
      .slice(1)
      .map((r) => r.textContent?.slice(0, 7)),
  ).toEqual(["2026-09", "2026-09", "2026-08", "2026-08"]);
  expect(query.getAllByText("123456.123456789").length).toBe(2);
  expect(query.getByText("金额（百万当期美元）")).toBeTruthy();
  expect(query.getByText("官方 MPCSM（percent）")).toBeTruthy();
  expect(query.queryByRole("alert")).toBeNull();
  fireEvent.click(query.getByRole("tab", { name: "原始响应" }));
  expect(query.getByLabelText("原始 HTTP 正文").textContent).toBe(h.raw());
  const actual = h.calls.find((c) => c.path.includes("/query?"))!.path;
  expect(query.getByLabelText("本次请求 URL").textContent).toContain(actual);
  expect(new URL(actual, "http://fixture").searchParams.has("measure")).toBe(
    false,
  );
  fireEvent.click(screen.getByRole("button", { name: "展开窗体" }));
  expect(screen.getByRole("button", { name: "收起窗体" })).toBeTruthy();
  fireEvent.click(query.getByRole("tab", { name: "API说明" }));
  expect(query.getByText("GET /api/r5/retail/timeseries/query")).toBeTruthy();
  expect(
    h.calls.some(
      (c) =>
        /\/cpi\/(timeseries\/)?(query|jobs)/.test(c.path) ||
        c.path.includes("retail-2026-09"),
    ),
  ).toBe(false);
});
it("uses shared new collection, editable 240-month preset, single submission and selected execution detail", async () => {
  const h = fixture();
  location.hash = `ui=r5&view=collection&tab=new&asset=${RETAIL.identity}&asset_revision=${RETAIL.revision}`;
  render(<App transport={h.transport} />);
  const form = within(
    await screen.findByRole("region", { name: "零售销售 受控采集" }),
  );
  fireEvent.change(form.getByLabelText("采集方式"), {
    target: { value: "initialize" },
  });
  await flush();
  expect((form.getByLabelText("采集开始月份") as HTMLInputElement).value).toBe(
    "2006-10",
  );
  fireEvent.change(form.getByLabelText("采集开始月份"), {
    target: { value: "2026-08" },
  });
  fireEvent.click(form.getByRole("checkbox"));
  const submit = form.getByRole("button", { name: "提交 零售销售 采集" });
  fireEvent.click(submit);
  fireEvent.click(submit);
  await flush();
  expect(h.calls.filter((c) => c.method === "POST")).toHaveLength(1);
  expect(h.jobs[0].start).toBe("2026-08");
  expect(h.jobs[0].job_id).toBe(h.jobs[0].request_id);
  fireEvent.click(
    form.getByRole("button", { name: /美国 零售销售 · 初始化采集/ }),
  );
  await screen.findByRole("heading", { name: "采集执行记录" });
  expect(location.hash).toContain(`job=${h.jobs[0].job_id}`);
  expect(location.hash).toContain(encodeURIComponent(keyOf(RETAIL)));
  expect(
    screen.queryByRole("region", { name: "零售销售 受控采集" }),
  ).toBeNull();
  expect(await screen.findByText(h.jobs[0].request_id)).toBeTruthy();
});
it("builds retail history with encoded Beijing cutoff and no price-index parameters", () => {
  const request = buildCpiQueryRequest(
    {
      start: "2026-08",
      end: "2026-09",
      basket: "all",
      adjustment: "sa",
      measure: "index",
      query_mode: "history",
      as_of: "2026-11-01T08:00:00+08:00",
    },
    profile,
  );
  expect(request.path).toContain("%2B08%3A00");
  const p = new URL(request.path, "http://fixture").searchParams;
  expect(p.get("local_only")).toBe("true");
  expect(p.has("basket")).toBe(false);
  expect(p.has("measure")).toBe(false);
});

it.each(["matching", "wrong_source", "wrong_binding"])(
  "retail real catalog exact operation %s guards the shared form",
  async (variant) => {
    const bridge = asgi();
    const catalog = await bridge.exchange({
      method: "GET",
      path: "/api/r5/sources",
    });
    const entry = catalog.body.entries.find(
      (e: { asset?: { identity: string } }) =>
        e.asset?.identity === RETAIL.identity,
    );
    expect(entry).toBeTruthy();
    if (variant === "wrong_source") entry.source_contract.revision = "other";
    if (variant === "wrong_binding")
      entry.binding_contract.source.revision = "other";
    const h = fixture();
    location.hash = `ui=r5&view=collection&tab=new&asset=${RETAIL.identity}&asset_revision=${RETAIL.revision}&operation=${entry.identity}`;
    render(
      <App
        transport={async (input, init) =>
          new URL(String(input), "http://fixture").pathname ===
          "/api/r5/sources"
            ? response({ ...catalog.body, entries: [entry] })
            : h.transport(input, init)
        }
      />,
    );
    if (variant === "matching")
      await screen.findByRole("region", { name: "零售销售 受控采集" });
    else {
      await screen.findByLabelText("采集目标");
      await flush();
      expect(
        screen.queryByRole("region", { name: "零售销售 受控采集" }),
      ).toBeNull();
      expect(h.calls.some((c) => c.path.startsWith(profile.basePath))).toBe(
        false,
      );
    }
  },
);

it("retains a lost submission across remount and explicitly resumes the same durable retail id", async () => {
  const h = fixture();
  location.hash = `ui=r5&view=collection&tab=new&asset=${RETAIL.identity}&asset_revision=${RETAIL.revision}`;
  const view = render(<App transport={h.transport} />);
  let form = within(
    await screen.findByRole("region", { name: "零售销售 受控采集" }),
  );
  h.loseNext();
  fireEvent.click(form.getByRole("checkbox"));
  fireEvent.click(form.getByRole("button", { name: "提交 零售销售 采集" }));
  await flush();
  expect(h.jobs).toHaveLength(1);
  const id = h.jobs[0].request_id;
  expect(localStorage.getItem("mdf.retail.timeseries.pending.v1")).toContain(
    id,
  );
  view.unmount();
  h.jobs[0].state = "unknown";
  render(<App transport={h.transport} />);
  form = within(
    await screen.findByRole("region", { name: "零售销售 受控采集" }),
  );
  const resume = await form.findByRole("button", { name: /按原请求恢复采集/ });
  expect((resume as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(form.getByRole("checkbox"));
  fireEvent.click(resume);
  await flush();
  const posts = h.calls.filter((c) => c.method === "POST");
  expect(posts).toHaveLength(2);
  expect(posts[1].path).toBe(profile.basePath + "/jobs/resume");
  expect(posts[1].body?.request_id).toBe(id);
  expect(h.jobs).toHaveLength(1);
});

it("uses the real shared App retail periodic row with start/pause and truthful details", async () => {
  const h = fixture();
  let enabled = false;
  const strategyCalls: string[] = [];
  const transport: Transport = async (input, init) => {
    const path = String(input);
    if (path === `${profile.basePath}/strategy`) {
      strategyCalls.push(init?.method ?? "GET");
      if (init?.method === "PUT")
        enabled = JSON.parse(String(init.body)).enabled;
      return response({
        exists: strategyCalls.includes("PUT"),
        enabled,
        history_start: "1992-01",
        next_due: null,
        next_release_at: "2026-11-17T13:30:00Z",
        execution_state: enabled ? "idle" : "paused",
        worker: { state: "paused", last_heartbeat_at: "2026-11-01T03:00:00Z" },
        policy: {},
        last_result: null,
      });
    }
    if (path === `${profile.basePath}/schedule`)
      return response({
        calendar_status: "fresh",
        calendar: {
          acquired_at: "2026-11-01T02:00:00Z",
          error: "census_retail_calendar_failure",
        },
        targets: [
          {
            reference_month: "2026-10",
            release_at: "2026-11-17T13:30:00Z",
            target: {
              state: "awaiting_provider",
              classification: "target_month_not_returned",
              next_attempt_at: "2026-11-17T13:31:00Z",
            },
            history: { state: "blocked" },
          },
        ],
      });
    return h.transport(input, init);
  };
  location.hash = `ui=r5&view=collection&tab=periodic&periodic_asset=${keyOf(RETAIL)}`;
  render(<App transport={transport} />);
  const row = await screen.findByRole("row", {
    name: /官方发布自动采集.*美国 零售销售/,
  });
  await waitFor(() =>
    expect(
      within(row)
        .getByRole("button", { name: "启动" })
        .hasAttribute("disabled"),
    ).toBe(false),
  );
  expect(strategyCalls.every((m) => m === "GET")).toBe(true);
  fireEvent.click(row);
  expect(await screen.findByText("零售销售 策略详情")).toBeTruthy();
  expect(screen.getByText("2026-11-17 21:30:00 北京时间")).toBeTruthy();
  expect(screen.getByText("target_month_not_returned")).toBeTruthy();
  expect(screen.getByText(/继续使用有效缓存/)).toBeTruthy();
  fireEvent.click(within(row).getByRole("button", { name: "启动" }));
  await screen.findByRole("button", { name: "暂停" });
  fireEvent.click(screen.getByRole("button", { name: "暂停" }));
  await screen.findByRole("button", { name: "启动" });
  expect(strategyCalls.filter((m) => m === "PUT")).toHaveLength(2);
  expect(h.calls.some((c) => /\/cpi\/timeseries/.test(c.path))).toBe(false);
});

it.each(["loading", "failed"])(
  "keeps retail periodic identity when availability is %s",
  async (state) => {
    const h = fixture();
    const transport: Transport = async (input, init) => {
      if (String(input) === `${profile.basePath}/status`) {
        if (state === "loading") return new Promise<Response>(() => {});
        return response({ classification: "retail_not_bound" }, 503);
      }
      return h.transport(input, init);
    };
    location.hash = `ui=r5&view=collection&tab=periodic&periodic_asset=${keyOf(RETAIL)}`;
    render(<App transport={transport} />);
    const row = await screen.findByRole("row", {
      name: /官方发布自动采集.*美国 零售销售/,
    });
    expect(
      within(row)
        .getByRole("button", { name: "启动" })
        .hasAttribute("disabled"),
    ).toBe(true);
    fireEvent.click(row);
    expect(
      await screen.findByText(
        state === "loading"
          ? "零售销售 策略正在读取…"
          : "零售销售 策略读取失败，等待重读。",
      ),
    ).toBeTruthy();
    expect(h.calls.every((c) => c.method === "GET")).toBe(true);
    expect(h.calls.some((c) => /\/cpi\/timeseries/.test(c.path))).toBe(false);
  },
);

it("shows retail partial warning with the retained shared App table and exact raw body", async () => {
  const h = fixture(true);
  location.hash = "ui=r5&view=assets";
  render(<App transport={h.transport} />);
  fireEvent.click(await screen.findByRole("row", { name: /美国零售销售/ }));
  const query = within(
    await screen.findByRole("region", { name: "零售销售 范围查询" }),
  );
  fireEvent.change(query.getByLabelText("开始月份"), {
    target: { value: "2025-10" },
  });
  fireEvent.change(query.getByLabelText("结束月份"), {
    target: { value: "2026-12" },
  });
  fireEvent.click(query.getByRole("button", { name: "查询数据" }));
  await flush();
  expect(query.getByRole("alert").textContent).toContain("Census 补取失败");
  expect(query.getByRole("alert").textContent).toContain("部分返回");
  expect(query.getByRole("alert").textContent).toContain("HTTP 429");
  expect(query.getByRole("alert").textContent).toContain("可重试时间");
  expect(query.getByRole("table", { name: "零售销售 范围数据" })).toBeTruthy();
  expect(query.getAllByText("123456.123456789")).toHaveLength(12);
  const rows = within(query.getByRole("table", { name: "零售销售 范围数据" }))
    .getAllByRole("row")
    .slice(1);
  expect(rows).toHaveLength(30);
  expect(rows.slice(0, 6).map((r) => r.textContent?.slice(0, 7))).toEqual([
    "2026-12",
    "2026-12",
    "2026-11",
    "2026-11",
    "2026-10",
    "2026-10",
  ]);
  expect(query.queryByText(/所选指标数据完整/)).toBeNull();
  fireEvent.click(query.getByRole("tab", { name: "原始响应" }));
  expect(query.getByLabelText("原始 HTTP 正文").textContent).toBe(h.raw());
  expect(h.raw()).toContain('"status":"PARTIAL"');
  expect(h.raw()).toContain('"source_failure"');
});

it("shows 204 no returned records in the shared App without calling it a service failure", async () => {
  const h = fixture(true, true);
  location.hash = "ui=r5&view=assets";
  render(<App transport={h.transport} />);
  fireEvent.click(await screen.findByRole("row", { name: /美国零售销售/ }));
  const query = within(
    await screen.findByRole("region", { name: "零售销售 范围查询" }),
  );
  fireEvent.change(query.getByLabelText("开始月份"), {
    target: { value: "2025-10" },
  });
  fireEvent.change(query.getByLabelText("结束月份"), {
    target: { value: "2026-12" },
  });
  fireEvent.click(query.getByRole("button", { name: "查询数据" }));
  await flush();
  expect(query.getByRole("alert").textContent).toContain(
    "Census 本次未返回记录（HTTP 204）",
  );
  expect(query.queryByText(/补取失败/)).toBeNull();
  expect(query.getAllByText("123456.123456789")).toHaveLength(12);
  expect(
    within(
      query.getByRole("table", { name: "零售销售 范围数据" }),
    ).getAllByRole("row"),
  ).toHaveLength(31);
  fireEvent.click(query.getByRole("tab", { name: "原始响应" }));
  expect(query.getByLabelText("原始 HTTP 正文").textContent).toBe(h.raw());
  expect(h.raw()).toContain('"source_failure":null');
  expect(h.raw()).toContain('"http_status":204');
});

it("validates backend-serialized Decimal values through the complete retail query", async () => {
  const raw = decimalWire();
  const observer = vi.fn();
  const h = fixture(false, false, raw);
  const result = await queryCpiRange(
    h.transport,
    decimalScope,
    new AbortController().signal,
    observer,
    profile,
  );
  expect(result.points).toHaveLength(24);
  expect(result.missing).toHaveLength(6);
  expect(result.complete).toBe(false);
  expect(result.coverage_complete).toBe(false);
  expect(result.points[0].official_mpcsm_percent).toBe("0E-9");
  expect(result.points[0].change_1m_ratio).toBe("0E-9");
  expect(result.points[1].index_value).toBe("1.00000000000000000001E+30");
  expect(result.points[2].change_1m_ratio).toBe("1.25E-7");
  expect(result).toHaveProperty("backend_extension", {
    exact: "12345678901234567890.123456789",
  });
  expect(observer.mock.calls[0][0].bodyText).toBe(raw);
  expect(observer.mock.calls[0][0].url).toBe(
    new URL(h.calls[0].path, location.origin).href,
  );
});

it.each(["NaN", "Infinity", "-Infinity", "1e", "1e2junk", "", " 1", "1\n"])(
  "rejects invalid Decimal %j in every retail numeric field",
  async (invalid) => {
    for (const field of [
      "headline_level",
      "control_level",
      "official_headline_mpcsm",
      "headline_rates",
      "control_rates",
    ]) {
      const body = JSON.parse(decimalWire());
      const target = body.points[0][field];
      if (field.endsWith("rates")) target["1"].value = invalid;
      else target.value = invalid;
      await expect(
        queryCpiRange(
          async () => response(body),
          decimalScope,
          new AbortController().signal,
          undefined,
          profile,
        ),
      ).rejects.toThrow(/retail_(month|calculation)_unknown/);
    }
  },
);

it.each(["range", "units", "duplicate", "incomplete", "attempts"])(
  "retains retail %s contract checks with Decimal exponents",
  async (invalid) => {
    const body = JSON.parse(decimalWire());
    if (invalid === "range") body.start_month = "2025-09";
    if (invalid === "units") body.units.rates = "percent";
    if (invalid === "duplicate") body.points[0] = body.points[1];
    if (invalid === "incomplete") body.points.pop();
    if (invalid === "attempts") body.provider_attempts = -1;
    await expect(
      queryCpiRange(
        async () => response(body),
        decimalScope,
        new AbortController().signal,
        undefined,
        profile,
      ),
    ).rejects.toThrow();
  },
);

it("renders backend Decimal zero and exponents in the shared App with exact raw HTTP", async () => {
  const raw = decimalWire();
  const h = fixture(false, false, raw);
  location.hash = "ui=r5&view=assets";
  render(<App transport={h.transport} />);
  fireEvent.click(await screen.findByRole("row", { name: /美国零售销售/ }));
  const query = within(
    await screen.findByRole("region", { name: "零售销售 范围查询" }),
  );
  fireEvent.change(query.getByLabelText("开始月份"), {
    target: { value: decimalScope.start },
  });
  fireEvent.change(query.getByLabelText("结束月份"), {
    target: { value: decimalScope.end },
  });
  fireEvent.click(query.getByRole("button", { name: "查询数据" }));
  await flush();
  const rows = within(query.getByRole("table", { name: "零售销售 范围数据" }))
    .getAllByRole("row")
    .slice(1);
  expect(rows).toHaveLength(30);
  const months = rows.map(
    (row) => within(row).getAllByRole("cell")[0].textContent,
  );
  expect(months).toEqual([...months].sort().reverse());
  expect(query.getAllByText("123456.123456789")).toHaveLength(12);
  expect(query.getAllByText("1.00000000000000000001E+30")).toHaveLength(12);
  expect(
    rows.slice(0, 6).every((row) => row.textContent?.includes("金额输入缺值")),
  ).toBe(true);
  const zero = within(
    rows.find(
      (row) =>
        row.textContent?.startsWith("2025-10") &&
        row.textContent.includes("总体"),
    )!,
  );
  expect(zero.getByText("0%")).toBeTruthy();
  expect(zero.getByText("0.00%")).toBeTruthy();
  expect(zero.getAllByText("1.23%")).toHaveLength(3);
  expect(query.getAllByText("1.25E-7%")).toHaveLength(11);
  expect(query.queryByText(/结果未知/)).toBeNull();
  fireEvent.click(query.getByRole("tab", { name: "原始响应" }));
  expect(query.getByLabelText("原始 HTTP 正文").textContent).toBe(raw);
  expect(query.getByLabelText("本次请求 URL").textContent).toContain(
    h.calls.find((c) => c.path.includes("/query?"))!.path,
  );
  expect(raw).toContain('"exact":"12345678901234567890.123456789"');
});
