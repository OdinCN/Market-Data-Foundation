import { afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";
import { resolve } from "node:path";
import App from "./App";
import { monotonicNow, ObservabilityView } from "./Observability";
import type { Observability, Transport } from "./api";

// Host-only: actual ASGI → strict projection of explicit synthetic fixtures.
const children: ChildProcessWithoutNullStreams[] = [];
function bridge(scenario = "observability") {
  const root = resolve(process.cwd(), "..");
  const child = spawn(
    `${root}/.venv/bin/python`,
    ["-u", "tests/r5_ui_exchange.py", scenario],
    {
      cwd: root,
      env: { ...process.env, PYTHONPATH: `${root}/src:${root}` },
    },
  );
  children.push(child);
  const pending: {
    resolve: (v: { status: number; body: unknown }) => void;
    reject: (e: Error) => void;
  }[] = [];
  const requests: string[] = [];
  createInterface({ input: child.stdout }).on("line", (line) =>
    pending.shift()?.resolve(JSON.parse(line)),
  );
  child.on("error", (error) =>
    pending.splice(0).forEach((p) => p.reject(error)),
  );
  child.on("exit", () =>
    pending.splice(0).forEach((p) => p.reject(new Error("ASGI bridge exited"))),
  );
  const transport = (async (input, init) => {
    expect(init?.method).toBe("GET");
    expect(init?.cache).toBe("no-store");
    const path = String(input);
    requests.push(path);
    const response = await new Promise<{ status: number; body: unknown }>(
      (resolve, reject) => {
        pending.push({ resolve, reject });
        child.stdin.write(JSON.stringify({ path }) + "\n");
      },
    );
    return {
      ok: response.status === 200,
      status: response.status,
      json: async () => response.body,
    } as Response;
  }) as Transport;
  return { transport, requests };
}
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  children.splice(0).forEach((child) => {
    child.stdin.end();
    child.kill();
  });
  location.hash = "";
  vi.useRealTimers();
});
const query = { reference: "2026-08", as_of: "2026-10-06T00:00:10+00:00" };

it("projects real contract fixtures, seven menus, alarm → same diagnostic → I1/I2 identity", async () => {
  const api = bridge();
  location.hash = "page=0";
  render(<App transport={api.transport} initialQuery={query} />);
  await screen.findByText("0.25 / 0.5 / 0.75");
  expect(
    screen.getByText(/接收 123456 \/ 发送 654321 cumulative bytes/),
  ).toBeTruthy();
  expect(
    within(screen.getByRole("navigation")).getAllByRole("button"),
  ).toHaveLength(7);
  fireEvent.click(
    screen.getByRole("button", { name: "查看同一诊断 fixture.diagnostic.1" }),
  );
  await screen.findByRole("heading", { name: "受信结构化诊断" });
  await screen.findByRole("button", {
    name: /清除诊断定位 fixture.diagnostic.1/,
  });
  expect(location.hash).toContain("job=fixture.job.1");
  expect(screen.queryByText("fixture.diagnostic.2")).toBeNull();
  expect(screen.getByText("fixture.diagnostic.1")).toBeTruthy();
  expect(screen.getByText("fixture.attempt.1")).toBeTruthy();
  fireEvent.click(
    screen.getByRole("button", {
      name: /查看关联资产 mdf.macro.bls.cpi.headlines/,
    }),
  );
  await screen.findByRole("heading", { name: "本地数据预览" });
  expect(location.hash).toContain("asset=mdf.macro.bls.cpi.headlines");
  expect(location.hash).toContain("asset_revision=p3-r1");
  expect(
    api.requests.every(
      (path) => path.startsWith("/api/r5/") && !path.includes("/read?"),
    ),
  ).toBe(true);
});

it("keeps three tabs, filters instances, stores and structured diagnostics without requests", async () => {
  const api = bridge();
  render(
    <ObservabilityView
      transport={api.transport}
      generation={0}
      page={6}
      onNavigate={vi.fn()}
    />,
  );
  await screen.findByRole("heading", { name: "offline-fixture-reader" });
  expect(screen.getAllByRole("tab")).toHaveLength(3);
  fireEvent.change(screen.getByLabelText("实例状态"), {
    target: { value: "RUNNING" },
  });
  expect(
    screen.queryByRole("heading", { name: "offline-fixture-reader" }),
  ).toBeNull();
  expect(screen.getByText(/没有匹配的受信实例/)).toBeTruthy();
  fireEvent.click(screen.getByRole("tab", { name: "数据库与存储" }));
  expect(screen.getByText("UNAVAILABLE")).toBeTruthy();
  expect(screen.getByText("DEFER")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("筛选具名记录"), {
    target: { value: "garage" },
  });
  expect(screen.getByRole("heading", { name: "Garage" })).toBeTruthy();
  expect(screen.queryByRole("heading", { name: "PostgreSQL" })).toBeNull();
  fireEvent.click(screen.getByRole("tab", { name: "日志与诊断" }));
  expect(screen.getByText("fixture.diagnostic.1")).toBeTruthy();
  expect(screen.getByText("fixture.diagnostic.2")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("严重程度"), {
    target: { value: "ERROR" },
  });
  expect(screen.queryByText("fixture.diagnostic.2")).toBeNull();
  fireEvent.change(screen.getByLabelText("Job 身份"), {
    target: { value: "missing-job" },
  });
  expect(screen.queryByText("fixture.diagnostic.1")).toBeNull();
  expect(screen.getByText(/没有匹配的受信诊断/)).toBeTruthy();
  expect(api.requests).toHaveLength(1);
});

it.each(["observability_unconfigured", "observability_stale"])(
  "fails closed without provider or fresh clocks: %s",
  async (scenario) => {
    const api = bridge(scenario);
    const view = render(
      <ObservabilityView
        transport={api.transport}
        generation={0}
        page={0}
        onNavigate={vi.fn()}
      />,
    );
    await screen.findByText(/^(UNCONFIGURED|OFFLINE_FIXTURE) · UNKNOWN$/);
    expect(screen.queryByText("0.25 / 0.5 / 0.75")).toBeNull();
    expect(screen.queryByRole("button", { name: /查看同一诊断/ })).toBeNull();
    expect(screen.getByText("DEFER")).toBeTruthy();
    view.rerender(
      <ObservabilityView
        transport={api.transport}
        generation={0}
        page={6}
        onNavigate={vi.fn()}
      />,
    );
    await screen.findByText(/没有匹配的受信实例/);
  },
);

it("discards delayed refreshes, page changes and unmounted responses", async () => {
  const oldApi = bridge();
  const newApi = bridge("observability_unconfigured");
  let release: (() => void) | undefined;
  let delayed = false;
  const slow = (async (...args: Parameters<Transport>) => {
    const response = await oldApi.transport(...args);
    await new Promise<void>((resolve) => {
      release = resolve;
      delayed = true;
    });
    return response;
  }) as Transport;
  const view = render(
    <ObservabilityView
      transport={slow}
      generation={0}
      page={0}
      onNavigate={vi.fn()}
    />,
  );
  await waitFor(() => expect(delayed).toBe(true));
  view.rerender(
    <ObservabilityView
      transport={newApi.transport}
      generation={1}
      page={6}
      onNavigate={vi.fn()}
    />,
  );
  await screen.findByText("UNCONFIGURED · UNKNOWN");
  await act(async () => {
    release?.();
  });
  expect(screen.queryByText("offline-fixture-reader")).toBeNull();
  expect(screen.getByText("UNCONFIGURED · UNKNOWN")).toBeTruthy();
  delayed = false;
  view.rerender(
    <ObservabilityView
      transport={slow}
      generation={2}
      page={0}
      onNavigate={vi.fn()}
    />,
  );
  await waitFor(() => expect(delayed).toBe(true));
  view.unmount();
  await act(async () => {
    release?.();
  });
  expect(screen.queryByText("0.25 / 0.5 / 0.75")).toBeNull();
});

it("expires displayed facts locally without polling, and hides a failed refresh", async () => {
  const api = bridge();
  const view = render(
    <ObservabilityView
      transport={api.transport}
      generation={0}
      page={0}
      onNavigate={vi.fn()}
    />,
  );
  await screen.findByText("0.25 / 0.5 / 0.75");
  const wall = Date.now();
  const clock = vi
    .spyOn(performance, "now")
    .mockReturnValue(performance.now() + 61000);
  await waitFor(
    () => expect(screen.queryByText("0.25 / 0.5 / 0.75")).toBeNull(),
    { timeout: 2500 },
  );
  expect(screen.queryByRole("button", { name: /查看同一诊断/ })).toBeNull();
  expect(api.requests).toHaveLength(1);
  vi.spyOn(Date, "now").mockReturnValue(wall - 30000);
  const ticks = clock.mock.calls.length;
  await waitFor(() => expect(clock.mock.calls.length).toBeGreaterThan(ticks), {
    timeout: 2500,
  });
  expect(screen.queryByText("0.25 / 0.5 / 0.75")).toBeNull();
  expect(screen.queryByRole("button", { name: /查看同一诊断/ })).toBeNull();
  expect(api.requests).toHaveLength(1);
  const broken = (async () => {
    throw new Error("private-sensitive-message");
  }) as Transport;
  view.rerender(
    <ObservabilityView
      transport={broken}
      generation={1}
      page={0}
      onNavigate={vi.fn()}
    />,
  );
  await screen.findByText("UNKNOWN · request_failed");
  expect(screen.queryByText(/private-sensitive-message/)).toBeNull();
  expect(screen.queryByText("0.25 / 0.5 / 0.75")).toBeNull();
});

function clockReading(value: number | Error): number {
  if (!(value instanceof Error)) return value;
  // Exercise the real exception boundary synchronously, then restore the global
  // clock before React or the Vitest runner can read it.
  const clock = vi.spyOn(performance, "now").mockImplementationOnce(() => {
    throw value;
  });
  let reading: number;
  try {
    reading = monotonicNow();
  } finally {
    clock.mockRestore();
  }
  expect(reading).toBeNaN();
  return reading;
}

// Reuse the actual contract fixture, then control browser clocks and only the
// local expiry interval. No Python clock or server behavior is mocked.
async function clockView(
  page = 0,
  receipt: { wallOffset?: number; monotonic?: number } = {},
  prepare: (data: Observability) => void = () => {},
) {
  const api = bridge();
  const response = await api.transport("/api/r5/observability?", {
    method: "GET",
    cache: "no-store",
  });
  const data = (await response.json()) as Observability;
  prepare(data);
  const generated = Date.parse(data.generated_at);
  const clock = {
    wall: generated + (receipt.wallOffset ?? 1000),
    monotonic: receipt.monotonic ?? 1000,
  };
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  vi.spyOn(Date, "now").mockImplementation(() => clock.wall);
  vi.spyOn(performance, "now").mockImplementation(() => clock.monotonic);
  const transport = vi.fn(
    async () =>
      ({
        ok: true,
        status: 200,
        json: async () => data,
      }) as Response,
  );
  const props = { transport, generation: 0, page, onNavigate: vi.fn() };
  const view = await act(async () => render(<ObservabilityView {...props} />));
  const tick = (monotonic: number) => {
    clock.monotonic = monotonic;
    act(() => vi.advanceTimersByTime(1000));
  };
  return { clock, generated, data, transport, props, view, tick };
}

it.each([0, 6])(
  "keeps expired facts, instance states and diagnostics UNKNOWN after wall rollback on page %s",
  async (page) => {
    const { clock, generated, transport, tick } = await clockView(page);
    expect(screen.getAllByText(/OBSERVED · observed/).length).toBeGreaterThan(
      0,
    );
    if (page === 0) {
      expect(screen.getByText("0.25 / 0.5 / 0.75")).toBeTruthy();
      expect(screen.getByRole("button", { name: /查看同一诊断/ })).toBeTruthy();
    } else {
      expect(screen.getByText(/DEGRADED ·/)).toBeTruthy();
    }
    tick(62000);
    expect(screen.queryByText(/OBSERVED ·/)).toBeNull();
    clock.wall = generated + 30000;
    tick(63000);
    expect(screen.queryByText(/OBSERVED ·/)).toBeNull();
    expect(screen.queryByText("0.25 / 0.5 / 0.75")).toBeNull();
    expect(screen.queryByText(/DEGRADED ·/)).toBeNull();
    expect(screen.queryByRole("button", { name: /查看同一诊断/ })).toBeNull();
    if (page === 6) {
      fireEvent.click(screen.getByRole("tab", { name: "数据库与存储" }));
      expect(screen.queryByText("UNAVAILABLE")).toBeNull();
      fireEvent.click(screen.getByRole("tab", { name: "日志与诊断" }));
      expect(screen.getByText("fixture.diagnostic.1")).toBeTruthy();
      expect(screen.queryByText(/OBSERVED ·/)).toBeNull();
      expect(screen.getAllByText(/UNKNOWN · observed/).length).toBeGreaterThan(
        0,
      );
    }
    expect(transport).toHaveBeenCalledTimes(1);
  },
);

it("uses only the remaining lifetime at receipt, regardless of subsequent wall changes", async () => {
  const { clock, generated, transport, tick } = await clockView(0, {
    wallOffset: 59000,
  });
  expect(screen.getByText("0.25 / 0.5 / 0.75")).toBeTruthy();
  clock.wall = generated + 3600000;
  tick(1500);
  expect(screen.getByText("0.25 / 0.5 / 0.75")).toBeTruthy();
  clock.wall = generated + 30000;
  tick(2001);
  expect(screen.queryByText("0.25 / 0.5 / 0.75")).toBeNull();
  expect(screen.queryByRole("button", { name: /查看同一诊断/ })).toBeNull();
  expect(transport).toHaveBeenCalledTimes(1);
});

it.each([NaN, Infinity, -Infinity, -1, 61000])(
  "rejects an untrusted receipt wall clock permanently: offset %s",
  async (wallOffset) => {
    const { clock, generated, transport, tick } = await clockView(0, {
      wallOffset,
    });
    expect(screen.queryByText(/OBSERVED ·/)).toBeNull();
    clock.wall = generated + 1000;
    tick(2000);
    expect(screen.queryByText(/OBSERVED ·/)).toBeNull();
    expect(screen.queryByRole("button", { name: /查看同一诊断/ })).toBeNull();
    expect(transport).toHaveBeenCalledTimes(1);
  },
);

it.each([NaN, Infinity, -1, new Error("clock unavailable")])(
  "fails closed when the monotonic clock is invalid at receipt: %s",
  async (monotonic) => {
    const reading = clockReading(monotonic);
    const { transport, tick } = await clockView(0, { monotonic: reading });
    expect(screen.queryByText(/OBSERVED ·/)).toBeNull();
    tick(2000);
    expect(screen.queryByText(/OBSERVED ·/)).toBeNull();
    expect(screen.queryByRole("button", { name: /查看同一诊断/ })).toBeNull();
    expect(transport).toHaveBeenCalledTimes(1);
  },
);

it.each([1500, NaN, Infinity, new Error("clock unavailable")])(
  "latches monotonic anomalies even above receipt time: %s",
  async (anomaly) => {
    const reading = clockReading(anomaly);
    const { transport, tick } = await clockView();
    tick(2000);
    expect(screen.getByText("0.25 / 0.5 / 0.75")).toBeTruthy();
    tick(reading);
    expect(screen.queryByText(/OBSERVED ·/)).toBeNull();
    tick(3000);
    expect(screen.queryByText(/OBSERVED ·/)).toBeNull();
    expect(screen.queryByRole("button", { name: /查看同一诊断/ })).toBeNull();
    expect(transport).toHaveBeenCalledTimes(1);
  },
);

it.each([-61000, 1])(
  "does not admit a stale or post-generation sample after wall correction: %s",
  async (sampleOffset) => {
    const { clock, generated, tick } = await clockView(0, {}, (data) => {
      data.resources.cpu_load.observation.sampled_at = new Date(
        Date.parse(data.generated_at) + sampleOffset,
      ).toISOString();
    });
    expect(screen.queryByText("0.25 / 0.5 / 0.75")).toBeNull();
    clock.wall = generated - 30000;
    tick(2000);
    expect(screen.queryByText("0.25 / 0.5 / 0.75")).toBeNull();
  },
);

it.each(["expiry", "monotonic anomaly", "untrusted receipt"])(
  "resets %s only on a new trusted receipt, hiding facts while refresh is pending",
  async (reason) => {
    const { clock, generated, data, props, view, transport, tick } =
      await clockView(0, {
        wallOffset: reason === "untrusted receipt" ? -1 : 1000,
      });
    if (reason === "monotonic anomaly") tick(NaN);
    tick(62000);
    expect(screen.queryByText(/OBSERVED ·/)).toBeNull();
    let release!: (response: Response) => void;
    transport.mockImplementationOnce(
      () => new Promise<Response>((resolve) => (release = resolve)),
    );
    view.rerender(<ObservabilityView {...props} generation={1} />);
    expect(screen.getByText("正在核实运行观测…")).toBeTruthy();
    expect(screen.queryByText(/OBSERVED ·/)).toBeNull();
    clock.wall = generated + 1000;
    await act(async () => {
      release({ ok: true, json: async () => data } as Response);
    });
    expect(screen.getByText("0.25 / 0.5 / 0.75")).toBeTruthy();
    expect(screen.getByRole("button", { name: /查看同一诊断/ })).toBeTruthy();
    tick(123000);
    expect(screen.queryByText(/OBSERVED ·/)).toBeNull();
    expect(transport).toHaveBeenCalledTimes(2);
  },
);
