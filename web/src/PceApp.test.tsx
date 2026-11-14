import { openSubmittedBrief } from "./openSubmittedBrief.testHelper";
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
import "./offlineTestSetup";
import App from "./App";
import { asgi, closeBridges } from "./workflowTestBridge";
import { PCE, PCE_OPERATION, CPI, MANUAL_OPERATION } from "./workflowModel";
import { pceExecution, pceLabels, type PceJob } from "./pce";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// Synthetic PCE payloads use the actual bounded API shape. Python tests exercise
// the native composition, journal and local Reader against offline domain data.
const binding = {
  operator: "ao-dev-bea-pce",
  definition: PCE.identity,
  definition_revision: PCE.revision,
  source: "mdf.source-candidate.bea.nipa.pce",
  source_revision: "t04-retained-candidate-r1",
  binding: "mdf.binding-candidate.bea.nipa.pce",
  binding_revision: "t04-retained-candidate-r1",
  operation: "GetData:NIPA:T20807:T20804:M",
  reference: "2026-07",
  source_qualified: false as const,
  target: {
    host: "192.168.10.4",
    database: "market_data_foundation_dev",
    schema: "mdf_macro_bea_pce_vertical_r1",
  },
};
const catalog = {
  binding,
  parameters: [],
  measurements: Object.keys(pceLabels),
  source_qualified: false,
  write_eligible: true,
  submission: {
    confirmation: "确认登记一次 DEV BEA PCE 2026-07 候选采集",
    job_identity: "synthetic-pce",
    binding,
  },
};
const at = "2026-10-25T09:00:00Z";
const oldVersion = "retained-pce:version";
const newVersion = "new-pce:version";
function host(lost = false) {
  const r5 = asgi("available");
  const requests: { path: string; method: string; body?: unknown }[] = [];
  let jobs: PceJob[] = [];
  let versions = [oldVersion];
  const transport: typeof fetch = vi.fn(async (input, init) => {
    const path = String(input),
      method = init?.method ?? "GET";
    requests.push({
      path,
      method,
      ...(init?.body ? { body: JSON.parse(String(init.body)) } : {}),
    });
    if (!path.startsWith("/api/pce/")) return r5.transport(input, init);
    if (method === "POST") {
      expect(JSON.parse(String(init?.body))).toEqual(catalog.submission);
      jobs = [
        {
          job_id: "synthetic-pce",
          binding,
          state: "pending",
          stage: "registered",
          attempts: 0,
          attempt_id: null,
          classification: null,
          updated_at: at,
        },
      ];
      if (lost) throw new Error("synthetic_lost_response");
      return Response.json(jobs[0]);
    }
    if (path === "/api/pce/catalog") return Response.json(catalog);
    if (path === "/api/pce/jobs")
      return Response.json({ jobs: lost ? [] : jobs });
    if (path === "/api/pce/assets")
      return Response.json({
        binding,
        as_of: at,
        versions: versions.map((v) => ({
          version_identity: v,
          frozen_at: v === oldVersion ? "2026-10-16T00:00:00Z" : at,
          selection_state: "stored_candidate_read_unverified",
          publications: [
            {
              publication_identity: `${v}:publication`,
              decision: "publish",
              known_at: at,
              usable_at: at,
            },
          ],
        })),
      });
    const q = new URL(path, "http://localhost").searchParams;
    return Response.json({
      binding,
      reference: q.get("reference"),
      as_of: q.get("as_of"),
      version_identity: q.get("version") ?? versions.at(-1),
      publication_identity: `${q.get("version") ?? versions.at(-1)}:publication`,
      acquisition_identity: "synthetic-acquisition",
      known_at: at,
      usable_at: at,
      source_qualified: false,
      measurements: Object.fromEntries(
        Object.keys(pceLabels).map((k, i) => [
          k,
          { value: `${i}.25`, classification: "synthetic" },
        ]),
      ),
    });
  });
  return {
    transport,
    requests,
    jobs: () => jobs,
    forget: () => {
      jobs = [];
    },
    finish: () => {
      versions = [oldVersion, newVersion];
      jobs = [
        {
          ...jobs[0],
          state: "succeeded",
          stage: "local_read_verified",
          attempt_id: "synthetic-pce-a1",
          attempts: 1,
          events: [
            {
              ...jobs[0],
              state: "running",
              stage: "provider_read",
              attempt_id: "synthetic-pce-a1",
            },
          ],
          reconciliation: {
            evidence: {
              version_identity: newVersion,
              publication_identity: `${newVersion}:publication`,
              usable_at: at,
            },
          },
        },
      ];
    },
  };
}
const btn = (name: string | RegExp) => screen.getByRole("button", { name });
const confirm = () =>
  fireEvent.click(
    screen.getByRole("checkbox", { name: "我明确提交此目标的采集意图" }),
  );
const posts = (h: ReturnType<typeof host>) =>
  h.requests.filter((r) => r.method === "POST");
async function ready() {
  await waitFor(() =>
    expect((btn("刷新") as HTMLButtonElement).disabled).toBe(false),
  );
}
async function open(
  transport: typeof fetch,
  fields: Record<string, string> = {},
) {
  location.hash = new URLSearchParams({
    ui: "r5",
    view: "collection",
    asset: PCE.identity,
    asset_revision: PCE.revision,
    operation: PCE_OPERATION,
    ...fields,
  }).toString();
  const view = render(
    <App
      transport={transport}
      initialQuery={{ reference: "2026-07", as_of: at }}
    />,
  );
  if (fields.tab === "executions")
    await screen.findByRole("heading", { name: "执行详情" });
  else await screen.findByText(/BEA PCE 开发测试/);
  await waitFor(() =>
    expect(screen.queryByText(/持久任务状态尚未核实/)).toBeNull(),
  );
  await ready();
  return view;
}
afterEach(() => {
  cleanup();
  closeBridges();
  history.replaceState(null, "", "/");
  sessionStorage.clear();
  vi.restoreAllMocks();
});

it("developer contract navigation preserves PCE binding and the only create button routes a strict PCE POST", async () => {
  const entry = readFileSync(resolve(process.cwd(), "src/main.tsx"), "utf8");
  expect(entry).toContain("<App />");
  expect(
    readFileSync(resolve(process.cwd(), "src/App.tsx"), "utf8"),
  ).not.toContain("PceCandidate");
  const h = host();
  history.replaceState(null, "", "/pce");
  render(<App transport={h.transport} />);
  fireEvent.click(btn("平台管理"));
  fireEvent.click(screen.getByRole("tab", { name: "日志与诊断" }));
  await ready();
  fireEvent.click(
    within(screen.getByRole("region", { name: "注册接口" })).getByRole(
      "button",
      { name: /选择 美国 PCE/ },
    ),
  );
  expect(screen.getByText(/T20807 → T20804/)).toBeTruthy();
  fireEvent.click(btn("查看关联资产 →"));
  await ready();
  expect(screen.queryByRole("button", { name: "创建采集任务" })).toBeNull();
  fireEvent.click(btn("采集任务 →"));
  await waitFor(() =>
    expect(screen.queryByText(/持久任务状态尚未核实/)).toBeNull(),
  );
  expect((screen.getByLabelText("开始参考月") as HTMLInputElement).value).toBe(
    "2026-07",
  );
  expect((screen.getByLabelText("结束参考月") as HTMLInputElement).value).toBe(
    "2026-07",
  );
  expect(screen.getAllByRole("button", { name: "创建采集任务" })).toHaveLength(
    1,
  );
  expect(document.querySelectorAll("form")).toHaveLength(1);
  expect((btn("创建采集任务") as HTMLButtonElement).disabled).toBe(true);
  confirm();
  await waitFor(() =>
    expect((btn("创建采集任务") as HTMLButtonElement).disabled).toBe(false),
  );
  fireEvent.click(btn("创建采集任务"));
  await openSubmittedBrief();
  expect(
    screen.getByRole("tab", { name: "执行记录" }).getAttribute("aria-selected"),
  ).toBe("true");
  expect(posts(h)).toEqual([
    { path: "/api/pce/jobs", method: "POST", body: catalog.submission },
  ]);
  expect(h.requests.some((r) => r.path.includes("worker"))).toBe(false);
  expect(document.querySelectorAll("nav svg")).toHaveLength(5);
  for (const name of ["新建采集", "周期任务", "执行记录"])
    expect(screen.getByRole("tab", { name })).toBeTruthy();
  fireEvent.click(btn("运行总览"));
  expect(screen.getByRole("heading", { name: "系统资源监控" })).toBeTruthy();
  expect(screen.getByText("待处理异常")).toBeTruthy();
  fireEvent.click(btn("平台管理"));
  expect(screen.getByRole("tab", { name: "运行实例" })).toBeTruthy();
});

it("preserves UNKNOWN reservation across refresh, navigation, remount and stale empty snapshots", async () => {
  const h = host(true);
  const view = await open(h.transport);
  confirm();
  fireEvent.click(btn("创建采集任务"));
  await screen.findByText(/提交回执未核实/);
  h.forget();
  fireEvent.click(btn("刷新"));
  await ready();
  fireEvent.click(btn("数据源管理"));
  fireEvent.click(btn("采集任务"));
  await screen.findByText(/此意图已预留/);
  view.unmount();
  render(<App transport={h.transport} />);
  await screen.findByText(/此意图已预留/);
  confirm();
  expect((btn("创建采集任务") as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(btn("创建采集任务"));
  expect(posts(h)).toHaveLength(1);
});

it("late pre-submit GET cannot unlock accepted intent, including reopening", async () => {
  const h = host();
  let release!: (r: Response) => void;
  let hold = false;
  const pending = new Promise<Response>((resolve) => {
    release = resolve;
  });
  const transport: typeof fetch = (input, init) =>
    hold && String(input) === "/api/pce/jobs" && init?.method === "GET"
      ? pending
      : h.transport(input, init);
  const view = await open(transport);
  confirm();
  fireEvent.click(btn("创建采集任务"));
  await openSubmittedBrief();
  hold = true;
  fireEvent(document, new Event("visibilitychange"));
  fireEvent.click(screen.getByRole("tab", { name: "新建采集" }));
  await act(async () => {
    release(Response.json({ jobs: [] }));
    await pending;
  });
  await screen.findByText(/此意图已预留/);
  confirm();
  expect((btn("创建采集任务") as HTMLButtonElement).disabled).toBe(true);
  view.unmount();
  render(<App transport={transport} />);
  await screen.findByText(/此意图已预留/);
  expect(posts(h)).toHaveLength(1);
});

it("restores a durable job without session storage and links only its new worker version to 04/06", async () => {
  const h = host();
  const view = await open(h.transport);
  confirm();
  fireEvent.click(btn("创建采集任务"));
  await openSubmittedBrief();
  h.finish();
  view.unmount();
  sessionStorage.clear();
  render(<App transport={h.transport} />);
  expect(
    (await screen.findAllByText(/synthetic-pce-a1/)).length,
  ).toBeGreaterThan(0);
  expect(screen.getByText(/provider_read/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "指定范围回补 →" })).toBeNull();
  fireEvent.click(btn("精确读取结果 →"));
  await ready();
  expect((screen.getByLabelText("保存版本") as HTMLInputElement).value).toBe(
    newVersion,
  );
  fireEvent.click(btn("读取本地数据"));
  await screen.findByRole("region", { name: "读取结果" });
  expect(screen.getByText(`${newVersion}:publication`)).toBeTruthy();
  for (const value of ["0.25", "1.25", "2.25", "3.25"])
    expect(screen.getByText(value)).toBeTruthy();
  fireEvent.click(btn("数据资产"));
  await ready();
  fireEvent.change(screen.getByLabelText("读取方式"), {
    target: { value: "exact_version" },
  });
  const savedVersions = screen.getByLabelText("保存版本") as HTMLSelectElement;
  expect([...savedVersions.options].map((o) => o.value)).toEqual(
    expect.arrayContaining([oldVersion, newVersion]),
  );
  fireEvent.change(savedVersions, { target: { value: newVersion } });
  await ready();
  expect((screen.getByLabelText("保存版本") as HTMLSelectElement).value).toBe(
    newVersion,
  );
  fireEvent.click(btn("读取本地数据"));
  await screen.findByText(`${newVersion}`);
  fireEvent.change(screen.getByLabelText("读取方式"), {
    target: { value: "as_of" },
  });
  fireEvent.click(btn("读取本地数据"));
  await screen.findByText(`${newVersion}`);
  const reads = h.requests.filter((r) => r.path.startsWith("/api/pce/read?"));
  expect(reads.some((r) => r.path.includes("version=new-pce%3Aversion"))).toBe(
    true,
  );
  expect(reads.every((r) => r.path.includes("reference=2026-07"))).toBe(true);
  expect(posts(h)).toHaveLength(1);
});

it("04 old publication does not manufacture a job or a successful collection", async () => {
  const h = host();
  await open(h.transport);
  fireEvent.click(btn("数据资产"));
  await ready();
  fireEvent.change(screen.getByLabelText("读取方式"), {
    target: { value: "exact_version" },
  });
  const savedVersions = screen.getByLabelText("保存版本") as HTMLSelectElement;
  expect([...savedVersions.options].map((o) => o.value)).toContain(oldVersion);
  fireEvent.change(savedVersions, { target: { value: oldVersion } });
  expect(screen.queryByRole("button", { name: "创建采集任务" })).toBeNull();
  fireEvent.click(btn("读取本地数据"));
  await screen.findByText(`${oldVersion}`);
  expect(h.jobs()).toEqual([]);
  expect(posts(h)).toHaveLength(0);
});

it("keeps CPI and PCE records partitioned, with both providers in unfiltered executions", async () => {
  const h = host();
  await open(h.transport);
  confirm();
  fireEvent.click(btn("创建采集任务"));
  await openSubmittedBrief();
  await act(async () => {
    location.hash = "ui=r5&view=collection&tab=executions";
    window.dispatchEvent(new Event("hashchange"));
  });
  await screen.findByRole("button", { name: /^选择 synthetic-pce / });
  expect(h.requests.some((r) => r.path.startsWith("/api/r5/cpi/jobs"))).toBe(
    true,
  );
  fireEvent.click(screen.getByRole("tab", { name: "新建采集" }));
  fireEvent.change(screen.getByLabelText("采集目标"), {
    target: { value: `${CPI.identity}@${CPI.revision}` },
  });
  fireEvent.click(screen.getByRole("tab", { name: "执行记录" }));
  expect(
    screen.queryByRole("button", { name: /^选择 synthetic-pce / }),
  ).toBeNull();
  expect(posts(h)).toHaveLength(1);
});

it("rejects wrong month and source identities without CPI fallback", async () => {
  const h = host();
  await open(h.transport);
  fireEvent.change(screen.getByLabelText("开始参考月"), {
    target: { value: "2026-08" },
  });
  confirm();
  expect(
    (screen.getByLabelText("开始参考月") as HTMLInputElement).checkValidity(),
  ).toBe(false);
  fireEvent.click(btn("创建采集任务"));
  expect(h.requests.filter((r) => r.method === "POST")).toHaveLength(0);
  expect(screen.getByText(/不得晚于 2026-07/)).toBeTruthy();
  await act(async () => {
    location.hash = new URLSearchParams({
      ui: "r5",
      view: "collection",
      asset: PCE.identity,
      asset_revision: PCE.revision,
      operation: MANUAL_OPERATION,
    }).toString();
    window.dispatchEvent(new Event("hashchange"));
  });
  await screen.findByText(/关联对象缺失或身份冲突/);
  expect((btn("创建采集任务") as HTMLButtonElement).disabled).toBe(true);
  expect(posts(h)).toHaveLength(0);
});

it("rejects catalog binding mismatch and read-only preview writes", async () => {
  const h = host();
  let readonly = true;
  const transport: typeof fetch = (input, init) =>
    String(input) === "/api/pce/catalog"
      ? Promise.resolve(
          Response.json(
            readonly
              ? { ...catalog, write_eligible: false }
              : { ...catalog, binding: { ...binding, source: "foreign" } },
          ),
        )
      : h.transport(input, init);
  await open(transport);
  confirm();
  expect((btn("创建采集任务") as HTMLButtonElement).disabled).toBe(true);
  await screen.findByText(/当前为只读预览/);
  readonly = false;
  fireEvent.click(btn("刷新"));
  await ready();
  expect((btn("创建采集任务") as HTMLButtonElement).disabled).toBe(true);
  expect(posts(h)).toHaveLength(0);
});

it("discards an old in-flight read after refresh fails", async () => {
  const h = host();
  let hold = false;
  let fail = false;
  let started = false;
  let release!: (r: Response) => void;
  const pending = new Promise<Response>((resolve) => {
    release = resolve;
  });
  const transport: typeof fetch = (input, init) => {
    if (fail && String(input) === "/api/pce/catalog")
      return Promise.resolve(new Response("{}", { status: 503 }));
    if (hold && String(input).startsWith("/api/pce/read?")) {
      started = true;
      return pending;
    }
    return h.transport(input, init);
  };
  await open(transport);
  fireEvent.click(btn("数据资产"));
  await ready();
  hold = true;
  fireEvent.click(btn("读取本地数据"));
  await waitFor(() => expect(started).toBe(true));
  fail = true;
  fireEvent.click(btn("刷新"));
  await ready();
  await act(async () => {
    release(await h.transport(`/api/pce/read?reference=2026-07&as_of=${at}`));
    await pending;
  });
  expect(screen.queryByRole("region", { name: "读取结果" })).toBeNull();
  expect(posts(h)).toHaveLength(0);
});

it("uses real PCE API shapes for native submit → independent worker → new inventory → LocalReader", async () => {
  const h = asgi("native", "pce_native_ui_exchange.py");
  await open(h.transport);
  expect(h.effects).toEqual({ source: 0, raw: 0, jobs: 0 });
  confirm();
  await waitFor(() =>
    expect((btn("创建采集任务") as HTMLButtonElement).disabled).toBe(false),
  );
  fireEvent.click(btn("创建采集任务"));
  await openSubmittedBrief();
  expect(h.effects).toEqual({ source: 0, raw: 0, jobs: 1 });
  const result = await h.exchange({ control: "run" });
  expect(result.body.state).toBe("succeeded");
  fireEvent(document, new Event("visibilitychange"));
  await screen.findByRole("button", { name: "精确读取结果 →" });
  fireEvent.click(btn("精确读取结果 →"));
  await ready();
  fireEvent.click(btn("读取本地数据"));
  await screen.findByRole("region", { name: "读取结果" });
  expect(screen.getAllByText("0.3")).toHaveLength(2);
  expect(screen.getAllByText("10.000000")).toHaveLength(2);
  expect(screen.getByText(/pce-init-.*:version$/)).toBeTruthy();
  expect(screen.getByText(/pce-init-.*:publication$/)).toBeTruthy();
  fireEvent.click(btn("数据资产"));
  await ready();
  expect(screen.queryByRole("button", { name: "精确读取此版本 →" })).toBeNull();
  await ready();
  fireEvent.change(screen.getByLabelText("读取方式"), {
    target: { value: "exact_version" },
  });
  const versionSelect = screen.getByLabelText("保存版本") as HTMLSelectElement;
  const fresh = [...versionSelect.options].find((option) =>
    option.value.startsWith("pce-init-"),
  )!.value;
  fireEvent.change(versionSelect, { target: { value: fresh } });
  await ready();
  fireEvent.click(btn("读取本地数据"));
  expect(new URLSearchParams(location.hash.slice(1)).get("version")).toBe(
    fresh,
  );
  await screen.findByText(`${fresh}`);
  expect(h.effects.source).toBe(1);
  expect(h.effects.jobs).toBe(1);
  expect(h.requests.filter((r) => r.method === "POST")).toHaveLength(1);
});

it("pending POST cannot be sent again after unmount or a late receipt", async () => {
  const h = host();
  let release!: (r: Response) => void;
  let started = false;
  const pending = new Promise<Response>((resolve) => {
    release = resolve;
  });
  const transport: typeof fetch = async (input, init) => {
    if (init?.method === "POST") {
      started = true;
      await h.transport(input, init);
      return pending;
    }
    return h.transport(input, init);
  };
  const view = await open(transport);
  confirm();
  fireEvent.click(btn("创建采集任务"));
  await waitFor(() => expect(started).toBe(true));
  view.unmount();
  h.forget();
  render(<App transport={transport} />);
  await screen.findByText(/此意图已预留/);
  await act(async () => {
    release(
      Response.json({ job_id: "synthetic-pce", binding, state: "pending" }),
    );
    await pending;
  });
  confirm();
  expect((btn("创建采集任务") as HTMLButtonElement).disabled).toBe(true);
  expect(posts(h)).toHaveLength(1);
});

it("retains the previous standalone UI UNKNOWN marker without exposing its workflow", async () => {
  const h = host();
  sessionStorage.setItem("mdf-pce-202607-submission-pending", "pending");
  await open(h.transport);
  confirm();
  expect((btn("创建采集任务") as HTMLButtonElement).disabled).toBe(true);
  expect(screen.queryByRole("button", { name: "确认登记 PG Job" })).toBeNull();
  expect(posts(h)).toHaveLength(0);
  const briefs = within(
    screen.getByRole("complementary", { name: "执行简报" }),
  );
  expect(briefs.getByText(catalog.submission.job_identity)).toBeTruthy();
  expect(
    (
      briefs.getByRole("button", {
        name: new RegExp(catalog.submission.job_identity),
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  const before = h.requests.length;
  fireEvent.click(briefs.getByRole("button", { name: "核实原请求" }));
  await screen.findByText(/原请求尚未核实/);
  expect(
    h.requests
      .slice(before)
      .some((r) => r.method === "GET" && r.path === "/api/pce/jobs"),
  ).toBe(true);
  expect(posts(h)).toHaveLength(0);
});

const originalPce = "pce-init-698446184a6df3c77bfb182b4411fa73b001143c";
it("failed native execution → exact backfill POST → independent successor worker → exact read", async () => {
  const h = asgi("backfill", "pce_native_ui_exchange.py");
  await open(h.transport, { tab: "executions", job: originalPce });
  await screen.findByRole("heading", { name: "执行详情" });
  fireEvent.click(btn("指定范围回补 →"));
  const parent = (await screen.findByLabelText(
    "关联失败 / 异常执行",
  )) as HTMLSelectElement;
  await waitFor(() => expect(parent.value).toBe(originalPce));
  expect((btn("创建采集任务") as HTMLButtonElement).disabled).toBe(true);
  confirm();
  await waitFor(() =>
    expect((btn("创建采集任务") as HTMLButtonElement).disabled).toBe(false),
  );
  fireEvent.click(btn("创建采集任务"));
  await openSubmittedBrief();
  const writes = h.requests.filter((r) => r.method === "POST");
  expect(writes).toHaveLength(1);
  expect(writes[0].path).toBe("/api/pce/jobs/backfill");
  expect(writes[0].status).toBe(202);
  expect(writes[0].body.parent_job_id).toBe(originalPce);
  const successor = writes[0].body.job_identity;
  expect(successor).not.toBe(originalPce);
  expect(h.effects).toEqual({ source: 0, raw: 0, jobs: 2 });
  expect(screen.queryByRole("button", { name: "精确读取结果 →" })).toBeNull();
  expect(screen.getByText(`原失败 Job：${originalPce}`)).toBeTruthy();
  expect(btn("查看原失败执行 →")).toBeTruthy();
  const jobs = await h.exchange({ path: "/api/pce/jobs" });
  const old = jobs.body.jobs.find((j: PceJob) => j.job_id === originalPce);
  expect(old.state).toBe("failed");
  expect(old.classification).toBe("credential_echo_rejected");
  expect(jobs.body.backfill.eligible).toBe(false);
  expect((await h.exchange({ control: "run" })).body.state).toBe("succeeded");
  fireEvent(document, new Event("visibilitychange"));
  await screen.findByRole("button", { name: "精确读取结果 →" });
  fireEvent.click(btn("精确读取结果 →"));
  await ready();
  fireEvent.click(btn("读取本地数据"));
  await screen.findByText(`${successor}:version`);
  expect(screen.getByText(`${successor}:publication`)).toBeTruthy();
  expect(h.effects.source).toBe(1); // One acquirer invocation reserves two BEA table reads.
  expect(h.requests.filter((r) => r.method === "POST")).toHaveLength(1);
});

it.each([
  ["backfill-blocked", "测试库尚未准入这笔回补任务"],
  ["backfill-unknown", "原执行效果未知"],
])("%s shows actual reason and cannot submit", async (scenario, reason) => {
  const h = asgi(scenario, "pce_native_ui_exchange.py");
  await open(h.transport, { tab: "new", job: originalPce, mode: "backfill" });
  await screen.findByText(new RegExp(reason));
  confirm();
  expect((btn("创建采集任务") as HTMLButtonElement).disabled).toBe(true);
  expect(h.requests.filter((r) => r.method === "POST")).toHaveLength(0);
  expect(h.effects).toEqual({ source: 0, raw: 0, jobs: 1 });
});

it("late parent loading preserves an edited backfill range and requires fresh confirmation", async () => {
  const h = asgi("backfill", "pce_native_ui_exchange.py");
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  const transport: typeof fetch = async (input, init) => {
    if (
      String(input) === "/api/pce/jobs" &&
      (!init?.method || init.method === "GET")
    )
      await pending;
    return h.transport(input, init);
  };
  await open(transport, { tab: "new", job: originalPce, mode: "backfill" });
  expect(
    screen.queryByRole("option", { name: new RegExp(originalPce) }),
  ).toBeNull();
  fireEvent.change(screen.getByLabelText("开始参考月"), {
    target: { value: "2026-08" },
  });
  confirm();
  await act(async () => release());
  await screen.findByRole("option", { name: new RegExp(originalPce) });
  expect((screen.getByLabelText("开始参考月") as HTMLInputElement).value).toBe(
    "2026-08",
  );
  expect(
    (screen.getByLabelText("开始参考月") as HTMLInputElement).checkValidity(),
  ).toBe(false);
  expect((btn("创建采集任务") as HTMLButtonElement).disabled).toBe(true);
  expect(h.requests.filter((r) => r.method === "POST")).toHaveLength(0);
  fireEvent.change(screen.getByLabelText("开始参考月"), {
    target: { value: "2026-07" },
  });
  expect(
    (
      screen.getByRole("checkbox", {
        name: "我明确提交此目标的采集意图",
      }) as HTMLInputElement
    ).checked,
  ).toBe(false);
  confirm();
  await waitFor(() =>
    expect((btn("创建采集任务") as HTMLButtonElement).disabled).toBe(false),
  );
  expect(h.requests.filter((r) => r.method === "POST")).toHaveLength(0);
});

it("backfill rejects a changed reference and a lost receipt cannot produce another POST", async () => {
  const h = asgi("backfill", "pce_native_ui_exchange.py");
  const view = await open(h.transport, {
    tab: "new",
    job: originalPce,
    mode: "backfill",
  });
  fireEvent.change(screen.getByLabelText("开始参考月"), {
    target: { value: "2026-08" },
  });
  confirm();
  expect(
    (screen.getByLabelText("开始参考月") as HTMLInputElement).checkValidity(),
  ).toBe(false);
  fireEvent.click(btn("创建采集任务"));
  expect(h.requests.filter((r) => r.method === "POST")).toHaveLength(0);
  fireEvent.change(screen.getByLabelText("开始参考月"), {
    target: { value: "2026-07" },
  });
  confirm();
  await waitFor(() =>
    expect((btn("创建采集任务") as HTMLButtonElement).disabled).toBe(false),
  );
  h.loseNext();
  fireEvent.click(btn("创建采集任务"));
  await screen.findByText(/此回补意图已预留/);
  view.unmount();
  await open(h.transport, { tab: "new", job: originalPce, mode: "backfill" });
  confirm();
  expect((btn("创建采集任务") as HTMLButtonElement).disabled).toBe(true);
  expect(h.requests.filter((r) => r.method === "POST")).toHaveLength(1);
  expect(h.effects).toEqual({ source: 0, raw: 0, jobs: 2 });
});

it("successor cannot borrow the retained publication as its successful result", () => {
  const job: PceJob = {
    job_id: "pce-backfill-test",
    parent_job_id: originalPce,
    binding,
    attempts: 1,
    attempt_id: "pce-backfill-test-a1",
    state: "succeeded",
    stage: "verified",
    classification: null,
    updated_at: at,
    reconciliation: {
      outcome: "confirmed_published",
      evidence: {
        version_identity: oldVersion,
        publication_identity: `${oldVersion}:publication`,
        usable_at: at,
      },
    },
  };
  expect(pceExecution(job).version).toBeNull();
  expect(pceExecution(job).publication).toBeNull();
});
