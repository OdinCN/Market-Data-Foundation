import { afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { Transport } from "./api";
import { CpiTimeSeriesCollection } from "./CpiTimeSeriesCollection";
import { CpiTimeSeries } from "./CpiTimeSeries";
import { CollectionWorkbench } from "./CollectionWorkbench";
import {
  CpiExecutionDetail,
  CpiStrategyWorkbench,
  cpiExecutionStateText,
} from "./CpiStrategyWorkbench";
import { GDP_PROFILE as profile, CPI_PROFILE } from "./macroTimeSeriesProfile";
import { GDP, keyOf } from "./workflowModel";
import {
  readGdpStatus,
  normalizeGdpResult,
  buildGdpQueryRequest,
  type GdpResult,
} from "./gdpTimeSeriesAdapter";
import {
  readCpiJobs,
  readCpiExecutions,
  type CpiAvailability,
  type CpiJob,
  type CpiExecution,
} from "./cpiTimeSeriesApi";

const fixture = JSON.parse(
  readFileSync(
    resolve(process.cwd(), "../tests/fixtures/gdp_ui_http.json"),
    "utf8",
  ),
);
const response = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status });
const flush = () => act(async () => {});
const button = (name: string) =>
  screen.getByRole("button", { name }) as HTMLButtonElement;
const nativeStatus = {
  ...fixture.status,
  latest_months: {},
  initialization: undefined,
  quarter_initialization: fixture.status.initialization,
};
const availability: CpiAvailability = {
  data: nativeStatus,
  busy: false,
  failure: null,
  legacy: false,
};
const job: CpiJob = fixture.jobs.jobs[0];
const execution: CpiExecution = fixture.executions.executions[0];
const scope = { start: "2026-Q2", end: "2026-Q3" };
afterEach(() => {
  cleanup();
  localStorage.clear();
  sessionStorage.clear();
  vi.useRealTimers();
  vi.restoreAllMocks();
  location.hash = "";
});

it("retains native status, init bounds and real quarterly HTTP receipts", async () => {
  const transport: Transport = async (path) =>
    response(
      String(path).endsWith("/status")
        ? fixture.status
        : String(path).endsWith("/jobs")
          ? fixture.jobs
          : fixture.executions,
    );
  const status = await readGdpStatus(transport, new AbortController().signal);
  expect(status.quarter_initialization?.quarters).toBe(80);
  expect(status.latest_months).toEqual({});
  expect(status.source_allowed).toBe(true);
  expect(status.collection_scope?.max_quarters).toBe(800);
  expect((await readCpiJobs(transport, undefined, profile)).jobs[0]).toEqual(
    job,
  );
  expect(
    (await readCpiExecutions(transport, undefined, profile)).executions[0],
  ).toEqual(execution);
});

it("requires confirmation, sends init for an editable 80-quarter preset, and tracks 202 until verified", async () => {
  vi.useFakeTimers();
  const calls: { path: string; body?: Record<string, string> }[] = [];
  let receipt: CpiJob | null = null;
  const transport: Transport = async (path, init) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ path: String(path), body });
    if (body) {
      receipt = {
        ...job,
        ...body,
        job_id: body.request_id,
        result: null,
        state: "running",
        finished_at: null,
      };
      return response(receipt, 202);
    }
    return response({ jobs: receipt ? [receipt] : [] });
  };
  render(
    <CpiTimeSeriesCollection
      profile={profile}
      transport={transport}
      availability={availability}
      tab="new"
    />,
  );
  await flush();
  fireEvent.change(screen.getByLabelText("采集方式"), {
    target: { value: "initialize" },
  });
  expect(
    (screen.getByLabelText("采集开始季度") as HTMLInputElement).value,
  ).toBe("2006-Q4");
  expect(
    (screen.getByLabelText("采集结束季度") as HTMLInputElement).value,
  ).toBe("2026-Q3");
  expect(screen.queryByLabelText("采集开始月份")).toBeNull();
  expect(button("提交 GDP与增长 采集").disabled).toBe(true);
  fireEvent.change(screen.getByLabelText("采集开始季度"), {
    target: { value: "2026-Q2" },
  });
  fireEvent.click(
    screen.getByRole("checkbox", { name: "我确认将以上范围采集到开发测试库" }),
  );
  fireEvent.click(button("提交 GDP与增长 采集"));
  await flush();
  const submitted = calls.find((c) => c.body)!.body!;
  expect(submitted.action).toBe("init");
  expect(Object.keys(submitted).sort()).toEqual([
    "action",
    "end",
    "request_id",
    "start",
  ]);
  expect(submitted.start).toBe("2026-Q2");
  expect(screen.getByLabelText("执行简报").textContent).toContain("执行中");
  expect(screen.getByLabelText("执行简报").textContent).not.toContain("已完成");
  receipt = { ...job, ...submitted, job_id: submitted.request_id } as CpiJob;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(screen.getByLabelText("执行简报").textContent).toContain(
    "已完成（回读已验证）",
  );
  expect(calls.filter((c) => c.body)).toHaveLength(1);
  expect(calls.every((c) => c.path.startsWith(profile.basePath))).toBe(true);
});

it("sends update and invalidates confirmation when the quarterly range changes", async () => {
  const bodies: Record<string, string>[] = [];
  const transport: Transport = async (_path, init) => {
    if (init?.body) {
      const body = JSON.parse(String(init.body));
      bodies.push(body);
      return response(
        { ...job, ...body, state: "running", result: null, finished_at: null },
        202,
      );
    }
    return response({ jobs: [] });
  };
  render(
    <CpiTimeSeriesCollection
      profile={profile}
      transport={transport}
      availability={availability}
      tab="new"
    />,
  );
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.change(screen.getByLabelText("采集开始季度"), {
    target: { value: "2026-Q2" },
  });
  expect(button("提交 GDP与增长 采集").disabled).toBe(true);
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(button("提交 GDP与增长 采集"));
  await flush();
  expect(bodies[0].action).toBe("update");
});

it("blocks disabled collection using the service reason", async () => {
  const transport = vi.fn(
    async (_path: Parameters<Transport>[0], _init?: RequestInit) =>
      response({ jobs: [] }),
  );
  render(
    <CpiTimeSeriesCollection
      profile={profile}
      transport={transport}
      availability={{
        ...availability,
        data: {
          ...nativeStatus,
          web_capabilities: {
            query: true,
            collect: false,
            reason: "gdp_capability_disabled",
          },
        },
      }}
      tab="new"
    />,
  );
  expect(screen.getByText(/gdp_capability_disabled/)).toBeTruthy();
  fireEvent.click(screen.getByRole("checkbox"));
  expect(button("提交 GDP与增长 采集").disabled).toBe(true);
  expect(transport.mock.calls.every(([, init]) => !init?.body)).toBe(true);
});

it("retains an unknown request across reloads and never automatically resubmits", async () => {
  const pending = {
    request_id: job.request_id,
    action: "update",
    start: job.start,
    end: job.end,
  };
  localStorage.setItem(
    "mdf.gdp.timeseries.pending.v1",
    JSON.stringify(pending),
  );
  const writes: string[] = [];
  const transport: Transport = async (_path, init) => {
    if (init?.method === "POST") writes.push("POST");
    return response({
      jobs: [
        {
          ...job,
          ...pending,
          state: "unknown",
          result: null,
          classification: "execution_interrupted",
        },
      ],
    });
  };
  render(
    <CpiTimeSeriesCollection
      profile={profile}
      transport={transport}
      availability={availability}
      tab="new"
    />,
  );
  await screen.findByText("结果未知，保留请求并自动跟踪，请勿重复提交。");
  expect(button("提交 GDP与增长 采集").disabled).toBe(true);
  expect(screen.queryByRole("button", { name: "开始另一项采集" })).toBeNull();
  expect(writes).toEqual([]);
});

it("shows native source gaps, official nulls and uncertain readback without monthly coverage", () => {
  const partial = {
    ...execution,
    result: {
      ...job.result,
      completion: "source_coverage_partial",
      source_missing: [{ series_id: "A191RL", quarter: "2026-Q3" }],
    },
  };
  render(<CpiExecutionDetail profile={profile} execution={partial} />);
  expect(screen.getByText("已完成（来源有缺口）")).toBeTruthy();
  expect(screen.getByLabelText("GDP季度回读覆盖").textContent).toContain(
    "2026-Q3 · A191RL",
  );
  expect(screen.getByLabelText("GDP季度回读覆盖").textContent).toContain(
    "官方空值1",
  );
  expect(screen.getByText(/版本关联尚未确认/)).toBeTruthy();
  expect(
    cpiExecutionStateText(
      "complete",
      { readback: { verified: false } },
      profile,
    ),
  ).toBe("结果未知（回读未确认）");
  expect(cpiExecutionStateText("unknown", partial.result, profile)).toBe(
    "结果未知",
  );
});

it("requires a second explicit action to enable GDP strategy and does not fabricate a worker heartbeat", async () => {
  const writes: unknown[] = [];
  let strategy = fixture.strategy;
  const transport: Transport = async (path, init) => {
    if (String(path).endsWith("/schedule")) return response(fixture.schedule);
    if (init?.method === "PUT") {
      const body = JSON.parse(String(init.body));
      writes.push(body);
      strategy = { ...strategy, ...body, exists: true };
    }
    return response(strategy);
  };
  render(
    <CpiStrategyWorkbench
      profile={profile}
      transport={transport}
      availability={availability}
      selected
    />,
  );
  await screen.findByText("GDP与增长 策略详情");
  fireEvent.click(button("启动"));
  expect(writes).toHaveLength(0);
  fireEvent.click(button("取消"));
  expect(writes).toHaveLength(0);
  fireEvent.click(button("启动"));
  fireEvent.click(button("确认启动GDP策略"));
  await flush();
  expect(writes).toEqual([{ enabled: true }]);
  const heartbeat = screen.getByText("最近心跳（北京时间）").parentElement!;
  expect(heartbeat.textContent).toContain("未记录");
  expect(screen.getByText("后台进程").parentElement!.textContent).toContain(
    "尚未启动",
  );
});

it("keeps each quarterly estimate distinct and preserves cancelled/conflicted/unrecorded plans", async () => {
  const target = fixture.schedule.targets[0];
  const schedule = {
    ...fixture.schedule,
    targets: ["third", "advance", "second"].map((kind, i) => ({
      ...target,
      key: `2026-Q3:${kind}`,
      estimate_kind: kind,
      release_at: `2026-11-${23 + i}T13:30:00Z`,
      publication_status: i === 0 ? "cancelled" : "scheduled",
      calendar_execution_blocked: i < 2,
      calendar_validation: i === 1 ? "gdp_calendar_conflict" : null,
      target: i === 2 ? {} : { state: "pending" },
      history: {},
    })),
  };
  render(
    <CpiStrategyWorkbench
      profile={profile}
      transport={async (path) =>
        response(
          String(path).endsWith("/schedule") ? schedule : fixture.strategy,
        )
      }
      availability={availability}
      selected
    />,
  );
  const plans = await screen.findByRole("list", { name: "GDP与增长 季度计划" });
  expect(within(plans).getAllByRole("listitem")).toHaveLength(3);
  for (const kind of ["初值", "第二次估计", "第三次估计"])
    expect(plans.textContent).toContain(`2026-Q3 · ${kind}`);
  expect(plans.textContent).toContain("计划已取消");
  expect(plans.textContent).toContain("gdp_calendar_conflict");
  expect(plans.textContent).toContain("未记录");
  expect(plans.textContent).not.toContain("已完成");
  expect(plans.textContent).not.toContain("月份");
  expect(plans.textContent).toContain("21:30:00");
});

it("includes GDP in the common execution list and opens its own detail with schedule origin", async () => {
  const scheduled = {
    ...execution,
    ...job,
    id: job.job_id,
    origin: "schedule",
    stage: "target",
  };
  const navigate = vi.fn();
  render(
    <CollectionWorkbench
      assets={[]}
      asset={undefined}
      source={undefined}
      invalidIdentity={false}
      route={{
        view: "collection",
        invalid: false,
        fields: new URLSearchParams({
          tab: "executions",
          execution_asset: keyOf(GDP),
          asset: GDP.identity,
          asset_revision: GDP.revision,
          job: job.job_id,
        }),
      }}
      navigate={navigate}
      transport={async (path) => {
        expect(String(path)).toBe(profile.basePath + "/executions");
        return response({ executions: [scheduled] });
      }}
      generation={0}
      gdpAvailability={availability}
    />,
  );
  const table = within(
    await screen.findByRole("region", { name: "执行记录" }),
  ).getByRole("table");
  await screen.findByText("执行详情");
  expect(table.textContent).toContain("GDP与增长 · 自动阶段 · 当期采集");
  expect(screen.getByText("关联资产").parentElement!.textContent).toContain(
    "GDP与增长",
  );
  expect(screen.getByLabelText("GDP季度回读覆盖")).toBeTruthy();
});

it("adds GDP beside an existing family in the common strategy table", async () => {
  const cpiStrategy = { ...fixture.strategy, history_start: "2006-01" };
  const transport: Transport = async (path) =>
    response(
      String(path).endsWith("/schedule")
        ? fixture.schedule
        : String(path).startsWith(profile.basePath)
          ? fixture.strategy
          : cpiStrategy,
    );
  render(
    <CpiStrategyWorkbench
      transport={transport}
      profile={CPI_PROFILE}
      availability={availability}
      seventhProfile={profile}
      seventhAvailability={availability}
    />,
  );
  await flush();
  const table = within(
    screen.getByRole("region", { name: "周期任务" }),
  ).getByRole("table");
  expect(table.textContent).toContain("美国 CPI");
  expect(table.textContent).toContain("美国 GDP与增长");
});

it("keeps current upstream opt-in, URL preview, raw result and historical local mode consistent", async () => {
  const paths: string[] = [];
  const upstream = {
    ...fixture.query,
    local_only: false,
    provider_requests: 1,
    provider_attempts: 1,
  };
  const transport: Transport = async (path) => {
    paths.push(String(path));
    return response(
      String(path).includes("query_mode=history") ? fixture.history : upstream,
    );
  };
  render(
    <CpiTimeSeries
      profile={profile}
      transport={transport}
      availability={availability}
    />,
  );
  fireEvent.change(screen.getByLabelText("开始季度"), {
    target: { value: scope.start },
  });
  fireEvent.change(screen.getByLabelText("结束季度"), {
    target: { value: scope.end },
  });
  const toggle = screen.getByRole("checkbox", {
    name: "允许本次当前查询临时补源（不写库）",
  }) as HTMLInputElement;
  expect(toggle.checked).toBe(false);
  fireEvent.click(toggle);
  expect(screen.getAllByText(/local_only=false/)[0]).toBeTruthy();
  fireEvent.click(button("查询数据"));
  await screen.findByRole("table", { name: "GDP与增长 范围数据" });
  fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
  expect(screen.getByLabelText("原始 HTTP 正文").textContent).toBe(
    JSON.stringify(upstream),
  );
  expect(paths[0]).toContain("local_only=false");
  fireEvent.click(screen.getByRole("checkbox", { name: "历史截止查询" }));
  expect(toggle.disabled).toBe(true);
  expect(toggle.checked).toBe(false);
  expect(screen.getAllByText(/local_only=true/)[0]).toBeTruthy();
});

it("disallows upstream without service permission and accepts partial local values only after explicit opt-in", () => {
  render(
    <CpiTimeSeries
      profile={profile}
      transport={async () => response(fixture.query)}
      availability={{
        ...availability,
        data: { ...nativeStatus, source_allowed: false },
      }}
    />,
  );
  expect(
    (
      screen.getByRole("checkbox", {
        name: "允许本次当前查询临时补源（不写库）",
      }) as HTMLInputElement
    ).disabled,
  ).toBe(true);
  const partial: GdpResult = {
    ...fixture.query,
    local_only: false,
    provider_requests: 1,
    provider_attempts: 1,
    source_failure: "gdp_source_http_503",
    status: "partial",
  };
  expect(
    normalizeGdpResult(partial, { ...scope, local_only: false }).points[0]
      .value,
  ).toBe(fixture.query.points[0].value);
  expect(
    normalizeGdpResult(partial, { ...scope, local_only: false }).source_failure
      ?.classification,
  ).toBe("gdp_source_http_503");
  expect(() => normalizeGdpResult(partial, scope)).toThrow();
  expect(
    buildGdpQueryRequest({
      ...scope,
      local_only: false,
      query_mode: "history",
      as_of: "2026-11-02T12:00:00Z",
    }).path,
  ).toContain("local_only=true");
});

it.each([400, 403, 409])(
  "does not fabricate successful collection after a refusal/conflict (%s)",
  async (status) => {
    const transport: Transport = async (_path, init) =>
      init?.body
        ? response(
            {
              classification:
                status === 409
                  ? "request_id_conflict"
                  : "gdp_capability_disabled",
            },
            status,
          )
        : response({ jobs: [] });
    render(
      <CpiTimeSeriesCollection
        profile={profile}
        transport={transport}
        availability={availability}
        tab="new"
      />,
    );
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(button("提交 GDP与增长 采集"));
    await flush();
    const brief = screen.getByLabelText("执行简报");
    expect(brief.textContent).not.toContain("已完成");
    if (status === 409) {
      expect(brief.textContent).toContain("回执不明");
      expect(
        localStorage.getItem("mdf.gdp.timeseries.pending.v1"),
      ).not.toBeNull();
    } else {
      expect(brief.textContent).toContain("gdp_capability_disabled");
      expect(localStorage.getItem("mdf.gdp.timeseries.pending.v1")).toBeNull();
    }
  },
);

it("rejects monthly, unverified and duplicated GDP receipts", async () => {
  for (const jobs of [
    [{ ...job, start: "2026-07" }],
    [{ ...job, result: { ...job.result, readback: { verified: false } } }],
    [job, job],
  ])
    await expect(
      readCpiJobs(async () => response({ jobs }), undefined, profile),
    ).rejects.toThrow();
});

it("accepts nonpersisted upstream points only in an explicitly permitted current scope", () => {
  const result = structuredClone(fixture.query) as GdpResult;
  result.local_only = false;
  result.provider_requests = 1;
  result.provider_attempts = 1;
  result.points[0].origin = "upstream";
  result.points[0].available_at = null;
  expect(
    normalizeGdpResult(result, { ...scope, local_only: false }).points[0]
      .origin,
  ).toBe("upstream");
  expect(() => normalizeGdpResult(result, scope)).toThrow();
});

it("includes GDP in the shared workbench periodic list when the host is available", async () => {
  const empty: CpiAvailability = {
    data: null,
    busy: false,
    failure: null,
    legacy: false,
  };
  const paths: string[] = [];
  render(
    <CollectionWorkbench
      assets={[]}
      asset={undefined}
      source={undefined}
      invalidIdentity={false}
      route={{
        view: "collection",
        invalid: false,
        fields: new URLSearchParams({
          tab: "periodic",
          periodic_asset: "all",
          schedule: "gdp-auto",
        }),
      }}
      navigate={vi.fn()}
      transport={async (path) => {
        paths.push(String(path));
        return response(
          String(path).endsWith("/schedule")
            ? fixture.schedule
            : fixture.strategy,
        );
      }}
      generation={0}
      cpiAvailability={empty}
      gdpAvailability={availability}
    />,
  );
  await screen.findByText("GDP与增长 策略详情");
  expect(
    within(screen.getByRole("region", { name: "周期任务" })).getByRole("table")
      .textContent,
  ).toContain("美国 GDP与增长");
  expect(paths.every((path) => path.startsWith(profile.basePath))).toBe(true);
});

it("keeps GDP and monthly executions with identical IDs distinct in the all-assets list", async () => {
  const monthly = {
    ...execution,
    start: "2026-08",
    end: "2026-09",
    origin: "manual",
    result: null,
  };
  const transport: Transport = async (path) =>
    response(
      String(path).startsWith(profile.basePath)
        ? fixture.executions
        : { executions: [monthly] },
    );
  render(
    <CollectionWorkbench
      assets={[]}
      asset={undefined}
      source={undefined}
      invalidIdentity={false}
      route={{
        view: "collection",
        invalid: false,
        fields: new URLSearchParams({
          tab: "executions",
          execution_asset: "all",
          asset: GDP.identity,
          asset_revision: GDP.revision,
          job: job.job_id,
        }),
      }}
      navigate={vi.fn()}
      transport={transport}
      generation={0}
      cpiAvailability={availability}
      gdpAvailability={availability}
    />,
  );
  await screen.findByText("执行详情");
  const table = within(
    screen.getByRole("region", { name: "执行记录" }),
  ).getByRole("table");
  expect(within(table).getAllByRole("row")).toHaveLength(3);
  expect(table.textContent).toContain("CPI");
  expect(table.textContent).toContain("GDP与增长");
  expect(screen.getByLabelText("GDP季度回读覆盖")).toBeTruthy();
});

it("clears upstream consent when source permission is revoked and requires a fresh choice after restoration", () => {
  const transport: Transport = async () => response(fixture.query);
  const view = render(
    <CpiTimeSeries
      profile={profile}
      transport={transport}
      availability={availability}
    />,
  );
  const toggle = () =>
    screen.getByRole("checkbox", {
      name: "允许本次当前查询临时补源（不写库）",
    }) as HTMLInputElement;
  fireEvent.click(toggle());
  expect(toggle().checked).toBe(true);
  view.rerender(
    <CpiTimeSeries
      profile={profile}
      transport={transport}
      availability={{
        ...availability,
        data: { ...nativeStatus, source_allowed: false },
      }}
    />,
  );
  expect(toggle().checked).toBe(false);
  expect(toggle().disabled).toBe(true);
  view.rerender(
    <CpiTimeSeries
      profile={profile}
      transport={transport}
      availability={availability}
    />,
  );
  expect(toggle().checked).toBe(false);
});
