import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
  waitFor,
} from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import App from "./App";
import { CpiTimeSeries } from "./CpiTimeSeries";
import { GDP_PROFILE as profile } from "./macroTimeSeriesProfile";
import { GDP } from "./workflowModel";
import {
  previousCompleteQuarter,
  quarterRangeValid,
  shiftQuarter,
} from "./QuarterField";
import {
  buildGdpQueryRequest,
  normalizeGdpResult,
  readGdpStatus,
  type GdpResult,
  type GdpStatus,
} from "./gdpTimeSeriesAdapter";
import {
  normalizeGdpReleases,
  type GdpReleaseResponse,
} from "./gdpReleaseAdapter";
import type { CpiAvailability, CpiScope } from "./cpiTimeSeriesApi";
import type { Transport } from "./api";

// Real router/JSONResponse serialization of synthetic in-memory inputs only.
const fixture = JSON.parse(
  readFileSync(
    resolve(process.cwd(), "../tests/fixtures/gdp_ui_http.json"),
    "utf8",
  ),
) as {
  status: GdpStatus;
  query: GdpResult;
  history: GdpResult;
  releases: GdpReleaseResponse;
  source_history: GdpReleaseResponse;
};
const scope: CpiScope = {
  start: "2026-Q2",
  end: "2026-Q3",
  series: profile.series.map(([id]) => id).join(","),
  basket: "all",
  adjustment: "sa",
  measure: "native",
};
const availability: CpiAvailability = {
  legacy: false,
  busy: false,
  failure: null,
  data: { ...fixture.status, latest_months: {}, initialization: undefined },
};
const response = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status });
function wire() {
  const calls: string[] = [];
  const transport: Transport = async (input) => {
    const path = String(input);
    calls.push(path);
    const url = new URL(path, "http://fixture.invalid");
    if (url.pathname === profile.basePath + "/status")
      return response(fixture.status);
    if (url.pathname === profile.basePath + "/query")
      return response(
        url.searchParams.get("query_mode") === "history"
          ? fixture.history
          : fixture.query,
      );
    if (url.pathname === profile.basePath + "/releases")
      return response(
        url.searchParams.get("read_mode") === "source_history"
          ? fixture.source_history
          : fixture.releases,
      );
    if (url.pathname === "/api/r5/packages")
      return response({
        status: "OK",
        packages: [
          {
            package_id: "macro",
            name: "宏观",
            history_completeness: "UNKNOWN",
            capabilities: [{ title: "美国 GDP 与增长", definition_ref: GDP }],
            assets: [
              {
                identity: GDP.identity,
                revisions: [
                  {
                    definition_ref: GDP,
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
      return response({ status: "OK", entries: [], instances: [] });
    return response({ classification: "not_enabled" }, 503);
  };
  return { calls, transport };
}
function change(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}
function setRange() {
  change("开始季度", scope.start);
  change("结束季度", scope.end);
}
afterEach(() => {
  cleanup();
  location.hash = "";
  vi.restoreAllMocks();
});

it("uses native quarters across year boundaries and anchors before the current UTC quarter", () => {
  expect(shiftQuarter("2026-Q1", -1)).toBe("2025-Q4");
  expect(shiftQuarter("2026-Q4", 1)).toBe("2027-Q1");
  expect(previousCompleteQuarter(new Date("2026-01-01T00:00:00Z"))).toBe(
    "2025-Q4",
  );
  expect(quarterRangeValid("2026-01", "2026-03")).toBe(false);
  expect(quarterRangeValid("2026-Q4", "2026-Q3")).toBe(false);
});
it("builds GDP-only parameters and forces local reads with an aware historical cutoff", async () => {
  const params = new URL(buildGdpQueryRequest(scope).path, "http://fixture")
    .searchParams;
  expect([...params.keys()].sort()).toEqual([
    "end",
    "local_only",
    "query_mode",
    "series",
    "start",
  ]);
  expect(params.get("local_only")).toBe("true");
  const history = buildGdpQueryRequest({
    ...scope,
    query_mode: "history",
    as_of: "2026-11-02T19:00:00+08:00",
  });
  expect(history.path).toContain("%2B08%3A00");
  expect(history.path).toContain("as_of_basis=available");
  const status = await readGdpStatus(
    wire().transport,
    new AbortController().signal,
  );
  expect(status.latest_quarters?.A191RX).toBe("2026-Q3");
  expect(status.latest_months).toEqual({});
});
it("adapts actual GDP bodies without monthly fields or fabricated estimate bindings", () => {
  const result = normalizeGdpResult(fixture.query, scope);
  expect(result.points).toHaveLength(16);
  expect(
    result.points.find(
      (p) => p.period === "2026-Q3" && p.series_id === "A191RL",
    )?.value,
  ).toBe("3.4");
  expect(
    result.points.find(
      (p) => p.period === "2026-Q3" && p.series_id === "A019RY",
    )?.value,
  ).toBe("-0.5");
  expect(
    result.points.find(
      (p) => p.period === "2026-Q2" && p.series_id === "A014RY",
    )?.missing_reason,
  ).toBe("official_null");
  expect(result.points.every((p) => !("month" in p))).toBe(true);
  expect(
    normalizeGdpReleases(fixture.releases)
      .events.map((e) => e.estimate_kind)
      .sort(),
  ).toEqual(["second", "third"]);
  expect(
    normalizeGdpReleases(fixture.source_history).events.every(
      (e) => e.reference_quarter === "2026-Q2",
    ),
  ).toBe(true);
});
it.each([
  "unit",
  "duplicate",
  "monthly",
  "official-ratio",
  "history",
  "missing-point",
])("rejects incompatible GDP contract: %s", (kind) => {
  const changed = structuredClone(fixture.query);
  if (kind === "unit") changed.points[0].value_unit = "percent";
  if (kind === "duplicate") changed.points[1] = changed.points[0];
  if (kind === "monthly") changed.points[0].quarter = "2026-07";
  if (kind === "official-ratio") changed.points[2].change_1q_ratio = "0.034";
  if (kind === "history") changed.query_mode = "history";
  if (kind === "missing-point") changed.points.pop();
  expect(() => normalizeGdpResult(changed, scope)).toThrow(
    "gdp_result_contract_invalid",
  );
});
it("shares the query table and raw exchange, with descending quarters and native units", async () => {
  const f = wire();
  render(
    <CpiTimeSeries
      transport={f.transport}
      profile={profile}
      availability={availability}
    />,
  );
  expect((screen.getByLabelText("结束季度") as HTMLInputElement).value).toBe(
    previousCompleteQuarter(),
  );
  expect((screen.getByLabelText("开始季度") as HTMLInputElement).value).toBe(
    shiftQuarter(previousCompleteQuarter(), -7),
  );
  expect(screen.queryByLabelText("开始月份")).toBeNull();
  setRange();
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  const table = await screen.findByRole("table", {
    name: "GDP与增长 范围数据",
  });
  const rows = within(table).getAllByRole("row").slice(1);
  expect(rows).toHaveLength(16);
  expect(
    rows.slice(0, 8).every((row) => row.textContent?.includes("2026-Q3")),
  ).toBe(true);
  expect(
    rows.slice(8).every((row) => row.textContent?.includes("2026-Q2")),
  ).toBe(true);
  expect(rows[0].textContent).toContain("24,000,000.123");
  expect(rows[2].textContent).toContain("3.4%");
  expect(rows[2].textContent).not.toContain("340");
  expect(rows[5].textContent).toContain("0 百分点");
  expect(rows[7].textContent).toContain("-0.5 百分点");
  expect(rows[13].textContent).toContain("官方空值");
  expect(
    within(rows[2])
      .getAllByRole("cell")
      .slice(3, 5)
      .every((cell) => cell.textContent === "—"),
  ).toBe(true);
  expect(table.textContent).toContain("second");
  expect(table.textContent).toContain("2026-11-25 21:30:00");
  fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
  expect(screen.getByLabelText("原始 HTTP 正文").textContent).toBe(
    JSON.stringify(fixture.query),
  );
  expect(screen.getByRole("button", { name: "复制原始响应" })).toBeTruthy();
  fireEvent.click(screen.getByRole("tab", { name: "表格" }));
  expect(f.calls.filter((path) => path.includes("/query?")).length).toBe(1);
});
it("requires a Beijing cutoff and displays historical missing records without upstream requests", async () => {
  const f = wire();
  render(
    <CpiTimeSeries
      transport={f.transport}
      profile={profile}
      availability={availability}
    />,
  );
  setRange();
  fireEvent.click(screen.getByRole("checkbox", { name: "历史截止查询" }));
  const button = screen.getByRole("button", {
    name: "查询数据",
  }) as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  change("截止时间（北京时间）", "2026-11-02T19:00:00");
  fireEvent.click(button);
  const table = await screen.findByRole("table", {
    name: "GDP与增长 范围数据",
  });
  expect(table.textContent).toContain("本地尚无记录");
  expect(table.textContent).not.toContain("3.4%");
  const path = f.calls.find((p) => p.includes("/query?"))!;
  expect(path).toContain("local_only=true");
  expect(path).toContain("%2B08%3A00");
});
it("opens quarterly estimate evidence and retains a working source-history filter", async () => {
  const f = wire();
  const third = fixture.releases.events.find(
    (e) => e.estimate_kind === "third",
  )!;
  render(
    <CpiTimeSeries
      transport={f.transport}
      profile={profile}
      availability={availability}
      initialReleaseEvent={third.event_id}
    />,
  );
  const list = await screen.findByRole("table", { name: "发布事件列表" });
  expect(list.textContent).toContain("2026-Q2 · third");
  expect(list.textContent).toContain("2026-Q2 · second");
  const detail = screen.getByRole("region", { name: "发布事件详情" });
  expect(detail.textContent).toContain("同季度前一次估计");
  expect(detail.textContent).toContain("0.7 百分点");
  expect(detail.textContent).toContain("未知（未获足够证据）");
  expect(detail.textContent).toContain("上季度修订前值");
  change("发布参考季度", "2026-Q2");
  change("发布读取类型", "source_history");
  fireEvent.click(screen.getByRole("button", { name: "读取发布记录" }));
  await screen.findByRole("table", { name: "发布事件列表" });
  expect(f.calls.at(-1)).toContain("reference_quarter=2026-Q2");
  expect(f.calls.at(-1)).toContain("read_mode=source_history");
  expect(f.calls.at(-1)).not.toContain("reference_month");
});
it("provides native parameter, field, unit, release and error documentation", () => {
  render(
    <CpiTimeSeries
      transport={wire().transport}
      profile={profile}
      availability={availability}
    />,
  );
  fireEvent.click(screen.getByRole("tab", { name: "API说明" }));
  const help = screen.getByRole("region", { name: "GDP与增长 API说明" });
  for (const name of ["查询参数", "返回字段", "错误响应"])
    expect(
      within(help).getByRole("table", { name, hidden: true }),
    ).toBeTruthy();
  for (const text of [
    "start_quarter",
    "percentage_points",
    "previous_estimate",
    "not_covered_by_summary_adapter",
    "gdp_timeseries_not_enabled",
    "默认",
    "true",
  ])
    expect(help.textContent).toContain(text);
});
it("routes the runtime GDP asset through shared compact/expanded panes and its own collection form", async () => {
  location.hash = `ui=r5&view=assets&asset=${GDP.identity}&asset_revision=r1`;
  const f = wire();
  render(<App transport={f.transport} />);
  await screen.findByLabelText("开始季度");
  fireEvent.click(screen.getByRole("button", { name: "展开窗体" }));
  expect(document.querySelector(".workbench.cpi-asset-expanded")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "收起窗体" }));
  expect(document.querySelector(".workbench.cpi-asset-expanded")).toBeNull();
  expect(f.calls.some((p) => p.includes("/cpi/timeseries"))).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "采集数据" }));
  expect(await screen.findByLabelText("采集开始季度")).toBeTruthy();
  expect(new URLSearchParams(location.hash.slice(1)).get("asset")).toBe(
    GDP.identity,
  );
  expect(f.calls.some((p) => p.includes("/cpi/timeseries"))).toBe(false);
});

it("waits for a delayed asset directory before mounting an editable GDP query", async () => {
  location.hash = `ui=r5&view=assets&asset=${GDP.identity}&asset_revision=r1`;
  const f = wire();
  let release!: () => void;
  const directory = new Promise<void>((resolve) => {
    release = resolve;
  });
  const transport: Transport = async (path, init) => {
    if (String(path) === "/api/r5/packages") await directory;
    return f.transport(path, init);
  };
  render(<App transport={transport} />);
  await waitFor(() => expect(f.calls).toContain(profile.basePath + "/status"));
  expect(screen.queryByLabelText("开始季度")).toBeNull();
  release();
  await screen.findByLabelText("开始季度");
  setRange();
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  await screen.findByRole("table", { name: "GDP与增长 范围数据" });
  expect((screen.getByLabelText("开始季度") as HTMLInputElement).value).toBe(
    scope.start,
  );
});

it("requests a native series subset and validates its own quarterly coverage", () => {
  const subsetScope = { ...scope, series: "A191RL,A019RY" };
  const subset = structuredClone(fixture.query);
  subset.series_ids = ["A191RL", "A019RY"];
  subset.points = subset.points.filter((point) =>
    subset.series_ids.includes(point.series_id),
  );
  subset.coverage = { expected: 4, available: 4, official_null: 0, missing: 0 };
  subset.status = "complete";
  expect(normalizeGdpResult(subset, subsetScope).points).toHaveLength(4);
  expect(
    new URL(
      buildGdpQueryRequest(subsetScope).path,
      "http://fixture",
    ).searchParams.get("series"),
  ).toBe("A191RL,A019RY");
});

it("keeps a disabled GDP host unavailable without probing a monthly provider", async () => {
  const calls: string[] = [];
  const transport: Transport = async (path) => {
    calls.push(String(path));
    return response(
      { status: "ERROR", classification: "gdp_timeseries_not_enabled" },
      503,
    );
  };
  await expect(
    readGdpStatus(transport, new AbortController().signal),
  ).rejects.toThrow("gdp_timeseries_not_enabled");
  expect(calls).toEqual([profile.basePath + "/status"]);
});
