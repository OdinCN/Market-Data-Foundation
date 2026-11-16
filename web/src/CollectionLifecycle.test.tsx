import "./offlineTestSetup";
import { StrictMode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  within,
} from "@testing-library/react";
import { ExecutionBriefCard } from "./DailyFormControls";
import {
  ExecutionDetailShell,
  collectionRecent,
  useCollectionRecords,
} from "./CollectionLifecycle";
import { useCollectionSubmit } from "./useCollectionSubmit";

afterEach(cleanup);
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}
it.each([
  "running",
  "QUEUED",
  "pending",
  "unknown",
  "outcome_unknown",
  "waiting",
])("never reopens an unverified or in-flight %s card", (state) => {
  render(
    <ExecutionBriefCard
      id="original"
      title="original"
      createdAt=""
      result={state}
      current
      state={state}
      onReopen={vi.fn()}
      onSelect={vi.fn()}
    />,
  );
  expect(screen.queryByRole("button", { name: "开始另一项采集" })).toBeNull();
});
it("only the active verified terminal card can reopen, and domain restrictions still apply", () => {
  const reopen = vi.fn();
  const view = render(
    <ExecutionBriefCard
      id="one"
      title="one"
      createdAt=""
      result="complete"
      state="COMPLETE"
      current
      onReopen={reopen}
      onSelect={vi.fn()}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "开始另一项采集" }));
  expect(reopen).toHaveBeenCalledOnce();
  view.rerender(
    <ExecutionBriefCard
      id="one"
      title="one"
      createdAt=""
      result="complete"
      state="COMPLETE"
      current
      canReopen={false}
      onReopen={reopen}
      onSelect={vi.fn()}
    />,
  );
  expect(screen.queryByRole("button", { name: "开始另一项采集" })).toBeNull();
});
it("retains actual history and rejects a late running snapshot over a newer complete receipt", () => {
  const complete: {
    request_id: string;
    state: string;
    created_at: string;
    finished_at: string | null;
  } = {
    request_id: "one",
    state: "complete",
    created_at: "2026-11-16T01:00:00Z",
    finished_at: "2026-11-16T02:00:00Z",
  };
  const hook = renderHook(({ rows }) => useCollectionRecords(rows), {
    initialProps: { rows: [complete] },
  });
  hook.rerender({
    rows: [{ ...complete, state: "running", finished_at: null }],
  });
  expect(hook.result.current[0].state).toBe("complete");
  hook.rerender({ rows: [{ ...complete, request_id: "two" }] });
  expect(hook.result.current.map((r) => r.request_id).sort()).toEqual([
    "one",
    "two",
  ]);
  expect(collectionRecent(hook.result.current, "one")[0].request_id).toBe(
    "one",
  );
});
it("late replies and reset intents cannot overwrite the selected scope, including StrictMode", async () => {
  const first = deferred<{ id: string }>();
  const hook = renderHook(({ scope }) => useCollectionSubmit(scope), {
    initialProps: { scope: "asset-a" },
    wrapper: StrictMode,
  });
  let pending!: Promise<unknown>;
  act(() => {
    pending = hook.result.current.submit(
      "old",
      () => first.promise,
      async () => ({ id: "old" }),
      (r) => r.id === "old",
    );
  });
  hook.rerender({ scope: "asset-b" });
  await act(async () => {
    await hook.result.current.submit(
      "new",
      async () => ({ id: "new" }),
      async () => ({ id: "new" }),
      (r) => r.id === "new",
    );
  });
  await act(async () => {
    first.resolve({ id: "old" });
    await pending;
  });
  expect(hook.result.current.attempt?.id).toBe("new");
  expect(hook.result.current.attempt?.state).toBe("accepted");
  act(() => hook.result.current.reset());
  expect(hook.result.current.attempt).toBeNull();
  const oldRecall = deferred<{ id: string }>();
  let recall!: Promise<unknown>;
  act(() => {
    recall = hook.result.current.recall(
      "new",
      () => oldRecall.promise,
      () => true,
    );
  });
  act(() => hook.result.current.reset());
  await act(async () => {
    oldRecall.resolve({ id: "new" });
    await recall;
  });
  expect(hook.result.current.attempt).toBeNull();
});
it("the shared detail resets recovery confirmation on a different identity and omits unsupported actions", () => {
  const resume = vi.fn();
  const props = {
    assetKey: "a@r1",
    id: "one",
    asset: "a",
    source: "bound",
    action: "update",
    result: "failed",
    onResume: resume,
    confirmResume: true,
  };
  const view = render(<ExecutionDetailShell {...props} />);
  fireEvent.click(screen.getByRole("checkbox"));
  view.rerender(<ExecutionDetailShell {...props} id="two" />);
  const shell = screen.getByRole("region", { name: "执行详情主体" });
  expect(shell.dataset.executionId).toBe("two");
  expect(
    (within(shell).getByRole("checkbox") as HTMLInputElement).checked,
  ).toBe(false);
  expect(
    (
      within(shell).getByRole("button", {
        name: "按原请求恢复采集",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  view.rerender(
    <ExecutionDetailShell
      {...props}
      id="two"
      result="complete"
      onResume={undefined}
    />,
  );
  expect(screen.queryByRole("button", { name: "按原请求恢复采集" })).toBeNull();
});

it("an openable brief selects its exact record with no duplicate recall action", () => {
  const select = vi.fn();
  render(
    <ExecutionBriefCard
      id="original"
      title="daily"
      createdAt=""
      result="running"
      state="running"
      current
      onSelect={select}
    />,
  );
  expect(screen.getAllByRole("button")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: /original/ }));
  expect(select).toHaveBeenCalledOnce();
  expect(screen.queryByRole("button", { name: /回查同一任务/ })).toBeNull();
});
it("a completed detail exposes only real destinations, with no self-refresh action", () => {
  const result = vi.fn(),
    periodic = vi.fn();
  render(
    <ExecutionDetailShell
      assetKey="a@r1"
      id="original"
      asset="a"
      source="bound"
      action="update"
      result="complete"
      onResult={result}
      onPeriodic={periodic}
    />,
  );
  expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual([
    "查看资产数据",
    "查看周期任务",
  ]);
  fireEvent.click(screen.getByRole("button", { name: "查看资产数据" }));
  fireEvent.click(screen.getByRole("button", { name: "查看周期任务" }));
  expect(result).toHaveBeenCalledOnce();
  expect(periodic).toHaveBeenCalledOnce();
});
