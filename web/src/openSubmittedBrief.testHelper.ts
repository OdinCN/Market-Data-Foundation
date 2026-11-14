import { fireEvent, screen, within } from "@testing-library/react";
import { expect } from "vitest";

export async function openSubmittedBrief() {
  const briefs = within(
    await screen.findByRole("complementary", { name: "执行简报" }),
  );
  await briefs.findByText("已接收", { exact: true });
  expect(
    screen.getByRole("tab", { name: "新建采集" }).getAttribute("aria-selected"),
  ).toBe("true");
  const id = briefs
    .getByText("请求编号")
    .parentElement!.querySelector("dd")!.textContent!;
  fireEvent.click(briefs.getByRole("button", { name: new RegExp(id) }));
  await screen.findByRole("heading", { name: "执行详情" });
  expect(new URLSearchParams(location.hash.slice(1)).get("job")).toBe(id);
}
