import { afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { CpiTimeSeriesStrategy } from "./CpiTimeSeriesCollection";
import type { Transport } from "./api";
import {
  CPI_TIMESERIES,
  type CpiAvailability,
  type CpiStrategy,
} from "./cpiTimeSeriesApi";
const availability: CpiAvailability = {
  legacy: false,
  busy: false,
  failure: null,
  data: {
    latest_months: {},
    observation_count: 0,
    revision_count: 0,
    batch_count: 0,
    web_capabilities: { query: true, collect: true, reason: null },
  },
};
const response = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });
const button = (name: string) =>
  screen.getByRole("button", { name }) as HTMLButtonElement;
const flush = () => act(async () => {});
const advance = () =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});
function fixture(initial: Partial<CpiStrategy> = {}) {
  let strategy: CpiStrategy = {
    exists: false,
    enabled: false,
    state: "not_created",
    history_start: "1999-01",
    worker: { state: "not_started", last_heartbeat_at: null },
    next_due: "2026-10-29T00:00:00Z",
    next_release_at: "2026-11-10T13:30:00Z",
    execution_state: "not_started",
    last_result: null,
    policy: { private_policy_marker: true },
    ...initial,
  };
  let failRead = false,
    failSave = false;
  const calls: {
    path: string;
    method: string;
    body?: unknown;
    signal?: AbortSignal | null;
  }[] = [];
  const transport: Transport = async (input, init) => {
    const path = String(input),
      method = init?.method ?? "GET",
      body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ path, method, body, signal: init?.signal });
    if (path === `${CPI_TIMESERIES}/strategy`) {
      if (method === "PUT") {
        if (failSave)
          return response({ classification: "synthetic_failure" }, 503);
        strategy = {
          ...strategy,
          ...body,
          exists: true,
          state: body.enabled ? "enabled" : "paused",
          worker: { state: "paused", last_heartbeat_at: null },
        };
      } else if (failRead) return response({}, 503);
      return response(strategy);
    }
    if (path === `${CPI_TIMESERIES}/schedule`)
      return response({
        calendar_status: "fresh",
        calendar: { error: "calendar_refresh_failed" },
        targets: [
          {
            reference_month: "2026-09",
            release_at: "2026-10-24T12:30:00Z",
            target: { state: "pending" },
            history: { state: "blocked" },
          },
          {
            reference_month: "2026-08",
            release_at: "2026-09-24T12:30:00Z",
            target: { state: "complete" },
            history: { state: "blocked" },
          },
        ],
      });
    return response({}, 404);
  };
  return {
    transport,
    calls,
    set: (value: Partial<CpiStrategy>) => {
      strategy = { ...strategy, ...value };
    },
    failRead: (value = true) => {
      failRead = value;
    },
    failSave: () => {
      failSave = true;
    },
  };
}
function select() {
  fireEvent.click(screen.getByText("官方发布自动采集"));
}
it("has one preset row and one inline toggle; detail has no create, history editor, duplicate toggle or JSON policy", async () => {
  const h = fixture();
  render(
    <CpiTimeSeriesStrategy
      transport={h.transport}
      availability={availability}
    />,
  );
  await flush();
  expect(screen.getAllByRole("table")).toHaveLength(1);
  expect(h.calls.every((c) => c.method === "GET")).toBe(true);
  select();
  const detail = within(
    screen.getByRole("complementary", { name: "周期任务详情" }),
  );
  expect(detail.queryByRole("button")).toBeNull();
  expect(detail.queryByRole("textbox")).toBeNull();
  expect(screen.queryByText(/private_policy_marker/)).toBeNull();
  expect(
    screen.queryByRole("button", { name: /创建|刷新|新建周期/ }),
  ).toBeNull();
  expect(screen.getAllByRole("button", { name: "启动" })).toHaveLength(1);
  fireEvent.click(button("启动"));
  await flush();
  fireEvent.click(button("暂停"));
  await flush();
  expect(h.calls.filter((c) => c.method === "PUT").map((c) => c.body)).toEqual([
    { enabled: true },
    { enabled: false },
  ]);
  expect(detail.getByText("策略已暂停。")).toBeTruthy();
  expect(detail.getByText("后台维护时间（非官方日历时间）")).toBeTruthy();
  expect(detail.getByText("2026-11-10 21:30:00 北京时间")).toBeTruthy();
  expect(detail.getByText("等待当期采集")).toBeTruthy();
  expect(detail.getByText("受阻")).toBeTruthy();
  expect(
    detail.getByText(/最近刷新失败.*calendar_refresh_failed/),
  ).toBeTruthy();
});
it("preserves selection and last evidence on polling error, blocks toggle until a successful read", async () => {
  vi.useFakeTimers();
  const h = fixture({ exists: true, enabled: true, state: "enabled" });
  render(
    <CpiTimeSeriesStrategy
      transport={h.transport}
      availability={availability}
    />,
  );
  await flush();
  select();
  h.failRead();
  await advance();
  expect(screen.getByRole("heading", { name: "CPI 策略详情" })).toBeTruthy();
  expect(screen.getByText(/状态已陈旧/)).toBeTruthy();
  expect(button("暂停").disabled).toBe(true);
  h.failRead(false);
  h.set({
    enabled: false,
    state: "paused",
    worker: { state: "stale", last_heartbeat_at: "2026-10-29T00:01:02Z" },
  });
  await advance();
  expect(button("启动").disabled).toBe(false);
  expect(screen.getByText("2026-10-29 08:01:02 北京时间")).toBeTruthy();
  expect(h.calls.filter((c) => c.method === "PUT")).toHaveLength(0);
});
it("does not cancel or resend an inflight PUT during polling, visibility changes or unmount", async () => {
  vi.useFakeTimers();
  const h = fixture();
  let release!: () => void;
  let signal: AbortSignal | null | undefined;
  let writes = 0;
  const transport: Transport = async (input, init) => {
    if (init?.method === "PUT") {
      writes++;
      signal = init.signal;
      await new Promise<void>((resolve) => {
        release = resolve;
      });
    }
    return h.transport(input, init);
  };
  const view = render(
    <CpiTimeSeriesStrategy transport={transport} availability={availability} />,
  );
  await flush();
  fireEvent.click(button("启动"));
  fireEvent.click(button("保存中…"));
  await advance();
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "hidden",
  });
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  view.unmount();
  expect(signal?.aborted).toBe(false);
  expect(writes).toBe(1);
  await act(async () => release());
  expect(writes).toBe(1);
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "visible",
  });
});
it("allows pausing after collection permission is revoked, but cannot start", async () => {
  const h = fixture({ exists: true, enabled: true, state: "enabled" });
  render(
    <CpiTimeSeriesStrategy
      transport={h.transport}
      availability={{
        ...availability,
        data: {
          ...availability.data!,
          web_capabilities: { query: true, collect: false, reason: "disabled" },
        },
      }}
    />,
  );
  await flush();
  expect(button("暂停").disabled).toBe(false);
  fireEvent.click(button("暂停"));
  await flush();
  expect(button("启动").disabled).toBe(true);
});
it("reconciles an uncertain PUT through read-only polling without repeating the write", async () => {
  vi.useFakeTimers();
  const h = fixture();
  render(
    <CpiTimeSeriesStrategy
      transport={h.transport}
      availability={availability}
    />,
  );
  await flush();
  h.failSave();
  h.failRead();
  fireEvent.click(button("启动"));
  await flush();
  expect(screen.getByText(/策略保存结果未知/)).toBeTruthy();
  expect(button("启动").disabled).toBe(true);
  h.failRead(false);
  h.set({ exists: true, enabled: true, state: "enabled" });
  await advance();
  expect(button("暂停").disabled).toBe(false);
  expect(h.calls.filter((c) => c.method === "PUT")).toHaveLength(1);
});
it("blocks toggles while a strategy read is inflight so pre-write reads cannot unlock an uncertain mutation", async () => {
  vi.useFakeTimers();
  const h = fixture();
  let release!: () => void;
  let reads = 0;
  const transport: Transport = async (input, init) => {
    if (
      String(input).endsWith("/strategy") &&
      init?.method !== "PUT" &&
      ++reads === 2
    )
      await new Promise<void>((resolve) => {
        release = resolve;
      });
    return h.transport(input, init);
  };
  render(
    <CpiTimeSeriesStrategy transport={transport} availability={availability} />,
  );
  await flush();
  await advance();
  expect(button("启动").disabled).toBe(true);
  fireEvent.click(button("启动"));
  expect(h.calls.filter((c) => c.method === "PUT")).toHaveLength(0);
  await act(async () => release());
  expect(button("启动").disabled).toBe(false);
  fireEvent.click(button("启动"));
  await flush();
  expect(h.calls.filter((c) => c.method === "PUT")).toHaveLength(1);
});

it.each([
  ["fresh", null, false, true],
  ["fresh", "calendar_http_403", true, true],
  ["expired", "calendar_http_403", true, false],
] as const)(
  "uses current calendar error and validity: %s / %s",
  async (calendar_status, error, failed, usable) => {
    const h = fixture();
    const transport: Transport = (input, init) =>
      String(input).endsWith("/schedule")
        ? Promise.resolve(
            response({
              calendar_status,
              calendar: {
                error,
                last_refresh_error: "calendar_http_403",
                acquired_at: "2026-10-28T19:48:31Z",
              },
              targets: [],
            }),
          )
        : h.transport(input, init);
    render(
      <CpiTimeSeriesStrategy
        transport={transport}
        availability={availability}
      />,
    );
    await flush();
    select();
    expect(screen.getByText("2026-10-29 03:48:31 北京时间")).toBeTruthy();
    expect(!!screen.queryByText(/最近刷新失败/)).toBe(failed);
    expect(!!screen.queryByText("日历可用")).toBe(usable);
    expect(!!screen.queryByText(/继续使用有效缓存/)).toBe(failed && usable);
    expect(screen.queryByText(/IP.*封禁/)).toBeNull();
    expect(h.calls.every((c) => c.method === "GET")).toBe(true);
  },
);

it("renders month cards with stage grids, real attempt evidence and separate history results", async () => {
  const h = fixture();
  const transport: Transport = (input, init) =>
    String(input).endsWith("/schedule")
      ? Promise.resolve(
          response({
            calendar_status: "fresh",
            calendar: { error: null },
            targets: [
              {
                reference_month: "2026-09",
                release_at: "2026-10-24T12:30:00Z",
                target: {
                  state: "complete",
                  last_attempt_at: "2026-10-24T12:31:00Z",
                  result: { saved_points: 4 },
                },
                history: {
                  state: "pending",
                  next_attempt_at: "2026-10-25T00:00:00Z",
                  result: { saved_revisions: 2 },
                },
              },
            ],
          }),
        )
      : h.transport(input, init);
  render(
    <CpiTimeSeriesStrategy transport={transport} availability={availability} />,
  );
  await flush();
  select();
  const list = screen.getByRole("list", { name: "CPI 月份计划" });
  expect(list.querySelector("table")).toBeNull();
  const card = within(list).getByRole("listitem");
  expect(card.querySelector("header")?.textContent).toContain("2026-09");
  expect(card.querySelectorAll(".month-stage-grid > div")).toHaveLength(4);
  expect(within(card).getByText("2026-10-24 20:31:00")).toBeTruthy();
  expect(within(card).getByText("2026-10-25 08:00:00")).toBeTruthy();
  expect(within(card).getByText("新增观测")).toBeTruthy();
  expect(within(card).getByText("新增修订")).toBeTruthy();
  expect(card.textContent).not.toContain("北京时间");
  expect(screen.getByText(/计划时间不代表已经执行/)).toBeTruthy();
  expect(h.calls.every((c) => c.method === "GET")).toBe(true);
});

it.each([
  ["2026-10-28T19:48:31Z", true],
  ["2026-10-27T19:48:31Z", false],
] as const)(
  "keeps degraded cached plans unconfirmed regardless of acquisition age: %s",
  async (acquired_at, _usable) => {
    const h = fixture();
    const transport: Transport = (input, init) =>
      String(input).endsWith("/schedule")
        ? Promise.resolve(
            response({
              calendar_status: "degraded_cached_calendar",
              checked_at: "2026-10-29T12:00:00Z",
              calendar: { error: "calendar_http_403", acquired_at },
              targets: [],
            }),
          )
        : h.transport(input, init);
    render(
      <CpiTimeSeriesStrategy
        transport={transport}
        availability={availability}
      />,
    );
    await flush();
    select();
    expect(screen.queryByText("日历可用")).toBeNull();
    expect(screen.queryByText(/继续使用有效缓存/)).toBeNull();
    expect(
      screen.getByText("本次日历刷新失败，保留已缓存计划，等待刷新确认。"),
    ).toBeTruthy();
  },
);
