import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
  act,
} from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import App from "./App";
import type { Transport } from "./api";
import { CpiTimeSeries } from "./CpiTimeSeries";
import { CpiTimeSeriesCollection } from "./CpiTimeSeriesCollection";
import { CpiStrategyWorkbench } from "./CpiStrategyWorkbench";
import { CollectionWorkbench } from "./CollectionWorkbench";
import {
  CLAIMS_PROFILE as profile,
  CPI_PROFILE,
} from "./macroTimeSeriesProfile";
import {
  CLAIMS,
  CLAIMS_OPERATION,
  claimsOperationMatches,
  keyOf,
} from "./workflowModel";
import {
  buildClaimsQueryRequest,
  normalizeClaimsResult,
  readClaimsStatus,
  dayRangeValid,
  weekCount,
} from "./claimsTimeSeriesAdapter";
import { normalizeClaimsReleases } from "./claimsReleaseAdapter";
import {
  readCpiJobs,
  readCpiExecutions,
  validStrategy,
  type CpiScope,
  type CpiAvailability,
} from "./cpiTimeSeriesApi";
const fixture = JSON.parse(
  readFileSync(
    resolve(process.cwd(), "../tests/fixtures/claims_ui_http.json"),
    "utf8",
  ),
);
const scope: CpiScope = {
  start: "2026-10-10",
  end: "2026-10-24",
  series: profile.series.map(([id]) => id).join(","),
  basket: "all",
  adjustment: "all",
  measure: "index",
};
const response = (v: unknown, status = 200) =>
  new Response(JSON.stringify(v), { status });
const availability: CpiAvailability = {
  legacy: false,
  busy: false,
  failure: null,
  data: {
    ...fixture.status,
    latest_months: {},
    initialization: undefined,
    week_initialization: fixture.status.initialization,
  },
};
const change = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
const flush = () => act(async () => {});
afterEach(() => {
  cleanup();
  localStorage.clear();
  sessionStorage.clear();
  location.hash = "";
  vi.restoreAllMocks();
});
function wire() {
  const calls: string[] = [];
  const transport: Transport = async (input, init) => {
    const path = String(input),
      url = new URL(path, "http://fixture.invalid");
    calls.push(path);
    if (url.pathname.startsWith(profile.basePath)) {
      const op = url.pathname.split("/").at(-1)!;
      return response(
        op === "query"
          ? url.searchParams.get("query_mode") === "history"
            ? fixture.history
            : url.searchParams.get("local_only") === "false"
              ? fixture.source_failure
              : fixture.query
          : op === "releases"
            ? url.searchParams.get("read_mode") === "source_history"
              ? fixture.source_history
              : fixture.releases
            : fixture[op],
      );
    }
    if (url.pathname === "/api/r5/packages")
      return response({
        status: "OK",
        packages: [
          {
            package_id: "macro",
            name: "宏观",
            history_completeness: "UNKNOWN",
            capabilities: [{ title: profile.label, definition_ref: CLAIMS }],
            assets: [
              {
                identity: CLAIMS.identity,
                revisions: [
                  {
                    definition_ref: CLAIMS,
                    capability_enabled: false,
                    source_entries: [],
                    metadata_view_permission: "allowed",
                    local_holding: "PRESENT",
                    local_read: { allowed: true },
                    upstream_read: { allowed: false },
                    collect: { allowed: false },
                  },
                ],
              },
            ],
          },
        ],
      });
    if (["/api/r5/sources", "/api/r3/management"].includes(url.pathname))
      return response({
        status: "OK",
        entries:
          url.pathname === "/api/r5/sources" ? [fixture.directory_entry] : [],
        instances: [],
      });
    return response({ classification: "not_enabled" }, 503);
  };
  return { calls, transport };
}
const setRange = () => {
  change("开始日期（截止周范围）", scope.start);
  change("结束日期（截止周范围）", scope.end);
};
it("keeps native day ranges, six independent latest weeks and twenty calendar year status", async () => {
  const f = wire();
  const status = await readClaimsStatus(
    f.transport,
    new AbortController().signal,
  );
  expect(status.latest_months).toEqual({});
  expect(status.latest_weeks?.CCSA).toBe("2026-10-17");
  expect(status.latest_weeks?.ICSA).toBe("2026-10-31");
  expect(status.week_initialization?.years).toBe(20);
  expect(dayRangeValid("2026-10", "2026-11")).toBe(false);
  expect(weekCount(scope.start, scope.end)).toBe(3);
  const p = new URL(buildClaimsQueryRequest(scope).path, "http://fixture")
    .searchParams;
  expect([...p.keys()].sort()).toEqual([
    "as_of_basis",
    "end",
    "local_only",
    "query_mode",
    "series",
    "start",
  ]);
  expect(p.get("local_only")).toBe("true");
  expect(p.get("as_of_basis")).toBe("available");
  const historical = buildClaimsQueryRequest({
    ...scope,
    query_mode: "history",
    as_of: "2026-10-31T18:00:00Z",
    local_only: false,
  });
  expect(historical.path).toContain("local_only=true");
});
it("validates actual Python serialized success, missing, history and source failure contracts", () => {
  expect(normalizeClaimsResult(fixture.query, scope).points).toHaveLength(18);
  expect(
    normalizeClaimsResult(fixture.success, {
      ...scope,
      start: "2026-10-03",
      end: "2026-10-03",
    }).complete,
  ).toBe(true);
  const v = normalizeClaimsResult(fixture.query, scope);
  expect(v.points.every((p) => !("month" in p))).toBe(true);
  expect(
    v.points.find((p) => p.series_id === "ICSA" && p.period === "2026-10-10")
      ?.value,
  ).toBe("0");
  expect(
    v.points.find((p) => p.series_id === "ICNSA" && p.period === "2026-10-17")
      ?.missing_reason,
  ).toBe("provider_missing");
  expect(
    normalizeClaimsResult(fixture.history, {
      ...scope,
      query_mode: "history",
      as_of: "2026-10-31T18:00:00Z",
    }).points.every((p) => p.value === null),
  ).toBe(true);
  expect(
    normalizeClaimsResult(fixture.source_failure, {
      ...scope,
      local_only: false,
    }).source_failure?.classification,
  ).toBe("claims_fred_http_503");
  expect(
    normalizeClaimsReleases(fixture.releases).events[0].claims,
  ).toHaveLength(18);
  expect(
    normalizeClaimsReleases(fixture.source_history).events[0].reference_weeks,
  ).toContain("2026-10-17");
});
it.each([
  "duplicate",
  "month",
  "unit",
  "missing-row",
  "cross-family",
  "cutoff",
  "wrong-week-link",
  "fake-plan",
])("rejects incompatible real response mutation %s", (kind) => {
  const v = structuredClone(fixture.query);
  if (kind === "duplicate") v.rows[1] = v.rows[0];
  if (kind === "month") v.rows[0].week_ending = "2026-10";
  if (kind === "unit") v.rows[0].unit = "Thousands";
  if (kind === "missing-row") v.rows.pop();
  if (kind === "cross-family") v.asset_id = "mdf.macro.bea.gdp-growth";
  if (kind === "cutoff") v.as_of_basis = "observed";
  if (kind === "wrong-week-link")
    v.rows[0].related_releases[0].reference_week = "2026-10-17";
  if (kind === "fake-plan") {
    const r = v.rows.find(
      (r: { related_releases: unknown[] }) =>
        r.related_releases.length &&
        (r.related_releases[0] as { roles: string[] }).roles.includes(
          "prior_as_reported_in_this_event",
        ),
    );
    r.related_releases[0].scheduled_publication_at =
      "2026-10-29T08:30:00-04:00";
  }
  expect(() => normalizeClaimsResult(v, scope)).toThrow(
    "claims_result_contract_invalid",
  );
});
it("renders serialized rows in descending weeks, Number precision, distinct means and actual HTTP body", async () => {
  const f = wire();
  render(
    <CpiTimeSeries
      profile={profile}
      availability={availability}
      transport={f.transport}
    />,
  );
  expect(
    (screen.getByLabelText("开始日期（截止周范围）") as HTMLInputElement).value,
  ).toBe("2025-11-01");
  expect(
    screen.getByRole("button", { name: /申领系列/ }).textContent,
  ).toContain("续请官方四周均值");
  setRange();
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  const table = await screen.findByRole("table", {
    name: `${profile.label} 范围数据`,
  });
  const rows = within(table).getAllByRole("row").slice(1);
  expect(rows).toHaveLength(18);
  expect(
    rows.slice(0, 6).every((r) => r.textContent?.includes("2026-10-24")),
  ).toBe(true);
  expect(
    rows.slice(6, 12).every((r) => r.textContent?.includes("2026-10-17")),
  ).toBe(true);
  expect(table.textContent).toContain("200,007.25");
  expect(table.textContent).toContain("FRED提供方返回缺值");
  expect(table.textContent).not.toContain("官方空值");
  expect(table.textContent).toContain("本地未采集");
  expect(rows[0].textContent).toContain("材料embargo计划");
  expect(rows[6].textContent).toContain("无本周actual计划证据");
  expect(within(rows[4]).getAllByRole("cell")[5].textContent).toBe("—");
  const link = within(rows[0]).getAllByRole("link")[0];
  expect(link.getAttribute("href")).toContain("asset=mdf.macro.dol.claims");
  expect(link.getAttribute("href")).toContain(
    "release_event=dol-claims-2026-10-29",
  );
  fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
  expect(screen.getByLabelText("原始 HTTP 正文").textContent).toBe(
    JSON.stringify(fixture.query),
  );
  expect(f.calls.filter((p) => p.includes("/query?")).length).toBe(1);
});
it("forces historical local available cutoff and retains local values on source failure", async () => {
  const f = wire();
  render(
    <CpiTimeSeries
      profile={profile}
      availability={availability}
      transport={f.transport}
    />,
  );
  setRange();
  fireEvent.click(
    screen.getByRole("checkbox", {
      name: "允许本次当前查询临时补源（不写库）",
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  await screen.findByRole("table", { name: `${profile.label} 范围数据` });
  expect(
    screen.getAllByText("claims_fred_http_503", { exact: false }),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("checkbox", { name: "历史截止查询" }));
  expect(
    (screen.getByRole("button", { name: "查询数据" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  change("截止时间（北京时间）", "2026-11-01T02:00:00");
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  await screen.findByRole("table", { name: `${profile.label} 范围数据` });
  expect(f.calls.at(-1)).toContain("local_only=true");
  expect(f.calls.at(-1)).toContain("as_of_basis=available");
  expect(f.calls.at(-1)).toContain("%2B08%3A00");
});
it("uses strict native release locator filters and keeps unknown original priors", async () => {
  const f = wire();
  render(
    <CpiTimeSeries
      profile={profile}
      availability={availability}
      transport={f.transport}
      initialReleaseEvent="dol-claims-2026-10-29"
    />,
  );
  await screen.findByRole("table", { name: "发布事件列表" });
  const detail = screen.getByRole("region", { name: "发布事件详情" });
  expect(detail.textContent).toContain("官方原前值");
  expect(detail.textContent).toContain("本次报告前值");
  expect(detail.textContent).toContain("unknown（材料未说明）");
  expect(detail.textContent).toContain("未绑定（不以数值相等推断）");
  change("发布参考周（周六）", "2026-10-24");
  change("来源版本日期", "2026-10-29");
  fireEvent.click(screen.getByRole("button", { name: "读取发布记录" }));
  await flush();
  expect(f.calls.at(-1)).toContain("reference_week=2026-10-24");
  expect(f.calls.at(-1)).toContain("source_version=2026-10-29");
  expect(f.calls.at(-1)).not.toContain("reference_month");
});
it("consumes actual compact jobs/executions and editable calendar-year collection preset", async () => {
  const f = wire();
  expect(
    (await readCpiJobs(f.transport, undefined, profile)).jobs,
  ).toHaveLength(1);
  expect(
    (await readCpiExecutions(f.transport, undefined, profile)).executions,
  ).toHaveLength(1);
  expect(validStrategy(fixture.strategy, profile)).toBe(true);
  const writes: Record<string, string>[] = [];
  const transport: Transport = async (path, init) => {
    if (init?.method === "POST") {
      const b = JSON.parse(String(init.body));
      writes.push(b);
      return response(
        {
          ...fixture.jobs.jobs[0],
          ...b,
          job_id: b.request_id,
          state: "running",
          result: null,
          finished_at: null,
        },
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
  await flush();
  change("采集方式", "initialize");
  expect(
    (screen.getByLabelText("采集开始日期（截止周范围）") as HTMLInputElement)
      .value,
  ).toBe("2006-11-02");
  expect(screen.getByText(/20日历年，按后端/)).toBeTruthy();
  const button = screen.getByRole("button", {
    name: `提交 ${profile.label} 采集`,
  }) as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  change("采集开始日期（截止周范围）", "2026-10-10");
  fireEvent.click(
    screen.getByRole("checkbox", { name: "我确认将以上范围采集到开发测试库" }),
  );
  fireEvent.click(button);
  await flush();
  expect(writes).toHaveLength(1);
  expect(Object.keys(writes[0]).sort()).toEqual([
    "action",
    "end",
    "request_id",
    "start",
  ]);
  expect(writes[0].action).toBe("init");
});
it("shows null reference-week calendar as release plans and toggles strategy once without extra confirmation", async () => {
  let strategy = fixture.strategy;
  const writes: unknown[] = [];
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
  await screen.findByText(`${profile.label} 策略详情`);
  const plans = await screen.findByRole("list", {
    name: `${profile.label} 周频计划`,
  });
  expect(plans.textContent).toContain("参考周待发布材料确认");
  expect(plans.textContent).toContain("2026-11-05");
  expect(plans.textContent).not.toContain("月份");
  const source = screen.getByText("当前日历入口").parentElement!;
  expect(within(source).getByRole("link").getAttribute("href")).toBe(
    fixture.schedule.calendar.source,
  );
  expect(within(source).queryByRole("button")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "启动" }));
  await flush();
  expect(writes).toEqual([{ enabled: true }]);
  expect(screen.getByText("后台进程").parentElement!.textContent).toContain(
    "尚未启动",
  );
  fireEvent.click(screen.getByRole("button", { name: "暂停" }));
  await flush();
  expect(writes).toEqual([{ enabled: true }, { enabled: false }]);
});
it("adds Claims to common periodic/execution tables with its own weekly detail", async () => {
  const transport: Transport = async (path) =>
    response(
      String(path).endsWith("/executions")
        ? fixture.executions
        : fixture.strategy,
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
          execution_asset: keyOf(CLAIMS),
          asset: CLAIMS.identity,
          asset_revision: CLAIMS.revision,
          job: fixture.jobs.jobs[0].job_id,
        }),
      }}
      navigate={vi.fn()}
      transport={transport}
      generation={0}
      claimsAvailability={availability}
    />,
  );
  await screen.findByText("执行详情");
  expect(screen.getByText("关联资产").parentElement!.textContent).toContain(
    profile.label,
  );
  expect(screen.getByText(/周频覆盖/)).toBeTruthy();
  cleanup();
  render(
    <CpiStrategyWorkbench
      profile={CPI_PROFILE}
      transport={async (path) =>
        response(
          String(path).startsWith(profile.basePath)
            ? fixture.strategy
            : { ...fixture.strategy, history_start: "2006-11" },
        )
      }
      availability={availability}
      eighthProfile={profile}
      eighthAvailability={availability}
    />,
  );
  await flush();
  const table = within(
    screen.getByRole("region", { name: "周期任务" }),
  ).getByRole("table");
  expect(table.textContent).toContain("美国 CPI");
  expect(table.textContent).toContain(profile.label);
});
it("routes the Claims asset through App without another family query", async () => {
  const f = wire();
  location.hash = `#ui=r5&view=assets&asset=${CLAIMS.identity}&asset_revision=r1`;
  render(<App transport={f.transport} />);
  await screen.findByRole("heading", {
    name: /美国初请与续请失业金.*范围查询/,
  });
  setRange();
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  await screen.findByRole("table", { name: `${profile.label} 范围数据` });
  expect(
    f.calls
      .filter((p) => p.includes("/query?"))
      .every((p) => p.startsWith(profile.basePath)),
  ).toBe(true);
  fireEvent.click(screen.getByRole("tab", { name: "API说明" }));
  expect(screen.getByRole("heading", { name: "发布记录与定位" })).toBeTruthy();
  expect(
    screen.getByRole("button", { name: "复制Claims策略示例" }),
  ).toBeTruthy();
});

it("checks the actual registered Claims provider binding and permits its source collection route", async () => {
  expect(claimsOperationMatches(fixture.directory_entry)).toBe(true);
  const invalid = structuredClone(fixture.directory_entry);
  invalid.binding_contract.silent_fallback = true;
  expect(claimsOperationMatches(invalid)).toBe(false);
  invalid.binding_contract.silent_fallback = false;
  invalid.source_name = "BLS";
  expect(claimsOperationMatches(invalid)).toBe(false);
  const f = wire();
  location.hash = `#ui=r5&view=collection&asset=${CLAIMS.identity}&asset_revision=r1&operation=${CLAIMS_OPERATION}&tab=new`;
  render(<App transport={f.transport} />);
  await screen.findByLabelText("采集开始日期（截止周范围）");
  const target = screen.getByLabelText("采集目标") as HTMLSelectElement;
  expect(
    Array.from(target.options).filter((o) => o.value === keyOf(CLAIMS)),
  ).toHaveLength(1);
  expect(
    f.calls
      .filter((p) => p.includes("/timeseries/jobs"))
      .every((p) => p.startsWith(profile.basePath)),
  ).toBe(true);
});
it("selects each retained evidence version of the same Claims event independently", async () => {
  const f = wire();
  render(
    <CpiTimeSeries
      profile={profile}
      availability={availability}
      transport={f.transport}
      initialReleaseEvent="dol-claims-2026-10-29"
    />,
  );
  await screen.findByRole("table", { name: "发布事件列表" });
  change("发布读取类型", "source_history");
  fireEvent.click(screen.getByRole("button", { name: "读取发布记录" }));
  await flush();
  const table = screen.getByRole("table", { name: "发布事件列表" });
  const buttons = within(table).getAllByRole("button");
  expect(buttons).toHaveLength(2);
  fireEvent.click(buttons[1]);
  expect(
    screen.getByRole("region", { name: "发布事件详情" }).textContent,
  ).toContain("synthetic-prior-parser");
  fireEvent.click(buttons[0]);
  expect(
    screen.getByRole("region", { name: "发布事件详情" }).textContent,
  ).not.toContain("synthetic-prior-parser");
});

it("cancels a pending Claims supplement when source permission changes and restores a local preview", async () => {
  let signal: AbortSignal | undefined;
  const transport: Transport = async (_path, init) => {
    signal = init?.signal as AbortSignal;
    return await new Promise<Response>(() => {});
  };
  const view = render(
    <CpiTimeSeries
      profile={profile}
      availability={availability}
      transport={transport}
    />,
  );
  setRange();
  fireEvent.click(
    screen.getByRole("checkbox", {
      name: "允许本次当前查询临时补源（不写库）",
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  await flush();
  expect(signal?.aborted).toBe(false);
  view.rerender(
    <CpiTimeSeries
      profile={profile}
      availability={{
        ...availability,
        data: { ...availability.data!, source_allowed: false },
      }}
      transport={transport}
    />,
  );
  await flush();
  expect(signal?.aborted).toBe(true);
  const toggle = screen.getByRole("checkbox", {
    name: "允许本次当前查询临时补源（不写库）",
  }) as HTMLInputElement;
  expect(toggle.disabled).toBe(true);
  expect(toggle.checked).toBe(false);
  expect(
    screen
      .getAllByLabelText("请求 URL")
      .every((e) => e.textContent?.includes("local_only=true")),
  ).toBe(true);
});
