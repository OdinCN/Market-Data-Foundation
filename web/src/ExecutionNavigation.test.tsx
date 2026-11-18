import { expectedPolicy } from "../scripts/collection-protocol-evidence";
import "./offlineTestSetup";
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
import { readCpiExecutions } from "./cpiTimeSeriesApi";
import {
  CpiExecutionDetail,
  CpiStrategyWorkbench,
} from "./CpiStrategyWorkbench";
import { ExecutionNavigation, PeriodicNavigation } from "./ExecutionNavigation";
import type { RuntimeFamily } from "./RuntimeFamily";
import { TDX_FAMILY } from "./TdxDailyFamily";
import { ExecutionDetailShell } from "./CollectionLifecycle";
import { collectionRegistrations } from "./collectionRegistry";
import { RUNTIME_FAMILIES } from "./runtimeFamilies";
import { executionOrigin } from "./executionOrigin";
import { FinancialDailyWorkbench, FINANCIAL } from "./FinancialDailyWorkbench";
import {
  executions,
  keyOf,
  CPI,
  HISTORY_OPERATION,
  type Plans,
  sameKey,
} from "./workflowModel";
import { asgi, closeBridges } from "./workflowTestBridge";
import type { Transport } from "./api";

afterEach(() => {
  cleanup();
  closeBridges();
  localStorage.clear();
});
const rows = collectionRegistrations(RUNTIME_FAMILIES).filter(
  (r) => !r.protocol.startsWith("legacy"),
);
const id = "00000000-0000-4000-8000-000000000011";
const facts = {
  id,
  asset: "registered",
  source: "bound",
  action: "update",
  result: "complete",
};

// All registrations are samples, including ones whose strategy declaration was
// accidentally removed. Expectations use independent protocol response data.
it.each(rows)(
  "$asset.identity inherits a verified read-only strategy destination",
  async (row) => {
    const backend = asgi(
      row.profile?.id ?? row.protocol,
      "collection_submit_fixture.py",
    );
    const policy = await expectedPolicy(row, backend.exchange);
    expect(row.strategy).toEqual(policy);
    const transport: Transport = vi.fn(backend.transport);
    const family = RUNTIME_FAMILIES.find((f) => sameKey(f.asset, row.asset));
    const nav = vi.fn();
    render(
      <ExecutionNavigation
        asset={row.asset}
        job={id}
        registration={row}
        family={family}
        transport={transport}
        navigate={nav}
        generation={0}
      >
        <ExecutionDetailShell
          {...facts}
          assetKey={keyOf(row.asset)}
          trigger="手动采集"
        />
      </ExecutionNavigation>,
    );
    if (!policy) {
      await act(async () => {});
      expect(screen.queryByRole("button", { name: "查看周期任务" })).toBeNull();
      expect(transport).not.toHaveBeenCalled();
      return;
    }
    const expectedId = policy.id;
    fireEvent.click(
      await screen.findByRole("button", { name: "查看周期任务" }),
    );
    expect(screen.getByText("手动采集")).toBeTruthy();
    expect(nav).toHaveBeenCalledExactlyOnceWith(
      "collection",
      {
        asset: row.asset.identity,
        asset_revision: row.asset.revision,
        operation: null,
        tab: "periodic",
        periodic_asset: keyOf(row.asset),
        schedule: expectedId,
        job: id,
      },
      true,
    );
  },
);

it.each(["denied", "malformed"])(
  "omits an unverifiable %s policy, including a private callback bypass",
  async (mode) => {
    const row = rows[0];
    const transport: Transport = vi.fn(async () =>
      Response.json({}, { status: mode === "denied" ? 403 : 200 }),
    );
    const nav = vi.fn();
    render(
      <ExecutionNavigation
        asset={row.asset}
        job={id}
        registration={row}
        transport={transport}
        navigate={nav}
        generation={0}
      >
        <ExecutionDetailShell
          {...facts}
          assetKey={keyOf(row.asset)}
          onPeriodic={() => nav("private-wrong-target")}
        />
      </ExecutionNavigation>,
    );
    await waitFor(() => expect(transport).toHaveBeenCalledOnce());
    await act(async () => {});
    expect(screen.queryByRole("button", { name: "查看周期任务" })).toBeNull();
    expect(nav).not.toHaveBeenCalled();
  },
);

it("a late policy response cannot bind the next asset or task", async () => {
  const row = rows.find((r) => r.protocol === "financial")!;
  let release!: (r: Response) => void;
  const transport: Transport = vi.fn(
    () =>
      new Promise<Response>((resolve) => {
        release = resolve;
      }),
  );
  const view = render(
    <ExecutionNavigation
      asset={row.asset}
      job={id}
      registration={row}
      transport={transport}
      navigate={vi.fn()}
      generation={0}
    >
      <ExecutionDetailShell {...facts} assetKey={keyOf(row.asset)} />
    </ExecutionNavigation>,
  );
  view.rerender(
    <ExecutionNavigation
      asset={CPI}
      job="other"
      transport={transport}
      navigate={vi.fn()}
      generation={0}
    >
      <ExecutionDetailShell {...facts} id="other" assetKey={keyOf(CPI)} />
    </ExecutionNavigation>,
  );
  await act(async () =>
    release(
      Response.json({ enabled: true, tasks: [], schedule_semantics: "old" }),
    ),
  );
  expect(screen.queryByRole("button", { name: "查看周期任务" })).toBeNull();
  expect(
    screen.getByRole("region", { name: "执行详情主体" }).dataset.executionId,
  ).toBe("other");
});

it.each([undefined, null, "legacy_unknown", "future-trigger"])(
  "never invents manual/automatic provenance from %s",
  (origin) => {
    expect(executionOrigin(origin)).toBe("触发来源待核实");
  },
);

it.each([false, undefined, "unknown"])(
  "financial failed records remain readable but collect=%s cannot authorize recovery",
  async (collect) => {
    const calls: string[] = [];
    const transport: Transport = async (url, init) => {
      calls.push(init?.method ?? "GET");
      return Response.json(
        String(url).endsWith("/status")
          ? { status: "OK", web_capabilities: { query: true, collect } }
          : {
              status: "OK",
              job: {
                request_id: id,
                state: "failed",
                created_at: "2026-11-16T01:00:00Z",
                finished_at: "2026-11-16T02:00:00Z",
                classification: "financial_http_failure",
                command: {
                  action: "update",
                  start: "2026-11-01",
                  end: "2026-11-02",
                },
                steps: [],
                provider_requests: 1,
              },
            },
      );
    };
    render(
      <FinancialDailyWorkbench
        detailOnly
        transport={transport}
        generation={0}
        navigate={vi.fn()}
        route={{
          view: "collection",
          invalid: false,
          fields: new URLSearchParams({
            asset: FINANCIAL.identity,
            asset_revision: FINANCIAL.revision,
            tab: "executions",
            job: id,
          }),
        }}
      />,
    );
    const resume = await screen.findByRole("button", { name: "续跑此作业" });
    await waitFor(() => expect(calls.length).toBeGreaterThanOrEqual(2));
    await act(async () => {});
    expect((resume as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("触发来源待核实")).toBeTruthy();
    expect(
      screen.getByText("结果分类", { selector: "dt" }).nextElementSibling
        ?.textContent,
    ).toContain("具名来源暂未成功响应");
    fireEvent.click(resume);
    expect(calls.every((method) => method === "GET")).toBe(true);
  },
);

it("historical plan association is exact and ambiguous children never select the last candidate", async () => {
  const fixture = asgi("default", "r5_schedules_ui_exchange.py");
  const created = await fixture.exchange({
    method: "POST",
    path: "/api/r5/cpi/plans",
    body: {
      plan_identity: "contract-plan",
      asset_identity: CPI.identity,
      asset_revision: CPI.revision,
      name: "contract-plan",
      start: "2024-12",
      end: "2025-01",
      timezone: "Asia/Shanghai",
      window_start: "19:00",
      window_end: "22:00",
      first_due: "2028-03-10T12:00:00+00:00",
      interval_seconds: 86400,
      cycles: 1,
    },
  });
  expect(created.status).toBe(202);
  await fixture.exchange({
    control: "tick",
    plan: "contract-plan",
    failure: true,
  });
  const response = await fixture.exchange({ path: "/api/r5/cpi/plans" });
  const plans = response.body as Plans;
  const records = executions([], plans);
  expect(records.length).toBeGreaterThan(0);
  expect(
    records.every(
      (e) =>
        e.schedule === "contract-plan" && e.operation === HISTORY_OPERATION,
    ),
  ).toBe(true);
  const duplicate = structuredClone(plans.plans[0]);
  duplicate.identity = "another-real-plan";
  const ambiguous = executions([], {
    ...plans,
    plans: [...plans.plans, duplicate],
  });
  expect(ambiguous.every((e) => e.schedule === null)).toBe(true);
});

it("an unknown periodic deep link never displays the first asset policy as its detail", () => {
  render(
    <CpiStrategyWorkbench
      transport={vi.fn()}
      availability={{ data: null, busy: false, failure: null, legacy: false }}
      selectedId="missing-policy"
    />,
  );
  expect(
    screen.getByText("请求的周期任务当前不可核实，未改选其他策略。"),
  ).toBeTruthy();
  expect(screen.queryByRole("heading", { name: "CPI 策略详情" })).toBeNull();
});

it.each([undefined, null, "legacy_unknown"])(
  "monthly historical %s origin remains readable without inventing a trigger",
  async (origin) => {
    const transport: Transport = async () =>
      Response.json({
        executions: [
          {
            id,
            origin,
            action: "update",
            start: "2026-09",
            end: "2026-09",
            state: "failed",
            created_at: "2026-11-15T01:00:00Z",
            started_at: null,
            finished_at: null,
            classification: "source_failed",
            result: null,
          },
        ],
      });
    const value = await readCpiExecutions(transport);
    render(<CpiExecutionDetail execution={value.executions[0]} />);
    const trigger = screen.getByText("触发方式", { selector: "dt" });
    expect(trigger.nextElementSibling?.textContent).toBe("触发来源待核实");
    expect(screen.getByText("source_failed")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /恢复采集/ })).toBeNull();
  },
);

it("a registered Macro preset remains a real destination before its enable intent is persisted", async () => {
  const row = rows.find((r) => keyOf(r.asset) === keyOf(CPI))!,
    nav = vi.fn();
  const transport: Transport = async () =>
    Response.json({
      exists: false,
      enabled: false,
      history_start: "2000-01",
      worker: { state: "not_started", last_heartbeat_at: null },
      next_due: null,
      policy: {},
    });
  render(
    <ExecutionNavigation
      asset={row.asset}
      job={id}
      registration={row}
      transport={transport}
      navigate={nav}
      generation={0}
    >
      <ExecutionDetailShell
        {...facts}
        assetKey={keyOf(row.asset)}
        trigger="手动采集"
      />
    </ExecutionNavigation>,
  );
  fireEvent.click(await screen.findByRole("button", { name: "查看周期任务" }));
  expect(nav).toHaveBeenCalledWith(
    "collection",
    expect.objectContaining({ schedule: "cpi-auto", job: id }),
    true,
  );
  expect(screen.getByText("手动采集")).toBeTruthy();
});

it("does not invent a strategy binding for a read-only or unregistered asset", () => {
  const transport = vi.fn(),
    nav = vi.fn();
  render(
    <ExecutionNavigation
      asset={CPI}
      job={id}
      transport={transport}
      navigate={nav}
      generation={0}
    >
      <ExecutionDetailShell
        {...facts}
        assetKey={keyOf(CPI)}
        onPeriodic={() => nav("private")}
      />
    </ExecutionNavigation>,
  );
  expect(screen.queryByRole("button", { name: "查看周期任务" })).toBeNull();
  expect(transport).not.toHaveBeenCalled();
  expect(nav).not.toHaveBeenCalled();
});

it("a future collectible API without a policy requires no strategy request or action", async () => {
  const row = {
    ...rows[0],
    asset: { identity: "future.no-policy", revision: "v2" },
    strategy: null,
  };
  const transport = vi.fn(),
    nav = vi.fn();
  expect(
    await expectedPolicy(
      { protocol: "future", path: "/fixture/jobs" },
      vi.fn(),
      { future: async () => null },
    ),
  ).toBeNull();
  render(
    <ExecutionNavigation
      asset={row.asset}
      job={id}
      registration={row}
      transport={transport}
      navigate={nav}
      generation={0}
    >
      <ExecutionDetailShell {...facts} assetKey={keyOf(row.asset)} />
    </ExecutionNavigation>,
  );
  await act(async () => {});
  expect(screen.queryByRole("button", { name: "查看周期任务" })).toBeNull();
  expect(transport).not.toHaveBeenCalled();
});

it("a non-TDX registered family verifies its actual periodic row instead of borrowing the TDX protocol", async () => {
  const asset = { identity: "future.non-tdx", revision: "v2" };
  const readPeriodic = vi.fn(async () => ({ policy_id: "future-policy-42" }));
  const family: RuntimeFamily = {
    ...TDX_FAMILY,
    asset,
    readPeriodic,
    periodic: (data, _props, refresh) => ({
      row: {
        id: (data as { policy_id: string }).policy_id,
        search: "future",
        cells: [],
      },
      detail: null,
      refresh,
    }),
  };
  const row = {
    ...rows[0],
    asset,
    profile: undefined,
    strategy: { id: "future-policy-42", path: "/fixture/policy" },
  };
  const nav = vi.fn(),
    transport = vi.fn();
  render(
    <ExecutionNavigation
      asset={asset}
      job={id}
      registration={row}
      family={family}
      transport={transport}
      navigate={nav}
      generation={0}
    >
      <ExecutionDetailShell {...facts} assetKey={keyOf(asset)} />
    </ExecutionNavigation>,
  );
  fireEvent.click(await screen.findByRole("button", { name: "查看周期任务" }));
  expect(readPeriodic).toHaveBeenCalledOnce();
  expect(transport).not.toHaveBeenCalled();
  expect(nav).toHaveBeenCalledWith(
    "collection",
    expect.objectContaining({
      schedule: "future-policy-42",
      asset: asset.identity,
      asset_revision: "v2",
      job: id,
    }),
    true,
  );
  await expect(
    expectedPolicy({ protocol: "future", path: "/fixture/jobs" }, vi.fn()),
  ).rejects.toThrow("missing policy evidence adapter");
});

it("a strategy execution list is exact, while return to a manual record clears that filter", () => {
  const nav = vi.fn();
  render(
    <PeriodicNavigation asset={CPI} id="cpi-auto" job={id} navigate={nav} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "查看执行记录" }));
  expect(nav).toHaveBeenLastCalledWith(
    "collection",
    expect.objectContaining({
      tab: "executions",
      execution_asset: keyOf(CPI),
      execution_schedule: "cpi-auto",
      job: null,
      asset_revision: CPI.revision,
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "返回此执行记录" }));
  expect(nav).toHaveBeenLastCalledWith(
    "collection",
    expect.objectContaining({
      tab: "executions",
      execution_schedule: "all",
      job: id,
    }),
    true,
  );
});
