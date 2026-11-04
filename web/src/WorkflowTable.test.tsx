import { afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { useState } from "react";
import { WorkflowTable, type Row } from "./WorkflowTable";
afterEach(cleanup);
const rows: Row[] = Array.from({ length: 25 }, (_, n) => ({
  id: `record-${n}`,
  search: `record-${n}`,
  cells: [`任务 ${n}`],
}));
it("resets search and pages only for an explicit locate request, including delayed rows", async () => {
  const select = () => {};
  const view = render(
    <WorkflowTable
      label="测试执行"
      columns={["任务名称"]}
      rows={rows}
      onSelect={select}
      pageSize={20}
    />,
  );
  fireEvent.change(screen.getByLabelText("搜索测试执行"), {
    target: { value: "does-not-match" },
  });
  view.rerender(
    <WorkflowTable
      label="测试执行"
      columns={["任务名称"]}
      rows={[]}
      onSelect={select}
      pageSize={20}
      locateId="record-24"
      selected="record-24"
    />,
  );
  expect(
    (screen.getByLabelText("搜索测试执行") as HTMLInputElement).value,
  ).toBe("");
  view.rerender(
    <WorkflowTable
      label="测试执行"
      columns={["任务名称"]}
      rows={rows}
      onSelect={select}
      pageSize={20}
      locateId="record-24"
      selected="record-24"
    />,
  );
  await act(async () => {});
  expect(screen.getByText("第 2 页")).toBeTruthy();
  expect(
    screen
      .getByRole("button", { name: "选择 record-24" })
      .closest("tr")
      ?.getAttribute("aria-selected"),
  ).toBe("true");
  fireEvent.change(screen.getByLabelText("搜索测试执行"), {
    target: { value: "record-24" },
  });
  view.rerender(
    <WorkflowTable
      label="测试执行"
      columns={["任务名称"]}
      rows={[...rows]}
      onSelect={select}
      pageSize={20}
      locateId="record-24"
      selected="record-24"
    />,
  );
  expect(
    (screen.getByLabelText("搜索测试执行") as HTMLInputElement).value,
  ).toBe("record-24");
});
it("preserves search and pagination when selecting an ordinary row", () => {
  function Probe() {
    const [selected, setSelected] = useState<string | null>(null);
    return (
      <WorkflowTable
        label="测试执行"
        columns={["任务名称"]}
        rows={rows}
        onSelect={setSelected}
        selected={selected}
        pageSize={20}
      />
    );
  }
  render(<Probe />);
  fireEvent.click(screen.getByRole("button", { name: "后一页记录" }));
  fireEvent.click(screen.getByRole("button", { name: "选择 record-24" }));
  expect(screen.getByText("第 2 页")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("搜索测试执行"), {
    target: { value: "record-2" },
  });
  fireEvent.click(screen.getByRole("button", { name: "选择 record-24" }));
  expect(
    (screen.getByLabelText("搜索测试执行") as HTMLInputElement).value,
  ).toBe("record-2");
});

it("reveals stacked details only after an explicit selection and offers a return", () => {
  const original = HTMLElement.prototype.scrollIntoView;
  const scroll = vi.fn();
  HTMLElement.prototype.scrollIntoView = scroll;
  let stacked = true;
  const geometry = vi
    .spyOn(HTMLElement.prototype, "getBoundingClientRect")
    .mockImplementation(function (this: HTMLElement) {
      return {
        x: 0,
        y: this.classList.contains("detail-pane") && stacked ? 400 : 0,
        top: this.classList.contains("detail-pane") && stacked ? 400 : 0,
        left: 0,
        right: 900,
        bottom: this.classList.contains("detail-pane") && stacked ? 700 : 300,
        width: 900,
        height: 300,
        toJSON: () => ({}),
      };
    });
  const frame = vi
    .spyOn(window, "requestAnimationFrame")
    .mockImplementation((callback) => {
      callback(0);
      return 1;
    });
  try {
    const select = vi.fn();
    const props = {
      label: "周期任务",
      columns: ["策略"],
      rows,
      selected: "record-0",
      onSelect: select,
      revealDetailsOnSelect: true,
    };
    const view = render(
      <WorkflowTable {...props}>
        <h3>策略详情</h3>
      </WorkflowTable>,
    );
    expect(scroll).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "选择 record-0" }));
    expect(scroll).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(screen.getByRole("complementary"));
    view.rerender(
      <WorkflowTable {...props} rows={[...rows]}>
        <h3>轮询后的详情</h3>
      </WorkflowTable>,
    );
    expect(scroll).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "返回周期列表" }));
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "选择 record-0" }),
    );
    expect(scroll).toHaveBeenCalledTimes(2);
    stacked = false;
    fireEvent.click(screen.getByRole("button", { name: "选择 record-0" }));
    expect(scroll).toHaveBeenCalledTimes(2);
  } finally {
    geometry.mockRestore();
    frame.mockRestore();
    HTMLElement.prototype.scrollIntoView = original;
  }
});
