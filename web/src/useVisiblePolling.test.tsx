import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import { useVisiblePolling } from "./useVisiblePolling";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});
const flush = () => act(async () => {});
const advance = (ms = 5000) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
function visibility(value: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value,
  });
  act(() => document.dispatchEvent(new Event("visibilitychange")));
}
it("polls every five seconds, retains last data on failure and marks it stale until recovery", async () => {
  vi.useFakeTimers();
  visibility("visible");
  let fail = false;
  const load = vi.fn(async () => {
    if (fail) throw new Error("synthetic");
    return { count: load.mock.calls.length };
  });
  const view = renderHook(() => useVisiblePolling(load, []));
  await flush();
  expect(view.result.current.data).toEqual({ count: 1 });
  fail = true;
  await advance();
  expect(view.result.current.data).toEqual({ count: 1 });
  expect(view.result.current.stale).toBe(true);
  fail = false;
  await advance();
  expect(view.result.current.data).toEqual({ count: 3 });
  expect(view.result.current.stale).toBe(false);
});
it("never overlaps slow reads, stops while hidden and reads immediately on visibility restoration", async () => {
  vi.useFakeTimers();
  visibility("visible");
  const pending: { resolve: (n: number) => void; signal: AbortSignal }[] = [];
  const load = vi.fn(
    (signal: AbortSignal) =>
      new Promise<number>((resolve) => pending.push({ resolve, signal })),
  );
  const view = renderHook(() => useVisiblePolling(load, []));
  await advance(15000);
  expect(load).toHaveBeenCalledTimes(1);
  visibility("hidden");
  expect(pending[0].signal.aborted).toBe(true);
  await advance(15000);
  expect(load).toHaveBeenCalledTimes(1);
  visibility("visible");
  expect(load).toHaveBeenCalledTimes(1);
  await act(async () => pending[0].resolve(1));
  expect(load).toHaveBeenCalledTimes(2);
  await act(async () => pending[1].resolve(2));
  expect(view.result.current.data).toBe(2);
  visibility("hidden");
  await advance(15000);
  expect(load).toHaveBeenCalledTimes(2);
  visibility("visible");
  expect(load).toHaveBeenCalledTimes(3);
  view.unmount();
  expect(pending[2].signal.aborted).toBe(true);
  await advance(15000);
  expect(load).toHaveBeenCalledTimes(3);
});
it("starts no reads on an initially hidden page", async () => {
  vi.useFakeTimers();
  visibility("hidden");
  const load = vi.fn(async () => 1);
  renderHook(() => useVisiblePolling(load, []));
  await advance(15000);
  expect(load).not.toHaveBeenCalled();
  visibility("visible");
  await flush();
  expect(load).toHaveBeenCalledOnce();
  visibility("visible");
});
it("masks a changed read scope before effects and retains confirmed data when only polling mode changes", async () => {
  const snapshots: { busy: boolean; data: number | null }[] = [];
  let release!: (value: number) => void;
  const first = async () => 1;
  const second = () =>
    new Promise<number>((resolve) => {
      release = resolve;
    });
  const view = renderHook(
    ({ enabled, load, polling }) => {
      const state = useVisiblePolling(
        load,
        [load],
        enabled,
        undefined,
        polling,
      );
      snapshots.push({ busy: state.busy, data: state.data });
      return state;
    },
    {
      initialProps: {
        enabled: false,
        load: first as (signal: AbortSignal) => Promise<number>,
        polling: true,
      },
    },
  );
  snapshots.length = 0;
  view.rerender({ enabled: true, load: first, polling: true });
  expect(snapshots[0]).toEqual({ busy: true, data: null });
  await flush();
  expect(view.result.current.data).toBe(1);
  snapshots.length = 0;
  view.rerender({ enabled: true, load: first, polling: false });
  expect(snapshots[0].data).toBe(1);
  await flush();
  snapshots.length = 0;
  view.rerender({ enabled: true, load: second, polling: false });
  expect(snapshots[0]).toEqual({ busy: true, data: null });
  await act(async () => release(2));
  expect(view.result.current.data).toBe(2);
});
