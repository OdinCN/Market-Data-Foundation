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
import {
  CpiQueryApiHelp,
  HttpExchangePanel,
  RequestPreview,
} from "./CpiQueryDetails";
import type { CpiAvailability, CpiHttpExchange } from "./cpiTimeSeriesApi";
import {
  CPI_PROFILE,
  PPI_PROFILE,
  PCE_PROFILE,
  EMPLOYMENT_PROFILE,
} from "./macroTimeSeriesProfile";

const request = {
  method: "GET" as const,
  path: "/api/r5/cpi/timeseries/query?start=2026-05&end=2026-05",
};
const url = new URL(request.path, location.origin).href;
const exchange: CpiHttpExchange = {
  method: "GET",
  url: `${url}&basket=core`,
  status: 200,
  bodyText:
    '\n  { "unknown_integer":9007199254740993, "unknown_decimal":0.12345678901234567890123456789, "extra": [true] }\n\n',
  elapsedMs: 12,
  completedAt: "2026-10-29T12:00:00Z",
};
const targets = [
  ["复制请求预览URL", url],
  ["复制cURL", `curl --request GET '${url}'`],
  ["复制本次请求URL", exchange.url],
  ["复制原始响应", exchange.bodyText],
] as const;
const originalClipboard = Object.getOwnPropertyDescriptor(
  navigator,
  "clipboard",
);
const originalCopy = Object.getOwnPropertyDescriptor(document, "execCommand");
function clipboard(value: unknown) {
  Object.defineProperty(navigator, "clipboard", { configurable: true, value });
}
function legacyCopy(value: unknown) {
  Object.defineProperty(document, "execCommand", { configurable: true, value });
}
function renderBlocks() {
  const submit = vi.fn((event: React.FormEvent) => event.preventDefault());
  const view = render(
    <form onSubmit={submit}>
      <RequestPreview request={request} />
      <HttpExchangePanel exchange={exchange} showBody />
    </form>,
  );
  const details = view.container.querySelector("details")!;
  expect(details.open).toBe(false);
  expect(
    details.contains(screen.getByRole("button", { name: "复制cURL" })),
  ).toBe(true);
  fireEvent.click(screen.getByText("cURL 示例（当前服务地址）"));
  expect(details.open).toBe(true);
  return { ...view, details, submit };
}
afterEach(() => {
  cleanup();
  window.getSelection()?.removeAllRanges();
  if (originalClipboard)
    Object.defineProperty(navigator, "clipboard", originalClipboard);
  else Reflect.deleteProperty(navigator, "clipboard");
  if (originalCopy)
    Object.defineProperty(document, "execCommand", originalCopy);
  else Reflect.deleteProperty(document, "execCommand");
  vi.restoreAllMocks();
  vi.useRealTimers();
});

it.each(targets)(
  "copies the exact %s with icon-only feedback",
  async (label, text) => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    clipboard({ writeText });
    const { details, submit } = renderBlocks();
    const button = screen.getByRole("button", { name: label });
    expect(button.textContent).toBe("");
    expect(button.title).toBe(label);
    expect(button.querySelector("svg rect")).toBeTruthy();
    fireEvent.click(button);
    await screen.findByRole("button", { name: `${label}（已复制）` });
    expect(writeText).toHaveBeenCalledExactlyOnceWith(text);
    expect(button.querySelector("svg rect")).toBeNull();
    expect(button.querySelector("svg path")).toBeTruthy();
    expect(button.textContent).toBe("");
    expect(screen.queryByText(/已复制/)).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    expect(details.open).toBe(true);
    expect(submit).not.toHaveBeenCalled();
  },
);

it.each(["missing", "rejected"])(
  "copies all four targets with textarea fallback when clipboard is %s",
  async (mode) => {
    clipboard(
      mode === "missing"
        ? undefined
        : { writeText: vi.fn().mockRejectedValue(new Error("fixture")) },
    );
    let expected = "";
    const execCommand = vi.fn((_command: string) => {
      const active = document.activeElement as HTMLTextAreaElement;
      expect(active.tagName).toBe("TEXTAREA");
      expect(active.value).toBe(expected);
      expect(active.selectionStart).toBe(0);
      expect(active.selectionEnd).toBe(expected.length);
      return true;
    });
    legacyCopy(execCommand);
    renderBlocks();
    for (const [label, text] of targets) {
      expected = text;
      const button = screen.getByRole("button", { name: label });
      button.focus();
      fireEvent.click(button);
      await screen.findByRole("button", { name: `${label}（已复制）` });
      expect(button.querySelector("svg rect")).toBeNull();
      expect(document.querySelector("textarea")).toBeNull();
      expect(document.activeElement).toBe(button);
    }
    expect(execCommand).toHaveBeenCalledTimes(4);
    expect(
      execCommand.mock.calls.every(([command]) => command === "copy"),
    ).toBe(true);
  },
);

it.each(["unsupported", "false", "throws"])(
  "selects each exact original and offers manual copy when fallback is %s",
  async (mode) => {
    clipboard({ writeText: vi.fn().mockRejectedValue(new Error("fixture")) });
    legacyCopy(
      mode === "unsupported"
        ? undefined
        : vi.fn(() => {
            if (mode === "throws") throw new Error("fixture");
            return false;
          }),
    );
    const { details, submit } = renderBlocks();
    for (const [label, text] of targets) {
      const button = screen.getByRole("button", { name: label });
      fireEvent.click(button);
      const block = button.parentElement!;
      await waitFor(() =>
        expect(within(block).getByRole("status").textContent).toContain(
          "请按 Ctrl+C",
        ),
      );
      expect(window.getSelection()?.toString()).toBe(text);
      expect(document.activeElement?.textContent).toBe(text);
      expect(button.querySelector("svg rect")).toBeTruthy();
      expect(button.getAttribute("aria-label")).toBe(label);
      expect(document.querySelector("textarea")).toBeNull();
    }
    expect(details.open).toBe(true);
    expect(submit).not.toHaveBeenCalled();
    expect(screen.queryByText(/^已复制/)).toBeNull();
  },
);

it("restores the copy icon after two seconds and clears timers on changes and unmount", async () => {
  vi.useFakeTimers();
  clipboard({ writeText: vi.fn().mockResolvedValue(undefined) });
  const view = render(<HttpExchangePanel exchange={exchange} showBody />);
  const copyBody = async () => {
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "复制原始响应" }));
    });
  };
  await copyBody();
  expect(
    screen.getByRole("button", { name: "复制原始响应（已复制）" }),
  ).toBeTruthy();
  await act(async () => vi.advanceTimersByTime(2000));
  expect(
    screen.getByRole("button", { name: "复制原始响应" }).querySelector("rect"),
  ).toBeTruthy();
  await copyBody();
  expect(vi.getTimerCount()).toBe(1);
  view.rerender(
    <HttpExchangePanel
      exchange={{ ...exchange, bodyText: "new body" }}
      showBody
    />,
  );
  expect(vi.getTimerCount()).toBe(0);
  expect(screen.queryByRole("button", { name: /已复制/ })).toBeNull();
  await copyBody();
  view.unmount();
  expect(vi.getTimerCount()).toBe(0);
});

it.each([
  ["changed", "resolved"],
  ["changed", "rejected"],
  ["unmounted", "resolved"],
  ["unmounted", "rejected"],
])(
  "ignores %s code's late %s clipboard completion",
  async (lifecycle, outcome) => {
    vi.useFakeTimers();
    let resolve!: () => void;
    let reject!: (error: Error) => void;
    clipboard({
      writeText: vi.fn(
        () =>
          new Promise<void>((yes, no) => {
            resolve = yes;
            reject = no;
          }),
      ),
    });
    const execCommand = vi.fn(() => true);
    legacyCopy(execCommand);
    const view = render(<HttpExchangePanel exchange={exchange} showBody />);
    fireEvent.click(screen.getByRole("button", { name: "复制原始响应" }));
    if (lifecycle === "unmounted") view.unmount();
    else
      view.rerender(
        <HttpExchangePanel
          exchange={{ ...exchange, bodyText: "replacement" }}
          showBody
        />,
      );
    await act(async () => {
      if (outcome === "resolved") resolve();
      else reject(new Error("fixture"));
    });
    expect(execCommand).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /已复制/ })).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
    if (lifecycle === "changed")
      expect(screen.getByLabelText("原始 HTTP 正文").textContent).toBe(
        "replacement",
      );
  },
);

it.each([CPI_PROFILE, PPI_PROFILE, PCE_PROFILE, EMPLOYMENT_PROFILE])(
  "$label help shares the complete disclosure structure and uses the actual supplied request",
  (profile) => {
    const actualRequest = {
      method: "GET" as const,
      path: `${profile.basePath}/query?start=2025-10&end=2026-09&${profile.id === "employment" ? "series=LNS14000000" : "basket=core&adjustment=sa"}&query_mode=history&local_only=true&as_of_basis=available&as_of=2026-10-31T08%3A00%3A00%2B08%3A00`,
    };
    const availability: CpiAvailability = {
      busy: false,
      failure: null,
      legacy: false,
      data: {
        latest_months: {},
        observation_count: 0,
        revision_count: 0,
        batch_count: 0,
        web_capabilities: { query: true, collect: false, reason: null },
      },
    };
    const view = render(
      <CpiQueryApiHelp
        profile={profile}
        availability={availability}
        request={actualRequest}
      />,
    );
    expect(screen.getByText(/已开放查询/)).toBeTruthy();
    const parameters = within(screen.getByRole("table", { name: "查询参数" }));
    const defaultRow = parameters.getByText("local_only").closest("tr")!;
    expect(defaultRow.textContent).toContain(
      `否 / ${profile.id === "cpi" ? "true" : "false"}（Web）`,
    );
    expect(
      parameters.getByText("as_of_basis / vintage_mode").closest("tr")!
        .textContent,
    ).toContain("无截止 current: observed；history 或有截止: available");
    expect(
      parameters.getByText("measure").closest("tr")!.textContent,
    ).toContain("index");
    for (const title of [
      `${profile.series.length}个统计序列`,
      "返回字段、类型与单位",
      "来源与官方计算",
      "成功示例（字段节选，非本次响应）",
      "失败示例与诊断",
    ]) {
      const disclosure = screen.getByText(title).closest("details")!;
      expect(disclosure.open).toBe(false);
      fireEvent.click(screen.getByText(title));
      expect(disclosure.open).toBe(true);
    }
    expect(
      screen
        .getByRole("table", { name: "统计序列" })
        .querySelectorAll("tbody tr"),
    ).toHaveLength(profile.series.length);
    expect(
      screen.getByRole("table", { name: "返回字段" }).textContent,
    ).toContain("revision 1 仅首次本地观察");
    expect(screen.getByLabelText("请求 URL").textContent).toBe(
      `GET ${new URL(actualRequest.path, location.origin).href}`,
    );
    const example = JSON.parse(
      screen.getByLabelText(`${profile.label} 响应示例`).textContent!,
    );
    expect(example.persisted).toBe(false);
    if (profile.id === "employment") {
      expect(parameters.getByText("series")).toBeTruthy();
      expect(parameters.queryByText("basket")).toBeNull();
      expect(
        example.points.map((p: { value_unit: string }) => p.value_unit),
      ).toEqual(["thousand_jobs", "percent", "USD/hour"]);
      expect(
        example.points
          .slice(0, 2)
          .every((p: object) => !("mom_pct" in p) && !("yoy_pct" in p)),
      ).toBe(true);
      expect(
        example.points.every(
          (p: object) => !("batch_id" in p) && !("index_value" in p),
        ),
      ).toBe(true);
      expect("measure" in example).toBe(false);
      expect("unit" in example).toBe(false);
      expect("measure_complete" in example).toBe(false);
    } else {
      expect(parameters.getByText("basket")).toBeTruthy();
      expect(example.unit).toBe("index");
    }
    view.rerender(
      <CpiQueryApiHelp
        profile={profile}
        availability={{
          ...availability,
          data: {
            ...availability.data!,
            web_capabilities: { query: false, collect: false, reason: null },
          },
        }}
        request={actualRequest}
      />,
    );
    expect(screen.getByText(/未开放查询/)).toBeTruthy();
    view.rerender(
      <CpiQueryApiHelp
        profile={profile}
        availability={{ ...availability, failure: "offline" }}
        request={actualRequest}
      />,
    );
    expect(screen.getByText(/服务状态暂未核实/)).toBeTruthy();
  },
);
