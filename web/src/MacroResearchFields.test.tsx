import { afterEach, expect, it } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { asgi, closeBridges } from "./workflowTestBridge";
import {
  CPI_PROFILE,
  PPI_PROFILE,
  PCE_PROFILE,
  EMPLOYMENT_PROFILE,
  RETAIL_PROFILE,
  type MacroProfile,
} from "./macroTimeSeriesProfile";
import { CpiTimeSeries } from "./CpiTimeSeries";
import {
  buildCpiQueryRequest,
  queryCpiRange,
  type CpiAvailability,
  type CpiScope,
} from "./cpiTimeSeriesApi";
import type { Transport } from "./api";

afterEach(() => {
  cleanup();
  closeBridges();
  localStorage.clear();
});
const scope = (basket: string): CpiScope => ({
  start: "2026-09",
  end: "2026-09",
  basket,
  adjustment: "sa",
  measure: "index",
  query_mode: "current",
});
const availability = (profile: MacroProfile): CpiAvailability => ({
  busy: false,
  failure: null,
  legacy: false,
  data: {
    observation_count: 0,
    revision_count: 0,
    batch_count: 0,
    status: "OK",
    schema: "synthetic",
    asset_id: profile.asset,
    host_binding: "synthetic",
    store_revision: null,
    latest_months: Object.fromEntries(
      profile.series.map(([id]) => [id, "2026-09"]),
    ),
    web_capabilities: { query: true, collect: false, reason: null },
    source: {
      capability_enabled: true,
      credential_state: "configured",
      credential_usable: true,
    },
  } as CpiAvailability["data"],
});

it("retains the original query defaults while every approved basket and employment series is selectable", () => {
  expect(CPI_PROFILE.series).toHaveLength(14);
  expect(PPI_PROFILE.series).toHaveLength(6);
  expect(EMPLOYMENT_PROFILE.series).toHaveLength(18);
  const request = buildCpiQueryRequest(
    { ...scope("all"), series: undefined },
    EMPLOYMENT_PROFILE,
  );
  expect(
    new URL(request.path, "http://fixture").searchParams
      .get("series")
      ?.split(","),
  ).toEqual(EMPLOYMENT_PROFILE.defaultQuerySeries);
  render(
    <CpiTimeSeries
      profile={CPI_PROFILE}
      availability={availability(CPI_PROFILE)}
      transport={async () => {
        throw Error("no implicit query");
      }}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "统计篮子" }));
  const options = within(screen.getByRole("group", { name: "统计篮子选项" }));
  expect(options.getAllByRole("checkbox")).toHaveLength(7);
  expect(options.getByRole("checkbox", { name: "居住" })).toHaveProperty(
    "checked",
    false,
  );
  expect(screen.getAllByText(/短期研究使用季调/)[0].textContent).toContain(
    "年度研究使用未季调",
  );
});

it.each([
  [CPI_PROFILE, "shelter,energy", undefined, "居住 · 季调"],
  [PPI_PROFILE, "core_ex_trade", undefined, "初步值（官方脚注）"],
  [PCE_PROFILE, "all", undefined, "3月年化变化"],
  [
    EMPLOYMENT_PROFILE,
    "all",
    "CES0500000001,LNS12300000,CES4000000001",
    "就业人口比",
  ],
  [RETAIL_PROFILE, "ex_auto", undefined, "除汽车（MDF推导）"],
] as const)(
  "consumes real Python HTTP research fields for %s",
  async (profile, basket, series, expected) => {
    const bridge = asgi("research", "macro_research_ui_exchange.py");
    let text = "";
    const transport: Transport = async (path, init) => {
      const wire = await bridge.exchange({
        path: String(path),
        method: init?.method ?? "GET",
      });
      text = wire.text;
      return new Response(wire.text, { status: wire.status });
    };
    const result = await queryCpiRange(
      transport,
      { ...scope(basket), ...(series ? { series } : {}) },
      new AbortController().signal,
      undefined,
      profile,
    );
    expect(result.points.length).toBeGreaterThan(0);
    if (profile.id === "pce")
      expect(result.points[0].annualized_3m_ratio).toBeTruthy();
    if (profile.id === "ppi")
      expect(result.points[0].provider_status?.preliminary).toBe(true);
    // Render the actual shared form and choose the same scope through its controls.
    render(
      <CpiTimeSeries
        profile={profile}
        transport={transport}
        availability={availability(profile)}
      />,
    );
    fireEvent.change(screen.getByLabelText("开始月份"), {
      target: { value: "2026-09" },
    });
    fireEvent.click(
      screen.getByRole("button", {
        name: profile.id === "employment" ? "就业指标" : "统计篮子",
      }),
    );
    const options = within(
      screen.getByRole("group", {
        name: profile.id === "employment" ? "就业指标选项" : "统计篮子选项",
      }),
    );
    fireEvent.click(options.getByRole("button", { name: "清空" }));
    const selected =
      profile.id === "employment" ? series!.split(",") : basket.split(",");
    for (const [id, title, b] of profile.series) {
      if (
        profile.id === "employment"
          ? selected.includes(id)
          : basket === "all" || selected.includes(b)
      ) {
        const label =
          profile.id === "employment" ? title : title.split(" · ")[0];
        const checkbox = options.getByRole("checkbox", { name: label });
        if (!(checkbox as HTMLInputElement).checked) fireEvent.click(checkbox);
      }
    }
    if (profile.id === "pce") {
      fireEvent.click(screen.getByRole("button", { name: "显示指标" }));
      fireEvent.click(screen.getByRole("checkbox", { name: "3月年化变化" }));
    }
    fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
    const table = await screen.findByRole("table", {
      name: `${profile.label} 范围数据`,
    });
    expect(table.textContent).toContain(expected);
    if (profile.id === "employment")
      expect(table.textContent).toContain("千岗位");
    const raw = text;
    fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
    expect(screen.getByLabelText("原始 HTTP 正文").textContent).toBe(raw);
    // Server projects columns too; UI parser must accept exactly that projection.
    const columns = profile.id === "employment" ? "value" : "index_value";
    const projected = await queryCpiRange(
      transport,
      { ...scope(basket), ...(series ? { series } : {}), columns },
      new AbortController().signal,
      undefined,
      profile,
    );
    expect(
      projected.points.every(
        (p) => p.change_3m_ratio === undefined || profile.id === "retail",
      ),
    ).toBe(true);
  },
);

it.each([CPI_PROFILE, PPI_PROFILE, EMPLOYMENT_PROFILE])(
  "the shared collection form can backfill only new %s inputs",
  async (profile) => {
    const { CpiTimeSeriesCollection } = await import(
      "./CpiTimeSeriesCollection"
    );
    let submitted: { series?: string } | null = null;
    const status = availability(profile);
    status.data!.web_capabilities.collect = true;
    const transport: Transport = async (path, init) => {
      if (init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        submitted = body;
        return new Response(
          JSON.stringify({
            ...body,
            job_id: "synthetic-research",
            state: "complete",
            created_at: "2026-10-31T00:00:00Z",
            started_at: null,
            finished_at: null,
            classification: null,
            result: null,
          }),
        );
      }
      return new Response(JSON.stringify({ jobs: [] }));
    };
    render(
      <CpiTimeSeriesCollection
        profile={profile}
        transport={transport}
        availability={status}
        tab="new"
      />,
    );
    fireEvent.change(screen.getByLabelText("采集系列范围"), {
      target: { value: "research" },
    });
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "我确认将以上范围采集到开发测试库",
      }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: `提交 ${profile.label} 采集` }),
    );
    await screen.findByText("已收到任务状态，正在自动跟踪同一请求。");
    const ids = (submitted as { series: string } | null)?.series.split(",");
    expect(ids).toEqual(
      profile.series
        .filter(([id]) => !profile.defaultQuerySeries?.includes(id))
        .map(([id]) => id),
    );
  },
);
