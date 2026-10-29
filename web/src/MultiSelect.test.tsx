import { afterEach, expect, it } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { useState } from "react";
import { MultiSelect } from "./MultiSelect";

afterEach(cleanup);
function Probe() {
  const [value, setValue] = useState(["a", "b"]);
  return (
    <>
      <MultiSelect
        label="选择口径"
        options={[
          { value: "a", label: "甲" },
          { value: "b", label: "乙" },
        ]}
        value={value}
        onChange={setValue}
      />
      <button>外部</button>
    </>
  );
}
it("supports keyboard opening, checkbox navigation, escape and summaries", async () => {
  render(<Probe />);
  const trigger = screen.getByRole("button", { name: "选择口径" });
  trigger.focus();
  fireEvent.keyDown(trigger, { key: "ArrowDown" });
  const group = screen.getByRole("group", { name: "选择口径选项" });
  const first = within(group).getByRole("checkbox", { name: "甲" });
  const last = within(group).getByRole("checkbox", { name: "乙" });
  await waitFor(() => expect(document.activeElement).toBe(first));
  fireEvent.keyDown(first, { key: "ArrowDown" });
  expect(document.activeElement).toBe(last);
  fireEvent.keyDown(last, { key: "Home" });
  expect(document.activeElement).toBe(first);
  fireEvent.keyDown(first, { key: "End" });
  expect(document.activeElement).toBe(last);
  fireEvent.keyDown(last, { key: "ArrowUp" });
  expect(document.activeElement).toBe(first);
  fireEvent.click(first);
  expect(within(trigger).getByText("乙")).toBeTruthy();
  fireEvent.keyDown(first, { key: "Escape" });
  expect(document.activeElement).toBe(trigger);
  expect(trigger.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByRole("group")).toBeNull();
});
it("keeps the dropdown open for multiple choices and closes on outside focus or pointer", () => {
  render(<Probe />);
  const trigger = screen.getByRole("button", { name: "选择口径" });
  fireEvent.click(trigger);
  const group = screen.getByRole("group");
  fireEvent.click(within(group).getByRole("button", { name: "清空" }));
  expect(within(trigger).getByText("未选择")).toBeTruthy();
  fireEvent.click(within(group).getByRole("button", { name: "全选" }));
  expect(within(trigger).getByText("甲、乙")).toBeTruthy();
  fireEvent.pointerDown(screen.getByRole("button", { name: "外部" }));
  expect(screen.queryByRole("group")).toBeNull();
  fireEvent.click(trigger);
  const first = screen.getByRole("checkbox", { name: "甲" });
  first.focus();
  fireEvent.blur(first, {
    relatedTarget: screen.getByRole("button", { name: "外部" }),
  });
  expect(screen.queryByRole("group")).toBeNull();
});
