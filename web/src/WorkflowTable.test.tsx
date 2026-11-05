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

it("opens and closes right-side details only after user selection without scrolling", () => {
  vi.stubGlobal("matchMedia", () => ({
    matches: true,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));
  const scroll = vi.fn();
  const original = HTMLElement.prototype.scrollIntoView;
  HTMLElement.prototype.scrollIntoView = scroll;
  const frame = vi
    .spyOn(window, "requestAnimationFrame")
    .mockImplementation((callback) => {
      callback(0);
      return 1;
    });
  try {
    const props = {
      label: "周期任务",
      columns: ["策略"],
      rows,
      selected: "record-0",
      onSelect: vi.fn(),
      rightDetails: true,
    };
    const view = render(
      <WorkflowTable {...props}>
        <h3>策略详情</h3>
      </WorkflowTable>,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "选择 record-0" }));
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(scroll).not.toHaveBeenCalled();
    view.rerender(
      <WorkflowTable {...props} rows={[...rows]}>
        <h3>轮询详情</h3>
      </WorkflowTable>,
    );
    expect(scroll).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "关闭详情" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "选择 record-0" }),
    );
  } finally {
    frame.mockRestore();
    HTMLElement.prototype.scrollIntoView = original;
    vi.unstubAllGlobals();
  }
});

it("moves focus into an open detail when desktop becomes a drawer so Escape reaches it", () => {
  const media = {
    matches: false,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  };
  vi.stubGlobal("matchMedia", () => media);
  try {
    render(
      <WorkflowTable
        label="周期任务"
        columns={["策略"]}
        rows={rows}
        selected="record-0"
        onSelect={vi.fn()}
        rightDetails
      >
        <h3>策略详情</h3>
      </WorkflowTable>,
    );
    const row = screen.getByRole("button", { name: "选择 record-0" });
    fireEvent.click(row);
    row.focus();
    expect(screen.queryByRole("dialog")).toBeNull();
    act(() => {
      media.matches = true;
      media.addEventListener.mock.calls[0][1]();
    });
    expect(document.activeElement).toBe(screen.getByRole("dialog"));
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(row);
  } finally {
    vi.unstubAllGlobals();
  }
});
