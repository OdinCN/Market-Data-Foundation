import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { useAutoCollection, autoCollectionState } from "./useAutoCollection";
import { CPI_PROFILE } from "./macroTimeSeriesProfile";
import type { Transport } from "./api";
import type { CpiAvailability } from "./cpiTimeSeriesApi";
beforeEach(() => {
  // Freeze the release comparison only; polling and request timers stay real.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-11-10T13:29:59Z"));
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
const availability = {
  data: { web_capabilities: { collect: true } },
} as CpiAvailability;

it("requalifies optional statistics on changed heads and never reuses them after a failed status check", async () => {
  const f = fixture();
  let aggregates = 0;
  const transport: Transport = async (path, init) => {
    if (
      String(path).endsWith("/strategy") &&
      new Headers(init?.headers).get("Prefer") !== "return=minimal"
    ) {
      aggregates += 1;
      const response = await f.transport(path, init);
      return Response.json({
        ...(await response.json()),
        last_successful_collection_at: "2026-11-01T00:00:00Z",
      });
    }
    return f.transport(path, init);
  };
  const { result, rerender } = renderHook(
    ({ head, failure }: { head: number; failure: string | null }) =>
      useAutoCollection(
        transport,
        0,
        CPI_PROFILE,
        {
          ...availability,
          failure,
          data: { ...availability.data!, batch_count: head },
        },
        true,
      ),
    { initialProps: { head: 1, failure: null as string | null } },
  );
  await waitFor(() =>
    expect(result.current.data?.collection_facts_status).toBe("verified"),
  );
  expect(aggregates).toBe(1);
  expect(result.current.data?.last_successful_collection_at).toBe(
    "2026-11-01T00:00:00Z",
  );
  rerender({ head: 1, failure: null });
  await act(async () => {});
  expect(aggregates).toBe(1);
  rerender({ head: 2, failure: null });
  await waitFor(() => expect(aggregates).toBe(2));
  rerender({ head: 2, failure: "storage_read_failed" });
  await waitFor(() =>
    expect(result.current.data?.collection_facts_status).toBe("failed"),
  );
  expect(result.current.data?.last_successful_collection_at).toBeUndefined();
  rerender({ head: 2, failure: null });
  await waitFor(() => expect(aggregates).toBe(3));
});

it("publishes verified policy before optional reads without granting actions or letting late statistics replace it", async () => {
  const f = fixture();
  let releaseWorker!: () => void;
  let releaseFacts!: (value: Response) => void;
  const transport: Transport = async (path, init) => {
    if (
      String(path).endsWith("/strategy") &&
      new Headers(init?.headers).get("Prefer") !== "return=minimal"
    )
      return new Promise((resolve) => {
        releaseFacts = resolve;
      });
    if (String(path).endsWith("/worker"))
      await new Promise<void>((resolve) => {
        releaseWorker = resolve;
      });
    return f.transport(path, init);
  };
  const { result } = renderHook(() =>
    useAutoCollection(transport, 0, CPI_PROFILE, availability, true),
  );
  await waitFor(() => expect(result.current.data?.exists).toBe(true));
  expect(result.current.resource.busy).toBe(true);
  expect(result.current.diagnosis.state).toBe("正在读取");
  expect(result.current.data?.collection_facts_status).toBe("pending");
  await act(async () => {
    await result.current.toggle();
  });
  expect(f.calls.every((c) => c.method === "GET")).toBe(true);
  await act(async () => {
    releaseWorker();
  });
  await waitFor(() => expect(result.current.resource.busy).toBe(false));
  await act(async () => {
    releaseFacts(
      Response.json({
        exists: true,
        enabled: true,
        history_start: "2006-01",
        worker: { state: "idle", last_heartbeat_at: null },
        next_due: null,
        policy: {},
        last_successful_collection_at: "2026-11-01T00:00:00Z",
      }),
    );
  });
  await waitFor(() =>
    expect(result.current.data?.collection_facts_status).toBe("verified"),
  );
  expect(result.current.data?.enabled).toBe(false);
  expect(result.current.data?.last_successful_collection_at).toBe(
    "2026-11-01T00:00:00Z",
  );
});

function fixture(initialEnabled = false, initialOnline = false) {
  let enabled = initialEnabled,
    online = initialOnline,
    failStart = false,
    lostSave = false,
    held = false;
  let release = () => {};
  const calls: { path: string; method: string }[] = [];
  const transport: Transport = async (path, init) => {
    if (String(path).endsWith("/schedule"))
      return new Response(
        JSON.stringify({ calendar_status: "fresh", targets: [] }),
      );
    const method = init?.method ?? "GET";
    calls.push({ path: String(path), method });
    if (method === "POST") {
      if (held)
        await new Promise<void>((r) => {
          release = r;
        });
      if (failStart)
        return new Response(
          JSON.stringify({ classification: "worker_service_action_failed" }),
          { status: 503 },
        );
      online = true;
    }
    if (method === "PUT") {
      enabled = JSON.parse(String(init?.body)).enabled;
      if (lostSave) throw new TypeError("controlled lost receipt");
    }
    return new Response(
      JSON.stringify(
        String(path).endsWith("/worker")
          ? {
              registered: true,
              online,
              reason: online ? "worker_online" : "worker_service_stopped",
              service_state: online ? "active" : "inactive",
              process_id: online ? 123 : null,
              worker: {
                state: online ? "idle" : "stopped",
                last_heartbeat_at: null,
              },
            }
          : {
              exists: true,
              enabled,
              history_start: "2006-01",
              worker: {
                state: online ? "idle" : "stopped",
                last_heartbeat_at: null,
              },
              next_due: null,
              next_release_at: "2026-11-10T13:30:00Z",
              policy: {},
            },
      ),
    );
  };
  return {
    transport,
    calls,
    fail: () => {
      failStart = true;
    },
    lose: () => {
      lostSave = true;
    },
    hold: () => {
      held = true;
    },
    release: () => release(),
  };
}
it("never starts from loading; explicit enable confirms worker before policy and pause never kills it", async () => {
  const h = fixture();
  const { result } = renderHook(() =>
    useAutoCollection(h.transport, 0, CPI_PROFILE, availability, true),
  );
  await waitFor(() => expect(result.current.data).toBeTruthy());
  expect(h.calls.every((c) => c.method === "GET")).toBe(true);
  await act(async () => {
    await result.current.toggle();
  });
  expect(
    h.calls.filter((c) => c.method !== "GET").map((c) => c.method),
  ).toEqual(["POST", "PUT"]);
  expect(autoCollectionState(result.current)).toBe("待发布");
  await act(async () => {
    await result.current.toggle();
  });
  expect(
    h.calls.filter((c) => c.method !== "GET").map((c) => c.method),
  ).toEqual(["POST", "PUT", "PUT"]);
  expect(result.current.data?.enabled).toBe(false);
  expect(result.current.data?.control?.online).toBe(true);
});
it("restores only selected enabled asset without a second policy save", async () => {
  const h = fixture(true);
  const { result } = renderHook(() =>
    useAutoCollection(h.transport, 0, CPI_PROFILE, availability, true),
  );
  await waitFor(() =>
    expect(autoCollectionState(result.current)).toBe("需处理"),
  );
  await act(async () => {
    await result.current.recover();
  });
  expect(h.calls.filter((c) => c.method !== "GET")).toHaveLength(1);
  expect(h.calls.every((c) => c.path.includes("/cpi/"))).toBe(true);
  expect(result.current.data?.control?.online).toBe(true);
});
it("failed start cannot enable policy or announce success", async () => {
  const h = fixture();
  h.fail();
  const { result } = renderHook(() =>
    useAutoCollection(h.transport, 0, CPI_PROFILE, availability, true),
  );
  await waitFor(() => expect(result.current.data).toBeTruthy());
  await act(async () => {
    await result.current.toggle();
  });
  await waitFor(() => expect(result.current.uncertain).toBe(false));
  expect(h.calls.some((c) => c.method === "PUT")).toBe(false);
  expect(result.current.data?.enabled).toBe(false);
  expect(result.current.notice).toContain("服务启动失败");
});
it("lost save receipt is verified independently and never repeats start or save", async () => {
  const h = fixture();
  h.lose();
  const { result } = renderHook(() =>
    useAutoCollection(h.transport, 0, CPI_PROFILE, availability, true),
  );
  await waitFor(() => expect(result.current.data).toBeTruthy());
  await act(async () => {
    await result.current.toggle();
  });
  await waitFor(() => expect(result.current.uncertain).toBe(false));
  expect(result.current.notice).toContain("独立回读确认");
  expect(h.calls.filter((c) => c.method === "POST")).toHaveLength(1);
  expect(h.calls.filter((c) => c.method === "PUT")).toHaveLength(1);
});
it("prevents duplicate actions while start is pending", async () => {
  const h = fixture();
  h.hold();
  const { result } = renderHook(() =>
    useAutoCollection(h.transport, 0, CPI_PROFILE, availability, true),
  );
  await waitFor(() => expect(result.current.data).toBeTruthy());
  let pending: Promise<void>;
  act(() => {
    pending = result.current.toggle();
  });
  await waitFor(() => expect(result.current.saving).toBe(true));
  await act(async () => {
    await result.current.toggle();
  });
  expect(h.calls.filter((c) => c.method === "POST")).toHaveLength(1);
  await act(async () => {
    h.release();
    await pending;
  });
});
it("source capability refusal has no runtime or policy effects", async () => {
  const h = fixture();
  const denied = {
    data: { web_capabilities: { collect: false, reason: "binding_missing" } },
  } as CpiAvailability;
  const { result } = renderHook(() =>
    useAutoCollection(h.transport, 0, CPI_PROFILE, denied, true),
  );
  await waitFor(() => expect(result.current.data).toBeTruthy());
  await act(async () => {
    await result.current.toggle();
  });
  expect(h.calls.every((c) => c.method === "GET")).toBe(true);
  expect(result.current.notice).toContain("binding_missing");
});

it.each([
  ["2026-11-10T13:29:59Z", "待发布"],
  ["2026-11-10T13:30:00Z", "待核实（下次发布未确认）"],
  ["2026-11-11T00:00:00Z", "待核实（下次发布未确认）"],
])(
  "at %s distinguishes real task activity from loop heartbeat, retry and unknown outcomes",
  (now, idleState) => {
    vi.setSystemTime(new Date(now));
    const value = {
      data: {
        enabled: true,
        worker: { state: "running" },
        control: { online: true },
        next_release_at: "2026-11-10T13:30:00Z",
        schedule: { calendar_status: "fresh", targets: [] },
      },
      saving: false,
      uncertain: false,
      resource: { stale: false },
    } as unknown as Parameters<typeof autoCollectionState>[0];
    expect(autoCollectionState(value)).toBe(idleState);
    for (const [state, next_attempt_at, expected] of [
      ["running", null, "采集中"],
      ["failed", "2026-11-05T00:00:00Z", "重试中"],
      ["outcome_unknown", null, "待核实"],
      ["failed", null, "需处理"],
    ]) {
      value.data!.schedule!.targets = [{ target: { state, next_attempt_at } }];
      expect(autoCollectionState(value)).toBe(expected);
    }
  },
);
