import { afterEach, expect, it } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { readFileSync } from "node:fs";
import { CpiTimeSeries } from "./CpiTimeSeries";
import { CpiTimeSeriesCollection } from "./CpiTimeSeriesCollection";
import { CpiStrategyWorkbench } from "./CpiStrategyWorkbench";
import { JOLTS_PROFILE as profile } from "./macroTimeSeriesProfile";
import {
  buildCpiQueryRequest,
  queryCpiRange,
  type CpiAvailability,
} from "./cpiTimeSeriesApi";
const fixture = JSON.parse(
  readFileSync("../tests/fixtures/jolts_ui_http.json", "utf8"),
);
const availability: CpiAvailability = {
  data: fixture.status,
  legacy: false,
  busy: false,
  failure: null,
};
afterEach(() => {
  cleanup();
  localStorage.clear();
  sessionStorage.clear();
});
it("JOLTS uses native series, exact HTTP body, zero quantities and percent rates without index scaling", async () => {
  const requests: string[] = [];
  render(
    <CpiTimeSeries
      profile={profile}
      availability={availability}
      transport={async (path) => {
        requests.push(String(path));
        return new Response(fixture.query_wire);
      }}
    />,
  );
  fireEvent.change(screen.getByLabelText("开始月份"), {
    target: { value: "2026-07" },
  });
  fireEvent.click(screen.getByRole("button", { name: "JOLTS指标" }));
  const options = within(screen.getByRole("group", { name: "JOLTS指标选项" }));
  fireEvent.click(options.getByRole("button", { name: "清空" }));
  for (const id of ["JTS000000000000000JOL", "JTS000000000000000JOR"]) {
    fireEvent.click(
      options.getByRole("checkbox", {
        name: profile.series.find((s) => s[0] === id)![1],
      }),
    );
  }
  fireEvent.click(screen.getByRole("button", { name: "JOLTS指标" }));
  expect(screen.queryByRole("button", { name: "统计篮子" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  const table = await screen.findByRole("table", { name: "JOLTS 范围数据" });
  expect(within(table).getAllByRole("row")[1].textContent).toContain("2026-08");
  expect(table.textContent).toContain("4.3%");
  expect(table.textContent).not.toContain("430%");
  expect(table.textContent).toContain("0 千岗位");
  expect(table.textContent).toContain("0.1 百分点");
  expect(
    new URL(requests[0], "http://fixture").searchParams.get("local_only"),
  ).toBe("false");
  expect(
    new URL(requests[0], "http://fixture").searchParams.has("basket"),
  ).toBe(false);
  fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
  expect(screen.getByLabelText("原始 HTTP 正文").textContent).toBe(
    fixture.query_wire,
  );
  fireEvent.click(screen.getByRole("tab", { name: "API说明" }));
  const help = screen.getByRole("region", { name: "JOLTS API说明" });
  for (const text of [
    "source_qualified=false",
    "change_1m_level",
    "true（API默认）",
    "百分点",
    "采集与策略接口",
  ])
    expect(help.textContent).toContain(text);
});
it("JOLTS history forces local same cutoff and projects only selected columns", () => {
  const r = buildCpiQueryRequest(
    {
      start: "2026-07",
      end: "2026-08",
      series: profile.series[0][0],
      basket: "all",
      adjustment: "all",
      measure: "index",
      columns: "value",
      query_mode: "history",
      as_of: "2026-11-02T08:00:00+08:00",
    },
    profile,
  );
  const p = new URL(r.path, "http://fixture").searchParams;
  expect(p.get("local_only")).toBe("true");
  expect(p.get("columns")).toBe("value");
  expect(p.get("as_of_basis")).toBe("available");
});
it("JOLTS initialization retains an editable 240-month calendar anchor", async () => {
  render(
    <CpiTimeSeriesCollection
      profile={profile}
      availability={availability}
      tab="new"
      transport={async () => new Response('{"jobs":[]}')}
    />,
  );
  fireEvent.change(screen.getByLabelText("采集方式"), {
    target: { value: "initialize" },
  });
  expect(
    (screen.getByLabelText("采集开始月份") as HTMLInputElement).value,
  ).toBe(fixture.status.initialization.start);
  expect(screen.getByText(/范围依据：/).textContent).toContain("不代表已发布");
  fireEvent.change(screen.getByLabelText("采集开始月份"), {
    target: { value: "2010-01" },
  });
  expect(
    (screen.getByLabelText("采集开始月份") as HTMLInputElement).value,
  ).toBe("2010-01");
});
it("JOLTS unknown calendar preserves a direct official link and real execution detail navigation", async () => {
  render(
    <CpiStrategyWorkbench
      profile={profile}
      availability={availability}
      selected
      transport={async (path) =>
        new Response(
          JSON.stringify(
            String(path).endsWith("/strategy")
              ? fixture.strategy
              : String(path).endsWith("/schedule")
                ? {
                    ...fixture.schedule,
                    calendar: {
                      error: "http_403",
                      primary_source:
                        "https://www.bls.gov/schedule/news_release/bls.ics",
                      next_refresh_at: "2026-11-04T06:19:27+00:00",
                    },
                  }
                : fixture.executions,
          ),
        )
      }
    />,
  );
  const summary = await screen.findByRole("region", {
    name: "最近真实执行摘要",
  });
  const link = await within(summary).findByRole("link", {
    name: "打开对应执行详情",
  });
  expect(
    new URLSearchParams(link.getAttribute("href")!.slice(1)).get("job"),
  ).toBe(fixture.executions.executions[0].id);
  expect(
    screen
      .getByRole("link", {
        name: "https://www.bls.gov/schedule/news_release/bls.ics",
      })
      .getAttribute("href"),
  ).toContain("bls.gov/schedule/");
  for (const name of [
    "当前日历入口",
    "日历缓存有效期至（北京时间）",
    "下次日历刷新（北京时间）",
  ])
    expect(screen.getByText(name)).toBeTruthy();
  expect(screen.getByText(/最近刷新失败/)).toBeTruthy();
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("JOLTS parses actual projected and historical HTTP bodies with native null semantics", async () => {
  const scope = {
    start: "2026-07",
    end: "2026-08",
    series: "JTS000000000000000JOL,JTS000000000000000JOR",
    basket: "all",
    adjustment: "all",
    measure: "index",
    columns: "value",
  };
  const projected = await queryCpiRange(
    async () => new Response(fixture.projected_wire),
    scope,
    new AbortController().signal,
    undefined,
    profile,
  );
  expect(projected.points).toHaveLength(4);
  expect(projected.selected_columns).toEqual(["value"]);
  const history = await queryCpiRange(
    async () => new Response(fixture.history_wire),
    {
      ...scope,
      columns: undefined,
      query_mode: "history",
      as_of: "2026-11-02T00:00:00Z",
    },
    new AbortController().signal,
    undefined,
    profile,
  );
  expect(history.points).toHaveLength(0);
  expect(history.missing).toHaveLength(4);
  expect(history.provider_requests).toBe(0);
});
it("JOLTS zero-data form reports absent holdings and still permits range edits", () => {
  render(
    <CpiTimeSeriesCollection
      profile={profile}
      availability={{
        ...availability,
        data: {
          ...fixture.status,
          observation_count: 0,
          batch_count: 0,
          latest_months: Object.fromEntries(
            profile.series.map(([id]) => [id, null]),
          ),
        },
      }}
      tab="new"
      transport={async () => new Response('{"jobs":[]}')}
    />,
  );
  expect(screen.getAllByText("尚无本地记录")).toHaveLength(8);
  expect(screen.getByText(/本地已持有 0 项观测/)).toBeTruthy();
  expect(screen.queryByRole("checkbox", { name: /补源/ })).toBeNull();
});
it("JOLTS strategy toggles with one PUT and no confirmation dialog", async () => {
  let enabled = false;
  let puts = 0;
  render(
    <CpiStrategyWorkbench
      profile={profile}
      availability={availability}
      transport={async (path, init) => {
        if (init?.method === "PUT") {
          puts++;
          enabled = true;
        }
        return new Response(
          JSON.stringify(
            String(path).endsWith("/strategy")
              ? { ...fixture.strategy, exists: enabled, enabled }
              : String(path).endsWith("/schedule")
                ? fixture.schedule
                : fixture.executions,
          ),
        );
      }}
    />,
  );
  fireEvent.click(await screen.findByRole("button", { name: "启动" }));
  await screen.findByRole("button", { name: "暂停" });
  expect(puts).toBe(1);
  expect(screen.queryByRole("dialog")).toBeNull();
});
