import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  CpiStrategyWorkbench,
  latestRealExecution,
} from "./CpiStrategyWorkbench";
import { CpiQueryApiHelp } from "./CpiQueryDetails";
import {
  readCpiExecutions,
  type CpiExecution,
  type CpiAvailability,
} from "./cpiTimeSeriesApi";
import {
  CPI_PROFILE,
  PPI_PROFILE,
  PCE_PROFILE,
  EMPLOYMENT_PROFILE,
  RETAIL_PROFILE,
  CONSUMPTION_PROFILE,
  GDP_PROFILE,
  CLAIMS_PROFILE,
  type MacroProfile,
} from "./macroTimeSeriesProfile";
const profiles = [
  CPI_PROFILE,
  PPI_PROFILE,
  PCE_PROFILE,
  EMPLOYMENT_PROFILE,
  RETAIL_PROFILE,
  CONSUMPTION_PROFILE,
  GDP_PROFILE,
  CLAIMS_PROFILE,
];
const native = Object.fromEntries(
  ["gdp", "claims"].map((id) => [
    id,
    JSON.parse(
      readFileSync(
        resolve(process.cwd(), `../tests/fixtures/${id}_ui_http.json`),
        "utf8",
      ),
    ),
  ]),
);
const availability: CpiAvailability = {
  data: {
    latest_months: {},
    observation_count: 0,
    revision_count: 0,
    batch_count: 0,
    web_capabilities: { query: true, collect: true, reason: null },
  },
  busy: false,
  legacy: false,
  failure: null,
};
const response = (value: unknown) => new Response(JSON.stringify(value));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
function record(
  profile: MacroProfile,
  automatic = false,
  newer = false,
): CpiExecution {
  const source = native[profile.id]?.executions.executions[0];
  const at = newer ? "2026-11-03T02:00:00Z" : "2026-11-02T02:00:00Z";
  const id = newer
    ? "c1400000-0000-4000-8000-000000000002"
    : "c1400000-0000-4000-8000-000000000001";
  return {
    ...(source ?? {}),
    id,
    job_id: id,
    request_id: id,
    asset: profile.id,
    origin: automatic
      ? ["gdp", "claims"].includes(profile.id)
        ? "schedule"
        : "automatic"
      : "manual",
    action: "update",
    stage: automatic ? "target" : null,
    start: source?.start ?? "2026-09",
    end: source?.end ?? "2026-09",
    created_at: at,
    started_at: at,
    finished_at: at,
    state: "complete",
    classification: null,
    result: source?.result ?? {
      saved_points: 13,
      saved_revisions: 2,
      provider_requests: 3,
      provider_attempts: 4,
      coverage_complete: true,
    },
  } as CpiExecution;
}
const cases = [
  "manual",
  "automatic",
  "none",
  "failed",
  "running",
  "cancelled",
  "accepted",
  "nested",
] as const;
for (const profile of profiles)
  it.each(cases)(
    `${profile.id} latest real execution: %s`,
    async (scenario) => {
      const older = record(profile, true),
        recent = record(profile, scenario === "automatic", true);
      let records: CpiExecution[] =
        scenario === "none"
          ? []
          : scenario === "automatic"
            ? [recent, record(profile)]
            : scenario === "manual"
              ? [record(profile), recent]
              : [recent];
      if (scenario === "failed") {
        recent.state = "failed";
        recent.classification = "source_failed";
        recent.result = null;
      }
      if (scenario === "running") {
        recent.state = "running";
        recent.finished_at = null;
        recent.result = null;
      }
      if (scenario === "cancelled") {
        recent.state = "cancelled";
        recent.result = null;
      }
      if (scenario === "accepted") {
        recent.state = "running";
        recent.started_at = null;
        recent.finished_at = null;
        recent.result = null;
      }
      if (scenario === "nested") {
        // Native job contracts retain direct completion evidence; monthly automatic
        // stages can wrap job results and carry independent scheduler statistics.
        if (!native[profile.id])
          recent.result = {
            scheduler_attempts_total: 9,
            result: { result: recent.result },
          };
      }
      const transport = vi.fn(async (input: unknown, init?: RequestInit) => {
        expect(init?.method ?? "GET").toBe("GET");
        expect(String(input).startsWith(profile.basePath)).toBe(true);
        return response(
          String(input).endsWith("/executions")
            ? { executions: records }
            : String(input).endsWith("/schedule")
              ? { calendar_status: "fresh", targets: [] }
              : {
                  exists: true,
                  enabled: true,
                  state: "enabled",
                  history_start:
                    profile.id === "claims"
                      ? "2006-11-02"
                      : profile.id === "gdp"
                        ? "2006-Q4"
                        : "2006-10",
                  worker: { state: "idle", last_heartbeat_at: null },
                  next_due: null,
                  last_result: { state: "idle", saved_points: 12345 },
                  policy: {},
                },
        );
      });
      render(
        <CpiStrategyWorkbench
          transport={transport}
          profile={profile}
          availability={availability}
          selected
        />,
      );
      const summary = await screen.findByRole("region", {
        name: "最近真实执行摘要",
      });
      if (scenario === "none" || scenario === "accepted") {
        await within(summary).findByText(/无真实执行记录/);
        expect(within(summary).queryByRole("link")).toBeNull();
      } else {
        await within(summary).findByText(/本资产最近执行来源/);
        expect(summary.textContent).toContain(
          scenario === "automatic" ? "自动策略" : "手动采集",
        );
        if (scenario !== "automatic")
          expect(summary.textContent).toContain("自动策略尚无真实执行记录");
        expect(
          within(summary).queryByRole("heading", { name: "执行详情" }),
        ).toBeNull();
        expect(summary.querySelector(".cpi-coverage")).toBeNull();
        expect(summary.textContent).not.toContain("周频覆盖");
        expect(
          within(summary).getByText("范围").parentElement?.textContent,
        ).toContain(recent.start!);
        const url = new URLSearchParams(
          within(summary)
            .getByRole("link", { name: "打开对应执行详情" })
            .getAttribute("href")!
            .slice(1),
        );
        expect(url.get("job")).toBe(recent.id);
        expect(url.get("asset")).toBe(profile.asset.identity);
        expect(url.get("tab")).toBe("executions");
        expect(url.get("execution_schedule")).toBe("all");
        const savedLabel = "保存观测版本（含修订）";
        if (["failed", "running", "cancelled"].includes(scenario)) {
          expect(summary.textContent).toContain(
            { failed: "失败", running: "执行中", cancelled: "已取消" }[
              scenario as "failed" | "running" | "cancelled"
            ]!,
          );
          expect(within(summary).queryByText(savedLabel)).toBeNull();
        } else {
          const saved = within(summary).getByText(savedLabel).parentElement;
          expect(saved?.textContent).toContain(
            String(
              native[profile.id]?.executions.executions[0].result
                .saved_points ?? 13,
            ),
          );
          if (profile.id === "claims")
            expect(within(summary).queryByText("新增观测")).toBeNull();
        }
        if (scenario === "nested" && !native[profile.id])
          expect(
            within(summary).getByText("累计调度尝试").parentElement
              ?.textContent,
          ).toContain("9");
      }
      expect(summary.textContent).not.toContain("12345");
      expect(screen.getByText("策略上次执行回执与审计")).toBeTruthy();
    },
  );
it.each(profiles)(
  "%s rejects a cross-asset execution response",
  async (profile) => {
    const e = record(profile) as CpiExecution & { asset: string };
    e.asset = profile.id === "claims" ? "gdp" : "claims";
    await expect(
      readCpiExecutions(
        async () => response({ executions: [e] }),
        undefined,
        profile,
      ),
    ).rejects.toThrow("cpi_executions_unknown");
  },
);
it.each(profiles)(
  "%s shares help structure with native period semantics",
  (profile) => {
    render(
      <CpiQueryApiHelp
        profile={profile}
        availability={availability}
        request={{
          method: "GET",
          path: profile.basePath + "/query?local_only=false",
        }}
      />,
    );
    for (const title of [
      "返回字段、类型与单位",
      "来源与官方计算",
      "成功示例（字段节选，非本次响应）",
      "失败示例与诊断",
      "采集与策略接口",
    ])
      expect(screen.getByText(title)).toBeTruthy();
    if (native[profile.id]) {
      const body = screen.getByRole("region", {
        name: `${profile.label} API说明`,
      }).textContent!;
      expect(
        screen.getByRole("table", { name: "查询参数" }).textContent,
      ).not.toContain("YYYY-MM；");
      expect(body).not.toContain("暂未开放");
      expect(body).toContain("true（API默认）");
      expect(body).toContain("当前表单显式 false");
      expect(body).toContain(profile.id === "gdp" ? "YYYY-Qn" : "YYYY-MM-DD");
    }
  },
);
it("orders real executions by timestamps rather than array order and ignores heartbeats", () => {
  const a = record(CPI_PROFILE),
    b = record(CPI_PROFILE, true, true);
  expect(latestRealExecution([a, b])?.id).toBe(b.id);
  expect(latestRealExecution([{ ...b, state: "idle" }, a])?.id).toBe(a.id);
  expect(
    latestRealExecution([{ ...b, state: "pending", started_at: null }]),
  ).toBeUndefined();
  expect(
    latestRealExecution([{ ...b, state: "failed", started_at: null }])?.id,
  ).toBe(b.id);
});

it.each(profiles)(
  "%s labels a newer manual run while preserving an older automatic run",
  async (profile) => {
    const automatic = record(profile, true),
      manual = record(profile, false, true);
    const transport = async (input: unknown) =>
      response(
        String(input).endsWith("/executions")
          ? { executions: [automatic, manual] }
          : String(input).endsWith("/schedule")
            ? { calendar_status: "fresh", targets: [] }
            : {
                exists: true,
                enabled: true,
                state: "enabled",
                history_start:
                  profile.id === "claims"
                    ? "2006-11-02"
                    : profile.id === "gdp"
                      ? "2006-Q4"
                      : "2006-10",
                worker: { state: "idle", last_heartbeat_at: null },
                next_due: null,
                last_result: automatic,
                policy: {},
              },
      );
    render(
      <CpiStrategyWorkbench
        profile={profile}
        transport={transport}
        availability={availability}
        selected
      />,
    );
    const summary = await screen.findByRole("region", {
      name: "最近真实执行摘要",
    });
    await within(summary).findByText(/本资产最近执行来源：手动采集/);
    expect(within(summary).getByRole("link").getAttribute("href")).toContain(
      manual.id,
    );
    expect(summary.textContent).not.toContain(manual.id);
    expect(summary.textContent).not.toContain("自动策略尚无真实执行记录");
    expect(latestRealExecution([manual, automatic])?.id).toBe(manual.id);
    expect(
      screen.getByText("策略上次执行回执与审计").parentElement?.textContent,
    ).toContain("最近执行状态");
  },
);

it("documents the actual Claims nested provenance rather than inventing flat row fields", () => {
  const row = native.claims.query.rows[0];
  expect(row.provenance).toHaveProperty("origin");
  expect(row).not.toHaveProperty("origin");
  render(
    <CpiQueryApiHelp
      profile={CLAIMS_PROFILE}
      availability={availability}
      request={{
        method: "GET",
        path: CLAIMS_PROFILE.basePath + "/query?local_only=false",
      }}
    />,
  );
  const fields = screen.getByRole("table", {
    name: "返回字段",
    hidden: true,
  }).textContent!;
  expect(fields).toContain("rows[].provenance.origin");
  expect(fields).not.toContain("rows[].origin");
});

it.each(profiles)(
  "%s does not report empty or completed data when execution reading fails",
  async (profile) => {
    const transport = async (input: unknown) =>
      String(input).endsWith("/executions")
        ? new Response(
            JSON.stringify({ classification: "storage_unavailable" }),
            { status: 503 },
          )
        : response(
            String(input).endsWith("/schedule")
              ? { calendar_status: "fresh", targets: [] }
              : {
                  exists: true,
                  enabled: true,
                  state: "enabled",
                  history_start:
                    profile.id === "claims"
                      ? "2006-11-02"
                      : profile.id === "gdp"
                        ? "2006-Q4"
                        : "2006-10",
                  worker: { state: "idle", last_heartbeat_at: null },
                  next_due: null,
                  last_result: null,
                  policy: {},
                },
          );
    render(
      <CpiStrategyWorkbench
        profile={profile}
        transport={transport}
        availability={availability}
        selected
      />,
    );
    const summary = await screen.findByRole("region", {
      name: "最近真实执行摘要",
    });
    await within(summary).findByText(
      "执行记录暂不可读，无法确认最近真实执行。",
    );
    expect(summary.textContent).not.toContain("无真实执行记录");
    expect(summary.textContent).not.toContain("已完成");
    expect(within(summary).queryByText("新增观测")).toBeNull();
    expect(within(summary).queryByRole("link")).toBeNull();
  },
);
