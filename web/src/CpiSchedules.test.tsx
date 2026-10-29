import "./offlineTestSetup";
import { afterEach, expect, it } from "vitest";
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
import { CpiSchedules } from "./CpiSchedules";
import { CpiJobsView } from "./CpiJobs";
import { useState } from "react";
// Preserved bounded history core; calendar product flow is tested in App.test.
function HistoryCore({ transport }: { transport: Transport }) {
  const [generation, setGeneration] = useState(0);
  return (
    <>
      <button onClick={() => setGeneration((g) => g + 1)}>刷新</button>
      <CpiJobsView
        transport={transport}
        generation={generation}
        asset="mdf.macro.bls.cpi.headlines"
        revision="p3-r1"
      />
    </>
  );
}
import type { components } from "./api.generated";
import type { Transport } from "./api";

let child: ChildProcessWithoutNullStreams | undefined;
afterEach(() => {
  cleanup();
  child?.kill();
  location.hash = "";
});
it("persists two historical months, pauses, explicitly backfills failure and links exact lineage", async () => {
  const root = resolve(process.cwd(), "..");
  child = spawn(
    `${root}/.venv/bin/python`,
    ["-u", "tests/r5_schedules_ui_exchange.py"],
    {
      cwd: root,
      env: {
        ...process.env,
        PYTHONPATH: `${root}/src:${root}:${process.env.PYTHONPATH ?? ""}`,
      },
    },
  );
  const pending: ((value: any) => void)[] = [];
  createInterface({ input: child.stdout }).on("line", (line) =>
    pending.shift()?.(JSON.parse(line)),
  );
  async function exchange(message: object) {
    return await new Promise<any>((resolve) => {
      pending.push(resolve);
      child!.stdin.write(JSON.stringify(message) + "\n");
    });
  }
  const effects = { source: 0, raw: 0 };
  const transport = (async (input, init) => {
    const value = await exchange({
      path: String(input),
      method: init?.method ?? "GET",
      ...(init?.body ? { body: JSON.parse(String(init.body)) } : {}),
    });
    Object.assign(effects, value.effects);
    return {
      ok: value.status < 400,
      status: value.status,
      json: async () => value.body,
    } as Response;
  }) as Transport;
  location.hash =
    "page=4&asset=mdf.macro.bls.cpi.headlines&asset_revision=p3-r1";
  render(<HistoryCore transport={transport} />);
  await within(
    screen
      .getByRole("heading", { name: "CPI 多期历史采集" })
      .closest("section")!,
  ).findByText(/OFFLINE_FIXTURE · OK/);
  fireEvent.change(screen.getByLabelText("首次到期（北京时间）"), {
    target: { value: "2028-03-10T20:00:00" },
  });
  fireEvent.click(screen.getByLabelText("确认提交此有界历史计划"));
  fireEvent.click(screen.getByRole("button", { name: "持久提交历史计划" }));
  await screen.findByText(/意图已持久接受/);
  await screen.findByText(/已证实完成 0 \/ 2/);
  expect(effects.source).toBe(0);
  fireEvent.click(screen.getByRole("button", { name: "暂停计划" }));
  await waitFor(() => expect(screen.getByText(/paused ·/)).toBeTruthy());
  const paused = await exchange({
    control: "tick",
    plan: "cpi-history-example",
  });
  expect(paused.effects.source).toBe(0);
  fireEvent.click(screen.getByRole("button", { name: "恢复计划" }));
  await waitFor(() => expect(screen.getByText(/active ·/)).toBeTruthy());
  await act(async () => {
    await exchange({
      control: "tick",
      plan: "cpi-history-example",
      failure: true,
    });
    await exchange({
      control: "tick",
      plan: "cpi-history-example",
      crash_after_child: true,
    });
  });
  // The page-level refresh must update the schedule after a separate Host tick.
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  await screen.findByText(/已证实完成 1 \/ 2/);
  const href = screen
    .getByRole("link", { name: "06 精确读取此版本" })
    .getAttribute("href")!;
  const link = new URLSearchParams(href.slice(1));
  const projected = await exchange({ path: "/api/r5/cpi/plans" });
  const parentAt = projected.body.plans[0].updated_at;
  expect(Date.parse(parentAt)).toBeLessThan(Date.parse(link.get("as_of")!));
  const exact = new URLSearchParams({
    mode: "exact_version",
    reference: link.get("reference")!,
    version_identity: link.get("version")!,
    as_of: parentAt,
  });
  const old = await exchange({ path: `/api/r5/cpi/read?${exact}` });
  expect(old.status).toBe(409);
  expect(old.body.classification).toBe("unpublished_or_unavailable_as_of");
  exact.set("as_of", link.get("as_of")!);
  const read = await exchange({ path: `/api/r5/cpi/read?${exact}` });
  expect(read.status).toBe(200);
  expect(read.body.version_identity).toBe(link.get("version"));
  expect(read.body.reference).toBe(link.get("reference"));
  expect(read.effects).toEqual(old.effects);
  // Settle the already proven child without another source request.
  await exchange({ control: "tick", plan: "cpi-history-example" });
  fireEvent.click(screen.getByRole("button", { name: "提交指定失败窗口回补" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "回查历史计划" })).toBeTruthy(),
  );
  // Wait for durable backfill acceptance before the separately invoked test tick.
  await waitFor(() => expect(screen.getByText(/意图已持久接受/)).toBeTruthy());
  await act(async () => {
    await exchange({ control: "tick", plan: "cpi-history-example" });
  });
  fireEvent.click(screen.getByRole("button", { name: "回查历史计划" }));
  await screen.findByText(/已证实完成 2 \/ 2/);
  expect(
    screen.getAllByRole("link", { name: "06 精确读取此版本" }),
  ).toHaveLength(2);
  expect(screen.getAllByRole("link", { name: "01 关联异常" })).toHaveLength(3);
  expect(effects.source).toBe(3);
});

type Plans = components["schemas"]["PlansDTO"];
function plans(writable = true): Plans {
  return {
    status: "OK",
    classification: null,
    evidence_mode: "OFFLINE_FIXTURE",
    write_eligible: writable,
    asset_identity: "mdf.macro.bls.cpi.headlines",
    asset_revision: "p3-r1",
    tier: "unregistered",
    plans: [
      {
        identity: "refresh-plan",
        spec: {
          name: "refresh-plan",
          start: "2024-12",
          end: "2025-01",
          timezone: "UTC",
          window_start: "00:00",
          window_end: "23:59",
          first_due: "2028-03-10T12:00:00+00:00",
          interval_seconds: 86400,
          cycles: 1,
          effect_domain: "synthetic",
        },
        state: writable ? "active" : "completed",
        cycle: writable ? 0 : 1,
        cursor: 0,
        inflight_key: null,
        retry_queue: [],
        next_due: "2028-03-11T12:00:00+00:00",
        created_at: "2028-03-10T12:00:00+00:00",
        updated_at: "2028-03-10T12:00:00+00:00",
        batches: [],
        completed_batches: writable ? 0 : 2,
        total_batches: 2,
      },
    ],
  };
}

it("the preserved history core generation reloads periodic progress and write eligibility using only GET", async () => {
  location.hash =
    "page=4&tab=periodic&asset=mdf.macro.bls.cpi.headlines&asset_revision=p3-r1";
  let snapshot = plans();
  let queries = 0;
  const transport = (async (input, init) => {
    expect(init?.method).toBe("GET");
    const schedule = String(input).startsWith("/api/r5/cpi/plans");
    if (schedule) queries += 1;
    return {
      ok: schedule,
      json: async () =>
        schedule
          ? snapshot
          : { status: "UNKNOWN", classification: "storage_unavailable" },
    } as Response;
  }) as Transport;
  render(<HistoryCore transport={transport} />);
  await screen.findByText(/已证实完成 0 \/ 2/);
  fireEvent.click(screen.getByLabelText("确认提交此有界历史计划"));
  const submit = screen.getByRole("button", { name: "持久提交历史计划" });
  expect(submit.hasAttribute("disabled")).toBe(false);
  const refresh = screen.getByRole("button", { name: "刷新" });
  await waitFor(() => expect(refresh.hasAttribute("disabled")).toBe(false));
  snapshot = plans(false);
  fireEvent.click(refresh);
  await screen.findByText(/已证实完成 2 \/ 2/);
  expect(screen.getByText(/completed ·/)).toBeTruthy();
  expect(submit.hasAttribute("disabled")).toBe(true);
  expect(queries).toBe(2);
  expect(location.hash).toContain("asset_revision=p3-r1");
});

it.each(["success", "failure"])(
  "page generation reloads plans and ignores superseded %s without POST",
  async (stale) => {
    const pending: {
      resolve: (body: Plans) => void;
      reject: (error: Error) => void;
      signal: AbortSignal;
    }[] = [];
    const transport = (async (input, init) => {
      expect(init?.method).toBe("GET");
      if (!String(input).startsWith("/api/r5/cpi/plans"))
        return {
          ok: true,
          json: async () => ({ status: "UNKNOWN", jobs: [] }),
        } as Response;
      return await new Promise<Response>((resolve, reject) =>
        pending.push({
          resolve: (body) =>
            resolve({ ok: true, json: async () => body } as Response),
          reject,
          signal: init?.signal as AbortSignal,
        }),
      );
    }) as Transport;
    const view = (generation: number) => (
      <CpiJobsView
        transport={transport}
        generation={generation}
        asset="mdf.macro.bls.cpi.headlines"
        revision="p3-r1"
      />
    );
    const { rerender, unmount } = render(view(0));
    await act(async () => pending[0].resolve(plans()));
    fireEvent.click(screen.getByLabelText("确认提交此有界历史计划"));
    const submit = () =>
      screen.getByRole("button", { name: "持久提交历史计划" });
    expect(submit().hasAttribute("disabled")).toBe(false);
    rerender(view(1));
    expect(pending).toHaveLength(2);
    expect(pending[0].signal.aborted).toBe(true);
    expect(submit().hasAttribute("disabled")).toBe(true);
    expect(screen.queryByText(/active ·/)).toBeNull();
    rerender(view(2));
    expect(pending[1].signal.aborted).toBe(true);
    await act(async () => pending[2].resolve(plans(false)));
    expect(screen.getByText(/completed ·/)).toBeTruthy();
    expect(screen.getByText(/已证实完成 2 \/ 2/)).toBeTruthy();
    await act(async () => {
      if (stale === "success") pending[1].resolve(plans());
      else pending[1].reject(new Error("superseded"));
    });
    expect(screen.getByText(/completed ·/)).toBeTruthy();
    expect(submit().hasAttribute("disabled")).toBe(true);
    expect(screen.queryByText(/request_failed/)).toBeNull();
    expect(
      screen
        .getByRole("link", { name: "04 版本与读取资格" })
        .getAttribute("href"),
    ).toContain("asset=mdf.macro.bls.cpi.headlines&asset_revision=p3-r1");
    fireEvent.click(screen.getByRole("button", { name: "回查历史计划" }));
    expect(pending).toHaveLength(4);
    await act(async () => pending[3].reject(new Error("current query failed")));
    expect(screen.getByText(/UNKNOWN · request_failed/)).toBeTruthy();
    expect(screen.queryByText(/completed ·/)).toBeNull();
    expect(submit().hasAttribute("disabled")).toBe(true);
    rerender(view(3));
    unmount();
    expect(pending[4].signal.aborted).toBe(true);
    await act(async () => pending[4].resolve(plans()));
  },
);

it.each(["succeeded", "outcome_unknown"] as const)(
  "anchors exact links to verified child readback for %s and preserves named identity",
  async (state) => {
    const snapshot = plans(false);
    const observed = "2028-03-10T12:00:07+00:00";
    snapshot.plans[0].batches = [
      {
        key: "0:0",
        reference: "2024-12",
        complete: true,
        jobs: [
          {
            job_identity: "crash-child",
            state,
            attempt_identity: "crash-child-a1",
            raw_sha256: "a".repeat(64),
            version_identity: "stable-child:version",
            publication_occurrence: "stable-publication",
            classification: null,
            reconciliation: "confirmed_published",
            read_as_of: observed,
          },
        ],
      },
    ];
    const transport = (async () =>
      ({ ok: true, json: async () => snapshot }) as Response) as Transport;
    render(
      <CpiSchedules
        transport={transport}
        generation={0}
        asset={snapshot.asset_identity}
        revision={snapshot.asset_revision}
        mode="executions"
      />,
    );
    const link = await screen.findByRole("link", { name: "06 精确读取此版本" });
    const query = new URLSearchParams(link.getAttribute("href")!.slice(1));
    expect(Object.fromEntries(query)).toEqual({
      page: "5",
      asset: snapshot.asset_identity,
      asset_revision: snapshot.asset_revision,
      reference: "2024-12",
      version: "stable-child:version",
      as_of: observed,
    });
    expect(query.get("as_of")).not.toBe(snapshot.plans[0].updated_at);
  },
);

it.each([
  "unverified",
  "unpublished",
  "unknown-version",
  "wrong-asset",
  "wrong-revision",
  "unknown-snapshot",
])("does not advertise an exact read for %s evidence", async (scenario) => {
  const snapshot = plans();
  snapshot.plans[0].batches = [
    {
      key: "0:0",
      reference: "2024-12",
      complete: true,
      jobs: [
        {
          job_identity: "child",
          state: "outcome_unknown",
          attempt_identity: "child-a1",
          raw_sha256: null,
          version_identity: "version",
          publication_occurrence: "publication",
          classification: null,
          reconciliation: "unresolved",
          read_as_of: "2028-03-10T12:00:07+00:00",
        },
      ],
    },
  ];
  const child = snapshot.plans[0].batches[0].jobs[0];
  if (scenario === "unverified") child.read_as_of = null;
  if (scenario === "unpublished") child.publication_occurrence = null;
  if (scenario === "unknown-version") child.version_identity = null;
  if (scenario === "wrong-asset") snapshot.asset_identity = "foreign-asset";
  if (scenario === "wrong-revision")
    snapshot.asset_revision = "foreign-revision";
  if (scenario === "unknown-snapshot") snapshot.status = "UNKNOWN";
  const transport = (async () =>
    ({ ok: true, json: async () => snapshot }) as Response) as Transport;
  render(
    <CpiSchedules
      transport={transport}
      generation={0}
      asset="mdf.macro.bls.cpi.headlines"
      revision="p3-r1"
      mode="executions"
    />,
  );
  await screen.findByText(/Job/);
  expect(screen.queryByRole("link", { name: "06 精确读取此版本" })).toBeNull();
});

it("submits a Beijing first-due instant without changing historical month bounds or schedule timezone", async () => {
  const posts: Record<string, unknown>[] = [];
  const transport: Transport = async (_input, init) => {
    if (init?.method === "POST") {
      posts.push(JSON.parse(String(init.body)));
      return Response.json({ status: "OK", acceptance: "DURABLE" });
    }
    return Response.json(plans());
  };
  render(
    <CpiSchedules
      transport={transport}
      generation={0}
      asset="mdf.macro.bls.cpi.headlines"
      revision="p3-r1"
      mode="new"
    />,
  );
  await screen.findByText(/已证实完成 0 \/ 2/);
  const field = screen.getByLabelText("首次到期（北京时间）");
  fireEvent.change(field, { target: { value: "2028-03-11T04:30:05" } });
  fireEvent.click(screen.getByLabelText("确认提交此有界历史计划"));
  fireEvent.click(screen.getByRole("button", { name: "持久提交历史计划" }));
  await screen.findByText(/意图已持久接受/);
  expect(posts).toHaveLength(1);
  expect(posts[0]).toMatchObject({
    first_due: "2028-03-11T04:30:05+08:00",
    start: "2024-12",
    end: "2025-01",
    timezone: "Asia/Shanghai",
  });
  expect(Date.parse(String(posts[0].first_due))).toBe(
    Date.parse("2028-03-10T20:30:05Z"),
  );
  expect(screen.getByText(/UTC 00:00–23:59/)).toBeTruthy();
  expect(
    screen.getByText(/下次到期 2028-03-11 20:00:00 北京时间/),
  ).toBeTruthy();
});
