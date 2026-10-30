import { afterEach, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import {
  CpiExecutionDetail,
  cpiExecutionStateText,
} from "./CpiStrategyWorkbench";
import { CPI_PROFILE, PPI_PROFILE } from "./macroTimeSeriesProfile";
import { shiftMonth, type CpiExecution } from "./cpiTimeSeriesApi";

afterEach(cleanup);
for (const profile of [CPI_PROFILE, PPI_PROFILE]) {
  const point = (month = "2026-09", series_id = profile.series[0][0]) => ({
    series_id,
    month,
  });
  const show = (
    result: Record<string, unknown>,
    start: string | null = "2026-09",
    end: string | null = "2026-09",
  ) =>
    render(
      <CpiExecutionDetail
        profile={profile}
        execution={
          {
            id: "fixture",
            origin: "manual",
            action: "initialize",
            start,
            end,
            created_at: null,
            started_at: null,
            finished_at: null,
            state: "complete",
            classification: null,
            result,
          } satisfies CpiExecution
        }
      />,
    );
  it(`${profile.label} counts the real 960/158 case, deduplicates and separates 48 buffer points`, () => {
    const buffer = profile.series.flatMap(([id]) =>
      Array.from({ length: 12 }, (_, i) => point(shiftMonth("2005-10", i), id)),
    );
    const missing = Array.from({ length: 158 }, (_, i) =>
      point(shiftMonth("2006-10", Math.floor(i / 4)), profile.series[i % 4][0]),
    );
    const source_missing = [...buffer, ...missing];
    show(
      {
        saved_points: 850,
        coverage_complete: false,
        source_missing,
        readback: {
          expected_points: 960,
          point_count: 850,
          missing,
          source_missing,
          source_unavailable: [],
          source_unavailable_count: 0,
        },
      },
      "2006-10",
      "2026-09",
    );
    expect(
      screen.getByText("范围覆盖：802 / 960 条，来源未返回 158 条。"),
    ).toBeTruthy();
    const details = screen.getByText("查看缺口明细").closest("details")!;
    expect(details.open).toBe(false);
    expect(document.querySelectorAll("details")).toHaveLength(1);
    fireEvent.click(screen.getByText("查看缺口明细"));
    expect(details.open).toBe(true);
    expect(screen.getByText("请求范围：158 条")).toBeTruthy();
    expect(
      screen.getByText("计算缓冲期（请求前 12 个月，不计入范围覆盖）：48 条"),
    ).toBeTruthy();
    expect(document.querySelectorAll("li")).toHaveLength(98);
    fireEvent.click(screen.getByRole("button", { name: "再显示 50 条" }));
    expect(document.querySelectorAll("li")).toHaveLength(148);
    expect(screen.queryByText(/官方 null/)).toBeNull();
  });
  it(`${profile.label} shows complete and empty coverage without zero groups`, () => {
    const view = show({
      coverage_complete: true,
      readback: {
        expected_points: 4,
        point_count: 52,
        missing: [],
        source_missing: [],
        source_unavailable: [],
        source_unavailable_count: 0,
      },
    });
    expect(screen.getByText("范围覆盖：4 / 4 条。")).toBeTruthy();
    expect(screen.queryByText("查看缺口明细")).toBeNull();
    view.unmount();
    const missing = profile.series.map(([id]) => point("2026-09", id));
    show({
      coverage_complete: false,
      readback: {
        expected_points: 4,
        point_count: 48,
        missing,
        source_missing: missing,
      },
    });
    expect(
      screen.getByText("范围覆盖：0 / 4 条，来源未返回 4 条。"),
    ).toBeTruthy();
    expect(screen.queryByText(/计算缓冲期/)).toBeNull();
  });
  it(`${profile.label} distinguishes source nulls from absent source and unproven local loss`, () => {
    const nullPoint = point();
    const lost = point("2026-09", profile.series[1][0]);
    show({
      coverage_complete: false,
      readback: {
        expected_points: 4,
        missing: [lost],
        source_missing: [nullPoint],
        source_unavailable: [nullPoint],
        source_unavailable_count: 1,
      },
    });
    expect(
      screen.getByText(
        "范围覆盖：3 / 4 条，来源空值 1 条，本地缺口 1 条（来源缺席未证实，待核实）。",
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/来源未返回/)).toBeNull();
    expect(
      screen.getAllByText(`${nullPoint.series_id} · 2026-09`),
    ).toHaveLength(1);
    expect(
      screen.getByText("— 本地缺口（来源缺席未证实，待核实）"),
    ).toBeTruthy();
  });
  it(`${profile.label} keeps returned nulls in coverage and buffer-only gaps out of the state`, () => {
    const returnedNull = point();
    const view = show({
      coverage_complete: true,
      readback: {
        expected_points: 4,
        missing: [],
        source_unavailable: [returnedNull],
        source_unavailable_count: 1,
      },
    });
    expect(
      screen.getByText("范围覆盖：4 / 4 条，来源空值 1 条。"),
    ).toBeTruthy();
    expect(screen.getByText("已完成")).toBeTruthy();
    expect(screen.queryByText("已完成（来源有缺口）")).toBeNull();
    expect(screen.getByText("— 来源空值")).toBeTruthy();
    expect(screen.queryByText(/来源未返回/)).toBeNull();
    view.unmount();
    const result = {
      coverage_complete: true,
      readback: {
        expected_points: 4,
        missing: [],
        source_missing: [point("2026-08")],
      },
    };
    show(result);
    expect(screen.getByText("范围覆盖：4 / 4 条。")).toBeTruthy();
    expect(
      cpiExecutionStateText("complete", result, profile, {
        start: "2026-09",
        end: "2026-09",
      }),
    ).toBe("已完成");
    expect(screen.queryByText(/请求范围：/)).toBeNull();
  });
  it(`${profile.label} never labels unexplained local loss as a source-only outcome`, () => {
    const missing = [point(), point("2026-09", profile.series[1][0])];
    const result = {
      coverage_complete: false,
      completion: "source_coverage_partial",
      readback: { expected_points: 4, missing, source_missing: [point()] },
    };
    show(result);
    expect(
      screen.getByText(
        "范围覆盖：2 / 4 条，来源未返回 1 条，本地缺口 1 条（来源缺席未证实，待核实）。",
      ),
    ).toBeTruthy();
    expect(screen.getByText("已完成（存在缺口）")).toBeTruthy();
    expect(screen.queryByText("已完成（来源有缺口）")).toBeNull();
  });
  it(`${profile.label} respects explicit coverage without inventing counts`, () => {
    const view = show({
      coverage_complete: true,
      saved_points: 4,
      source_missing: [],
    });
    expect(screen.getByText("范围覆盖：完整（数量未记录）。")).toBeTruthy();
    expect(screen.queryByText(/4 \/ 4/)).toBeNull();
    view.unmount();
    show(
      { result: { coverage_complete: false, source_missing: [point()] } },
      null,
      null,
    );
    expect(screen.getByText("范围覆盖：有缺口（数量未记录）。")).toBeTruthy();
    expect(
      screen.getByText("范围未记录，无法区分请求范围与计算缓冲期：1 条"),
    ).toBeTruthy();
    expect(
      cpiExecutionStateText(
        "complete",
        { result: { coverage_complete: false, source_missing: [point()] } },
        profile,
      ),
    ).toBe("已完成（来源有缺口）");
    expect(
      cpiExecutionStateText(
        "complete",
        { coverage_complete: false, readback: { missing: [point()] } },
        profile,
      ),
    ).toBe("已完成（存在缺口）");
  });
  it.each([
    [true, "完整（数量未记录）"],
    [false, "有缺口（数量未记录）"],
    [undefined, "未知（缺少范围或回读明细）"],
  ] as const)(
    `${profile.label} distinguishes boolean coverage from missing metadata without a range: %s`,
    (coverage_complete, text) => {
      show({ result: { coverage_complete } }, null, null);
      expect(screen.getByText(`范围覆盖：${text}。`)).toBeTruthy();
      expect(screen.queryByText(/范围覆盖：\d+ \/ \d+/)).toBeNull();
    },
  );
  it.each([true, false, undefined])(
    `${profile.label} preserves unverified local gaps with coverage_complete=%s`,
    (coverage_complete) => {
      show({
        coverage_complete,
        readback: {
          expected_points: 4,
          missing: [point()],
          source_unavailable: [point()],
        },
      });
      expect(screen.getByText("已完成（存在缺口）")).toBeTruthy();
      expect(screen.queryByText("已完成")).toBeNull();
      expect(screen.getByText("— 来源空值；本地缺口待核实")).toBeTruthy();
    },
  );
  it.each([false, true])(
    `${profile.label} keeps null-only coverage unverified without a completion flag (source_missing overlap=%s)`,
    (overlap) => {
      const returnedNull = point();
      show({
        readback: {
          source_unavailable: [returnedNull],
          ...(overlap ? { source_missing: [returnedNull] } : {}),
        },
      });
      expect(screen.getByText("已完成")).toBeTruthy();
      expect(screen.queryByText("已完成（来源有缺口）")).toBeNull();
      expect(
        screen.getByText(
          "范围覆盖：未知（缺少范围或回读明细），来源空值 1 条。",
        ),
      ).toBeTruthy();
      expect(screen.getByText("— 来源空值")).toBeTruthy();
      expect(screen.queryByText(/来源未返回/)).toBeNull();
    },
  );
  it(`${profile.label} never masks a failed execution with complete coverage`, () => {
    expect(
      cpiExecutionStateText("failed", { coverage_complete: true }, profile),
    ).toBe("失败");
    expect(
      cpiExecutionStateText(
        "succeeded",
        { coverage_complete: true, source_unavailable: [point()] },
        profile,
      ),
    ).toBe("已完成");
  });
}

it.each([
  [{ expected_points: 4, missing: [], point_count: 4 }, "2026-09", "2026-09"],
  [{ readback: { expected_points: 4, point_count: 4 } }, "2026-09", "2026-09"],
  [{ readback: { missing: [], point_count: 4 } }, "2026-09", "2026-09"],
  [{ readback: { expected_points: 4, missing: [] } }, null, null],
  [{ readback: { expected_points: 4, missing: [] } }, "2026-10", "2026-09"],
  [{}, "2026-09", "2026-09"],
] as const)(
  "keeps incomplete or invalid range metadata unknown: %j",
  (result, start, end) => {
    render(
      <CpiExecutionDetail
        execution={
          {
            id: "unknown",
            origin: "automatic",
            action: "update",
            start,
            end,
            created_at: null,
            started_at: null,
            finished_at: null,
            state: "complete",
            classification: null,
            result,
          } satisfies CpiExecution
        }
      />,
    );
    expect(
      screen.getByText("范围覆盖：未知（缺少范围或回读明细）。"),
    ).toBeTruthy();
    expect(screen.queryByText(/4 \/ 4/)).toBeNull();
  },
);
