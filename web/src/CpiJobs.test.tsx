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
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";
import { resolve } from "node:path";
import App from "./App";
import {
  CPI,
  MANUAL_OPERATION,
  HISTORY_OPERATION,
  keyOf,
} from "./workflowModel";
import { CpiJobAlerts, CpiJobsView } from "./CpiJobs";
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
      env: {
        ...process.env,
        PYTHONPATH: `${root}/src:${root}:${process.env.PYTHONPATH ?? ""}`,
      },
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

it("preserves exact manual target through lost durable receipt, explicit resubmission, separate offline worker and local read", async () => {
  const api = bridge();
  const target = await api.exchange({ path: "/api/r5/cpi/jobs" });
  const operation =
    "mdf.source-candidate.bls-v2.cpi.release-2026-09@p4-release-reference-2026-09-r1";
  location.hash = new URLSearchParams({
    page: "4",
    asset: target.body.asset_identity,
    asset_revision: target.body.asset_revision,
    operation,
  }).toString();
  const view = render(<App transport={api.transport} initialQuery={query} />);
  await screen.findByText(/Operator：synthetic-operator/);
  for (const name of ["开始参考月", "结束参考月"])
    fireEvent.change(screen.getByLabelText(name), {
      target: { value: target.body.reference },
    });
  fireEvent.click(screen.getByRole("checkbox"));
  api.loseNext();
  fireEvent.click(screen.getByRole("button", { name: "创建采集任务" }));
  await screen.findByText(/提交回执未核实/);
  expect(api.effects).toEqual({ source: 0, raw: 0, jobs: 1 });
  expect(api.posts).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "创建采集任务" }));
  expect(api.posts).toHaveLength(1);
  view.unmount();
  render(<App transport={api.transport} initialQuery={query} />);
  await screen.findByText(/Operator：synthetic-operator/);
  for (const name of ["开始参考月", "结束参考月"])
    fireEvent.change(screen.getByLabelText(name), {
      target: { value: target.body.reference },
    });
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "创建采集任务" }));
  await screen.findByRole("heading", { name: "执行详情" });
  expect(api.posts[1]).toEqual(api.posts[0]);
  expect(api.effects).toEqual({ source: 0, raw: 0, jobs: 1 });
  const identity = (api.posts[0] as { job_identity: string }).job_identity;
  await act(async () => {
    await api.run(identity);
  });
  fireEvent.click(screen.getByRole("button", { name: "回查执行记录" }));
  await waitFor(() => {
    const detail = within(
      screen.getByRole("complementary", { name: "执行记录详情" }),
    );
    expect(
      detail.getByText("执行结果").parentElement?.querySelector("dd")
        ?.textContent,
    ).toBe("成功");
    expect(
      detail.getByText("执行阶段回报").parentElement?.querySelector("dd")
        ?.textContent,
    ).toBe("local_read_verified · 无错误分类");
  });
  expect(api.effects).toEqual({ source: 1, raw: 1, jobs: 1 });
  fireEvent.click(screen.getByRole("button", { name: "查看关联资产 →" }));
  await screen.findByRole("heading", { name: "CPI · 四项核心指标" });
  const read = await screen.findByRole("button", { name: "读取本地数据" });
  fireEvent.click(read);
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
  render(
    <CpiJobsView
      transport={api.transport}
      generation={0}
      asset="mdf.macro.bls.cpi.headlines"
      revision="p3-r1"
    />,
  );
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
  render(
    <CpiJobsView
      transport={api.transport}
      generation={0}
      asset="mdf.macro.bls.cpi.headlines"
      revision="p3-r1"
    />,
  );
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
  "unknown operation",
  "mismatched operation",
  "unsupported operation",
])(
  "current collection submission refuses %s with exact context and zero ASGI effects",
  async (invalid) => {
    const api = bridge();
    const sources = await api.exchange({ path: "/api/r5/sources" });
    const directory = sources.body as Directory;
    const route = new URLSearchParams({
      ui: "r5",
      view: "collection",
      tab: "new",
      asset: CPI.identity,
      asset_revision: CPI.revision,
      operation: MANUAL_OPERATION,
    });
    if (invalid === "identity") route.set("asset", "missing");
    if (invalid === "revision") route.set("asset_revision", "unrecognized");
    if (invalid === "missing operation") route.delete("operation");
    if (invalid === "unknown operation") route.set("operation", "missing");
    if (invalid === "mismatched operation") {
      const other = directory.entries.find(
        (entry) => entry.asset && entry.asset.identity !== CPI.identity,
      )!;
      route.set("operation", other.identity);
    }
    if (invalid === "unsupported operation")
      route.set("operation", HISTORY_OPERATION);
    location.hash = route.toString();
    render(<App transport={api.transport} initialQuery={query} />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "刷新" }).hasAttribute("disabled"),
      ).toBe(false),
    );
    const summary = within(screen.getByLabelText("本次采集摘要"));
    const hasAsset = ["missing operation", "unsupported operation"].includes(
      invalid,
    );
    expect((screen.getByLabelText("采集目标") as HTMLSelectElement).value).toBe(
      hasAsset ? keyOf(CPI) : "",
    );
    if (hasAsset) expect(summary.getByText("CPI · 四项核心指标")).toBeTruthy();
    else expect(screen.getByText(/关联对象缺失或身份冲突/)).toBeTruthy();
    const source = screen.getByLabelText("关联数据源 / Operation");
    if (invalid === "unsupported operation") {
      const entry = directory.entries.find(
        (e) => e.identity === HISTORY_OPERATION,
      )!;
      expect(source.textContent).toContain(entry.operation);
      expect(source.textContent).not.toContain("2026-09 公布窗口");
      await screen.findByText(/所选来源变体未绑定此人工执行器/);
    } else expect(source.textContent).toBe("未选择具名来源变体");
    expect(new URLSearchParams(location.hash.slice(1)).get("asset")).toBe(
      route.get("asset"),
    );
    expect(new URLSearchParams(location.hash.slice(1)).get("operation")).toBe(
      route.get("operation"),
    );
    for (const name of ["开始参考月", "结束参考月"])
      fireEvent.change(screen.getByLabelText(name), {
        target: { value: query.reference },
      });
    fireEvent.click(screen.getByRole("checkbox"));
    const submit = screen.getByRole("button", { name: "创建采集任务" });
    expect(submit.hasAttribute("disabled")).toBe(true);
    fireEvent.click(submit);
    // Bypass the disabled DOM control as well: the real submit handler must refuse.
    fireEvent.submit(submit.closest("form")!);
    await act(async () => {
      await api.exchange({ path: "/api/r5/cpi/jobs" });
    });
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
  await screen.findByRole("button", {
    name: `查看执行 ${data.intent_identity} →`,
  });
  fireEvent.click(
    screen.getByRole("button", { name: `查看执行 ${data.intent_identity} →` }),
  );
  await screen.findByRole("heading", { name: "执行详情" });
  await screen.findByText(/stopped · preflight_rejected/);
  expect(location.hash).toContain(`job=${data.intent_identity}`);
  expect(screen.queryByText(/private-worker-detail/)).toBeNull();
  expect(api.effects).toEqual({ source: 0, raw: 0, jobs: 1 });
});
