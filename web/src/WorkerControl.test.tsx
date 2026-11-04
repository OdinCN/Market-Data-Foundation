import { afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { WorkerControl } from "./WorkerControl";
import { CPI_PROFILE } from "./macroTimeSeriesProfile";
import type { Transport } from "./api";
afterEach(cleanup);
it("does not report repair until actual online readback and prevents duplicate actions", async () => {
  let online = false,
    writes = 0,
    release: () => void = () => {};
  const transport: Transport = async (_input, init) => {
    if (init?.method === "POST") {
      writes++;
      await new Promise<void>((r) => (release = r));
    }
    return new Response(
      JSON.stringify({
        registered: true,
        online,
        reason: online ? "worker_online" : "worker_service_stopped",
        process_id: online ? 123 : null,
        service_state: online ? "active" : "inactive",
        worker: { state: online ? "idle" : "stopped", last_heartbeat_at: null },
      }),
    );
  };
  const updated = vi.fn();
  render(
    <WorkerControl
      transport={transport}
      profile={CPI_PROFILE}
      onUpdated={updated}
    />,
  );
  fireEvent.click(await screen.findByRole("button", { name: "启动执行器" }));
  fireEvent.click(screen.getByRole("button", { name: "核对处理中…" }));
  expect(writes).toBe(1);
  await act(async () => release());
  expect(await screen.findByText(/尚未确认在线/)).toBeTruthy();
  expect(screen.queryByText("已回读确认执行器在线。")).toBeNull();
  online = true;
  fireEvent.click(screen.getByRole("button", { name: "刷新执行器状态" }));
  expect(await screen.findByText("执行器在线，进程和心跳已核对")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "启动执行器" })).toBeNull();
  expect(updated).toHaveBeenCalled();
});
