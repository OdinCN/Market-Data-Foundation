import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
  act,
} from "@testing-library/react";
import App from "./App";
import { CpiTimeSeries } from "./CpiTimeSeries";
import { CpiTimeSeriesCollection } from "./CpiTimeSeriesCollection";
import { CollectionWorkbench } from "./CollectionWorkbench";
import { CpiStrategyWorkbench } from "./CpiStrategyWorkbench";
import { EMPLOYMENT_PROFILE as profile } from "./macroTimeSeriesProfile";
import { EMPLOYMENT, keyOf } from "./workflowModel";
import {
  buildCpiQueryRequest,
  type CpiAvailability,
  type CpiJob,
  type CpiStrategy,
} from "./cpiTimeSeriesApi";
import type { Transport } from "./api";
import type { Route } from "./workflowRoute";
const flush = () => act(async () => {});
const response = (data: unknown) => new Response(JSON.stringify(data));
const availability: CpiAvailability = {
  busy: false,
  failure: null,
  legacy: false,
  data: {
    latest_months: Object.fromEntries(
      profile.series.map((s) => [s[0], "2026-01"]),
    ),
    observation_count: 6,
    revision_count: 0,
    batch_count: 1,
    initialization: {
      preset_id: "employment_latest_20y_v1",
      months: 240,
      start: "2006-02",
      end: "2026-01",
      available: true,
      reason: null,
      anchor_basis: "confirmed_local_available",
    },
    web_capabilities: { query: true, collect: true, reason: null },
  },
};
const job: CpiJob = {
  request_id: "11111111-2222-4333-8444-555555555555",
  job_id: "11111111-2222-4333-8444-555555555555",
  action: "initialize",
  start: "2025-12",
  end: "2026-01",
  state: "complete",
  created_at: "2026-02-01T00:00:00Z",
  started_at: "2026-02-01T00:00:01Z",
  finished_at: "2026-02-01T00:00:02Z",
  classification: null,
  result: {
    provider_requests: 1,
    provider_attempts: 1,
    saved_points: 6,
    saved_revisions: 0,
    coverage_complete: true,
    readback: {
      verified: true,
      point_count: 6,
      expected_points: 6,
      source_unavailable_count: 0,
    },
  },
};
afterEach(() => {
  cleanup();
  localStorage.clear();
  sessionStorage.clear();
});
it("employment filters series and units, reverses months and preserves exact HTTP text", async () => {
  const paths: string[] = [];
  let original = "";
  const transport: Transport = async (path) => {
    paths.push(String(path));
    const selected = new URL(String(path), "http://fixture").searchParams
      .get("series")!
      .split(",");
    original =
      " \n" +
      JSON.stringify(
        {
          start_month: "2025-12",
          end_month: "2026-01",
          series_ids: selected,
          points: ["2025-12", "2026-01"].flatMap((month) =>
            selected.map((series_id) => ({
              series_id,
              month,
              value: "4.2000",
              value_unit: "percent",
              change_1m_percentage_points: "0.1",
              origin: "local",
              availability: "available",
            })),
          ),
          missing: [],
          calculation_missing: [],
          complete: true,
          coverage_complete: true,
          persisted: false,
          provider_requests: 0,
          provider_attempts: 0,
          vintage_mode: "observed",
          as_of: null,
          unknown_field: "keep this",
        },
        null,
        2,
      ) +
      "\n";
    return new Response(original);
  };
  render(
    <CpiTimeSeries
      profile={profile}
      availability={availability}
      transport={transport}
    />,
  );
  expect(screen.queryByRole("button", { name: "统计篮子" })).toBeNull();
  expect(screen.queryByRole("button", { name: "季节调整" })).toBeNull();
  fireEvent.change(screen.getByLabelText("开始月份"), {
    target: { value: "2025-12" },
  });
  fireEvent.click(screen.getByRole("button", { name: "就业指标" }));
  const options = within(screen.getByRole("group", { name: "就业指标选项" }));
  fireEvent.click(options.getByRole("button", { name: "清空" }));
  fireEvent.click(
    options.getByRole("checkbox", { name: profile.series[1][1] }),
  );
  fireEvent.click(screen.getByRole("button", { name: "就业指标" }));
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  const table = await screen.findByRole("table", { name: "就业 范围数据" });
  expect(
    within(table)
      .getAllByRole("row")
      .slice(1)
      .map((r) => r.children[0].textContent),
  ).toEqual(["2026-01", "2025-12"]);
  expect(table.textContent).toContain("百分点");
  const url = new URL(paths[0], "http://fixture");
  expect(url.searchParams.get("series")).toBe("LNS14000000");
  expect(url.searchParams.has("basket")).toBe(false);
  expect(url.searchParams.has("adjustment")).toBe(false);
  fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
  expect(screen.getByLabelText("原始 HTTP 正文").textContent).toBe(original);
});
it("history uses an available cutoff and a local-only employment request", () => {
  const request = buildCpiQueryRequest(
    {
      start: "2025-12",
      end: "2026-01",
      series: "LNS14000000",
      basket: "all",
      adjustment: "all",
      measure: "index",
      query_mode: "history",
      as_of: "2026-02-01T00:00:00Z",
    },
    profile,
  );
  const params = new URL(request.path, "http://fixture").searchParams;
  expect(request.path.startsWith(profile.basePath)).toBe(true);
  expect(params.get("local_only")).toBe("true");
  expect(params.get("as_of_basis")).toBe("available");
  expect(params.get("series")).toBe("LNS14000000");
});
it("editable initialization confirms DEV, refreshes briefs and opens the shared execution detail", async () => {
  const submitted: unknown[] = [];
  const onExecution = vi.fn();
  let jobs: CpiJob[] = [];
  const transport: Transport = async (path, init) => {
    expect(String(path).startsWith(profile.basePath)).toBe(true);
    if (init?.method === "POST") {
      const command = JSON.parse(String(init.body));
      submitted.push(command);
      const receipt = { ...job, ...command, job_id: command.request_id };
      jobs = [receipt];
      return response(receipt);
    }
    return response({ jobs });
  };
  render(
    <CpiTimeSeriesCollection
      profile={profile}
      availability={availability}
      transport={transport}
      tab="new"
      onExecution={onExecution}
    />,
  );
  fireEvent.change(screen.getByLabelText("采集方式"), {
    target: { value: "initialize" },
  });
  expect(
    (screen.getByLabelText("采集开始月份") as HTMLInputElement).value,
  ).toBe("2006-02");
  fireEvent.change(screen.getByLabelText("采集开始月份"), {
    target: { value: "2025-12" },
  });
  const submit = screen.getByRole("button", {
    name: "提交 就业 采集",
  }) as HTMLButtonElement;
  expect(submit.disabled).toBe(true);
  fireEvent.click(
    screen.getByRole("checkbox", { name: "我确认将以上范围采集到开发测试库" }),
  );
  fireEvent.click(submit);
  await flush();
  expect(submitted).toHaveLength(1);
  const brief = within(screen.getByRole("complementary", { name: "执行简报" }));
  fireEvent.click(brief.getByRole("button", { name: /美国 就业/ }));
  expect(onExecution).toHaveBeenCalledWith(jobs[0].job_id);
  cleanup();
  const fields = new URLSearchParams({
    tab: "executions",
    asset: EMPLOYMENT.identity,
    asset_revision: EMPLOYMENT.revision,
    execution_asset: keyOf(EMPLOYMENT),
    job: jobs[0].job_id,
  });
  const route: Route = { view: "collection", fields, invalid: false };
  render(
    <CollectionWorkbench
      assets={[]}
      asset={undefined}
      source={undefined}
      route={route}
      navigate={vi.fn()}
      transport={async (path) => {
        expect(String(path)).toBe(profile.basePath + "/executions");
        return response({
          executions: [{ ...jobs[0], id: jobs[0].job_id, origin: "manual" }],
        });
      }}
      generation={0}
      invalidIdentity={false}
      employmentAvailability={availability}
    />,
  );
  await flush();
  expect(screen.getByText("执行详情")).toBeTruthy();
  expect(screen.getAllByText(jobs[0].job_id).length).toBeGreaterThan(0);
  expect(document.querySelectorAll(".execution-briefs")).toHaveLength(0);
});
it("employment strategy uses its independent plan and only start/pause controls", async () => {
  let state: CpiStrategy = {
    exists: false,
    enabled: false,
    history_start: "2006-02",
    worker: { state: "never_started", last_heartbeat_at: null },
    next_due: null,
    policy: {},
  };
  const writes: unknown[] = [];
  render(
    <CpiStrategyWorkbench
      profile={profile}
      availability={availability}
      selected
      transport={async (path, init) => {
        expect(String(path).startsWith(profile.basePath)).toBe(true);
        if (String(path).endsWith("/schedule"))
          return response({ targets: [], calendar: { state: "missing" } });
        if (init?.method === "PUT") {
          writes.push(JSON.parse(String(init.body)));
          state = { ...state, exists: true, enabled: !state.enabled };
        }
        return response(state);
      }}
    />,
  );
  await flush();
  fireEvent.click(screen.getByRole("button", { name: /启动/ }));
  await flush();
  expect(writes).toEqual([{ enabled: true }]);
  expect(screen.getByRole("button", { name: /暂停/ })).toBeTruthy();
});

it("App routes employment to the explicit Host without querying CPI or the T03 snapshot", async () => {
  const paths: string[] = [];
  location.hash = `ui=r5&view=assets&asset=${EMPLOYMENT.identity}&asset_revision=${EMPLOYMENT.revision}`;
  render(
    <App
      transport={async (input) => {
        const path = new URL(String(input), "http://fixture").pathname;
        paths.push(path);
        if (path === profile.basePath + "/status")
          return response(availability.data);
        if (path === "/api/r5/packages")
          return response({
            status: "OK",
            packages: [
              {
                package_id: "macro",
                name: "宏观",
                history_completeness: "COMPLETE",
                capabilities: [
                  { title: "美国就业", definition_ref: EMPLOYMENT },
                ],
                assets: [],
              },
            ],
          });
        if (path === "/api/r5/sources" || path === "/api/r3/management")
          return response({ status: "OK", entries: [], instances: [] });
        return new Response(
          JSON.stringify({
            status: "NOT_AVAILABLE",
            classification: "not_found",
          }),
          { status: 404 },
        );
      }}
    />,
  );
  expect(
    await screen.findByRole("region", { name: "就业 范围查询" }),
  ).toBeTruthy();
  expect(
    paths.some((p) => p.includes("/cpi/") || p.includes("bls_employment")),
  ).toBe(false);
});
