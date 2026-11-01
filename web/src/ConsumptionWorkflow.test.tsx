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
} from "@testing-library/react";
import App from "./App";
import {
  CONSUMPTION,
  CONSUMPTION_OPERATION,
  consumptionOperationMatches,
  keyOf,
} from "./workflowModel";
import { CONSUMPTION_PROFILE as profile } from "./macroTimeSeriesProfile";
import {
  buildCpiQueryRequest,
  queryCpiRange,
  type CpiScope,
  type CpiAvailability,
  type CpiJob,
} from "./cpiTimeSeriesApi";
import type { Transport } from "./api";
const load = (name: string) =>
  readFileSync(resolve(process.cwd(), "../tests/fixtures", name), "utf8");
const wire = load("consumption_http_response.json");
const status = JSON.parse(load("consumption_status_response.json"));
const scope: CpiScope = {
  start: "2026-08",
  end: "2026-09",
  basket: "all",
  adjustment: "sa",
  measure: "native",
  query_mode: "current",
  series: profile.series.map((s) => s[0]).join(","),
};
const availability: CpiAvailability = {
  data: status,
  busy: false,
  failure: null,
  legacy: false,
};
const flush = () => act(async () => {});
const response = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status });
function fixture() {
  const calls: { path: string; method: string; body?: any }[] = [];
  const jobs: CpiJob[] = [];
  let enabled = false;
  const transport: Transport = async (input, init) => {
    const path = String(input),
      url = new URL(path, "http://fixture"),
      method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ path, method, body });
    if (url.pathname === "/api/r5/packages")
      return response({
        status: "OK",
        packages: [
          {
            package_id: "macro",
            name: "宏观",
            history_completeness: "UNKNOWN",
            capabilities: [
              { title: "美国消费与收入", definition_ref: CONSUMPTION },
            ],
            assets: [
              {
                identity: CONSUMPTION.identity,
                revisions: [
                  {
                    definition_ref: CONSUMPTION,
                    capability_enabled: true,
                    local_holding: "PRESENT",
                    source_entries: [],
                    metadata_view_permission: "allowed",
                    local_read: { allowed: true },
                    upstream_read: { allowed: true },
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
    if (url.pathname.endsWith("/status")) return response(status);
    if (url.pathname.endsWith("/query"))
      return new Response(wire, {
        headers: { "Content-Type": "application/json" },
      });
    if (url.pathname.endsWith("/jobs")) {
      if (method === "POST")
        jobs.push({
          ...body,
          job_id: body.request_id,
          state: "complete",
          created_at: "2026-11-01T12:00:00Z",
          started_at: "2026-11-01T12:00:00Z",
          finished_at: "2026-11-01T12:00:01Z",
          classification: null,
          result: {
            provider_requests: 2,
            provider_attempts: 3,
            saved_points: 7,
            coverage_complete: true,
            readback: {
              verified: true,
              point_count: 7,
              expected_points: 7,
              missing: [],
              source_missing: [],
              source_unavailable_count: 0,
            },
          },
        });
      return response(
        method === "POST" ? jobs.at(-1) : { jobs },
        method === "POST" ? 202 : 200,
      );
    }
    if (url.pathname.endsWith("/executions"))
      return response({
        executions: jobs.map((j) => ({ ...j, id: j.job_id, origin: "manual" })),
      });
    if (url.pathname.endsWith("/strategy")) {
      if (method === "PUT") enabled = body.enabled;
      return response({
        exists: true,
        enabled,
        history_start: "2006-10",
        worker: { state: "not_started", last_heartbeat_at: null },
        policy: { provider: "BEA" },
        next_due: null,
      });
    }
    if (url.pathname.endsWith("/schedule"))
      return response({
        calendar_status: "not_acquired",
        calendar: {
          source: "https://www.bea.gov/news/schedule/full",
          events: {},
        },
        targets: [],
        worker: { state: "not_started" },
      });
    return response({ classification: "not_found" }, 404);
  };
  return { transport, calls, jobs };
}
afterEach(() => {
  cleanup();
  location.hash = "";
  localStorage.clear();
  sessionStorage.clear();
  vi.useRealTimers();
});
it("parses the exact production Python router body, preserves native values and explicit read policy", async () => {
  const h = fixture();
  const result = await queryCpiRange(
    h.transport,
    scope,
    new AbortController().signal,
    undefined,
    profile,
  );
  expect(
    result.points.find((p) => p.series_id === "DPCERC" && p.month === "2026-09")
      ?.value,
  ).toBe("22452780");
  expect(
    result.points.find((p) => p.series_id === "A072RC" && p.month === "2026-09")
      ?.change_1m_percentage_points,
  ).toBe("3.8");
  const current = new URL(
    buildCpiQueryRequest(scope, profile).path,
    "http://fixture",
  ).searchParams;
  expect(current.get("measure")).toBe("native");
  expect(current.get("local_only")).toBe("false");
  expect(current.has("basket")).toBe(false);
  const history = new URL(
    buildCpiQueryRequest(
      { ...scope, query_mode: "history", as_of: "2026-11-01T20:00:00+08:00" },
      profile,
    ).path,
    "http://fixture",
  ).searchParams;
  expect(history.get("local_only")).toBe("true");
  expect(history.get("as_of_basis")).toBe("available");
});
it("renders native amounts, zero saving rate and point changes in shared App with descending months and exact raw bytes", async () => {
  const h = fixture();
  location.hash = `ui=r5&view=assets&asset=${CONSUMPTION.identity}&asset_revision=r1`;
  render(<App transport={h.transport} />);
  const query = within(
    await screen.findByRole("region", { name: "美国消费与收入 范围查询" }),
  );
  fireEvent.change(query.getByLabelText("开始月份"), {
    target: { value: "2026-08" },
  });
  fireEvent.click(query.getByRole("button", { name: "查询数据" }));
  await flush();
  const table = query.getByRole("table", { name: "美国消费与收入 范围数据" });
  const rows = within(table).getAllByRole("row").slice(1);
  expect(rows.map((r) => r.textContent?.slice(0, 7))).toEqual([
    ...Array(7).fill("2026-09"),
    ...Array(7).fill("2026-08"),
  ]);
  expect(within(table).getByText("22,452,780")).toBeTruthy();
  const saving = rows.find(
    (r) =>
      r.textContent?.includes("2026-08") &&
      r.textContent?.includes("个人储蓄率"),
  )!;
  expect(within(saving).getByText("0%")).toBeTruthy();
  expect(query.queryByRole("alert")).toBeNull();
  expect(query.getByRole("button", { name: /消费与收入指标/ })).toBeTruthy();
  fireEvent.click(query.getByRole("tab", { name: "原始响应" }));
  expect(query.getByLabelText("原始 HTTP 正文").textContent).toBe(wire);
  expect(query.getByLabelText("本次请求 URL").textContent).toContain(
    "measure=native",
  );
  fireEvent.click(query.getByRole("tab", { name: "API说明" }));
  expect(query.getByText(`GET ${profile.basePath}/query`)).toBeTruthy();
  expect(
    query.getByRole("table", { name: "统计序列" }).querySelectorAll("tbody tr"),
  ).toHaveLength(7);
  expect(h.calls.some((c) => /\/pce\/.*(query|jobs)/.test(c.path))).toBe(false);
});
it("uses editable 240 month collection and follows the selected summary to its execution row", async () => {
  const h = fixture();
  location.hash = `ui=r5&view=collection&tab=new&asset=${CONSUMPTION.identity}&asset_revision=r1`;
  render(<App transport={h.transport} />);
  const form = within(
    await screen.findByRole("region", { name: "美国消费与收入 受控采集" }),
  );
  fireEvent.change(form.getByLabelText("采集方式"), {
    target: { value: "initialize" },
  });
  await flush();
  expect((form.getByLabelText("采集开始月份") as HTMLInputElement).value).toBe(
    "2006-10",
  );
  fireEvent.change(form.getByLabelText("采集开始月份"), {
    target: { value: "2026-09" },
  });
  fireEvent.click(form.getByRole("checkbox"));
  fireEvent.click(
    form.getByRole("button", { name: "提交 美国消费与收入 采集" }),
  );
  await flush();
  expect(h.jobs).toHaveLength(1);
  expect(h.jobs[0].start).toBe("2026-09");
  fireEvent.click(
    form.getByRole("button", { name: /美国消费与收入 · 初始化采集/ }),
  );
  await screen.findByRole("heading", { name: "采集执行记录" });
  expect(location.hash).toContain(`job=${h.jobs[0].job_id}`);
  expect(location.hash).toContain(encodeURIComponent(keyOf(CONSUMPTION)));
  expect(await screen.findByText(h.jobs[0].job_id)).toBeTruthy();
});

it.each(["matching", "wrong_source", "wrong_binding"])(
  "guards consumption real catalog binding %s",
  async (variant) => {
    const h = fixture();
    const catalog = JSON.parse(load("consumption_directory_response.json"));
    const entry = catalog.entries.find(
      (e: any) => e.identity === CONSUMPTION_OPERATION,
    );
    expect(consumptionOperationMatches(entry)).toBe(true);
    if (variant === "wrong_source") entry.source_contract.revision = "other";
    if (variant === "wrong_binding")
      entry.binding_contract.source.revision = "other";
    expect(consumptionOperationMatches(entry)).toBe(variant === "matching");
    location.hash = `ui=r5&view=collection&tab=new&asset=${CONSUMPTION.identity}&asset_revision=r1&operation=${encodeURIComponent(CONSUMPTION_OPERATION)}`;
    render(
      <App
        transport={async (input, init) =>
          String(input).split("?")[0] === "/api/r5/sources"
            ? response(catalog)
            : h.transport(input, init)
        }
      />,
    );
    await flush();
    if (variant === "matching")
      expect(
        await screen.findByRole("region", { name: "美国消费与收入 受控采集" }),
      ).toBeTruthy();
    else
      expect(
        screen.queryByRole("region", { name: "美国消费与收入 受控采集" }),
      ).toBeNull();
    expect(h.jobs).toHaveLength(0);
  },
);
it("validates the production router's consumption display-column projection", async () => {
  const projected = load("consumption_projected_http_response.json");
  const result = await queryCpiRange(
    async () => new Response(projected),
    { ...scope, columns: "value,change_1m_percentage_points" },
    new AbortController().signal,
    undefined,
    profile,
  );
  expect(
    result.points.find((p) => p.series_id === "A072RC" && p.month === "2026-09")
      ?.change_1m_percentage_points,
  ).toBe("3.8");
  expect(result.points.every((p) => !("change_1m_ratio" in p))).toBe(true);
});
