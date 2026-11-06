import "./offlineTestSetup";
import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import {
  FinancialCollection,
  financialRangeError,
} from "./FinancialCollection";
import {
  FinancialDailyWorkbench,
  FINANCIAL,
  type FinancialJob,
  type FinancialStatus,
} from "./FinancialDailyWorkbench";
import { financialIntentKey } from "./useFinancialSubmission";
import type { Transport } from "./api";
import type { Route } from "./workflowRoute";

const id = "00000000-0000-4000-8000-000000000004";
const status: FinancialStatus = {
  defaults: { start: "2006-11-05", end: "2026-11-05" },
  update_defaults: { start: "2026-10-23", end: "2026-11-05" },
  collection_limits: { latest_end: "2026-11-06", max_calendar_days: 10980 },
  series: [],
  cutoff_semantics: "MDF cutoff",
};
const job = (changes: Partial<FinancialJob> = {}): FinancialJob => ({
  request_id: id,
  state: "complete",
  created_at: "2026-11-05T12:00:00Z",
  command: { action: "update", ...status.update_defaults! },
  provider_requests: 3,
  steps: [],
  ...changes,
});
afterEach(() => {
  cleanup();
  localStorage.removeItem(financialIntentKey);
  vi.restoreAllMocks();
});
function fixture(
  post?: (body: any) => Promise<Response>,
  lookup?: (id: string) => FinancialJob | null,
) {
  const calls: { url: string; method: string; body: any }[] = [];
  const transport: Transport = async (u, i) => {
    const url = String(u),
      method = i?.method ?? "GET",
      body = i?.body ? JSON.parse(String(i.body)) : null;
    calls.push({ url, method, body });
    if (method === "POST")
      return post
        ? post(body)
        : Response.json({
            status: "OK",
            job: job({ request_id: body.request_id, command: body }),
          });
    if (url.endsWith("/jobs")) return Response.json({ status: "OK", jobs: [] });
    const j = lookup?.(url.split("/").at(-1)!);
    return Response.json(
      j
        ? { status: "OK", job: j }
        : { status: "UNKNOWN", classification: "financial_job_not_found" },
      { status: j ? 200 : 404 },
    );
  };
  return { calls, transport };
}
function mount(f: ReturnType<typeof fixture>, nav = vi.fn()) {
  return {
    ...render(
      <FinancialCollection
        transport={f.transport}
        generation={0}
        status={status}
        onExecution={nav}
      />,
    ),
    nav,
  };
}
const confirm = () =>
  fireEvent.click(
    screen.getByRole("checkbox", { name: "我确认将以上范围采集到开发测试库" }),
  );
const submit = () => screen.getByRole("button", { name: "提交日频作业" });
const value = (name: string) =>
  (screen.getByLabelText(name) as HTMLInputElement).value;
it("defaults to the documented fourteen-day update, keeps independent editable drafts and clears confirmation", async () => {
  const f = fixture();
  mount(f);
  expect(value("采集方式")).toBe("update");
  expect(value("采集开始日期")).toBe("2026-10-23");
  fireEvent.change(screen.getByLabelText("采集开始日期"), {
    target: { value: "2026-10-25" },
  });
  confirm();
  expect((submit() as HTMLButtonElement).disabled).toBe(false);
  fireEvent.change(screen.getByLabelText("采集方式"), {
    target: { value: "init" },
  });
  expect(value("采集开始日期")).toBe("2006-11-05");
  expect((submit() as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText("采集开始日期"), {
    target: { value: "2016-11-05" },
  });
  fireEvent.change(screen.getByLabelText("采集方式"), {
    target: { value: "update" },
  });
  expect(value("采集开始日期")).toBe("2026-10-25");
  fireEvent.change(screen.getByLabelText("采集方式"), {
    target: { value: "init" },
  });
  expect(value("采集开始日期")).toBe("2016-11-05");
  expect(f.calls.every((c) => c.method === "GET")).toBe(true);
});
it.each([
  ["2026-11-05", "2026-11-04", "不得晚于"],
  ["2026-02-30", "2026-11-05", "合法"],
  ["1990-01-01", "2026-11-05", "不得超过"],
  ["2026-11-05", "2026-11-07", "北京时间"],
])(
  "invalid range %s..%s never submits even when confirmed",
  async (start, end, message) => {
    const f = fixture();
    mount(f);
    expect(financialRangeError({ start, end }, 10980, "2026-11-06")).toContain(
      message,
    );
    // Use real form state for inverted/span/future, plus DateField's invalid-calendar guard.
    fireEvent.change(screen.getByLabelText("采集开始日期"), {
      target: { value: start },
    });
    fireEvent.change(screen.getByLabelText("采集结束日期"), {
      target: { value: end },
    });
    confirm();
    fireEvent.submit(submit().closest("form")!);
    expect(f.calls.filter((c) => c.method === "POST")).toHaveLength(0);
  },
);
it("deduplicates a pending POST, keeps the same request on uncertainty, then verifies a matching receipt", async () => {
  let reject!: (e: Error) => void;
  const f = fixture(
    () =>
      new Promise((_r, j) => {
        reject = j;
      }),
    (rid) => job({ request_id: rid }),
  );
  const { nav } = mount(f);
  confirm();
  const button = submit();
  fireEvent.click(button);
  fireEvent.submit(button.closest("form")!);
  await waitFor(() =>
    expect(f.calls.filter((c) => c.method === "POST")).toHaveLength(1),
  );
  const intent = JSON.parse(localStorage.getItem(financialIntentKey)!);
  expect(intent.command.action).toBe("update");
  reject(Error("offline"));
  await screen.findByText(/提交结果尚未确认/);
  expect((submit() as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "核实原请求" }));
  await waitFor(() => expect(nav).toHaveBeenCalledWith(intent.request_id));
  expect(f.calls.filter((c) => c.method === "POST")).toHaveLength(1);
  expect(
    f.calls.some(
      (c) => c.method === "GET" && c.url.endsWith("/jobs/" + intent.request_id),
    ),
  ).toBe(true);
  expect(localStorage.getItem(financialIntentKey)).toBeNull();
});
it("a reload retains an uncertain original ID and a 404 or mismatched receipt cannot release it", async () => {
  const intent = {
    request_id: id,
    command: { action: "update", ...status.update_defaults! },
  };
  localStorage.setItem(financialIntentKey, JSON.stringify(intent));
  const f = fixture();
  const view = mount(f);
  expect((submit() as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "核实原请求" }));
  await screen.findByText(/尚未取得匹配回执/);
  view.unmount();
  const g = fixture(undefined, (rid) =>
    job({
      request_id: rid,
      command: { action: "update", start: "2000-01-01", end: "2000-01-02" },
    }),
  );
  mount(g);
  fireEvent.click(screen.getByRole("button", { name: "核实原请求" }));
  await screen.findByText(/尚未取得匹配回执/);
  expect(localStorage.getItem(financialIntentKey)).toBe(JSON.stringify(intent));
  expect([...f.calls, ...g.calls].every((c) => c.method === "GET")).toBe(true);
});
it("a definite HTTP rejection never auto-retries and an explicit unchanged retry reuses request_id", async () => {
  const f = fixture(async () =>
    Response.json(
      { status: "UNKNOWN", classification: "financial_job_range_invalid" },
      { status: 400 },
    ),
  );
  mount(f);
  confirm();
  fireEvent.click(submit());
  await screen.findByText(/日期区间无效/);
  const first = f.calls.find((c) => c.method === "POST")!.body.request_id;
  expect(f.calls.filter((c) => c.method === "POST")).toHaveLength(1);
  expect((submit() as HTMLButtonElement).disabled).toBe(true);
  confirm();
  fireEvent.click(submit());
  await waitFor(() =>
    expect(f.calls.filter((c) => c.method === "POST")).toHaveLength(2),
  );
  expect(
    f.calls.filter((c) => c.method === "POST").map((c) => c.body.request_id),
  ).toEqual([first, first]);
});
it("late POST success after switching page never navigates, while its durable receipt safely clears the outbox", async () => {
  let done!: (r: Response) => void;
  const f = fixture(
    () =>
      new Promise((r) => {
        done = r;
      }),
  );
  const view = mount(f);
  confirm();
  fireEvent.click(submit());
  const body = f.calls.find((c) => c.method === "POST")!.body;
  view.unmount();
  done(
    Response.json({
      status: "OK",
      job: job({ request_id: body.request_id, command: body }),
    }),
  );
  await waitFor(() =>
    expect(localStorage.getItem(financialIntentKey)).toBeNull(),
  );
  expect(view.nav).not.toHaveBeenCalled();
});
it("unreadable or unwritable outbox blocks the mutation", async () => {
  localStorage.setItem(financialIntentKey, "bad");
  const f = fixture();
  const v = mount(f);
  confirm();
  expect((submit() as HTMLButtonElement).disabled).toBe(true);
  v.unmount();
  localStorage.removeItem(financialIntentKey);
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw Error("denied");
  });
  mount(f);
  confirm();
  fireEvent.click(submit());
  await screen.findByText(/本机请求身份无法可靠保存/);
  expect(f.calls.every((c) => c.method === "GET")).toBe(true);
});
it("manual briefs are whole-card actions, never replaced by the newer automatic job", async () => {
  const manual = job(),
    auto = job({
      request_id: "00000000-0000-4000-8000-000000000005",
      origin: "periodic",
    });
  const f = fixture();
  f.transport = async () =>
    Response.json({ status: "OK", jobs: [auto, manual] });
  const { nav } = mount(f);
  const aside = await screen.findByRole("complementary", { name: "执行简报" });
  const card = await within(aside).findByRole("button", {
    name: /金融日频 · 手动采集/,
  });
  expect(card.classList.contains("execution-brief-card")).toBe(true);
  expect(within(aside).queryByRole("button", { name: /自动策略/ })).toBeNull();
  fireEvent.click(card);
  expect(nav).toHaveBeenCalledWith(id);
});
it("execution statistics use persisted version counts, never valid rows or completed-step counts; coverage is read-only", async () => {
  const record = job({
    steps: [
      {
        series_id: "DGS10",
        start: "2026-10-23",
        end: "2026-11-05",
        source: "origin",
        state: "complete",
        valid_rows: 1000,
        result: { batch_id: "batch", saved_points: 3, saved_revisions: 1 },
      },
    ],
    coverage: {
      as_of: "2026-11-05T12:00:00Z",
      series: [
        {
          series_id: "DGS10",
          first: "2026-10-23",
          last: "2026-11-05",
          valid_rows: 10,
          explicit_missing: 1,
          calendar_days_without_row: 3,
          gap_explanation: "休市留缺",
        },
      ],
    },
  });
  const transport: Transport = async () =>
    Response.json({ status: "OK", job: record });
  const route: Route = {
    view: "collection",
    invalid: false,
    fields: new URLSearchParams({
      tab: "executions",
      job: id,
      asset: FINANCIAL.identity,
    }),
  };
  render(
    <FinancialDailyWorkbench
      route={route}
      navigate={vi.fn()}
      transport={transport}
      generation={0}
      detailOnly
    />,
  );
  const stats = await screen.findByLabelText("执行结果统计");
  expect(
    within(stats).getByText("新增观测版本（含修订）").closest(".fact")
      ?.textContent,
  ).toContain("3");
  expect(
    within(stats).getByText("新增修订").closest(".fact")?.textContent,
  ).toContain("1");
  expect(
    within(stats).getByText("本地回读").closest(".fact")?.textContent,
  ).toContain("未记录独立回读校验");
  fireEvent.click(screen.getByText("各指标覆盖与缺口"));
  expect(screen.getByRole("table", { name: "逐项覆盖与缺口" })).toBeTruthy();
  expect(screen.queryByText("选择一项查看详情")).toBeNull();
  expect(screen.queryByRole("button", { name: /选择 DGS10/ })).toBeNull();
});

it("late job A read cannot replace job B after selecting another execution", async () => {
  const other = "00000000-0000-4000-8000-000000000005";
  let done!: (r: Response) => void;
  const transport: Transport = async (u) =>
    String(u).endsWith("/jobs/" + id)
      ? new Promise((r) => {
          done = r;
        })
      : Response.json({ status: "OK", job: job({ request_id: other }) });
  const route: Route = {
    view: "collection",
    invalid: false,
    fields: new URLSearchParams({
      tab: "executions",
      job: id,
      asset: FINANCIAL.identity,
    }),
  };
  const view = render(
    <FinancialDailyWorkbench
      route={route}
      navigate={vi.fn()}
      transport={transport}
      generation={0}
      detailOnly
    />,
  );
  const next = { ...route, fields: new URLSearchParams(route.fields) };
  next.fields.set("job", other);
  view.rerender(
    <FinancialDailyWorkbench
      route={next}
      navigate={vi.fn()}
      transport={transport}
      generation={0}
      detailOnly
    />,
  );
  await screen.findByText(other);
  done(Response.json({ status: "OK", job: job() }));
  await waitFor(() => expect(screen.queryByText(id)).toBeNull());
  expect(screen.getByText(other)).toBeTruthy();
});
