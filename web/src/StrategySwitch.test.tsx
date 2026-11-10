import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { StrategySwitch } from "./StrategySwitch";

afterEach(cleanup);
it("keeps confirmed state while saving, labels the asset and stops row selection", () => {
  const toggle = vi.fn();
  const select = vi.fn();
  const props = {
    asset: "就业",
    enabled: true,
    saving: false,
    uncertain: false,
    disabled: false,
    onToggle: toggle,
  };
  const view = render(
    <div onClick={select}>
      <StrategySwitch {...props} />
    </div>,
  );
  const control = screen.getByRole("switch", {
    name: "就业自动采集策略",
    checked: true,
  });
  fireEvent.click(control);
  expect(toggle).toHaveBeenCalledTimes(1);
  expect(select).not.toHaveBeenCalled();
  view.rerender(
    <div onClick={select}>
      <StrategySwitch {...props} saving disabled />
    </div>,
  );
  expect(control.getAttribute("aria-checked")).toBe("true");
  expect(control.textContent).toBe("正在开启或暂停");
  fireEvent.click(control);
  expect(toggle).toHaveBeenCalledTimes(1);
  view.rerender(
    <div onClick={select}>
      <StrategySwitch {...props} uncertain disabled />
    </div>,
  );
  expect(control.textContent).toBe("待核实");
  expect(control.getAttribute("aria-description")).toContain("未知");
});
it("shows a clear paused label and distinguishes an unread state", () => {
  const props = {
    asset: "M3",
    enabled: false,
    saving: false,
    uncertain: false,
    disabled: false,
    onToggle: vi.fn(),
  };
  const view = render(<StrategySwitch {...props} />);
  expect(screen.getByRole("switch", { checked: false }).textContent).toBe(
    "暂停",
  );
  view.rerender(<StrategySwitch {...props} enabled={null} disabled />);
  expect(screen.getByRole("switch").textContent).toBe("未核实");
  expect(screen.getByRole("switch").getAttribute("data-unconfirmed")).toBe(
    "true",
  );
});
