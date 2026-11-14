import "./offlineTestSetup";
import { afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { Transport } from "./api";
import { FinancialDailyQuery } from "./FinancialDailyQuery";
import { FinancialCollection } from "./FinancialCollection";
import { financialResponse } from "./financialQueryTestFixture";
import { TdxDailyQuery, type TdxJob } from "./TdxDailyWorkbench";
import { TdxDailyCollection } from "./TdxDailyFamily";
import { tdxJobOrigin } from "./tdxJobPresentation";
import type { FamilyProps } from "./RuntimeFamily";
const date = "2026-11-11",
  id = "2679c123-8a3e-4ae9-8aa9-fce60c5f94e1";
const job: TdxJob = {
  request_id: id,
  state: "COMPLETE",
  origin: "manual",
  created_at: "2026-11-12T06:00:00Z",
  provider_requests: 2,
  command: { action: "update", start: date, end: date },
};
const props: Omit<FamilyProps, "transport"> = {
  route: { view: "assets", invalid: false, fields: new URLSearchParams() },
  navigate: vi.fn(),
  generation: 0,
};
afterEach(() => {
  cleanup();
  localStorage.clear();
  sessionStorage.clear();
  vi.restoreAllMocks();
});
function mountQuery(family: string, transport: Transport) {
  return render(
    family === "TDX" ? (
      <TdxDailyQuery {...props} transport={transport} />
    ) : (
      <FinancialDailyQuery
        transport={transport}
        summary={null}
        help={null}
        activeTab="query"
      />
    ),
  );
}
const change = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
function response(family: string, url: string) {
  return family === "TDX"
    ? {
        rows: [
          {
            market: 1,
            instrument: "600519",
            trading_day: date,
            open: 1000,
            high: 2000,
            low: 500,
            close: 1500,
            volume: 10,
            amount: 20000,
            available_at: "2026-11-11T10:00:00Z",
          },
        ],
        total: 1,
        next_offset: null,
        provider_requests: 0,
        as_of:
          new URL(url, "http://fixture").searchParams.get("as_of") ??
          "2026-11-12T07:00:00Z",
      }
    : financialResponse(url);
}
function fixture(
  family: string,
  query?: (url: string, init?: RequestInit) => Promise<Response>,
) {
  const calls: { url: string; method: string; body: any }[] = [];
  let raw = "";
  const transport: Transport = async (input, init) => {
    const url = String(input);
    calls.push({
      url,
      method: init?.method ?? "GET",
      body: init?.body ? JSON.parse(String(init.body)) : null,
    });
    if (url.includes("/query?")) {
      if (query) return query(url, init);
      raw = " \n" + JSON.stringify(response(family, url), null, 3) + "\n ";
      return new Response(raw, {
        headers: { "Content-Type": "application/json" },
      });
    }
    if (url.endsWith("/status"))
      return Response.json({
        enabled: true,
        collect: true,
        defaults: { start: "1990-01-01", end: date },
      });
    if (url.endsWith("/summary")) return new Promise<Response>(() => {});
    if (url.includes("/coverage?"))
      return Response.json({
        complete: false,
        markets: [],
        missing: [],
        missing_total: 0,
      });
    if (init?.method === "POST") {
      const command = JSON.parse(String(init.body));
      return Response.json(
        family === "TDX"
          ? { ...job, request_id: command.request_id, command }
          : {
              status: "OK",
              job: {
                ...job,
                state: "complete",
                steps: [],
                request_id: command.request_id,
                command,
              },
            },
      );
    }
    if (url.endsWith("/jobs"))
      return Response.json({
        status: "OK",
        jobs:
          family === "TDX" ? [job] : [{ ...job, state: "complete", steps: [] }],
      });
    throw Error("unexpected fixture " + url);
  };
  return { transport, calls, raw: () => raw };
}
it.each(["金融日频", "TDX"])(
  "%s uses the actual shared date/history controls, precise live preview, one explicit read and raw/table views",
  async (family) => {
    const f = fixture(family);
    const view = mountQuery(family, f.transport);
    if (family === "TDX")
      await waitFor(() =>
        expect(
          (screen.getByLabelText("结束日期") as HTMLInputElement).value,
        ).toBe(date),
      );
    expect(
      screen.getByLabelText("开始日期").closest(".datefield"),
    ).toBeTruthy();
    expect(view.container.querySelector(".empty")).toBeNull();
    change("开始日期", date);
    change("结束日期", date);
    fireEvent.click(screen.getByRole("checkbox", { name: "历史截止查询" }));
    change("截止时间（北京时间）", "2026-11-12T15:00:00.123456");
    expect(
      (screen.getByLabelText("截止时间（北京时间）") as HTMLInputElement).value,
    ).toBe("2026-11-12T15:00:00.123456");
    expect(view.container.textContent).toContain("123456");
    fireEvent.click(screen.getAllByText("cURL 示例（当前服务地址）").at(-1)!);
    expect(view.container.textContent).toContain("curl --request GET");
    expect(f.calls.filter((c) => c.url.includes("/query?"))).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
    await screen.findByRole("region", { name: "本次原始响应" });
    await waitFor(() =>
      expect(
        (screen.getByRole("button", { name: "查询数据" }) as HTMLButtonElement)
          .disabled,
      ).toBe(false),
    );
    const queries = f.calls.filter((c) => c.url.includes("/query?"));
    expect(queries).toHaveLength(1);
    const q = new URL(queries[0].url, "http://fixture").searchParams;
    expect(q.get("local_only")).toBe("true");
    expect(q.get("order")).toBe("desc");
    expect(q.get("as_of")).toContain(".123456");
    expect(q.get("as_of")).toBe(
      family === "TDX"
        ? "2026-11-12T15:00:00.123456+08:00"
        : "2026-11-12T07:00:00.123456Z",
    );
    fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
    expect(screen.getByLabelText("原始 HTTP 正文").textContent).toBe(f.raw());
    fireEvent.click(screen.getByRole("tab", { name: "表格" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "历史截止查询" }));
    expect(screen.queryByLabelText("截止时间（北京时间）")).toBeNull();
    expect(f.calls.filter((c) => c.url.includes("/query?"))).toHaveLength(1);
    expect(
      screen.getByRole("region", { name: "请求预览" }).textContent,
    ).not.toContain("as_of=");
  },
);
it.each(["金融日频", "TDX"])(
  "%s aborts changed parameters and ignores late results without automatic or duplicate reads",
  async (family) => {
    const pending: {
      url: string;
      signal: AbortSignal;
      resolve: (r: Response) => void;
    }[] = [];
    const f = fixture(
      family,
      (url, init) =>
        new Promise((resolve) =>
          pending.push({ url, signal: init!.signal as AbortSignal, resolve }),
        ),
    );
    mountQuery(family, f.transport);
    if (family === "TDX")
      await waitFor(() =>
        expect(
          (screen.getByLabelText("结束日期") as HTMLInputElement).value,
        ).toBe(date),
      );
    change("开始日期", date);
    change("结束日期", date);
    const button = screen.getByRole("button", { name: "查询数据" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(pending).toHaveLength(1);
    change("开始日期", "2026-11-10");
    expect(pending[0].signal.aborted).toBe(true);
    await act(async () =>
      pending[0].resolve(Response.json(response(family, pending[0].url))),
    );
    expect(screen.queryByRole("region", { name: "本次原始响应" })).toBeNull();
    expect(pending).toHaveLength(1);
    fireEvent.click(button);
    expect(pending).toHaveLength(2);
    await act(async () =>
      pending[1].resolve(Response.json(response(family, pending[1].url))),
    );
    await screen.findByRole("region", { name: "本次原始响应" });
  },
);
it.each(["金融日频", "TDX"])(
  "%s shares editable independent collection ranges, confirmation and real recent-job detail navigation",
  async (family) => {
    const f = fixture(family),
      navigate = vi.fn(),
      onExecution = vi.fn();
    render(
      family === "TDX" ? (
        <TdxDailyCollection
          {...props}
          transport={f.transport}
          navigate={navigate}
        />
      ) : (
        <FinancialCollection
          transport={f.transport}
          generation={0}
          status={{
            defaults: { start: "2006-11-11", end: date },
            update_defaults: { start: "2026-10-29", end: date },
            series: [],
            cutoff_semantics: "MDF",
            web_capabilities: { query: true, collect: true },
          }}
          onExecution={onExecution}
        />
      ),
    );
    await waitFor(() =>
      expect(
        (screen.getByLabelText("采集结束日期") as HTMLInputElement).value,
      ).toBe(date),
    );
    expect((screen.getByLabelText("采集方式") as HTMLSelectElement).value).toBe(
      "update",
    );
    expect(
      screen.getByLabelText("采集开始日期").closest(".datefield"),
    ).toBeTruthy();
    change("采集开始日期", "2026-11-09");
    change("采集方式", "init");
    change("采集开始日期", "2020-01-01");
    change("采集方式", "update");
    expect(
      (screen.getByLabelText("采集开始日期") as HTMLInputElement).value,
    ).toBe("2026-11-09");
    const side = screen.getByRole("complementary", { name: "执行简报" });
    const card = await within(side).findByRole("button", {
      name: new RegExp(id),
    });
    fireEvent.click(card);
    if (family === "TDX")
      expect(navigate).toHaveBeenCalledWith(
        "collection",
        expect.objectContaining({ job: id, tab: "executions" }),
      );
    else expect(onExecution).toHaveBeenCalledWith(id);
    const submit = screen.getByRole("button", { name: /^提交 .* 采集$/ });
    expect((submit as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "我确认将以上范围采集到开发测试库",
      }),
    );
    fireEvent.click(submit);
    fireEvent.click(submit);
    await waitFor(() =>
      expect(f.calls.filter((c) => c.method === "POST")).toHaveLength(1),
    );
    expect(f.calls.find((c) => c.method === "POST")!.body).toMatchObject({
      action: "update",
      start: "2026-11-09",
      end: date,
    });
  },
);
it("shows the job origin returned by the API", () => {
  expect(tdxJobOrigin({ ...job, origin: "periodic" })).toBe("自动策略");
  expect(tdxJobOrigin({ ...job, origin: undefined })).toBe("触发来源待核实");
  expect(tdxJobOrigin(job)).toBe("手动采集");
});
it.each(["金融日频", "TDX"])(
  "%s rejects a history response that differs by one microsecond",
  async (family) => {
    const f = fixture(family, async (url) =>
      Response.json({
        ...response(family, url),
        as_of: new URL(url, "http://fixture").searchParams
          .get("as_of")!
          .replace("123456", "123457"),
      }),
    );
    mountQuery(family, f.transport);
    if (family === "TDX")
      await waitFor(() =>
        expect(
          (screen.getByLabelText("结束日期") as HTMLInputElement).value,
        ).toBe(date),
      );
    change("开始日期", date);
    change("结束日期", date);
    fireEvent.click(screen.getByRole("checkbox", { name: "历史截止查询" }));
    change("截止时间（北京时间）", "2026-11-12T15:00:00.123456");
    fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
    await screen.findByRole("alert");
    expect(screen.queryByRole("region", { name: "日线结果" })).toBeNull();
    expect(
      screen.queryByRole("table", { name: "金融条件日频 范围数据" }),
    ).toBeNull();
    expect(f.calls.filter((c) => c.url.includes("/query?"))).toHaveLength(1);
  },
);

it.each(["金融日频", "TDX"])(
  "%s pages through one server cursor in both directions without a second client pager or extra coverage reads",
  async (family) => {
    const size = family === "TDX" ? 100 : 40;
    const f = fixture(family, async (url) => {
      const offset = Number(
        new URL(url, "http://fixture").searchParams.get("offset"),
      );
      return Response.json({
        ...response(family, url),
        total: family === "TDX" ? size + 1 : response(family, url).total,
        next_offset: offset === 0 ? size : null,
      });
    });
    mountQuery(family, f.transport);
    if (family === "TDX")
      await waitFor(() =>
        expect(
          (screen.getByLabelText("结束日期") as HTMLInputElement).value,
        ).toBe(date),
      );
    change("开始日期", family === "TDX" ? date : "2026-11-06");
    change("结束日期", date);
    fireEvent.click(screen.getByRole("button", { name: /^查询数据$/ }));
    const label = `${family === "TDX" ? "TDX 日线" : "金融条件日频"} 结果分页`;
    await screen.findByLabelText(label);
    const button = (name: string) =>
      within(screen.getByLabelText(label)).getByRole("button", {
        name,
      }) as HTMLButtonElement;
    await waitFor(() => expect(button("下一页").disabled).toBe(false));
    expect(button("上一页").disabled).toBe(true);
    fireEvent.click(button("下一页"));
    await waitFor(() => expect(button("上一页").disabled).toBe(false));
    expect(button("下一页").disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "后一页记录" })).toBeNull();
    fireEvent.click(button("上一页"));
    await waitFor(() => expect(button("下一页").disabled).toBe(false));
    expect(
      f.calls
        .filter((c) => c.url.includes("/query?"))
        .map((c) =>
          new URL(c.url, "http://fixture").searchParams.get("offset"),
        ),
    ).toEqual(["0", String(size), "0"]);
    expect(f.calls.filter((c) => c.url.includes("/coverage?")).length).toBe(
      family === "TDX" ? 2 : 0,
    );
  },
);

it("TDX keeps row paging usable while independent coverage is pending, and cancels stale coverage on edits", async () => {
  const base = fixture("TDX");
  let coverageSignal: AbortSignal | null | undefined;
  let finishCoverage: (response: Response) => void = () => {};
  const calls: string[] = [];
  const transport: Transport = async (input, init) => {
    const url = String(input);
    calls.push(url);
    if (url.includes("/coverage?")) {
      coverageSignal = init?.signal;
      return new Promise<Response>((resolve) => {
        finishCoverage = resolve;
      });
    }
    if (url.includes("/query?"))
      return Response.json({
        ...response("TDX", url),
        total: 101,
        next_offset: url.includes("offset=100") ? null : 100,
      });
    return base.transport(input, init);
  };
  mountQuery("TDX", transport);
  await waitFor(() =>
    expect((screen.getByLabelText("结束日期") as HTMLInputElement).value).toBe(
      date,
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  await waitFor(() =>
    expect(
      (screen.getByRole("button", { name: /^下一页$/ }) as HTMLButtonElement)
        .disabled,
    ).toBe(false),
  );
  expect(
    screen.getByText("正在读取覆盖证据；已有日线可继续查看与翻页。"),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /^下一页$/ }));
  await waitFor(() =>
    expect(
      (screen.getByRole("button", { name: /^上一页$/ }) as HTMLButtonElement)
        .disabled,
    ).toBe(false),
  );
  expect(calls.filter((url) => url.includes("/query?"))).toHaveLength(2);
  expect(calls.filter((url) => url.includes("/coverage?"))).toHaveLength(1);
  expect(coverageSignal?.aborted).toBe(false);
  change("证券代码", "000001");
  expect(coverageSignal?.aborted).toBe(true);
  await act(async () =>
    finishCoverage(
      Response.json({
        markets: [],
        missing: [],
        missing_total: 0,
        complete: true,
      }),
    ),
  );
  expect(screen.queryByText(/覆盖结论/)).toBeNull();
});

it("TDX uses the Beijing post-close boundary while status is pending", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-11-12T16:20:00Z"));
  try {
    mountQuery("TDX", async () => new Promise<Response>(() => {}));
    expect((screen.getByLabelText("结束日期") as HTMLInputElement).value).toBe(
      "2026-11-12",
    );
    expect(
      (screen.getByRole("button", { name: "查询数据" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  } finally {
    vi.useRealTimers();
  }
});

it.each(["金融日频", "TDX"])(
  "%s fills untouched fields when status arrives after an early edit in each collection mode",
  async (family) => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-12T16:20:00Z"));
    try {
      const f = fixture(family);
      let resolveStatus: (response: Response) => void = () => {};
      const transport: Transport = (input, init) =>
        String(input).endsWith("/status")
          ? new Promise<Response>((resolve) => {
              resolveStatus = resolve;
            })
          : f.transport(input, init);
      const status = {
        defaults: { start: "2006-11-11", end: date },
        update_defaults: { start: "2026-10-29", end: date },
        series: [],
        cutoff_semantics: "fixture",
        web_capabilities: { query: true, collect: true },
      };
      const financial = (ready: boolean) => (
        <FinancialCollection
          transport={transport}
          generation={0}
          status={ready ? status : null}
          onExecution={vi.fn()}
        />
      );
      const view = render(
        family === "TDX" ? (
          <TdxDailyCollection {...props} transport={transport} />
        ) : (
          financial(false)
        ),
      );
      if (family === "TDX")
        expect(
          (screen.getByLabelText("采集结束日期") as HTMLInputElement).value,
        ).toBe("2026-11-12");
      change("采集开始日期", "2026-11-09");
      change("采集方式", "init");
      change("采集开始日期", "2020-01-01");
      if (family === "TDX")
        await act(async () =>
          resolveStatus(
            Response.json({
              enabled: true,
              collect: true,
              defaults: status.defaults,
            }),
          ),
        );
      else view.rerender(financial(true));
      await waitFor(() =>
        expect(
          (screen.getByLabelText("采集结束日期") as HTMLInputElement).value,
        ).toBe(date),
      );
      expect(
        (screen.getByLabelText("采集开始日期") as HTMLInputElement).value,
      ).toBe("2020-01-01");
      change("采集方式", "update");
      expect(
        (screen.getByLabelText("采集开始日期") as HTMLInputElement).value,
      ).toBe("2026-11-09");
      expect(
        (screen.getByLabelText("采集结束日期") as HTMLInputElement).value,
      ).toBe(date);
      fireEvent.click(
        screen.getByRole("checkbox", {
          name: "我确认将以上范围采集到开发测试库",
        }),
      );
      expect(
        (
          screen.getByRole("button", {
            name: /^提交 .* 采集$/,
          }) as HTMLButtonElement
        ).disabled,
      ).toBe(false);
      expect(f.calls.some((c) => c.method === "POST")).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  },
);
