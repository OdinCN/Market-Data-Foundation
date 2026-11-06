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
import { FinancialDailyQuery } from "./FinancialDailyQuery";
import { financialResponse } from "./financialQueryTestFixture";
import type { Transport } from "./api";
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
function setup(transport: Transport) {
  return render(
    <FinancialDailyQuery
      transport={transport}
      summary={null}
      help={null}
      activeTab="query"
    />,
  );
}
function change(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}
function select(label: string, ids: string[]) {
  fireEvent.click(screen.getByRole("button", { name: label }));
  const g = screen.getByRole("group", { name: label + "选项" });
  fireEvent.click(within(g).getByRole("button", { name: "清空" }));
  for (const id of ids)
    fireEvent.click(within(g).getByRole("checkbox", { name: new RegExp(id) }));
  fireEvent.keyDown(g, { key: "Escape" });
}
const table = () =>
  screen.getByRole("table", { name: "金融条件日频 范围数据" });
const rowIds = () =>
  Array.from(table().querySelectorAll("tbody tr")).map(
    (r) => `${r.getAttribute("data-date")}:${r.getAttribute("data-series")}`,
  );
it("uses the shared date-first multi-select form, real display fields, full-range descending bidirectional pages and unique same-day identities", async () => {
  const calls: string[] = [];
  setup(async (input) => {
    calls.push(String(input));
    return Response.json(financialResponse(String(input)));
  });
  const form = screen
    .getByRole("button", { name: "查询数据" })
    .closest("form")!;
  expect(form.querySelector(".primary")?.textContent).toBe("查询数据");
  expect(
    form.querySelector(".field-grid")!.textContent!.indexOf("开始日期"),
  ).toBeLessThan(
    form.querySelector(".field-grid")!.textContent!.indexOf("金融指标"),
  );
  expect(screen.queryByLabelText("仅查询本地")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "金融指标" }));
  expect(
    within(screen.getByRole("group", { name: "金融指标选项" }))
      .getAllByRole("checkbox")
      .filter((x) => (x as HTMLInputElement).checked),
  ).toHaveLength(10);
  fireEvent.keyDown(screen.getByRole("group", { name: "金融指标选项" }), {
    key: "Escape",
  });
  select("金融指标", ["DGS10$", "BAMLC0A0CM$"]);
  change("开始日期", "2026-10-01");
  change("结束日期", "2026-10-25");
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  await screen.findByRole("table", { name: "金融条件日频 范围数据" });
  expect(rowIds().slice(0, 2)).toEqual([
    "2026-10-25:DGS10",
    "2026-10-25:BAMLC0A0CM",
  ]);
  expect(new Set(rowIds()).size).toBe(40);
  expect(screen.getByText("第 1 / 2 页，每页最多 40 条")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  await screen.findByText("第 2 / 2 页，每页最多 40 条");
  expect(rowIds().slice(0, 2)).toEqual([
    "2026-10-05:DGS10",
    "2026-10-05:BAMLC0A0CM",
  ]);
  expect(rowIds().at(-1)).toBe("2026-10-01:BAMLC0A0CM");
  fireEvent.click(screen.getByRole("button", { name: "上一页" }));
  await screen.findByText("第 1 / 2 页，每页最多 40 条");
  expect(rowIds()[0]).toBe("2026-10-25:DGS10");
  select("显示指标", ["信用利差"]);
  expect(
    within(table()).queryByRole("columnheader", { name: "原生值（%）" }),
  ).toBeNull();
  expect(table().textContent).toContain("不适用");
  expect(calls.length).toBe(3);
  expect(
    calls.every(
      (u) => new URL(u, "http://fixture").searchParams.get("order") === "desc",
    ),
  ).toBe(true);
  select("金融指标", []);
  expect(
    (screen.getByRole("button", { name: "查询数据" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  expect(
    screen.queryByRole("table", { name: "金融条件日频 范围数据" }),
  ).toBeNull();
  expect(calls.length).toBe(3);
});
it.each([200, 400])(
  "captures and copies actual HTTP body byte-for-byte including whitespace, unknown fields and failure status=%s",
  async (status) => {
    const copied = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: copied },
    });
    let raw = "";
    setup(async (input) => {
      raw =
        " \n" +
        JSON.stringify(
          status === 200
            ? financialResponse(String(input), {
                provider_extension: { note: "原样精度", value: "1.230000" },
              })
            : { status: "UNKNOWN", classification: "financial_query_invalid" },
          null,
          3,
        ) +
        "\n ";
      return new Response(raw, {
        status,
        headers: { "Content-Type": "application/json" },
      });
    });
    fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
    await screen.findByRole("region", { name: "本次原始响应" });
    fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
    expect(screen.getByLabelText("原始 HTTP 正文").textContent).toBe(raw);
    expect(
      screen.getByRole("region", { name: "本次原始响应" }).textContent,
    ).toContain(String(status));
    expect(
      screen.getByRole("region", { name: "本次原始响应" }).textContent,
    ).toContain("北京时间");
    fireEvent.click(screen.getByRole("button", { name: "复制原始响应" }));
    await waitFor(() => expect(copied).toHaveBeenCalledWith(raw));
    if (status === 400)
      expect(screen.getByRole("alert").textContent).toContain(
        "本次查询结果未知",
      );
  },
);
it("cancels changes and unmounts, rejects wrong request identities and never restores late response or raw body", async () => {
  const pending: {
    url: string;
    resolve: (r: Response) => void;
    signal: AbortSignal;
  }[] = [];
  const transport: Transport = (input, init) =>
    new Promise((resolve) =>
      pending.push({
        url: String(input),
        resolve,
        signal: init!.signal as AbortSignal,
      }),
    );
  const rendered = setup(transport);
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  expect(pending.length).toBe(1);
  change("结束日期", "2026-10-30");
  expect(pending[0].signal.aborted).toBe(true);
  pending[0].resolve(Response.json(financialResponse(pending[0].url)));
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  expect(pending.length).toBe(2);
  pending[1].resolve(
    Response.json(financialResponse(pending[1].url, { rows: [] })),
  );
  await screen.findByRole("alert");
  expect(
    screen.queryByRole("table", { name: "金融条件日频 范围数据" }),
  ).toBeNull();
  expect(
    screen.getByRole("region", { name: "本次原始响应" }).textContent,
  ).not.toContain(pending[0].url);
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  expect(pending.length).toBe(3);
  rendered.unmount();
  expect(pending[2].signal.aborted).toBe(true);
  pending[2].resolve(Response.json(financialResponse(pending[2].url)));
});

it("treats a changed historical deep link as a new scope and cancels the old request", async () => {
  let resolve!: (r: Response) => void;
  let signal!: AbortSignal;
  let url = "";
  const transport: Transport = (input, init) => {
    url = String(input);
    signal = init!.signal as AbortSignal;
    return new Promise((r) => (resolve = r));
  };
  const rendered = setup(transport);
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  rendered.rerender(
    <FinancialDailyQuery
      transport={transport}
      summary={null}
      help={null}
      activeTab="query"
      initialAsOf="2026-10-01T00:00:00Z"
    />,
  );
  expect(signal.aborted).toBe(true);
  expect(
    (screen.getByLabelText("截止时间（北京时间）") as HTMLInputElement).value,
  ).toBe("2026-10-01T08:00:00");
  resolve(Response.json(financialResponse(url)));
  await waitFor(() =>
    expect(screen.queryByRole("region", { name: "本次原始响应" })).toBeNull(),
  );
  expect(
    screen.queryByRole("table", { name: "金融条件日频 范围数据" }),
  ).toBeNull();
});
