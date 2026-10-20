import { afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";
import { resolve } from "node:path";
import App from "./App";
import { CpiJobAlerts } from "./CpiJobs";
import type { Directory, Jobs, Transport } from "./api";

const bridges: ChildProcessWithoutNullStreams[] = [];
const query = { reference: "2026-09", as_of: "2026-10-14T13:00:00+00:00" };
function bridge(scenario = "bound") {
  const root = resolve(process.cwd(), "..");
  const child = spawn(
    `${root}/.venv/bin/python`,
    ["-u", "tests/r5_jobs_ui_exchange.py", scenario],
    {
      cwd: root,
      env: { ...process.env, PYTHONPATH: `${root}/src:${root}` },
    },
  );
  bridges.push(child);
  const pending: {
    resolve: (data: any) => void;
    reject: (error: Error) => void;
  }[] = [];
  let errors = "";
  child.stderr.on("data", (chunk) => (errors += String(chunk)));
  createInterface({ input: child.stdout }).on("line", (line) =>
    pending.shift()?.resolve(JSON.parse(line)),
  );
  child.on("exit", (code) => {
    if (code !== 0)
      pending.splice(0).forEach((p) => p.reject(new Error(errors)));
  });
  const effects = { source: 0, raw: 0, jobs: 0 };
  async function exchange(message: object) {
    const result = await new Promise<any>((resolve, reject) => {
      pending.push({ resolve, reject });
      child.stdin.write(JSON.stringify(message) + "\n");
    });
    Object.assign(effects, result.effects);
    return result;
  }
  let loseResponse = false;
  const posts: object[] = [];
  const transport = (async (input, init) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    if (method === "POST") posts.push(body);
    const result = await exchange({
      path: String(input),
      method,
      ...(body ? { body } : {}),
    });
    if (method === "POST" && loseResponse) {
      loseResponse = false;
      throw new Error("private-http-timeout");
    }
    return {
      ok: result.status < 400,
      status: result.status,
      json: async () => result.body,
    } as Response;
  }) as Transport;
  return {
    transport,
    effects,
    posts,
    loseNext: () => {
      loseResponse = true;
    },
    run: (job: string, failure = false) =>
      exchange({ control: "run", job, failure }),
    exchange,
  };
}
afterEach(() => {
  cleanup();
  location.hash = "";
  bridges.splice(0).forEach((c) => c.kill());
});

// Controlled frontend responses: unrelated journal records exhaust the list
// budget, while each identity-filtered snapshot remains readable.
function diagnosticSnapshot(identity: string): Jobs {
  return {
    status: "OK",
    classification: null,
    evidence_mode: "OFFLINE_FIXTURE",
    operator_identity: null,
    write_eligible: false,
    asset_identity: "synthetic-cpi",
    asset_revision: "synthetic-revision",
    reference: "2026-09",
    release_slot: "synthetic-slot",
    intent_identity: null,
    storage_target: "INHERITED_03_04",
    jobs: [
      {
        job_identity: identity,
        asset_identity: "synthetic-cpi",
        asset_revision: "synthetic-revision",
        reference: "2026-09",
        release_slot: "synthetic-slot",
        state: "failed",
        stage: "stopped",
        classification: "preflight_rejected",
        attempt_identity: `${identity}.attempt`,
        worker_identity: "synthetic-worker",
        attempts: 1,
        cancel_requested: false,
        created_at: "2026-10-20T00:00:00+00:00",
        started_at: null,
        finished_at: null,
        updated_at: "2026-10-20T00:00:00+00:00",
        failure_stage: "preflight",
        version_identity: null,
        publication_occurrence: null,
        reconciliation: null,
        history: [],
      },
    ],
  };
}
const budgetSnapshot: Jobs = {
  ...diagnosticSnapshot("unrelated"),
  status: "UNKNOWN",
  classification: "job_query_budget_exceeded",
  jobs: [],
};
function diagnosticResponse(body: Jobs): Response {
  return { ok: true, status: 200, json: async () => body } as Response;
}

it.each(["response", "rejection"])(
  "queries named linked diagnostics, refetches on selection change and drops a stale %s",
  async (stale) => {
    let resolveFirst!: (response: Response) => void;
    let rejectFirst!: (error: Error) => void;
    const firstResponse = new Promise<Response>((resolve, reject) => {
      resolveFirst = resolve;
      rejectFirst = reject;
    });
    const transport = vi.fn<Transport>(async (input) => {
      const url = new URL(String(input), location.origin);
      const identity = url.searchParams.get("job_identity");
      if (!identity) return diagnosticResponse(budgetSnapshot);
      if (identity === "linked-first") return firstResponse;
      return diagnosticResponse(diagnosticSnapshot(identity));
    });
    const view = render(
      <CpiJobAlerts transport={transport} generation={0} job="linked-first" />,
    );
    await waitFor(() => expect(transport).toHaveBeenCalledTimes(1));
    expect(String(transport.mock.calls[0][0])).toBe(
      "/api/r5/cpi/jobs?job_identity=linked-first",
    );
    const firstSignal = transport.mock.calls[0][1]?.signal;
    expect(firstSignal?.aborted).toBe(false);

    view.rerender(
      <CpiJobAlerts transport={transport} generation={0} job="linked-second" />,
    );
    await screen.findByText(/linked-second · failed · preflight_rejected/);
    expect(transport).toHaveBeenCalledTimes(2);
    expect(String(transport.mock.calls[1][0])).toBe(
      "/api/r5/cpi/jobs?job_identity=linked-second",
    );
    expect(firstSignal?.aborted).toBe(true);

    // The transport intentionally ignores abort, so the component must also
    // drop a late response or rejection from the previous selection.
    await act(async () => {
      if (stale === "response")
        resolveFirst(diagnosticResponse(diagnosticSnapshot("linked-first")));
      else rejectFirst(new Error("synthetic-late-failure"));
    });
    expect(
      screen.getByText(/linked-second · failed · preflight_rejected/),
    ).toBeTruthy();
    const link = screen.getByRole<HTMLAnchorElement>("link", {
      name: "05 回查同一 Job / Attempt",
    });
    expect(new URLSearchParams(link.hash.slice(1)).get("job")).toBe(
      "linked-second",
    );
    expect(screen.queryByText(/linked-first · failed/)).toBeNull();
    expect(screen.queryByText(/UNKNOWN ·/)).toBeNull();
    expect(screen.queryByText(/job_query_budget_exceeded/)).toBeNull();
  },
);

it("preserves the bounded list query and budget failure without a selected job", async () => {
  const transport = vi.fn<Transport>(async () =>
    diagnosticResponse(budgetSnapshot),
  );
  render(<CpiJobAlerts transport={transport} generation={0} job="" />);
  await screen.findByText(/UNKNOWN · job_query_budget_exceeded/);
  expect(transport).toHaveBeenCalledTimes(1);
  expect(String(transport.mock.calls[0][0])).toBe("/api/r5/cpi/jobs?");
  expect(
    screen.queryByRole("link", { name: "05 回查同一 Job / Attempt" }),
  ).toBeNull();
});

it("carries 04 identity, keeps three tabs, accepts once, survives a lost HTTP response, separately runs and reads", async () => {
  const api = bridge();
  const catalog = await api.exchange({
    path: `/api/r5/cpi?${new URLSearchParams(query)}`,
  });
  expect(catalog.status).toBe(200);
  expect(catalog.body.asset).toBeNull();
  expect(catalog.body.service).toBeNull();
  location.hash = "page=3";
  const view = render(<App transport={api.transport} initialQuery={query} />);
  const create = await screen.findByRole("button", {
    name: "新建 CPI 一次性采集",
  });
  expect(screen.getByText(/关联资产不可核实/)).toBeTruthy();
  expect(screen.queryByText("VERIFIED_FOR_REQUEST")).toBeNull();
  expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
  expect(
    screen
      .getByRole("button", { name: "查看数据服务" })
      .hasAttribute("disabled"),
  ).toBe(true);
  expect(api.effects).toEqual({ source: 0, raw: 0, jobs: 0 });
  fireEvent.click(create);
  const route = new URLSearchParams(location.hash.slice(1));
  expect(route.get("page")).toBe("4");
  expect(route.get("asset")).toBe(catalog.body.definition.identity);
  expect(route.get("asset_revision")).toBe(catalog.body.definition.revision);
  expect(route.has("operation")).toBe(false);
  expect(screen.getAllByRole("tab")).toHaveLength(3);
  const submit = await screen.findByRole("button", {
    name: "持久提交一次性意图",
  });
  await waitFor(() =>
    expect(
      screen.getByText(/服务端 Operator：synthetic-operator/),
    ).toBeTruthy(),
  );
  expect(submit.hasAttribute("disabled")).toBe(true);
  fireEvent.click(screen.getByRole("checkbox", { name: /我明确提交/ }));
  api.loseNext();
  fireEvent.click(submit);
  await screen.findByText(/UNKNOWN · request_failed。先刷新回查/);
  expect(api.effects).toEqual({ source: 0, raw: 0, jobs: 1 });
  const identity = (api.posts[0] as { job_identity: string }).job_identity;
  // Browser close/reopen uses the same host-generated identity, without a new job.
  view.unmount();
  render(<App transport={api.transport} initialQuery={query} />);
  await screen.findByText(/服务端 Operator：synthetic-operator/);
  fireEvent.click(screen.getByRole("checkbox", { name: /我明确提交/ }));
  fireEvent.click(screen.getByRole("button", { name: "持久提交一次性意图" }));
  await screen.findByRole("heading", { name: "持久 Job / Attempt / History" });
  expect(api.posts[1]).toEqual(api.posts[0]);
  expect(api.effects).toEqual({ source: 0, raw: 0, jobs: 1 });
  await act(async () => {
    await api.run(identity);
  });
  fireEvent.click(screen.getByRole("button", { name: "回查执行记录" }));
  await screen.findByText(/succeeded · local_read_verified/);
  expect(api.effects).toEqual({ source: 1, raw: 1, jobs: 1 });
  fireEvent.click(screen.getByRole("link", { name: "04 版本与读取资格" }));
  await screen.findByRole("heading", { name: "逻辑资产目录" });
  // The separate worker changed holdings after App read its catalog snapshot.
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  fireEvent.click(await screen.findByRole("button", { name: "读取本地数据" }));
  await screen.findByText(/读取成功/);
});

it("trusted read-only requests and unsupported modes remain disabled; GET and tabs produce no effects", async () => {
  const api = bridge("readonly");
  const result = await api.exchange({ path: "/api/r5/cpi/jobs" });
  expect(result.status).toBe(200);
  expect(result.body.operator_identity).toBe("readonly-operator");
  expect(result.body.write_eligible).toBe(false);
  expect(result.body.jobs).toEqual([]);
  location.hash =
    "page=4&asset=mdf.macro.bls.cpi.headlines&asset_revision=p3-r1";
  render(<App transport={api.transport} initialQuery={query} />);
  await screen.findByText(/OFFLINE_FIXTURE · OK/);
  expect(screen.getByText(/服务端 Operator：readonly-operator/)).toBeTruthy();
  fireEvent.click(screen.getByRole("checkbox", { name: /我明确提交/ }));
  expect(
    screen
      .getByRole("button", { name: "持久提交一次性意图" })
      .hasAttribute("disabled"),
  ).toBe(true);
  expect(
    screen
      .getByRole("button", { name: "持久提交历史计划" })
      .hasAttribute("disabled"),
  ).toBe(true);
  expect(
    screen.getByRole("button", { name: /多来源/ }).hasAttribute("disabled"),
  ).toBe(true);
  fireEvent.click(screen.getByRole("tab", { name: "周期任务" }));
  expect(
    screen
      .getByRole("button", { name: "创建 Cron / 激活计划" })
      .hasAttribute("disabled"),
  ).toBe(true);
  fireEvent.click(screen.getByRole("tab", { name: "执行记录" }));
  await screen.findByText("当前范围无持久 Job。");
  expect(api.posts).toEqual([]);
  expect(api.effects).toEqual({ source: 0, raw: 0, jobs: 0 });
});

it("anonymous requests are denied GET and expose no jobs or write action", async () => {
  const api = bridge("unbound");
  const result = await api.exchange({ path: "/api/r5/cpi/jobs" });
  expect(result.status).toBe(403);
  expect(result.body.classification).toBe("operator_not_authorized");
  expect(result.body.jobs).toBeUndefined();
  location.hash =
    "page=4&asset=mdf.macro.bls.cpi.headlines&asset_revision=p3-r1";
  render(<App transport={api.transport} initialQuery={query} />);
  await screen.findByText("UNKNOWN · operator_not_authorized");
  fireEvent.click(screen.getByRole("checkbox", { name: /我明确提交/ }));
  expect(
    screen
      .getByRole("button", { name: "持久提交一次性意图" })
      .hasAttribute("disabled"),
  ).toBe(true);
  fireEvent.click(screen.getByRole("tab", { name: "执行记录" }));
  expect(screen.queryAllByRole("article")).toHaveLength(0);
  expect(screen.queryByText("当前范围无持久 Job。")).toBeNull();
  expect(api.posts).toEqual([]);
  expect(api.effects).toEqual({ source: 0, raw: 0, jobs: 0 });
});

it.each([
  "identity",
  "revision",
  "missing operation",
  "mismatched operation",
  "unsupported operation",
])(
  "04 refuses initialization for an unrecognized %s without holdings",
  async (invalid) => {
    const api = bridge();
    const catalog = await api.exchange({
      path: `/api/r5/cpi?${new URLSearchParams(query)}`,
    });
    const sources = await api.exchange({ path: "/api/r5/sources" });
    const directory = sources.body as Directory;
    const route = new URLSearchParams({
      page: "3",
      asset: catalog.body.definition.identity,
      asset_revision: catalog.body.definition.revision,
    });
    if (invalid === "identity") route.set("asset", "missing");
    if (invalid === "revision") route.set("asset_revision", "unrecognized");
    if (invalid === "missing operation") route.set("operation", "missing");
    if (invalid === "mismatched operation") {
      const cpi = directory.entries.find(
        (entry) => entry.web_read === "CPI_REQUEST_QUALIFIED",
      )!;
      route.set("operation", cpi.identity);
      route.set("asset_revision", "unrecognized");
    }
    if (invalid === "unsupported operation") {
      const other = directory.entries.find(
        (entry) => entry.web_read !== "CPI_REQUEST_QUALIFIED" && entry.asset,
      )!;
      route.set("operation", other.identity);
      route.set("asset", other.asset!.identity);
      route.set("asset_revision", other.asset!.revision);
    }
    location.hash = route.toString();
    render(<App transport={api.transport} initialQuery={query} />);
    await screen.findByText(directory.evidence_mode);
    await waitFor(() =>
      expect(screen.queryByText("正在核实本地只读证据…")).toBeNull(),
    );
    expect(
      screen.queryByRole("button", { name: "新建 CPI 一次性采集" }),
    ).toBeNull();
    expect(api.posts).toEqual([]);
    expect(api.effects).toEqual({ source: 0, raw: 0, jobs: 0 });
  },
);

it("failed attempts appear in 01 and link back to the same durable Job", async () => {
  const api = bridge();
  const initial = await api.exchange({ path: "/api/r5/cpi/jobs" });
  const data = initial.body;
  await api.exchange({
    path: "/api/r5/cpi/jobs",
    method: "POST",
    body: {
      job_identity: data.intent_identity,
      asset_identity: data.asset_identity,
      asset_revision: data.asset_revision,
      reference: data.reference,
      release_slot: data.release_slot,
      mode: "manual_once",
    },
  });
  await api.run(data.intent_identity, true);
  location.hash = `page=0&job=${data.intent_identity}`;
  render(<App transport={api.transport} initialQuery={query} />);
  await screen.findByText(/failed · preflight_rejected/);
  fireEvent.click(
    screen.getByRole("link", { name: "05 回查同一 Job / Attempt" }),
  );
  await screen.findByRole("heading", { name: "持久 Job / Attempt / History" });
  await screen.findByText(/failed · stopped · preflight_rejected/);
  expect(location.hash).toContain(`job=${data.intent_identity}`);
  expect(screen.queryByText(/private-worker-detail/)).toBeNull();
  expect(api.effects).toEqual({ source: 0, raw: 0, jobs: 1 });
});
