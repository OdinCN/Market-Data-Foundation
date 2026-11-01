import { afterEach, expect, it } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { CpiTimeSeries } from "./CpiTimeSeries";
import { CpiOrdinaryQuery } from "./CpiOrdinaryQuery";
import { CPI } from "./workflowModel";
import {
  CPI_PROFILE,
  PPI_PROFILE,
  PCE_PROFILE,
} from "./macroTimeSeriesProfile";
import type { CpiAvailability } from "./cpiTimeSeriesApi";

afterEach(cleanup);

it("legacy ordinary results keep same-month measure order across a year boundary", async () => {
  const measurements = ["2025-12", "2026-01", "2025-12", "2026-01"].map(
    (reference_period, index) => ({
      measure_identity: `metric-${index}`,
      name: `metric-${index}`,
      reference_period,
      value: "1.25",
      unit: "percent",
    }),
  );
  const original = JSON.stringify(measurements);
  render(
    <CpiOrdinaryQuery
      initialReference="2026-01"
      operationId={null}
      entries={[]}
      readExact={() => {}}
      transport={async () =>
        new Response(
          JSON.stringify({
            status: "OK",
            mode: "ordinary",
            reference: "2026-01",
            definition_ref: CPI,
            channel: "local",
            durable: true,
            version_identity: "v1",
            publication_identity: "p1",
            decision_at: "2026-02-01T00:00:00Z",
            evidence_grades: [],
            measurements,
          }),
        )
      }
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  const table = await screen.findByRole("table", { name: "普通查询测量结果" });
  expect(
    within(table)
      .getAllByRole("row")
      .slice(1)
      .map((row) => row.children[0].textContent),
  ).toEqual(["metric-1", "metric-3", "metric-0", "metric-2"]);
  expect(JSON.stringify(measurements)).toBe(original);
});

it.each([CPI_PROFILE, PPI_PROFILE, PCE_PROFILE])(
  "$label table shows newest year first with stable series and original HTTP text",
  async (profile) => {
    const points = ["2025-12", "2026-01"].flatMap((month) =>
      profile.series.map(([series_id]) => ({
        series_id,
        month,
        index_value: "123.456",
        mom_pct: null,
        yoy_pct: null,
        change_1m_ratio: null,
        change_3m_ratio: null,
        change_6m_ratio: null,
        change_12m_ratio: null,
        origin: "local",
        availability: "available",
        observed_at: "2026-02-01T00:00:00Z",
        available_at: "2026-02-01T00:00:01Z",
      })),
    );
    const body = JSON.stringify(
      {
        start_month: "2025-12",
        end_month: "2026-01",
        series_ids: profile.series.map(([id]) => id),
        points,
        missing: [],
        calculation_missing: [],
        complete: true,
        coverage_complete: true,
        persisted: false,
        provider_requests: 0,
        provider_attempts: 0,
        vintage_mode: "observed",
        as_of: null,
        measure: "index",
      },
      null,
      2,
    );
    const paths: string[] = [];
    const availability = {
      busy: false,
      legacy: false,
      failure: null,
      data: {
        latest_months: Object.fromEntries(
          profile.series.map(([id]) => [id, "2026-01"]),
        ),
        observation_count: 10,
        revision_count: 0,
        batch_count: 1,
        web_capabilities: { query: true, collect: false, reason: null },
      },
    } as CpiAvailability;
    render(
      <CpiTimeSeries
        profile={profile}
        availability={availability}
        transport={async (path) => {
          paths.push(String(path));
          return new Response(body);
        }}
      />,
    );
    fireEvent.change(screen.getByLabelText("开始月份"), {
      target: { value: "2025-12" },
    });
    fireEvent.click(screen.getByRole("button", { name: "统计篮子" }));
    fireEvent.click(
      within(screen.getByRole("group", { name: "统计篮子选项" })).getByRole(
        "button",
        { name: "全选" },
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
    const table = await screen.findByRole("table", {
      name: `${profile.label} 范围数据`,
    });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => row.children[0].textContent)).toEqual([
      ...profile.series.map(() => "2026-01"),
      ...profile.series.map(() => "2025-12"),
    ]);
    expect(
      rows
        .slice(0, profile.series.length)
        .map((row) => row.children[1].textContent),
    ).toEqual(profile.series.map(([, title]) => title));
    fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
    expect(screen.getByLabelText("原始 HTTP 正文").textContent).toBe(body);
    expect(points[0].month).toBe("2025-12");
    expect(paths).toHaveLength(1);
    expect(
      new URL(paths[0], "http://fixture.invalid").searchParams.get("start"),
    ).toBe("2025-12");
  },
);
