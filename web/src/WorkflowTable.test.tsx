import { afterEach, expect, it } from "vitest";
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
