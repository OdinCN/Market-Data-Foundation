import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { DateField, type DateFieldProps } from "./DateField";
import { beijingInputToIso } from "./beijingTime";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});
function Controlled(props: Partial<DateFieldProps> = {}) {
  const [value, setValue] = useState(props.value ?? "2024-02-15");
  return (
    <DateField
      label="日期字段"
      type="date"
      {...props}
      value={value}
      onChange={(next) => {
        props.onChange?.(next);
        setValue(next);
      }}
    />
  );
}
function openCalendar() {
  const trigger = screen.getByRole("button", { name: "日期字段：打开日历" });
  trigger.focus();
  fireEvent.click(trigger);
  return trigger;
}
const confirm = () =>
  fireEvent.click(screen.getByRole("button", { name: "确定" }));
const input = () => screen.getByLabelText("日期字段") as HTMLInputElement;

describe("precision and original statistical dates", () => {
  it("offers exactly twelve months with year navigation and an explicit commit", () => {
    const changed = vi.fn();
    render(<Controlled type="month" value="2024-02" onChange={changed} />);
    expect(input().type).toBe("text");
    openCalendar();
    expect(
      within(screen.getByRole("grid", { name: "月份" })).getAllByRole("button"),
    ).toHaveLength(12);
    fireEvent.click(screen.getByRole("button", { name: "下一年" }));
    fireEvent.click(screen.getByRole("button", { name: "2025-12" }));
    expect(changed).not.toHaveBeenCalled();
    confirm();
    expect(changed).toHaveBeenCalledExactlyOnceWith("2025-12");
    expect(input().value).toBe("2025-12");
  });
  it("uses a Monday-first leap-year calendar and preserves the selected date", () => {
    render(<Controlled />);
    openCalendar();
    expect(
      screen.getAllByRole("columnheader").map((el) => el.textContent),
    ).toEqual(["一", "二", "三", "四", "五", "六", "日"]);
    const firstRow = screen.getAllByRole("row")[1];
    expect(within(firstRow).getAllByRole("gridcell")[3].textContent).toBe("1");
    fireEvent.click(screen.getByRole("button", { name: "2024-02-29" }));
    confirm();
    expect(input().value).toBe("2024-02-29");
    openCalendar();
    fireEvent.click(screen.getByRole("button", { name: "下个月" }));
    expect(screen.getByRole("button", { name: "2024-03-31" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "上一年" }));
    expect(screen.getByRole("button", { name: "2023-03-31" })).toBeTruthy();
  });
  it("provides distinct hour, minute, second controls and commits Beijing wall time", () => {
    const changed = vi.fn();
    render(
      <Controlled
        type="datetime-local"
        value="2024-02-29T10:11:12"
        onChange={changed}
      />,
    );
    openCalendar();
    for (const label of ["时", "分", "秒"])
      expect(screen.getByLabelText(label)).toBeTruthy();
    expect(
      document.querySelector(
        'input[type="date"], input[type="month"], input[type="datetime-local"]',
      ),
    ).toBeNull();
    fireEvent.change(screen.getByLabelText("时"), { target: { value: "23" } });
    fireEvent.change(screen.getByLabelText("分"), { target: { value: "59" } });
    fireEvent.change(screen.getByLabelText("秒"), { target: { value: "58" } });
    expect(changed).not.toHaveBeenCalled();
    confirm();
    expect(changed).toHaveBeenCalledExactlyOnceWith("2024-02-29T23:59:58");
  });
  it("does not emit unchanged values and normalizes actual manual edits", () => {
    const changed = vi.fn();
    render(
      <Controlled
        type="datetime-local"
        value="2024-02-29T10:11:00"
        onChange={changed}
      />,
    );
    openCalendar();
    confirm();
    expect(changed).not.toHaveBeenCalled();
    fireEvent.change(input(), { target: { value: "2024-02-29T10:11:00" } });
    expect(changed).not.toHaveBeenCalled();
    fireEvent.change(input(), { target: { value: "2024-02-29T12:34" } });
    expect(changed).toHaveBeenCalledExactlyOnceWith("2024-02-29T12:34:00");
    expect(input().checkValidity()).toBe(true);
    confirm();
    expect(changed).toHaveBeenCalledTimes(1);
    expect(input().value).toBe("2024-02-29T12:34:00");
  });
});

describe("drafts, keyboard and forms", () => {
  it("focuses the selected date and commits valid manual edits immediately", () => {
    const changed = vi.fn();
    render(<Controlled onChange={changed} />);
    const trigger = openCalendar();
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "2024-02-15" }),
    );
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    expect(document.activeElement).toBe(trigger);
    input().focus();
    fireEvent.change(input(), { target: { value: "2024-03-01" } });
    expect(changed).toHaveBeenCalledExactlyOnceWith("2024-03-01");
    fireEvent.keyDown(input(), { key: "Tab" });
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "确定" }),
    );
    fireEvent.keyDown(document.activeElement!, { key: "Enter" });
    expect(changed).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(input());
  });
  it("cancels calendar drafts and preserves already committed manual edits", () => {
    const changed = vi.fn();
    render(<Controlled onChange={changed} />);
    const trigger = openCalendar();
    fireEvent.click(screen.getByRole("button", { name: "2024-02-29" }));
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(input().value).toBe("2024-02-15");
    expect(document.activeElement).toBe(trigger);
    expect(changed).not.toHaveBeenCalled();
    input().focus();
    fireEvent.change(input(), { target: { value: "2024-03-12" } });
    expect(changed).toHaveBeenCalledExactlyOnceWith("2024-03-12");
    fireEvent.keyDown(input(), { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(input().value).toBe("2024-03-12");
    expect(document.activeElement).toBe(input());
  });
  it("propagates invalid manual text and blocks confirmation until corrected", () => {
    const changed = vi.fn();
    render(<Controlled onChange={changed} />);
    fireEvent.change(input(), { target: { value: "2024-02-30" } });
    expect(input().getAttribute("aria-invalid")).toBe("true");
    expect(input().checkValidity()).toBe(false);
    expect(
      (screen.getByRole("button", { name: "确定" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(changed).toHaveBeenCalledExactlyOnceWith("2024-02-30");
    confirm();
    expect(changed).toHaveBeenCalledTimes(1);
    fireEvent.change(input(), { target: { value: "2024-03-01" } });
    expect(changed).toHaveBeenLastCalledWith("2024-03-01");
    expect(input().checkValidity()).toBe(true);
    confirm();
    expect(changed).toHaveBeenCalledTimes(2);
  });
  it("cannot query a stale instant or fall back to now after invalid optional input", () => {
    const query = vi.fn();
    function Query() {
      const [cutoff, setCutoff] = useState("2024-02-29T10:11:12");
      return (
        <>
          <DateField
            label="日期字段"
            type="datetime-local"
            value={cutoff}
            onChange={setCutoff}
          />
          <button
            disabled={Boolean(cutoff && !beijingInputToIso(cutoff))}
            onClick={() => query(cutoff)}
          >
            查询
          </button>
        </>
      );
    }
    render(<Query />);
    fireEvent.change(input(), { target: { value: "2024-02-30T10:11:12" } });
    const button = screen.getByRole("button", {
      name: "查询",
    }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.pointerDown(button);
    fireEvent.click(button);
    expect(query).not.toHaveBeenCalled();
    expect(input().value).toBe("2024-02-30T10:11:12");
    fireEvent.change(input(), { target: { value: "" } });
    expect(button.disabled).toBe(false);
    fireEvent.click(button);
    expect(query).toHaveBeenCalledExactlyOnceWith("");
  });
  it("stops Enter and Escape propagation and all buttons avoid form submission", () => {
    const submit = vi.fn((event) => event.preventDefault());
    const keys = vi.fn();
    render(
      <form onSubmit={submit} onKeyDown={keys}>
        <Controlled />
        <button type="submit">提交表单</button>
      </form>,
    );
    input().focus();
    expect(fireEvent.keyDown(input(), { key: "Enter" })).toBe(false);
    expect(screen.getByRole("dialog")).toBeTruthy();
    for (const button of within(screen.getByRole("dialog")).getAllByRole(
      "button",
    ))
      expect(button.getAttribute("type")).toBe("button");
    fireEvent.keyDown(screen.getByRole("button", { name: "2024-02-29" }), {
      key: "Enter",
    });
    expect(input().value).toBe("2024-02-29");
    fireEvent.keyDown(screen.getByLabelText("日期字段"), { key: "Enter" });
    expect(screen.getByRole("dialog")).toBeTruthy();
    fireEvent.keyDown(screen.getByRole("button", { name: "确定" }), {
      key: "Enter",
    });
    expect(screen.queryByRole("dialog")).toBeNull();
    openCalendar();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(submit).not.toHaveBeenCalled();
    expect(keys).not.toHaveBeenCalled();
  });
  it("supports arrow navigation, keyboard activation and Tab focus containment", async () => {
    render(<Controlled />);
    openCalendar();
    const day = screen.getByRole("button", { name: "2024-02-15" });
    day.focus();
    fireEvent.keyDown(day, { key: "ArrowRight" });
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("button", { name: "2024-02-16" }),
      ),
    );
    fireEvent.keyDown(document.activeElement!, { key: "Enter" });
    expect(input().value).toBe("2024-02-16");
    const last = screen.getByRole("button", { name: "确定" });
    last.focus();
    fireEvent.keyDown(last, { key: "Tab" });
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "上一年" }),
    );
    fireEvent.keyDown(document.activeElement!, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
  });
  it("can clear an invalid parent value through the calendar footer", () => {
    const changed = vi.fn();
    render(<Controlled value="invalid" onChange={changed} />);
    openCalendar();
    fireEvent.click(screen.getByRole("button", { name: "清除" }));
    confirm();
    expect(changed).toHaveBeenCalledExactlyOnceWith("");
    expect(input().value).toBe("");
  });
  it("clears as a draft and requires confirmation", () => {
    const changed = vi.fn();
    render(<Controlled onChange={changed} />);
    openCalendar();
    fireEvent.click(screen.getByRole("button", { name: "清除" }));
    expect(changed).not.toHaveBeenCalled();
    confirm();
    expect(changed).toHaveBeenCalledExactlyOnceWith("");
  });
  it("cancels on outside pointer interaction without stealing focus", () => {
    render(
      <>
        <Controlled />
        <button>外部按钮</button>
      </>,
    );
    openCalendar();
    fireEvent.click(screen.getByRole("button", { name: "2024-02-29" }));
    const outside = screen.getByRole("button", { name: "外部按钮" });
    outside.focus();
    fireEvent.pointerDown(outside);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(input().value).toBe("2024-02-15");
    expect(document.activeElement).toBe(outside);
  });
});

describe("constraints and overlay", () => {
  it("aligns now to a time step and reads the clock at activation", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-02-15T02:03:04Z"));
    render(
      <Controlled
        type="datetime-local"
        value="2024-02-15T00:00:00"
        step={60}
      />,
    );
    openCalendar();
    vi.setSystemTime(new Date("2024-02-15T03:04:05Z"));
    fireEvent.click(screen.getByRole("button", { name: "现在" }));
    confirm();
    expect(input().value).toBe("2024-02-15T11:05:00");
  });
  it("keeps an impossible time step disabled instead of normalizing a date", () => {
    render(
      <Controlled
        type="datetime-local"
        value="2024-02-15T10:00:00"
        step={1e100}
      />,
    );
    openCalendar();
    expect(
      (screen.getByRole("button", { name: "2024-02-15" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: "确定" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });
  it("rejects impossible time entries without emitting a normalized value", () => {
    const changed = vi.fn();
    render(
      <Controlled
        type="datetime-local"
        value="2024-02-15T10:00:00"
        onChange={changed}
      />,
    );
    openCalendar();
    fireEvent.change(screen.getByLabelText("时"), { target: { value: "24" } });
    confirm();
    expect(changed).not.toHaveBeenCalled();
    expect(input().getAttribute("aria-invalid")).toBe("true");
    fireEvent.keyDown(screen.getByLabelText("时"), { key: "Escape" });
    expect(input().value).toBe("2024-02-15T10:00:00");
  });
  it.each(["month", "date"] as const)(
    "enforces %s min/max with no date conversion",
    (type) => {
      const isMonth = type === "month";
      const min = isMonth ? "2024-02" : "2024-02-10";
      const max = isMonth ? "2024-03" : "2024-02-20";
      render(<Controlled type={type} value={min} min={min} max={max} />);
      openCalendar();
      expect(
        (
          screen.getByRole("button", {
            name: isMonth ? "2024-01" : "2024-02-09",
          }) as HTMLButtonElement
        ).disabled,
      ).toBe(true);
      expect(
        (screen.getByRole("button", { name: max }) as HTMLButtonElement)
          .disabled,
      ).toBe(false);
      fireEvent.change(input(), {
        target: { value: isMonth ? "2024-04" : "2024-02-21" },
      });
      expect(
        (screen.getByRole("button", { name: "确定" }) as HTMLButtonElement)
          .disabled,
      ).toBe(true);
    },
  );
  it("enforces time bounds and step anchored to min, including seconds", () => {
    render(
      <Controlled
        type="datetime-local"
        value="2024-02-15T10:00:05"
        min="2024-02-15T10:00:05"
        max="2024-02-15T10:01:05"
        step={15}
      />,
    );
    openCalendar();
    expect(
      (screen.getByRole("button", { name: "2024-02-14" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    fireEvent.change(screen.getByLabelText("秒"), { target: { value: "6" } });
    expect(
      (screen.getByRole("button", { name: "确定" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    fireEvent.change(screen.getByLabelText("秒"), { target: { value: "20" } });
    confirm();
    expect(input().value).toBe("2024-02-15T10:00:20");
    openCalendar();
    fireEvent.change(screen.getByLabelText("时"), { target: { value: "11" } });
    expect(
      (screen.getByRole("button", { name: "确定" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });
  it("respects required, disabled and explicit label/id", () => {
    const { rerender } = render(
      <DateField
        label="日期字段"
        id="my-date"
        type="date"
        value=""
        onChange={vi.fn()}
        required
      />,
    );
    expect(input().id).toBe("my-date");
    expect(input().required).toBe(true);
    openCalendar();
    expect(
      (screen.getByRole("button", { name: "清除" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: "确定" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    rerender(
      <DateField
        label="日期字段"
        type="date"
        value=""
        onChange={vi.fn()}
        disabled
      />,
    );
    expect(input().disabled).toBe(true);
    expect(
      (
        screen.getByRole("button", {
          name: "日期字段：打开日历",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
  it("portals outside overflow containers, clamps to viewport and repositions", () => {
    const { container } = render(
      <div style={{ overflow: "hidden" }}>
        <Controlled />
      </div>,
    );
    const anchor = input().parentElement!;
    vi.spyOn(anchor, "getBoundingClientRect").mockReturnValue({
      x: 1000,
      y: 700,
      left: 1000,
      right: 1200,
      top: 700,
      bottom: 740,
      width: 200,
      height: 40,
      toJSON: () => ({}),
    });
    openCalendar();
    const dialog = screen.getByRole("dialog");
    expect(container.contains(dialog)).toBe(false);
    expect(dialog.parentElement).toBe(document.body);
    expect(parseFloat(dialog.style.left)).toBeLessThanOrEqual(
      window.innerWidth - 320 - 8,
    );
    vi.spyOn(anchor, "getBoundingClientRect").mockReturnValue({
      x: -40,
      y: -30,
      left: -40,
      right: 160,
      top: -30,
      bottom: 10,
      width: 200,
      height: 40,
      toJSON: () => ({}),
    });
    fireEvent.scroll(window);
    expect(dialog.style.left).toBe("8px");
    fireEvent.resize(window);
    expect(parseFloat(dialog.style.top)).toBeGreaterThanOrEqual(8);
  });
  it("uses Beijing today and now at the UTC day boundary", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-02-29T16:01:02Z"));
    const { unmount } = render(<Controlled />);
    openCalendar();
    fireEvent.click(screen.getByRole("button", { name: "今天" }));
    confirm();
    expect(input().value).toBe("2024-03-01");
    unmount();
    render(<Controlled type="datetime-local" value="" />);
    openCalendar();
    fireEvent.click(screen.getByRole("button", { name: "现在" }));
    confirm();
    expect(input().value).toBe("2024-03-01T00:01:02");
  });
});
