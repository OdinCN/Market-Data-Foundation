import { useState } from "react";
import { afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { CollectionWorkbench } from "./CollectionWorkbench";
import { CpiTimeSeriesCollection } from "./CpiTimeSeriesCollection";
import { CpiTimeSeries } from "./CpiTimeSeries";
import {
  useCpiAvailability,
  type CpiAvailability,
  type CpiExecution,
  type CpiJob,
  type CpiStatus,
} from "./cpiTimeSeriesApi";
import { CPI, PCE, keyOf, type Asset } from "./workflowModel";
import type { Route, Navigate } from "./workflowRoute";
import type { Transport } from "./api";
const status: CpiStatus = {
  latest_months: { CUSR0000SA0: "2026-09" },
  observation_count: 1,
  revision_count: 0,
  batch_count: 1,
  web_capabilities: { query: true, collect: true, reason: null },
  initialization: {
    preset_id: "cpi_latest_20y_v1",
    months: 240,
    start: "2006-10",
    end: "2026-09",
    available: true,
    reason: null,
    anchor_basis: "confirmed_local_available",
  },
};
const availability: CpiAvailability = {
  data: status,
  busy: false,
  failure: null,
  legacy: false,
};
const asset = (key: Asset["key"], label: string): Asset => ({
  key,
  label,
  interfaces: [],
  catalog: null,
  holding: "",
  versions: null,
  publication: "",
  readability: "",
  storage: "",
});
const legacyPceBinding = {
  definition: PCE.identity,
  definition_revision: PCE.revision,
  source: "mdf.source-candidate.bea.nipa.pce",
  source_revision: "t04-retained-candidate-r1",
  operation: "GetData:NIPA:T20807:T20804:M",
  binding: "mdf.binding-candidate.bea.nipa.pce",
  binding_revision: "t04-retained-candidate-r1",
  reference: "2026-07",
  source_qualified: false,
  operator: "ao-dev-bea-pce",
  target: {
    host: "192.168.10.4",
    database: "market_data_foundation_dev",
    schema: "mdf_macro_bea_pce_vertical_r1",
  },
};
const assets = [asset(CPI, "美国 CPI"), asset(PCE, "美国 PCE")];
const response = (value: unknown, code = 200) =>
  new Response(JSON.stringify(value), { status: code });
const flush = () => act(async () => {});
const advance = (ms = 5000) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
const button = (name: string) =>
  screen.getByRole("button", { name }) as HTMLButtonElement;
afterEach(() => {
  cleanup();
  localStorage.clear();
  sessionStorage.clear();
  vi.useRealTimers();
  vi.restoreAllMocks();
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "visible",
  });
});
function fixture() {
  const records: CpiExecution[] = [
    ...["init", "update", "initialize", "update"].map(
      (action, index): CpiExecution => ({
        id: `manual-${index}`,
        origin: "manual",
        action,
        start: "2026-08",
        end: "2026-09",
        created_at: "2026-10-01T00:00:00Z",
        started_at: null,
        finished_at: null,
        state: "complete",
        classification: null,
        result: { saved_points: 4 },
      }),
    ),
    {
      id: "automatic-stage-stable-id",
      origin: "automatic",
      action: "update",
      stage: "history",
      start: null,
      end: null,
      created_at: null,
      started_at: null,
      finished_at: null,
      state: "unknown",
      classification: "execution_interrupted",
      result: { scheduler_attempts_total: 3, actual_http_total: 5 },
    },
  ];
  const calls: {
    path: string;
    method: string;
    body?: Record<string, string>;
    signal?: AbortSignal | null;
  }[] = [];
  let failed = false;
  const transport: Transport = async (input, init) => {
    const path = String(input),
      method = init?.method ?? "GET",
      body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ path, method, body, signal: init?.signal });
    if (path.endsWith("/executions"))
      return response(
        failed ? {} : { executions: records },
        failed ? 503 : 200,
      );
    if (path.endsWith("/status")) return response(status);
    if (path === "/api/pce/jobs") return response({ jobs: [] });
    if (path.endsWith("/strategy"))
      return response({
        exists: true,
        enabled: false,
        state: "paused",
        history_start: "1999-01",
        worker: { state: "idle", last_heartbeat_at: null },
        next_due: "2026-10-29T00:00:00Z",
        next_release_at: null,
        execution_state: "idle",
        last_result: { result: { scheduler_attempts_total: 2 } },
        policy: {},
      });
    if (path.endsWith("/schedule"))
      return response({
        calendar_status: "fresh",
        calendar: { last_refresh_error: "calendar_http_403" },
        targets: [],
      });
    if (path.endsWith("/jobs")) return response({ jobs: [] });
    return response({}, 404);
  };
  return {
    records,
    calls,
    transport,
    fail: () => {
      failed = true;
    },
  };
}
function Workbench({
  transport,
  tab = "executions",
  fields = {},
  modernPce = false,
}: {
  modernPce?: boolean;
  transport: Transport;
  tab?: string;
  fields?: Record<string, string>;
}) {
  const [route, setRoute] = useState<Route>({
    view: "collection",
    invalid: false,
    fields: new URLSearchParams({
      tab,
      ...(tab === "new"
        ? {
            asset: CPI.identity,
            asset_revision: CPI.revision,
            execution_asset: keyOf(PCE),
            execution_schedule: "previous-plan",
          }
        : {}),
      ...fields,
    }),
  });
  const navigate: Navigate = (view, fields = {}, preserve) =>
    setRoute((old) => {
      const next = new URLSearchParams(preserve ? old.fields : undefined);
      for (const [key, value] of Object.entries(fields)) {
        if (value === null) next.delete(key);
        else next.set(key, value);
      }
      return { view, fields: next, invalid: false };
    });
  return (
    <CollectionWorkbench
      assets={assets}
      asset={assets.find((a) => a.key.identity === route.fields.get("asset"))}
      source={undefined}
      route={route}
      navigate={navigate}
      transport={transport}
      generation={0}
      invalidIdentity={false}
      cpiAvailability={availability}
      pceAvailability={modernPce ? availability : undefined}
    />
  );
}
it.each(["all", keyOf(CPI)])(
  "uses one original execution list for filter %s, keeping all manual records and real automatic details",
  async (filter) => {
    const h = fixture();
    render(<Workbench transport={h.transport} />);
    await flush();
    fireEvent.change(screen.getByLabelText("执行资产筛选"), {
      target: { value: filter },
    });
    await flush();
    expect(screen.getAllByRole("table")).toHaveLength(1);
    for (let index = 0; index < 4; index++)
      expect(
        screen.getByRole("button", { name: new RegExp(`manual-${index} `) }),
      ).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: /选择 automatic-stage-stable-id/ }),
    );
    await flush();
    const detail = within(
      screen.getByRole("complementary", { name: "执行记录详情" }),
    );
    expect(detail.getByText("自动策略")).toBeTruthy();
    expect(detail.getByText("历史核实")).toBeTruthy();
    expect(detail.getAllByText("未记录")).toHaveLength(3);
    expect(detail.getByText("未记录 至 未记录")).toBeTruthy();
    expect(detail.getByText("累计调度尝试")).toBeTruthy();
    expect(detail.getByText("累计实际请求")).toBeTruthy();
    expect(detail.queryByRole("button", { name: "回查当前任务" })).toBeNull();
    expect(
      screen.getByRole("region", { name: "执行详情主体" }).dataset.executionId,
    ).toBe("automatic-stage-stable-id");
    expect(screen.queryByRole("region", { name: "CPI 任务回执" })).toBeNull();
    expect(h.calls.every((c) => c.method === "GET")).toBe(true);
  },
);
it("keeps the selected detail and search across polling failure and pages the combined records", async () => {
  vi.useFakeTimers();
  const h = fixture();
  for (let n = 0; n < 20; n++)
    h.records.push({ ...h.records[0], id: `extra-${n}` });
  render(<Workbench transport={h.transport} />);
  await flush();
  fireEvent.click(button("后一页记录"));
  fireEvent.click(screen.getByRole("button", { name: /选择 extra-19 / }));
  await flush();
  h.fail();
  await advance();
  expect(screen.getByText(/采集记录已陈旧/)).toBeTruthy();
  expect(
    within(
      screen.getByRole("complementary", { name: "执行记录详情" }),
    ).getByText("extra-19"),
  ).toBeTruthy();
  expect(screen.getByText("第 2 页")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("搜索执行记录"), {
    target: { value: "manual-0" },
  });
  await advance();
  expect(
    (screen.getByLabelText("搜索执行记录") as HTMLInputElement).value,
  ).toBe("manual-0");
  expect(
    within(screen.getByRole("region", { name: "执行记录" })).getAllByRole(
      "row",
    ),
  ).toHaveLength(2);
});
it.each(["all", keyOf(CPI), keyOf(PCE)])(
  "shows one preset policy list for %s without legacy periodic entries",
  async (filter) => {
    const h = fixture();
    render(<Workbench transport={h.transport} tab="periodic" />);
    await flush();
    fireEvent.change(screen.getByLabelText("周期资产筛选"), {
      target: { value: filter },
    });
    await flush();
    expect(screen.getAllByRole("table")).toHaveLength(1);
    expect(
      screen.queryByRole("button", { name: /新建周期|创建|回查/ }),
    ).toBeNull();
    expect(screen.getByRole("button", { name: "刷新页面" })).toBeTruthy();
    expect(screen.getAllByRole("row")).toHaveLength(
      filter === keyOf(PCE) ? 1 : 2,
    );
    if (filter !== keyOf(PCE)) {
      fireEvent.click(screen.getByRole("button", { name: /选择 美国 CPI/ }));
      await flush();
      const detail = within(
        screen.getByRole("complementary", { name: "周期任务详情" }),
      );
      expect(detail.getByRole("button", { name: "关闭详情" })).toBeTruthy();
      expect(
        detail.queryByRole("button", { name: /新建|保存|启动执行器/ }),
      ).toBeNull();
      fireEvent.click(detail.getByText("策略上次执行回执与审计"));
      fireEvent.click(detail.getByText("技术详情与日历状态"));
      expect(detail.queryByRole("textbox")).toBeNull();
      expect(detail.getByText("累计调度尝试")).toBeTruthy();
      expect(detail.getByText("日历可用")).toBeTruthy();
    }
    expect(h.calls.every((c) => c.method === "GET")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "刷新页面" }));
    await flush();
    expect(h.calls.every((c) => c.method === "GET")).toBe(true);
  },
);
it("shows two actions and leaves an unavailable default empty until the user enters a range", async () => {
  const h = fixture();
  render(
    <CpiTimeSeriesCollection
      transport={h.transport}
      tab="new"
      availability={{
        ...availability,
        data: {
          ...status,
          initialization: {
            ...status.initialization!,
            available: false,
            start: null,
            end: null,
            reason: "no_confirmed_anchor",
          },
        },
      }}
    />,
  );
  fireEvent.change(screen.getByLabelText("采集方式"), {
    target: { value: "initialize" },
  });
  expect(screen.getAllByRole("option")).toHaveLength(2);
  expect(screen.getByText(/服务暂无默认范围（尚未确认可用锚点/)).toBeTruthy();
  expect(
    screen.getByText(/初始化服务预置范围：240个月，可自行修改/),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("checkbox"));
  expect(button("提交 CPI 采集").disabled).toBe(true);
  expect(
    (screen.getByLabelText("采集开始月份") as HTMLInputElement).value,
  ).toBe("");
  expect(screen.queryByRole("table")).toBeNull();
  expect(screen.queryByRole("button", { name: /回查|刷新/ })).toBeNull();
});
it("retains edited range and does not cancel or resend a pending POST during status polling and visibility changes", async () => {
  vi.useFakeTimers();
  const h = fixture();
  let release!: () => void;
  let signal: AbortSignal | null | undefined;
  let writes = 0;
  const transport: Transport = async (input, init) => {
    if (init?.method === "POST") {
      writes++;
      signal = init.signal;
      const intent = JSON.parse(String(init.body));
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      return response({
        ...intent,
        job_id: intent.request_id,
        state: "complete",
        created_at: "2026-10-29T00:00:00Z",
        started_at: null,
        finished_at: null,
        classification: null,
        result: null,
      } satisfies CpiJob);
    }
    return h.transport(input, init);
  };
  function Probe() {
    const a = useCpiAvailability(transport, 0);
    return (
      <CpiTimeSeriesCollection
        transport={transport}
        availability={a}
        tab="new"
      />
    );
  }
  render(<Probe />);
  await flush();
  fireEvent.change(screen.getByLabelText("采集开始月份"), {
    target: { value: "2026-05" },
  });
  fireEvent.change(screen.getByLabelText("采集结束月份"), {
    target: { value: "2026-06" },
  });
  await advance();
  expect(
    (screen.getByLabelText("采集开始月份") as HTMLInputElement).value,
  ).toBe("2026-05");
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(button("提交 CPI 采集"));
  await advance(15000);
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "hidden",
  });
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "visible",
  });
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  expect(signal?.aborted).toBe(false);
  expect(writes).toBe(1);
  expect(
    (screen.getByLabelText("采集开始月份") as HTMLInputElement).value,
  ).toBe("2026-05");
  await act(async () => release());
  expect(screen.getByRole("button", { name: "开始另一项采集" })).toBeTruthy();
  expect(writes).toBe(1);
});

it("applies the host month limit to both initialization and update", async () => {
  const h = fixture();
  let posted: Record<string, string> | undefined;
  const transport: Transport = async (input, init) => {
    if (init?.method === "POST") {
      posted = JSON.parse(String(init.body));
      return response({
        ...posted,
        job_id: posted!.request_id,
        state: "complete",
        created_at: "2026-10-29T00:00:00Z",
        started_at: null,
        finished_at: null,
        classification: null,
        result: null,
      });
    }
    return h.transport(input, init);
  };
  render(
    <CpiTimeSeriesCollection
      transport={transport}
      tab="new"
      availability={{
        ...availability,
        data: {
          ...status,
          collection_scope: { max_months: 36, series: "all" },
        },
      }}
    />,
  );
  fireEvent.change(screen.getByLabelText("采集开始月份"), {
    target: { value: "2006-10" },
  });
  fireEvent.change(screen.getByLabelText("采集结束月份"), {
    target: { value: "2026-09" },
  });
  fireEvent.click(screen.getByRole("checkbox"));
  expect(button("提交 CPI 采集").disabled).toBe(true);
  fireEvent.change(screen.getByLabelText("采集方式"), {
    target: { value: "initialize" },
  });
  fireEvent.click(screen.getByRole("checkbox"));
  expect(button("提交 CPI 采集").disabled).toBe(true);
  fireEvent.change(screen.getByLabelText("采集开始月份"), {
    target: { value: "2026-08" },
  });
  fireEvent.click(screen.getByRole("checkbox"));
  expect(button("提交 CPI 采集").disabled).toBe(false);
  fireEvent.click(button("提交 CPI 采集"));
  await flush();
  expect(posted).toMatchObject({
    action: "initialize",
    start: "2026-08",
    end: "2026-09",
  });
});
it.each([
  [400, "initialization_preset_range_required"],
  [409, "initialization_anchor_unavailable"],
] as const)(
  "reconciles pre-journal initialize refusal %s/%s through a status read without reposting",
  async (code, classification) => {
    const h = fixture();
    let writes = 0,
      statusReads = 0;
    const transport: Transport = async (input, init) => {
      if (String(input).endsWith("/status")) {
        statusReads++;
        return response({
          ...status,
          initialization: writes
            ? { ...status.initialization, start: "2006-11", end: "2026-10" }
            : status.initialization,
        });
      }
      if (init?.method === "POST") {
        writes++;
        return response({ classification }, code);
      }
      return h.transport(input, init);
    };
    function Probe() {
      const a = useCpiAvailability(transport, 0);
      return (
        <CpiTimeSeriesCollection
          transport={transport}
          availability={a}
          tab="new"
        />
      );
    }
    render(<Probe />);
    await flush();
    fireEvent.change(screen.getByLabelText("采集方式"), {
      target: { value: "initialize" },
    });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(button("提交 CPI 采集"));
    await flush();
    expect(localStorage.getItem("mdf.cpi.timeseries.pending.v1")).toBeNull();
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(
      false,
    );
    expect(
      (screen.getByLabelText("采集结束月份") as HTMLInputElement).value,
    ).toBe("2026-10");
    expect(statusReads).toBe(2);
    expect(writes).toBe(1);
  },
);
it("retains the original initialization scope across an unknown outcome and a new anchor", async () => {
  vi.useFakeTimers();
  const h = fixture();
  let writes = 0;
  const transport: Transport = async (input, init) => {
    if (String(input).endsWith("/status"))
      return response({
        ...status,
        initialization: writes
          ? { ...status.initialization, start: "2006-11", end: "2026-10" }
          : status.initialization,
      });
    if (init?.method === "POST") {
      writes++;
      throw new Error("synthetic connection loss");
    }
    return h.transport(input, init);
  };
  function Probe() {
    const a = useCpiAvailability(transport, 0);
    return (
      <CpiTimeSeriesCollection
        transport={transport}
        availability={a}
        tab="new"
      />
    );
  }
  const view = render(<Probe />);
  await flush();
  fireEvent.change(screen.getByLabelText("采集方式"), {
    target: { value: "initialize" },
  });
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(button("提交 CPI 采集"));
  await flush();
  const stored = localStorage.getItem("mdf.cpi.timeseries.pending.v1");
  await advance();
  view.unmount();
  render(<Probe />);
  await flush();
  await advance();
  expect(localStorage.getItem("mdf.cpi.timeseries.pending.v1")).toBe(stored);
  expect(JSON.parse(stored!)).toMatchObject({
    action: "initialize",
    start: "2006-10",
    end: "2026-09",
  });
  expect(button("提交 CPI 采集").disabled).toBe(true);
  expect(writes).toBe(1);
  expect(screen.queryByRole("button", { name: "开始另一项采集" })).toBeNull();
});
it("does not turn a named CPI source context into timeseries write permission", async () => {
  const h = fixture();
  render(
    <CollectionWorkbench
      assets={assets}
      asset={assets[0]}
      source={undefined}
      route={{
        view: "collection",
        invalid: false,
        fields: new URLSearchParams({
          tab: "new",
          asset: CPI.identity,
          asset_revision: CPI.revision,
          operation: "other-cpi-source",
        }),
      }}
      navigate={() => {}}
      transport={h.transport}
      generation={0}
      invalidIdentity={false}
      cpiAvailability={availability}
    />,
  );
  await flush();
  expect(screen.queryByRole("button", { name: "提交 CPI 采集" })).toBeNull();
  const submit = button("创建采集任务");
  fireEvent.click(screen.getByRole("checkbox"));
  expect(submit.disabled).toBe(true);
  fireEvent.submit(submit.closest("form")!);
  await flush();
  expect(h.calls.every((call) => call.method === "GET")).toBe(true);
  expect(
    h.calls.some((call) => call.path === "/api/r5/cpi/timeseries/jobs"),
  ).toBe(false);
});

it("keeps custom initialization months across polling, late presets and mode switches", async () => {
  vi.useFakeTimers();
  const h = fixture();
  const initialAvailability = {
    ...availability,
    data: { ...status, initialization: undefined },
  };
  const view = render(
    <CpiTimeSeriesCollection
      transport={h.transport}
      tab="new"
      availability={initialAvailability}
    />,
  );
  fireEvent.change(screen.getByLabelText("采集方式"), {
    target: { value: "initialize" },
  });
  for (const [label, value] of [
    ["采集开始月份", "2025-01"],
    ["采集结束月份", "2025-12"],
  ]) {
    const input = screen.getByLabelText(label) as HTMLInputElement;
    expect(input.placeholder).toBe("YYYY-MM");
    expect(input.readOnly).toBe(false);
    fireEvent.change(input, { target: { value } });
  }
  view.rerender(
    <CpiTimeSeriesCollection
      transport={h.transport}
      tab="new"
      availability={availability}
    />,
  );
  await advance(15000);
  expect(
    (screen.getByLabelText("采集开始月份") as HTMLInputElement).value,
  ).toBe("2025-01");
  fireEvent.change(screen.getByLabelText("采集方式"), {
    target: { value: "update" },
  });
  fireEvent.change(screen.getByLabelText("采集开始月份"), {
    target: { value: "2026-06" },
  });
  fireEvent.change(screen.getByLabelText("采集方式"), {
    target: { value: "initialize" },
  });
  expect(
    (screen.getByLabelText("采集开始月份") as HTMLInputElement).value,
  ).toBe("2025-01");
  fireEvent.change(screen.getByLabelText("采集方式"), {
    target: { value: "update" },
  });
  expect(
    (screen.getByLabelText("采集开始月份") as HTMLInputElement).value,
  ).toBe("2026-06");
  expect(h.calls.every((call) => call.method === "GET")).toBe(true);
});

it("accepts a legal explicit initialization range without an available default", async () => {
  const h = fixture();
  render(
    <CpiTimeSeriesCollection
      transport={h.transport}
      tab="new"
      availability={{
        ...availability,
        data: {
          ...status,
          collection_scope: { max_months: 12, series: "all" },
          initialization: {
            ...status.initialization!,
            available: false,
            start: null,
            end: null,
          },
        },
      }}
    />,
  );
  fireEvent.change(screen.getByLabelText("采集方式"), {
    target: { value: "initialize" },
  });
  fireEvent.change(screen.getByLabelText("采集开始月份"), {
    target: { value: "2026-01" },
  });
  fireEvent.change(screen.getByLabelText("采集结束月份"), {
    target: { value: "2026-09" },
  });
  fireEvent.click(screen.getByRole("checkbox"));
  expect(button("提交 CPI 采集").disabled).toBe(false);
  fireEvent.click(button("提交 CPI 采集"));
  await flush();
  const posts = h.calls.filter((c) => c.method === "POST");
  expect(posts).toHaveLength(1);
  expect(posts[0].body).toMatchObject({
    action: "initialize",
    start: "2026-01",
    end: "2026-09",
  });
  expect(
    JSON.parse(localStorage.getItem("mdf.cpi.timeseries.pending.v1")!),
  ).toMatchObject(posts[0].body!);
});

it("does not request unbound legacy PCE or CPI endpoints and preserves genuine CPI failure", async () => {
  vi.useFakeTimers();
  const h = fixture();
  const transport: Transport = (input, init) =>
    String(input).startsWith("/api/pce/") ||
    String(input).startsWith("/api/r5/cpi/jobs") ||
    String(input).startsWith("/api/r5/cpi/plans")
      ? Promise.resolve(response({}, 404))
      : h.transport(input, init);
  render(<Workbench transport={transport} />);
  await flush();
  expect(screen.queryByText(/采集记录已陈旧/)).toBeNull();
  expect(screen.queryByText("技术详情")).toBeNull();
  expect(h.calls.some((c) => c.path === "/api/pce/jobs")).toBe(false);
  expect(screen.getAllByRole("columnheader").map((h) => h.textContent)).toEqual(
    ["任务名称", "创建时间（北京时间）", "采集范围", "结果"],
  );
  const list = within(screen.getByRole("region", { name: "执行记录" }));
  expect(list.queryByText("manual-0")).toBeNull();
  h.fail();
  await advance();
  expect(
    screen.getByText("CPI 执行记录读取失败，当前结果尚未核实。"),
  ).toBeTruthy();
});

it("opens the exact brief id after asynchronous execution loading and clears blocking route filters", async () => {
  const h = fixture();
  const id = "00000000-0000-4000-8000-000000000025";
  const job: CpiJob = {
    request_id: id,
    job_id: id,
    action: "initialize",
    start: "2025-01",
    end: "2025-12",
    state: "complete",
    created_at: "2026-10-29T01:00:00Z",
    started_at: null,
    finished_at: null,
    classification: null,
    result: null,
  };
  let release!: () => void;
  const records = Array.from({ length: 25 }, (_, n) => ({
    ...h.records[0],
    id: n === 24 ? id : `earlier-${n}`,
  }));
  const transport: Transport = async (input, init) => {
    if (String(input).endsWith("/jobs"))
      return response({
        jobs: [
          job,
          {
            ...job,
            job_id: "older",
            request_id: "00000000-0000-4000-8000-000000000024",
            created_at: "2026-10-28T01:00:00Z",
          },
        ],
      });
    if (String(input).endsWith("/executions")) {
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      return response({ executions: records });
    }
    return h.transport(input, init);
  };
  render(<Workbench transport={transport} tab="new" />);
  await flush();
  const brief = screen.getByRole("complementary", { name: "执行简报" });
  expect(within(brief).getAllByRole("listitem")[0].textContent).toContain(id);
  fireEvent.click(within(brief).getByRole("button", { name: new RegExp(id) }));
  await flush();
  expect(
    (screen.getByLabelText("执行资产筛选") as HTMLSelectElement).value,
  ).toBe(keyOf(CPI));
  expect(screen.queryByText("计划筛选：previous-plan")).toBeNull();
  await act(async () => release());
  expect(
    within(
      screen.getByRole("complementary", { name: "执行记录详情" }),
    ).getByText(id),
  ).toBeTruthy();
  expect(screen.getByText("第 2 页")).toBeTruthy();
  expect(
    screen
      .getByRole("button", { name: new RegExp(`选择 ${id}`) })
      .closest("tr")
      ?.getAttribute("aria-selected"),
  ).toBe("true");
  expect(h.calls.every((c) => c.method === "GET")).toBe(true);
});

it("keeps a selected unbound PCE identity from requesting native jobs", async () => {
  const h = fixture();
  render(
    <CollectionWorkbench
      assets={assets}
      asset={assets[1]}
      source={undefined}
      route={{
        view: "collection",
        invalid: false,
        fields: new URLSearchParams({
          tab: "executions",
          asset: PCE.identity,
          asset_revision: PCE.revision,
          execution_asset: keyOf(PCE),
        }),
      }}
      navigate={() => {}}
      transport={h.transport}
      generation={0}
      invalidIdentity={false}
      cpiAvailability={availability}
    />,
  );
  await flush();
  expect(h.calls.some((c) => c.path === "/api/pce/jobs")).toBe(false);
  expect(h.calls.every((c) => c.method === "GET")).toBe(true);
});

it("continues native PCE reads with a real binding and does not invent creation time from update events", async () => {
  const h = fixture();
  const boundAssets: Asset[] = [
    assets[0],
    {
      ...assets[1],
      interfaces: [
        { pce: { binding: legacyPceBinding } } as Asset["interfaces"][number],
      ],
    },
  ];
  let reads = 0;
  const transport: Transport = async (input, init) => {
    if (String(input) === "/api/pce/jobs") {
      reads++;
      return response({
        jobs: [
          {
            job_id: "bound-pce-job",
            binding: legacyPceBinding,
            state: "failed",
            stage: "provider",
            attempt_id: null,
            attempts: 1,
            classification: "provider_failed",
            updated_at: "2026-10-29T00:00:00Z",
            events: [{ updated_at: "2026-10-29T00:00:00Z" }],
          },
        ],
      });
    }
    return h.transport(input, init);
  };
  render(
    <CollectionWorkbench
      assets={boundAssets}
      asset={undefined}
      source={undefined}
      route={{
        view: "collection",
        invalid: false,
        fields: new URLSearchParams({ tab: "executions" }),
      }}
      navigate={() => {}}
      transport={transport}
      generation={0}
      invalidIdentity={false}
      cpiAvailability={availability}
    />,
  );
  await flush();
  expect(reads).toBe(1);
  const row = screen
    .getByRole("button", { name: /选择 bound-pce-job / })
    .closest("tr")!;
  expect(within(row).getAllByRole("cell")[1].textContent).toBe("未记录");
  expect(h.calls.every((c) => c.method === "GET")).toBe(true);
});

it("shows one right-side unknown intent card without inventing a journal record", async () => {
  const h = fixture();
  const intent = {
    request_id: "00000000-0000-4000-8000-000000000099",
    action: "initialize",
    start: "2025-01",
    end: "2025-12",
  };
  localStorage.setItem("mdf.cpi.timeseries.pending.v1", JSON.stringify(intent));
  render(
    <CpiTimeSeriesCollection
      transport={h.transport}
      availability={availability}
      tab="new"
    />,
  );
  await flush();
  const brief = within(screen.getByRole("complementary", { name: "执行简报" }));
  expect(screen.getAllByText(intent.request_id)).toHaveLength(1);
  expect(brief.getByText(intent.request_id)).toBeTruthy();
  expect(
    brief.getByText(
      /结果未知，保留请求并自动跟踪，请勿重复提交；尚未确认任务创建/,
    ),
  ).toBeTruthy();
  expect(brief.queryByRole("button", { name: "开始另一项采集" })).toBeNull();
  expect(button("提交 CPI 采集").disabled).toBe(true);
  expect(h.calls.every((c) => c.method === "GET")).toBe(true);
  expect(
    JSON.parse(localStorage.getItem("mdf.cpi.timeseries.pending.v1")!),
  ).toEqual(intent);
});

it.each(["complete", "unknown"] as const)(
  "deduplicates a %s journal receipt and preserves its request protections",
  async (state) => {
    const h = fixture();
    const intent = {
      request_id: "00000000-0000-4000-8000-000000000098",
      action: "initialize",
      start: "2025-01",
      end: "2025-12",
    };
    localStorage.setItem(
      "mdf.cpi.timeseries.pending.v1",
      JSON.stringify(intent),
    );
    const transport: Transport = (input, init) =>
      String(input).endsWith("/jobs")
        ? Promise.resolve(
            response({
              jobs: [
                {
                  ...intent,
                  job_id: intent.request_id,
                  state,
                  created_at: "2026-10-29T00:00:00Z",
                  started_at: null,
                  finished_at: null,
                  result: null,
                  classification: null,
                },
              ],
            }),
          )
        : h.transport(input, init);
    render(
      <CpiTimeSeriesCollection
        transport={transport}
        availability={availability}
        tab="new"
      />,
    );
    await flush();
    expect(screen.getAllByText(intent.request_id)).toHaveLength(1);
    const brief = within(
      screen.getByRole("complementary", { name: "执行简报" }),
    );
    expect(brief.getAllByRole("listitem")).toHaveLength(1);
    if (state === "complete") {
      expect(
        brief.getByRole("button", { name: "开始另一项采集" }),
      ).toBeTruthy();
    } else {
      expect(
        brief.queryByRole("button", { name: "开始另一项采集" }),
      ).toBeNull();
      expect(
        brief.getByText("结果未知，保留请求并自动跟踪，请勿重复提交。"),
      ).toBeTruthy();
      expect(button("提交 CPI 采集").disabled).toBe(true);
    }
    expect(
      JSON.parse(localStorage.getItem("mdf.cpi.timeseries.pending.v1")!),
    ).toEqual(intent);
    expect(h.calls.every((c) => c.method === "GET")).toBe(true);
  },
);

it.each(["ready", "pending", "failed", "unavailable"])(
  "all-assets selects modern PCE protocol (%s), while genuine modern failures remain visible",
  async (state) => {
    const h = fixture();
    let legacyReads = 0;
    const reads: string[] = [];
    const transport: Transport = async (input, init) => {
      const path = String(input);
      reads.push(path);
      if (path === "/api/pce/jobs") {
        legacyReads++;
        return response({ classification: "pce_legacy_not_enabled" }, 503);
      }
      if (path === "/api/r5/pce/timeseries/executions")
        return state === "failed"
          ? response({ classification: "real_pce_read_failure" }, 503)
          : response({ executions: [] });
      return h.transport(input, init);
    };
    const view = (pceAvailability: CpiAvailability) => (
      <CollectionWorkbench
        assets={[
          assets[0],
          {
            ...assets[1],
            interfaces: [
              {
                pce: { binding: legacyPceBinding },
              } as Asset["interfaces"][number],
            ],
          },
        ]}
        asset={undefined}
        source={undefined}
        route={{
          view: "collection",
          invalid: false,
          fields: new URLSearchParams({
            tab: "executions",
            execution_asset: "all",
          }),
        }}
        navigate={() => {}}
        transport={transport}
        generation={0}
        invalidIdentity={false}
        cpiAvailability={availability}
        pceAvailability={pceAvailability}
      />
    );
    const modern = {
      ...availability,
      data: ["pending", "unavailable"].includes(state)
        ? null
        : availability.data,
      busy: state === "pending",
      failure: state === "unavailable" ? "read_failed" : null,
    };
    const rendered = render(view(modern));
    await flush();
    expect(legacyReads).toBe(0);
    if (state === "unavailable")
      expect(
        screen.getByText("PCE 服务状态读取失败，当前执行记录尚未核实。"),
      ).toBeTruthy();
    else if (state === "failed")
      expect(
        screen.getByText("PCE 执行记录读取失败，当前结果尚未核实。"),
      ).toBeTruthy();
    else expect(screen.queryByText(/采集记录已陈旧/)).toBeNull();
    // A previous successful legacy read must not leak stale rows/alerts into
    // modern history when availability is requalified in the same component.
    if (state === "ready") {
      rendered.rerender(view({ ...availability, data: null, legacy: true }));
      await flush();
      expect(legacyReads).toBe(1);
      expect(
        screen.getByText("PCE 执行记录读取失败，当前结果尚未核实。"),
      ).toBeTruthy();
      rendered.rerender(view(modern));
      await flush();
      expect(screen.queryByText(/采集记录已陈旧/)).toBeNull();
    }
    expect(reads.every((path) => !path.includes("/jobs/resume"))).toBe(true);
  },
);

it("a direct CPI execution link locates its selected row beyond the first page, with no preparatory click", async () => {
  const h = fixture();
  h.records.splice(
    0,
    h.records.length,
    ...Array.from({ length: 25 }, (_, i) => ({
      ...h.records[0],
      id: `direct-${i}`,
      created_at: `2026-10-${String(29 - Math.floor(i / 24)).padStart(2, "0")}T01:00:00Z`,
    })),
  );
  render(
    <Workbench
      transport={h.transport}
      fields={{
        asset: CPI.identity,
        asset_revision: CPI.revision,
        job: "direct-24",
      }}
    />,
  );
  await flush();
  expect(
    screen.getByRole("region", { name: "执行详情主体" }).dataset.executionId,
  ).toBe("direct-24");
  expect(screen.getByText("第 2 页")).toBeTruthy();
  const selected = screen
    .getByRole("region", { name: "执行记录" })
    .querySelector<HTMLElement>('tr[aria-selected="true"]');
  expect(selected?.dataset.executionId).toBe("direct-24");
  expect(selected?.dataset.executionAsset).toBe(keyOf(CPI));
  expect(h.calls.every((c) => c.method === "GET")).toBe(true);
});

it("returns from a strategy to the selected execution beyond the first page after a manual row selection", async () => {
  const h = fixture();
  h.records.splice(
    0,
    h.records.length,
    ...Array.from({ length: 25 }, (_, i) => ({
      ...h.records[0],
      id: `cycle-${i}`,
      created_at: `2026-10-${String(29 - Math.floor(i / 24)).padStart(2, "0")}T01:00:00Z`,
    })),
  );
  render(
    <Workbench
      transport={h.transport}
      fields={{ asset: CPI.identity, asset_revision: CPI.revision }}
    />,
  );
  await flush();
  fireEvent.click(button("后一页记录"));
  fireEvent.click(screen.getByRole("button", { name: /选择 cycle-24 / }));
  await flush();
  expect(
    screen.getByRole("region", { name: "执行详情主体" }).dataset.executionId,
  ).toBe("cycle-24");
  fireEvent.click(await screen.findByRole("button", { name: "查看周期任务" }));
  await flush();
  expect(
    screen
      .getByRole("region", { name: "周期任务" })
      .querySelector('tr[aria-selected="true"]')
      ?.getAttribute("data-record-id"),
  ).toBe("cpi-auto");
  fireEvent.click(screen.getByRole("button", { name: "返回此执行记录" }));
  await flush();
  expect(screen.getByText("第 2 页")).toBeTruthy();
  expect(
    screen
      .getByRole("region", { name: "执行记录" })
      .querySelector<HTMLElement>('tr[aria-selected="true"]')?.dataset
      .executionId,
  ).toBe("cycle-24");
  expect(
    screen.getByRole("region", { name: "执行详情主体" }).dataset.executionId,
  ).toBe("cycle-24");
  expect(h.calls.every((c) => c.method === "GET")).toBe(true);
});

it("cold all-asset deep links follow the selected identity when a delayed family inserts newer records", async () => {
  const h = fixture();
  const seed = h.records[0];
  h.records.splice(
    0,
    h.records.length,
    ...Array.from({ length: 25 }, (_, i) => ({
      ...seed,
      id: `cold-${i}`,
      created_at: `2026-10-${String(29 - Math.floor(i / 24)).padStart(2, "0")}T01:00:00Z`,
    })),
  );
  let release!: (response: Response) => void;
  const late = new Promise<Response>((resolve) => {
    release = resolve;
  });
  const transport: Transport = (url, init) =>
    String(url) === "/api/r5/pce/timeseries/executions"
      ? late
      : h.transport(url, init);
  render(
    <Workbench
      modernPce
      transport={transport}
      fields={{
        asset: CPI.identity,
        asset_revision: CPI.revision,
        execution_asset: "all",
        job: "cold-24",
      }}
    />,
  );
  await flush();
  expect(screen.getByText("第 2 页")).toBeTruthy();
  await act(async () =>
    release(
      response({
        executions: Array.from({ length: 22 }, (_, i) => ({
          ...seed,
          id: `late-pce-${i}`,
          created_at: "2026-11-01T01:00:00Z",
        })),
      }),
    ),
  );
  expect(screen.getByText("第 3 页")).toBeTruthy();
  const selected = () =>
    screen
      .getByRole("region", { name: "执行记录" })
      .querySelector<HTMLElement>('tr[aria-selected="true"]')?.dataset
      .executionId;
  expect(selected()).toBe("cold-24");
  expect(
    screen.getByRole("region", { name: "执行详情主体" }).dataset.executionId,
  ).toBe("cold-24");
  fireEvent.click(screen.getByRole("button", { name: /选择 cold-23 / }));
  await flush();
  expect(selected()).toBe("cold-23");
  expect(
    screen.getByRole("region", { name: "执行详情主体" }).dataset.executionId,
  ).toBe("cold-23");
  expect(h.calls.every((c) => c.method === "GET")).toBe(true);
});

it.each([CPI, PCE])(
  "a concrete strategy excludes same-asset manual and unknown records: $identity",
  async (key) => {
    const h = fixture();
    const seed = h.records[0];
    h.records.splice(
      0,
      h.records.length,
      { ...seed, id: "original-manual" },
      { ...seed, id: "unknown-origin", origin: null },
      ...(key === CPI
        ? [{ ...seed, id: "known-automatic", origin: "automatic" }]
        : []),
    );
    render(
      <Workbench
        modernPce
        transport={h.transport}
        fields={{
          asset: key.identity,
          asset_revision: key.revision,
          execution_asset: keyOf(key),
          job: "original-manual",
        }}
      />,
    );
    await flush();
    fireEvent.click(
      await screen.findByRole("button", { name: "查看周期任务" }),
    );
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "返回此执行记录" }));
    await flush();
    expect(
      screen.getByRole("region", { name: "执行详情主体" }).dataset.executionId,
    ).toBe("original-manual");
    fireEvent.click(
      await screen.findByRole("button", { name: "查看周期任务" }),
    );
    await flush();
    fireEvent.click(screen.getByRole("button", { name: /^查看执行记录$/ }));
    await flush();
    const list = screen.getByRole("region", { name: "执行记录" });
    const ids = [
      ...list.querySelectorAll<HTMLElement>("tr[data-execution-id]"),
    ].map((el) => el.dataset.executionId);
    expect(ids).toEqual(key === CPI ? ["known-automatic"] : []);
    if (key === PCE)
      expect(
        within(list).getByText("当前已查询范围没有执行记录。"),
      ).toBeTruthy();
    expect(h.calls.every((c) => c.method === "GET")).toBe(true);
  },
);
