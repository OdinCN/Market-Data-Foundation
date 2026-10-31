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
import { asgi, closeBridges } from "./workflowTestBridge";
import { CpiTimeSeries } from "./CpiTimeSeries";
import { CollectionWorkbench } from "./CollectionWorkbench";
import { CpiStrategyWorkbench } from "./CpiStrategyWorkbench";
import { EMPLOYMENT_PROFILE as profile } from "./macroTimeSeriesProfile";
import { CPI, EMPLOYMENT, keyOf } from "./workflowModel";
import {
  buildCpiQueryRequest,
  type CpiAvailability,
  type CpiJob,
  type CpiResult,
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
  closeBridges();
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
  fireEvent.click(screen.getByRole("tab", { name: "API说明" }));
  const preview = within(
    screen.getByRole("region", { name: "就业 API说明" }),
  ).getByLabelText("请求 URL").textContent!;
  expect(new URL(preview.slice(4)).searchParams.get("series")).toBe(
    "LNS14000000",
  );
  expect(paths).toHaveLength(0);
  fireEvent.click(screen.getByRole("tab", { name: "查询数据" }));
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
  expect(preview).toBe(`GET ${new URL(paths[0], location.origin).href}`);
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

it("keeps employment source nulls and missing comparisons distinct, with Beijing calendar time", async () => {
  const result: CpiResult = {
    start_month: "2025-10",
    end_month: "2025-10",
    series_ids: profile.series.map((s) => s[0]),
    points: [
      {
        series_id: "CES0000000001",
        month: "2025-10",
        value: "150010",
        value_unit: "thousand_jobs",
        net_change_1m_thousand_jobs: "10",
      },
      {
        series_id: "CES0500000003",
        month: "2025-10",
        value: "30.10",
        value_unit: "USD/hour",
        mom_pct: null,
        yoy_pct: "3.08",
      },
      {
        series_id: "LNS11300000",
        month: "2025-10",
        value: "62.5",
        value_unit: "percent",
        change_1m_percentage_points: "0.1",
      },
      {
        series_id: "CES0500000002",
        month: "2025-10",
        value: "34.3",
        value_unit: "hours",
        change_1m_hours: "-0.1",
      },
    ],
    missing: [
      {
        series_id: "LNS14000000",
        month: "2025-10",
        reason: "source_unavailable",
      },
    ],
    calculation_missing: [
      {
        series_id: "CES0500000003",
        month: "2025-10",
        metric: "mom_pct",
        required_month: "2025-09",
        reason: "not_observed",
      },
    ],
    complete: false,
    coverage_complete: true,
    persisted: false,
    provider_requests: 0,
    provider_attempts: 0,
    vintage_mode: "observed",
    as_of: null,
    publication_events: [
      {
        reference_month: "2025-10",
        time: "2025-11-20T13:30:00Z",
        status: "published",
        evidence: { source: "synthetic-calendar" },
      },
    ],
  };
  let queries = 0;
  render(
    <CpiTimeSeries
      profile={profile}
      availability={availability}
      transport={async () => {
        queries++;
        return response(result);
      }}
    />,
  );
  for (const name of ["开始月份", "结束月份"])
    fireEvent.change(screen.getByLabelText(name), {
      target: { value: "2025-10" },
    });
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  const table = await screen.findByRole("table", { name: "就业 范围数据" });
  const rows = within(table).getAllByRole("row").slice(1);
  expect(rows).toHaveLength(5);
  expect(rows[1].textContent).toContain("失业率 · CPS · %");
  expect(rows[1].textContent).toContain("未公布 / 来源缺值");
  expect(rows[1].children[2].textContent).toBe("—");
  expect(rows[3].textContent).toContain("劳动参与率");
  expect(rows[4].textContent).toContain("-0.1");
  expect(rows[4].textContent).toContain("小时");
  expect(rows[2].textContent).toContain("30.10");
  expect(rows[2].textContent).toContain("3.08%");
  expect(rows[2].textContent).toContain(
    "计算基期缺值：2025-09（本地尚无记录）",
  );
  expect(rows[0].textContent).toContain("2025-11-20 21:30");
  fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
  expect(screen.getByLabelText("原始 HTTP 正文").textContent).toBe(
    JSON.stringify(result),
  );
  expect(queries).toBe(1);
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
    <CollectionWorkbench
      assets={[]}
      asset={undefined}
      source={undefined}
      route={{
        view: "collection",
        fields: new URLSearchParams({
          asset: EMPLOYMENT.identity,
          asset_revision: EMPLOYMENT.revision,
          tab: "new",
        }),
        invalid: false,
      }}
      navigate={(_, fields) => onExecution(fields?.job)}
      transport={transport}
      generation={0}
      invalidIdentity={false}
      employmentAvailability={availability}
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

it.each(["ui=r5&view=assets", "page=3"])(
  "App routes %s employment links to the explicit Host without querying CPI or the T03 snapshot",
  async (route) => {
    const paths: string[] = [];
    location.hash = `${route}&asset=${EMPLOYMENT.identity}&asset_revision=${EMPLOYMENT.revision}`;
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
  },
);

it("opens the ordinary employment asset row through the shared runtime catalog", async () => {
  location.hash = "ui=r5&view=assets";
  const paths: string[] = [];
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
                assets: [
                  {
                    identity: EMPLOYMENT.identity,
                    revisions: [
                      {
                        definition_ref: EMPLOYMENT,
                        capability_enabled: true,
                        metadata_view_permission: "allowed",
                        local_holding: "PRESENT",
                        local_read: { allowed: true },
                        upstream_read: { allowed: true },
                        collect: { allowed: true },
                        source_entries: [],
                      },
                    ],
                  },
                ],
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
  const row = await screen.findByRole("button", { name: "选择 美国就业 宏观" });
  paths.length = 0;
  fireEvent.click(row);
  expect(
    await screen.findByRole("region", { name: "就业 范围查询" }),
  ).toBeTruthy();
  expect(
    new URLSearchParams(location.hash.slice(1)).get("asset_revision"),
  ).toBe(EMPLOYMENT.revision);
  expect(
    paths.some((p) => p.includes("/cpi/") || p.includes("bls_employment")),
  ).toBe(false);
});

function publicAppTransport(
  paths: string[],
  writes: Record<string, string>[],
): Transport {
  let jobs: CpiJob[] = [];
  return async (input, init) => {
    const path = new URL(String(input), "http://fixture").pathname;
    paths.push(path);
    if (path === profile.basePath + "/status")
      return response(availability.data);
    if (path === profile.basePath + "/jobs") {
      if (init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        writes.push(body);
        jobs = [{ ...job, ...body, job_id: body.request_id }];
        return response(jobs[0]);
      }
      return response({ jobs });
    }
    if (path === profile.basePath + "/executions")
      return response({
        executions: jobs.map((j) => ({ ...j, id: j.job_id, origin: "manual" })),
      });
    if (path === "/api/r5/packages")
      return response({
        status: "OK",
        packages: [
          {
            package_id: "macro",
            name: "宏观",
            history_completeness: "COMPLETE",
            capabilities: [{ title: "美国就业", definition_ref: EMPLOYMENT }],
            assets: [
              {
                identity: EMPLOYMENT.identity,
                revisions: [
                  {
                    definition_ref: EMPLOYMENT,
                    capability_enabled: true,
                    metadata_view_permission: "allowed",
                    local_holding: "PRESENT",
                    local_read: { allowed: true },
                    upstream_read: { allowed: true },
                    collect: { allowed: true },
                    source_entries: [],
                  },
                ],
              },
            ],
          },
        ],
      });
    if (path === "/api/r5/sources" || path === "/api/r3/management")
      return response({ status: "OK", entries: [], instances: [] });
    return new Response(
      JSON.stringify({ status: "NOT_AVAILABLE", classification: "not_found" }),
      { status: 404 },
    );
  };
}
it.each(["ui=r5&view=collection", "page=4"])(
  "actual App %s renders employment controlled collection and briefs",
  async (route) => {
    location.hash = `${route}&asset=${EMPLOYMENT.identity}&asset_revision=${EMPLOYMENT.revision}`;
    const paths: string[] = [],
      writes: Record<string, string>[] = [];
    render(<App transport={publicAppTransport(paths, writes)} />);
    const region = await screen.findByRole("region", { name: "就业 受控采集" });
    expect(
      within(region).getByRole("complementary", { name: "执行简报" }),
    ).toBeTruthy();
    expect(region.textContent).toContain("共5种口径");
    expect(region.textContent).toContain("劳动参与率");
    expect(region.textContent).toContain("平均每周计薪工时");
    expect(
      within(region).queryByRole("group", { name: "采集指标" }),
    ).toBeNull();
    expect(region.textContent).not.toMatch(/Host|T03/);
    expect(
      paths.some((p) => p.includes("/cpi/") || p.includes("bls_employment")),
    ).toBe(false);
    expect(writes).toHaveLength(0);
  },
);
it("ordinary asset collection navigation submits all five employment series and selects its unified execution", async () => {
  location.hash = "ui=r5&view=assets";
  const paths: string[] = [],
    writes: Record<string, string>[] = [];
  render(<App transport={publicAppTransport(paths, writes)} />);
  fireEvent.click(
    await screen.findByRole("button", { name: "选择 美国就业 宏观" }),
  );
  fireEvent.click(await screen.findByRole("button", { name: "采集数据" }));
  await screen.findByRole("region", { name: "就业 受控采集" });
  fireEvent.change(screen.getByLabelText("采集方式"), {
    target: { value: "initialize" },
  });
  expect(
    (screen.getByLabelText("采集开始月份") as HTMLInputElement).value,
  ).toBe("2006-02");
  fireEvent.click(
    screen.getByRole("checkbox", { name: "我确认将以上范围采集到开发测试库" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "提交 就业 采集" }));
  await flush();
  expect(writes).toHaveLength(1);
  expect(writes[0].series).toBeUndefined();
  fireEvent.click(
    within(screen.getByRole("complementary", { name: "执行简报" })).getByRole(
      "button",
      { name: /美国 就业/ },
    ),
  );
  await screen.findByText("执行详情");
  expect(new URLSearchParams(location.hash.slice(1)).get("job")).toBe(
    writes[0].request_id,
  );
  expect(paths).toContain(profile.basePath + "/executions");
});
it.each(["legacy", "shared"])(
  "%s collection target fallback retains employment when absent from the asset directory",
  async (variant) => {
    const navigate = vi.fn();
    render(
      <CollectionWorkbench
        assets={[]}
        asset={undefined}
        source={undefined}
        route={{
          view: "collection",
          fields: new URLSearchParams(
            variant === "shared"
              ? { asset: CPI.identity, asset_revision: CPI.revision }
              : {},
          ),
          invalid: false,
        }}
        navigate={navigate}
        transport={async () => response({ jobs: [] })}
        generation={0}
        invalidIdentity={false}
        employmentAvailability={availability}
        cpiAvailability={variant === "shared" ? availability : undefined}
      />,
    );
    fireEvent.change(screen.getByLabelText("采集目标"), {
      target: { value: keyOf(EMPLOYMENT) },
    });
    expect(navigate).toHaveBeenCalledWith(
      "collection",
      expect.objectContaining({
        asset: EMPLOYMENT.identity,
        asset_revision: EMPLOYMENT.revision,
        operation: null,
      }),
    );
  },
);
it("an unrelated operation cannot enter employment collection", () => {
  render(
    <CollectionWorkbench
      assets={[]}
      asset={undefined}
      source={undefined}
      route={{
        view: "collection",
        fields: new URLSearchParams({
          asset: EMPLOYMENT.identity,
          asset_revision: EMPLOYMENT.revision,
          operation: "unrelated@r1",
        }),
        invalid: false,
      }}
      navigate={vi.fn()}
      transport={async () => response({})}
      generation={0}
      invalidIdentity={false}
      employmentAvailability={availability}
    />,
  );
  expect(screen.queryByRole("region", { name: "就业 受控采集" })).toBeNull();
  expect(screen.queryByRole("region", { name: "CPI 受控采集" })).toBeNull();
});

it.each(["matching", "wrong_source", "wrong_binding"])(
  "real catalog employment operation %s preserves the explicit binding boundary",
  async (variant) => {
    const fixture = asgi();
    const catalog = await fixture.exchange({
      method: "GET",
      path: "/api/r5/sources",
    });
    const entry = catalog.body.entries.find(
      (e: { asset?: { identity: string } }) =>
        e.asset?.identity === EMPLOYMENT.identity,
    );
    expect(entry).toBeTruthy();
    if (variant === "wrong_source") entry.source_contract.revision = "other";
    if (variant === "wrong_binding")
      entry.binding_contract.source.revision = "other";
    location.hash = `ui=r5&view=collection&asset=${EMPLOYMENT.identity}&asset_revision=${EMPLOYMENT.revision}&operation=${entry.identity}`;
    const paths: string[] = [],
      writes: Record<string, string>[] = [];
    const normal = publicAppTransport(paths, writes);
    render(
      <App
        transport={async (input, init) => {
          const path = new URL(String(input), "http://fixture").pathname;
          return path === "/api/r5/sources"
            ? response({ ...catalog.body, entries: [entry] })
            : normal(input, init);
        }}
      />,
    );
    if (variant === "matching")
      await screen.findByRole("region", { name: "就业 受控采集" });
    else {
      await screen.findByLabelText("采集目标");
      expect(
        screen.queryByRole("region", { name: "就业 受控采集" }),
      ).toBeNull();
      expect(
        paths.some(
          (p) => p.startsWith(profile.basePath) || p.includes("/cpi/"),
        ),
      ).toBe(false);
    }
  },
);
