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
import { HttpExchangePanel, RequestPreview } from "./CpiQueryDetails";
import type { CpiHttpExchange } from "./cpiTimeSeriesApi";

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
