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
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import App from "./App";
import { LocalReader } from "./WorkflowCatalog";
import type { Catalog, Directory, Transport } from "./api";
import { asgi, closeBridges } from "./workflowTestBridge";
import {
  CPI,
  MANUAL_OPERATION,
  catalogForOperation,
  keyOf,
  project,
} from "./workflowModel";
const query = { reference: "2026-08", as_of: "2026-10-06T00:00:10+00:00" };
const captureQuery = {
  reference: "2026-09",
  as_of: "2026-10-14T13:00:00+00:00",
};
afterEach(() => {
  cleanup();
  closeBridges();
  location.hash = "";
  vi.restoreAllMocks();
});
function nav(name: string) {
  if (name === "数据源管理") {
    nav("平台管理");
    fireEvent.click(screen.getByRole("tab", { name: "日志与诊断" }));
    const summary = screen.getByText("接口实现目录（开发诊断）");
    if (!summary.closest("details")?.open) fireEvent.click(summary);
    return;
  }
  fireEvent.click(
    within(screen.getByRole("navigation")).getByRole("button", { name }),
  );
}
const btn = (name: string | RegExp) => screen.getByRole("button", { name });
async function ready() {
  await waitFor(() => expect(btn("刷新").hasAttribute("disabled")).toBe(false));
}
async function hash(fields: Record<string, string>) {
  await act(async () => {
    location.hash = new URLSearchParams(fields).toString();
    window.dispatchEvent(new Event("hashchange"));
  });
}
function select(label: string, pattern: RegExp) {
  const summary = screen.queryByText("接口实现目录（开发诊断）");
  if (summary && !summary.closest("details")?.open) fireEvent.click(summary);
  fireEvent.click(
    within(screen.getByRole("region", { name: label })).getByRole("button", {
      name: pattern,
    }),
  );
}
async function journey(transport: Transport) {
  location.hash = "ui=r5&view=platform&tab=diagnostics&component=Macro";
  render(<App transport={transport} initialQuery={captureQuery} />);
  await ready();
  expect(new URLSearchParams(location.hash.slice(1)).get("component")).toBe(
    "Macro",
  );
  const developerDirectory = screen.getByText("接口实现目录（开发诊断）");
  if (!developerDirectory.closest("details")?.open)
    fireEvent.click(developerDirectory);
  const table = screen.getByRole("region", { name: "注册接口" });
  expect(within(table).queryByText("TDX / TDX")).toBeNull();
  select("注册接口", /选择 CPI .*2026-09 公布窗口/);
  expect(location.hash).toContain(encodeURIComponent(MANUAL_OPERATION));
  fireEvent.click(btn("查看关联资产 →"));
  await ready();
  expect(
    screen.getByRole("heading", { name: "CPI · 四项核心指标" }),
  ).toBeTruthy();
  expect(new URLSearchParams(location.hash.slice(1)).get("asset")).toBe(
    CPI.identity,
  );
  expect(
    new URLSearchParams(location.hash.slice(1)).get("asset_revision"),
  ).toBe(CPI.revision);
  fireEvent.click(btn("采集任务 →"));
  await ready();
  await waitFor(() => expect(screen.getByLabelText("采集目标")).toBeTruthy());
  expect((screen.getByLabelText("采集目标") as HTMLSelectElement).value).toBe(
    keyOf(CPI),
  );
  const summary = within(screen.getByLabelText("本次采集摘要"));
  expect(summary.getByText("CPI · 四项核心指标")).toBeTruthy();
  expect(summary.getByText("BLS · 2026-09 公布窗口")).toBeTruthy();
  expect(summary.getByText(/继承资产 \/ 接口的 Host 存储绑定/)).toBeTruthy();
}
it("A2: Operation binding survives 02→03→04→06 and both explicit canonical read modes", async () => {
  const api = asgi("available");
  const directory: Directory = (await api.exchange({ path: "/api/r5/sources" }))
    .body;
  const catalog: Catalog = (
    await api.exchange({
      path: `/api/r5/cpi?${new URLSearchParams(query)}`,
    })
  ).body;
  const version = catalog.asset!.versions[0];
  const source = directory.entries.find(
    (e) =>
      e.binding_contract?.identity === version.binding_identity &&
      e.binding_contract?.revision === version.binding_revision,
  )!;
  render(<App transport={api.transport} initialQuery={query} />);
  await hash({
    ui: "r5",
    view: "interfaces",
    operation: source.identity,
    ...query,
  });
  await ready();
  fireEvent.click(btn("查看关联资产 →"));
  await ready();
  expect(screen.getByRole("region", { name: "本地读取" })).toBeTruthy();
  expect(screen.queryByRole("region", { name: "数据资产" })).toBeNull();
  expect(new URLSearchParams(location.hash.slice(1)).get("operation")).toBe(
    source.identity,
  );
  for (const mode of ["as_of", "exact_version"]) {
    fireEvent.change(screen.getByLabelText("读取方式"), {
      target: { value: mode },
    });
    fireEvent.click(btn("读取本地数据"));
    await screen.findByText(/读取成功/);
    expect(screen.getByLabelText("读取结果").textContent).toContain(
      version.identity,
    );
  }
  const reads = api.requests.filter((r) => r.path.includes("/read?"));
  expect(reads).toHaveLength(2);
  for (const request of reads) {
    const params = new URL(request.path, "http://offline").searchParams;
    expect(params.get("reference")).toBe(query.reference);
    expect(params.get("as_of")).toBe("2026-10-06T08:00:10+08:00");
    expect(Date.parse(params.get("as_of")!)).toBe(Date.parse(query.as_of));
  }
  expect(
    new URL(reads[1].path, "http://offline").searchParams.get(
      "version_identity",
    ),
  ).toBe(version.identity);
  expect(api.effects).toEqual({ source: 0, raw: 0, jobs: 0 });
});
it("A2: another CPI variant cannot use a qualified version or bypass with a named exact link", async () => {
  const api = asgi("available");
  location.hash = new URLSearchParams({
    ui: "r5",
    view: "services",
    asset: CPI.identity,
    asset_revision: CPI.revision,
    operation: MANUAL_OPERATION,
    version: "p6-cpi-live:version",
    ...query,
  }).toString();
  render(<App transport={api.transport} initialQuery={query} />);
  await ready();
  expect(screen.queryByText("可本地读取")).toBeNull();
  expect(btn("读取本地数据").hasAttribute("disabled")).toBe(true);
  fireEvent.click(btn("读取本地数据"));
  expect(api.requests.filter((r) => r.path.includes("/read?"))).toHaveLength(0);
  expect(new URLSearchParams(location.hash.slice(1)).get("operation")).toBe(
    MANUAL_OPERATION,
  );
});
it("A2: binding revision and source mismatch fail closed; stale as-of replacement cannot show rows", async () => {
  const api = asgi("available");
  const directory: Directory = (await api.exchange({ path: "/api/r5/sources" }))
    .body;
  const catalog: Catalog = (
    await api.exchange({ path: `/api/r5/cpi?${new URLSearchParams(query)}` })
  ).body;
  const source = directory.entries.find(
    (e) =>
      e.binding_contract?.identity ===
      catalog.asset!.versions[0].binding_identity,
  )!;
  const wrongRevision = structuredClone(source);
  wrongRevision.binding_contract!.revision = "unqualified-revision";
  expect(catalogForOperation(catalog, wrongRevision)?.service).toBeNull();
  const wrongSource = structuredClone(source);
  wrongSource.binding_contract!.source.identity = "wrong-source";
  expect(catalogForOperation(catalog, wrongSource)).toBeNull();
  const transport: Transport = async (...args) => {
    const response = await api.transport(...args);
    if (!String(args[0]).includes("/read?")) return response;
    const result = await response.json();
    return {
      ...response,
      json: async () => ({
        ...result,
        version_identity: "different-operation-version",
      }),
    } as Response;
  };
  location.hash = new URLSearchParams({
    ui: "r5",
    view: "services",
    asset: CPI.identity,
    asset_revision: CPI.revision,
    operation: source.identity,
    ...query,
  }).toString();
  render(<App transport={transport} initialQuery={query} />);
  await ready();
  fireEvent.click(btn("读取本地数据"));
  await screen.findByRole("alert");
  expect(screen.queryByLabelText("读取结果")).toBeNull();
});
it("A2: conflicting PPI Operation and CPI asset never start a CPI catalog/read request", async () => {
  const api = asgi("available");
  const directory: Directory = (await api.exchange({ path: "/api/r5/sources" }))
    .body;
  const ppi = directory.entries.find((e) => e.asset?.identity.includes("ppi"))!;
  location.hash = new URLSearchParams({
    ui: "r5",
    view: "services",
    asset: CPI.identity,
    asset_revision: CPI.revision,
    operation: ppi.identity,
    ...query,
  }).toString();
  render(<App transport={api.transport} initialQuery={query} />);
  await ready();
  expect(
    api.requests.filter((r) => r.path.startsWith("/api/r5/cpi")),
  ).toHaveLength(0);
  expect(screen.queryByLabelText("本地读取")).toBeNull();
});
it("A: Macro → exact CPI variant → deduplicated asset → inherited capture, unconfigured blocks without POST and returns to same asset", async () => {
  const api = asgi();
  await journey(api.transport);
  await screen.findByText(/未绑定执行器或可信操作者/);
  fireEvent.change(screen.getByLabelText("开始参考月"), {
    target: { value: "2026-09" },
  });
  fireEvent.change(screen.getByLabelText("结束参考月"), {
    target: { value: "2026-09" },
  });
  fireEvent.click(screen.getByRole("checkbox"));
  expect(btn("创建采集任务").hasAttribute("disabled")).toBe(true);
  fireEvent.click(btn("创建采集任务"));
  expect(api.requests.every((r) => r.method === "GET")).toBe(true);
  fireEvent.click(btn("查看资产 →"));
  expect(
    screen.getByRole("heading", { name: "CPI · 四项核心指标" }),
  ).toBeTruthy();
  expect(location.hash).toContain(encodeURIComponent(MANUAL_OPERATION));
  expect(screen.queryByRole("region", { name: "数据资产" })).toBeNull();
  expect(screen.getByText(/代码合同参考；运行接入/)).toBeTruthy();
  expect(screen.queryByText("已有版本")).toBeNull();
  await ready();
  expect(screen.getByText(/当前条件下尚无可确认可读的本地数据/)).toBeTruthy();
  expect((btn("读取本地数据") as HTMLButtonElement).disabled).toBe(true);
});
it("B: trusted offline fixture explicitly accepts a durable Job, selects its execution and returns to same asset without running a worker", async () => {
  const api = asgi("bound", "r5_jobs_ui_exchange.py");
  await journey(api.transport);
  await waitFor(() =>
    expect(screen.getByText(/Operator：synthetic-operator/)).toBeTruthy(),
  );
  for (const name of ["开始参考月", "结束参考月"])
    fireEvent.change(screen.getByLabelText(name), {
      target: { value: "2026-09" },
    });
  fireEvent.click(screen.getByRole("checkbox"));
  await waitFor(() =>
    expect(btn("创建采集任务").hasAttribute("disabled")).toBe(false),
  );
  fireEvent.click(btn("创建采集任务"));
  fireEvent.click(btn("创建采集任务"));
  await screen.findByRole("heading", { name: "执行详情" });
  expect(api.requests.filter((r) => r.method === "POST")).toHaveLength(1);
  expect(api.effects).toEqual({ source: 0, raw: 0, jobs: 1 });
  const id = new URLSearchParams(location.hash.slice(1)).get("job");
  expect(id).toBe(
    api.requests.find((r) => r.method === "POST")?.body.job_identity,
  );
  expect(screen.getAllByText("待执行").length).toBeGreaterThan(0);
  fireEvent.click(btn("查看关联资产 →"));
  await ready();
  expect(
    screen.getByRole("heading", { name: "CPI · 四项核心指标" }),
  ).toBeTruthy();
  expect(location.hash).toContain(encodeURIComponent(MANUAL_OPERATION));
});
it("B: separately qualified offline asset opens only its eligible service and explicitly reads exact/as-of through actual ASGI", async () => {
  const api = asgi("available");
  location.hash = new URLSearchParams({
    page: "3",
    asset: CPI.identity,
    asset_revision: CPI.revision,
  }).toString();
  render(<App transport={api.transport} initialQuery={query} />);
  await ready();
  await ready();
  expect(
    screen.getAllByRole("heading", { name: "CPI · 四项核心指标" }),
  ).toHaveLength(1);
  expect(api.requests.some((r) => r.path.includes("/read?"))).toBe(false);
  fireEvent.click(btn("读取本地数据"));
  await screen.findByText(/读取成功/);
  fireEvent.change(screen.getByLabelText("读取方式"), {
    target: { value: "exact_version" },
  });
  fireEvent.click(btn("读取本地数据"));
  await screen.findByText(/读取成功/);
  expect(
    api.requests
      .filter((r) => r.path.includes("/read?"))
      .map((r) => new URL(r.path, "http://test").searchParams.get("mode")),
  ).toEqual(["as_of", "exact_version"]);
  expect(api.requests.every((r) => r.method === "GET")).toBe(true);
});
it.each(["PPI", "TDX"])(
  "C: %s keeps exact asset context and never issues CPI read/job or switches target",
  async (name) => {
    const api = asgi();
    location.hash = "ui=r5&view=interfaces";
    render(<App transport={api.transport} />);
    await ready();
    select(
      "注册接口",
      name === "PPI" ? /选择 PPI / : /选择 TDX .*具名日线合同/,
    );
    fireEvent.click(btn("查看关联资产 →"));
    await ready();
    const identity = new URLSearchParams(location.hash.slice(1)).get("asset");
    fireEvent.click(btn("采集任务 →"));
    await ready();
    expect(
      (screen.getByLabelText("采集目标") as HTMLSelectElement).value,
    ).toContain(identity!);
    expect(btn("创建采集任务").hasAttribute("disabled")).toBe(true);
    fireEvent.click(btn("查看资产 →"));
    await ready();
    expect(screen.getByText(/尚无通用 Web Reader/)).toBeTruthy();
    expect(new URLSearchParams(location.hash.slice(1)).get("asset")).toBe(
      identity,
    );
    expect(api.requests.some((r) => /cpi\/(read|jobs)/.test(r.path))).toBe(
      false,
    );
  },
);
it("C: B043 keeps a known definition with no service; B044 has unavailable mapping and clears previous asset", async () => {
  const api = asgi();
  render(<App transport={api.transport} />);
  nav("数据源管理");
  await ready();
  select("注册接口", /选择 KD · 板块当前排名/);
  fireEvent.click(btn("查看关联资产 →"));
  await ready();
  expect(
    screen.getByRole("heading", { name: "KD · 板块当前排名" }),
  ).toBeTruthy();
  await ready();
  expect(screen.getByText(/尚无通用 Web Reader/)).toBeTruthy();
  nav("数据源管理");
  select("注册接口", /选择 market-stock-ranking.current_a_share/);
  fireEvent.click(btn("查看关联资产 →"));
  await ready();
  expect(screen.getByText(/该接口无已准入 Definition/)).toBeTruthy();
  expect(new URLSearchParams(location.hash.slice(1)).has("asset")).toBe(false);
  expect(
    screen.queryByRole("heading", { name: "KD · 板块当前排名" }),
  ).toBeNull();
});
it("D: projection keeps components/interfaces/assets/materials/services separate and source-qualified is not activation", async () => {
  const api = asgi();
  const d = (await api.exchange({ path: "/api/r5/sources" })).body as Directory;
  const c = (
    await api.exchange({ path: `/api/r5/cpi?${new URLSearchParams(query)}` })
  ).body as Catalog;
  const model = project(d, c, null);
  expect(model.components.map((c) => c.id)).toEqual(["Macro", "TDX", "KD"]);
  expect(model.interfaces).toHaveLength(17);
  expect(
    model.assets.filter((a) => a.key.identity === CPI.identity),
  ).toHaveLength(1);
  expect(
    model.assets.find((a) => a.key.identity === CPI.identity)?.interfaces,
  ).toHaveLength(4);
  expect(model.services).toEqual([]);
  expect(model.instances).toEqual([]);
  expect(model.assets.every((a) => a.holding === "持有状态待确认")).toBe(true);
});
it("E: removed calendar creation cannot convert monthly/yearly rules into interval writes", async () => {
  const api = asgi("bound", "r5_jobs_ui_exchange.py");
  await journey(api.transport);
  expect(screen.queryByRole("button", { name: "周期采集" })).toBeNull();
  expect(screen.queryByLabelText("执行周期")).toBeNull();
  fireEvent.click(screen.getByRole("tab", { name: "周期任务" }));
  await screen.findByRole("region", { name: "周期任务" });
  expect(screen.queryByRole("button", { name: "保存周期任务" })).toBeNull();
  expect(screen.queryByRole("button", { name: /新建周期采集/ })).toBeNull();
  expect(api.requests.every((r) => r.method === "GET")).toBe(true);
});
it.each([
  "asset=missing&asset_revision=p3-r1",
  `asset=${CPI.identity}&asset_revision=bad`,
  `asset=${CPI.identity}&asset_revision=p3-r1&operation=missing`,
  "operation=missing",
])(
  "E: invalid identity is explicit without falling back: %s",
  async (fields) => {
    const api = asgi("available");
    location.hash = `ui=r5&view=services&${fields}`;
    render(<App transport={api.transport} initialQuery={query} />);
    await screen.findByText(/关联对象缺失或身份冲突/);
    expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
    expect(api.requests.some((r) => /cpi\/(read|jobs)/.test(r.path))).toBe(
      false,
    );
  },
);
it.each([
  "page=3&asset=a&asset=b",
  "ui=r5&view=services&page=5",
  "page=5&as_of=2026-10-06",
  "page=5&reference=2026-13",
  "page=99",
  "ui=r5&view=missing",
  "page=5&operation=has%20spaces",
  "ui=r5&view=assets&asset_revision=a&asset_revision=b",
  "ui=unknown&view=collection",
  "view=platform",
  "ui=r5",
  "ui=r5&ui=r5&view=overview",
  "ui=r5&view=assets&view=collection",
  "page=4&page=5",
  "page=-1",
  "page=9",
  "page=",
  "page=4&job=a&job=b",
  "page=6&tab=storage&tab=diagnostics",
  "ui=r5&view=services&asset_revision=%3Cscript%3E",
  "page=5&version=" + "x".repeat(257),
  "ui=r5&view=collection&tab=unsupported",
])(
  "E: syntactically invalid/conflicting route makes zero requests: %s",
  async (route) => {
    const api = asgi();
    location.hash = route;
    render(<App transport={api.transport} />);
    expect(screen.getByRole("alert").textContent).toContain(
      "路由或关联参数无效",
    );
    expect(api.requests).toEqual([]);
  },
);
it("F: no registered runtime records retains three platform tabs, expected runtime labels and searchable storage detail", async () => {
  const api = asgi();
  location.hash = "page=7";
  render(<App transport={api.transport} />);
  await ready();
  expect(screen.getByText(/实例读取范围未配置/)).toBeTruthy();
  expect(screen.getByText(/MDF API · TDX Driver · KD/)).toBeTruthy();
  expect(screen.queryByRole("group", { name: "Macro 九族" })).toBeNull();
  expect(screen.getAllByRole("tab")).toHaveLength(3);
  fireEvent.click(screen.getByRole("tab", { name: "数据库与存储" }));
  select("数据库与存储", /选择 MDF Garage/);
  expect(screen.getByRole("heading", { name: "MDF Garage" })).toBeTruthy();
  expect(screen.getByText(/没有连接测试或数据库初始化/)).toBeTruthy();
  fireEvent.click(screen.getByRole("tab", { name: "日志与诊断" }));
  expect(screen.getByText(/没有受信诊断记录/)).toBeTruthy();
  expect(api.requests.every((r) => r.method === "GET")).toBe(true);
});
it.each([0, 1, 2, 3, 4, 5, 6, 7, 8])(
  "G: legacy page=%s normalizes into five entries and never posts, probes or auto-reads",
  async (page) => {
    const api = asgi();
    location.hash = `page=${page}`;
    render(<App transport={api.transport} />);
    await ready();
    expect(
      within(screen.getByRole("navigation")).getAllByRole("button"),
    ).toHaveLength(5);
    expect(api.requests.every((r) => r.method === "GET")).toBe(true);
    expect(
      api.requests.some((r) => /\/read\?|provider|probe|tdx/.test(r.path)),
    ).toBe(false);
  },
);
it("G: actual unconfigured ASGI read statuses are preserved and are not preview-generated 404", async () => {
  const api = asgi();
  for (const path of [
    "/api/r5/sources",
    "/api/r3/management",
    "/api/r5/observability",
    `/api/r5/cpi?${new URLSearchParams(query)}`,
    "/api/r5/cpi/jobs",
    "/api/r5/cpi/plans",
  ])
    expect((await api.exchange({ path })).status).toBe(200);
  expect(
    (
      await api.exchange({
        path: `/api/r5/cpi/read?${new URLSearchParams({ ...query, mode: "as_of" })}`,
      })
    ).status,
  ).toBe(503);
});
it("H: desktop keeps table left/detail right at 960/1440, mobile stacks at 390/320 (CSS/DOM only, NOT Chrome evidence)", () => {
  const css = readFileSync(resolve("src/style.css"), "utf8");
  expect(css).toContain("grid-template-columns: 160px minmax(0, 1fr)");
  expect(css).toContain("grid-template-columns: 214px minmax(0, 1fr)");
  expect(css).toContain("@media (max-width: 760px)");
  expect(css).toContain("@media (max-width: 480px)");
  expect(css).toContain("outline: 2px solid var(--accent)");
  expect(css).not.toContain("overflow-x: hidden");
});
it.each([320, 390, 960, 1440])(
  "H: focus, search reset, collapse, theme and exact context at %s (jsdom DOM only)",
  async (width) => {
    Object.defineProperty(window, "innerWidth", {
      value: width,
      configurable: true,
    });
    const api = asgi();
    render(<App transport={api.transport} />);
    expect(screen.getByRole("heading", { name: "运行总览" })).toBeTruthy();
    expect(document.querySelector(".shell.dark")).toBeNull();
    for (const b of within(screen.getByRole("navigation")).getAllByRole(
      "button",
    )) {
      b.focus();
      expect(document.activeElement).toBe(b);
    }
    nav("数据源管理");
    await ready();
    const search = screen.getByLabelText("搜索注册接口");
    fireEvent.change(search, { target: { value: "Macro" } });
    expect(
      within(screen.getByRole("region", { name: "注册接口" })).queryByText(
        "TDX / TDX",
      ),
    ).toBeNull();
    fireEvent.click(
      within(screen.getByRole("region", { name: "注册接口" })).getByRole(
        "button",
        { name: "清除" },
      ),
    );
    expect(document.activeElement).toBe(search);
    expect(
      within(screen.getByRole("region", { name: "注册接口" })).getAllByText(
        "TDX / TDX",
      ).length,
    ).toBeGreaterThan(0);
    fireEvent.click(btn("切换主题"));
    expect(document.querySelector(".shell.dark")).toBeTruthy();
    fireEvent.click(btn("收起侧栏"));
    expect(document.querySelector(".shell.collapsed")).toBeTruthy();
  },
);
it.each([
  ["over_budget", query.reference, "p6-cpi-live:version"],
  ["history_over_budget", "2025-09", "r5-cpi-history-budget-128-a1:version"],
])(
  "preserves named exact Reader beyond bounded catalog budget: %s",
  async (scenario, reference, version) => {
    const api = asgi(scenario);
    location.hash = new URLSearchParams({
      page: "5",
      asset: CPI.identity,
      asset_revision: CPI.revision,
      ...query,
      reference,
      version,
    }).toString();
    render(<App transport={api.transport} />);
    await ready();
    expect(screen.getByText(/已有合同与本地版本入口/)).toBeTruthy();
    expect(api.requests.some((r) => r.path.includes("/read?"))).toBe(false);
    expect(btn("读取本地数据").hasAttribute("disabled")).toBe(false);
    fireEvent.click(btn("读取本地数据"));
    await screen.findByText(/读取成功/, {}, { timeout: 10000 });
    fireEvent.change(screen.getByLabelText("读取方式"), {
      target: { value: "as_of" },
    });
    expect(btn("读取本地数据").hasAttribute("disabled")).toBe(true);
    fireEvent.change(screen.getByLabelText("读取方式"), {
      target: { value: "exact_version" },
    });
    fireEvent.change(screen.getByLabelText("保存版本"), {
      target: { value: version.slice(0, -1) },
    });
    expect(btn("读取本地数据").hasAttribute("disabled")).toBe(true);
  },
);
it("preserves a nonexistent named exact version for actual Reader denial without publishing a service", async () => {
  const api = asgi("over_budget");
  location.hash = new URLSearchParams({
    page: "5",
    asset: CPI.identity,
    asset_revision: CPI.revision,
    ...query,
    version: "absent-version",
  }).toString();
  render(<App transport={api.transport} />);
  await ready();
  fireEvent.click(btn("读取本地数据"));
  await screen.findByText("version_not_found");
  expect(screen.queryByText(/读取成功/)).toBeNull();
  expect(screen.getByText(/已有合同与本地版本入口/)).toBeTruthy();
});
it.each([
  "unconfigured",
  "empty",
  "missing_content",
  "corrupt_content",
  "withdrawn",
  "ambiguous",
  "partial",
  "over_budget",
  "content_storage_fault",
  "scope_exit_fault",
])("does not fabricate a service or auto-read: %s", async (scenario) => {
  const api = asgi(scenario);
  location.hash = new URLSearchParams({
    page: "5",
    asset: CPI.identity,
    asset_revision: CPI.revision,
  }).toString();
  render(<App transport={api.transport} initialQuery={query} />);
  await ready();
  expect(screen.getByText(/已有合同与本地版本入口/)).toBeTruthy();
  expect(screen.queryByText("可本地读取")).toBeNull();
  const read = screen.queryByRole("button", { name: "读取本地数据" });
  if (read) expect(read.hasAttribute("disabled")).toBe(true);
  expect(api.requests.some((r) => r.path.includes("/read?"))).toBe(false);
  expect(screen.queryByText(/private-sensitive/)).toBeNull();
});
it.each(["content_storage_fault", "scope_exit_fault"])(
  "clears old holdings, publication and read result after failing refresh: %s",
  async (scenario) => {
    const good = asgi("available"),
      bad = asgi(scenario);
    let fault = false;
    const transport: Transport = (...args) =>
      (fault ? bad : good).transport(...args);
    location.hash = new URLSearchParams({
      page: "5",
      asset: CPI.identity,
      asset_revision: CPI.revision,
    }).toString();
    render(<App transport={transport} initialQuery={query} />);
    await ready();
    fireEvent.click(btn("读取本地数据"));
    await screen.findByText(/读取成功/);
    fault = true;
    fireEvent.click(btn("刷新"));
    await ready();
    expect(screen.queryByText(/读取成功/)).toBeNull();
    expect(screen.queryByText("p6-cpi-live:version")).toBeNull();
    expect((btn("读取本地数据") as HTMLButtonElement).disabled).toBe(true);
  },
);
it("applying reference conditions clears old result; exact misses never fetch Source", async () => {
  const api = asgi("available");
  location.hash = new URLSearchParams({
    page: "5",
    asset: CPI.identity,
    asset_revision: CPI.revision,
    ...query,
    version: "absent",
  }).toString();
  render(<App transport={api.transport} />);
  await ready();
  fireEvent.click(btn("读取本地数据"));
  await screen.findByText("version_not_found");
  fireEvent.change(screen.getByLabelText("参考月"), {
    target: { value: "2026-09" },
  });
  expect(screen.queryByText("version_not_found")).toBeNull();
  fireEvent.click(btn("读取本地数据"));
  await screen.findByText("version_not_found");
  expect(new URLSearchParams(location.hash.slice(1)).get("reference")).toBe(
    "2026-09",
  );
  expect(api.requests.every((r) => r.method === "GET")).toBe(true);
});
it("discards a delayed read after refresh, query change, navigation and unmount", async () => {
  const api = asgi("available");
  let release: (() => void) | undefined;
  const transport: Transport = async (...args) => {
    const r = await api.transport(...args);
    if (String(args[0]).includes("/read?"))
      await new Promise<void>((r) => {
        release = r;
      });
    return r;
  };
  location.hash = new URLSearchParams({
    page: "5",
    asset: CPI.identity,
    asset_revision: CPI.revision,
  }).toString();
  const view = render(<App transport={transport} initialQuery={query} />);
  await ready();
  fireEvent.click(btn("读取本地数据"));
  await waitFor(() => expect(release).toBeTypeOf("function"));
  fireEvent.click(btn("刷新"));
  await ready();
  await act(async () => release!());
  expect(screen.queryByText(/读取成功/)).toBeNull();
  fireEvent.click(btn("读取本地数据"));
  await waitFor(() =>
    expect(api.requests.filter((r) => r.path.includes("/read?"))).toHaveLength(
      2,
    ),
  );
  nav("数据源管理");
  await act(async () => release!());
  expect(screen.queryByText(/读取成功/)).toBeNull();
  view.unmount();
});
it("directory refresh failure clears stale selection and hides private error messages", async () => {
  const api = asgi();
  let fault = false;
  const transport: Transport = (...args) =>
    fault
      ? Promise.reject(new Error("private-sensitive"))
      : api.transport(...args);
  location.hash = "ui=r5&view=interfaces";
  render(<App transport={transport} />);
  await ready();
  select("注册接口", /选择 CPI .*2026-09 公布窗口/);
  fault = true;
  fireEvent.click(btn("刷新"));
  await ready();
  expect(screen.getByText("目录暂不可读取。")).toBeTruthy();
  expect(
    screen.queryByRole("heading", { name: "CPI · 四项核心指标" }),
  ).toBeNull();
  expect(screen.queryByText(/private-sensitive/)).toBeNull();
});
it("E: actual offline UNKNOWN execution retains original identity and blocks blind retry/backfill without POST", async () => {
  const api = asgi("workflow_unknown");
  location.hash = new URLSearchParams({
    page: "4",
    tab: "executions",
    asset: CPI.identity,
    asset_revision: CPI.revision,
    operation: MANUAL_OPERATION,
    job: "first",
  }).toString();
  render(<App transport={api.transport} initialQuery={captureQuery} />);
  await screen.findByRole("heading", { name: "执行详情" });
  expect(screen.getAllByText("结果未知").length).toBeGreaterThan(0);
  expect(screen.getByText(/原执行效果未知/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "精确读取结果 →" })).toBeNull();
  expect(screen.queryByRole("button", { name: "指定范围回补 →" })).toBeNull();
  expect(new URLSearchParams(location.hash.slice(1)).get("job")).toBe("first");
  expect(api.requests.every((r) => r.method === "GET")).toBe(true);
  await hash({ page: "0", tab: "diagnostics", job: "first" });
  const original = await screen.findByRole("button", {
    name: "查看执行 first →",
  });
  expect(
    api.requests.some((r) =>
      r.path.includes("/api/r5/cpi/jobs?job_identity=first"),
    ),
  ).toBe(true);
  fireEvent.click(original);
  await screen.findByRole("heading", { name: "执行详情" });
  expect(new URLSearchParams(location.hash.slice(1)).get("job")).toBe("first");
});
it("E: real fixture plan → child execution → same plan/asset and accepted failed-range backfill preserve lineage", async () => {
  const api = asgi("", "r5_schedules_ui_exchange.py");
  const target = (await api.exchange({ path: "/api/r5/cpi/jobs" })).body;
  const accepted = await api.exchange({
    path: "/api/r5/cpi/plans",
    method: "POST",
    body: {
      plan_identity: "workflow-plan",
      name: "workflow-plan",
      asset_identity: target.asset_identity,
      asset_revision: target.asset_revision,
      start: "2024-12",
      end: "2024-12",
      timezone: "UTC",
      window_start: "00:00",
      window_end: "23:59",
      first_due: "2028-03-10T12:00:00+00:00",
      interval_seconds: 86400,
      cycles: 1,
    },
  });
  expect(accepted.status).toBe(202);
  await api.exchange({ control: "tick", plan: "workflow-plan", failure: true });
  const before = api.effects.source;
  location.hash = new URLSearchParams({
    page: "4",
    tab: "periodic",
    asset: CPI.identity,
    asset_revision: CPI.revision,
    operation:
      "mdf.source-candidate.bls-v2.cpi.history-index@r5-cpi-history-index-v1",
    schedule: "workflow-plan",
  }).toString();
  render(<App transport={api.transport} />);
  await screen.findByRole("heading", { name: "workflow-plan" });
  fireEvent.click(btn("查看执行记录 →"));
  const row = await within(
    screen.getByRole("region", { name: "执行记录" }),
  ).findByRole("button", { name: /选择 .*失败/ });
  fireEvent.click(row);
  expect(screen.getByText("workflow-plan")).toBeTruthy();
  fireEvent.click(btn("查看周期任务 →"));
  expect(new URLSearchParams(location.hash.slice(1)).get("schedule")).toBe(
    "workflow-plan",
  );
  fireEvent.click(btn("查看执行记录 →"));
  fireEvent.click(
    await within(screen.getByRole("region", { name: "执行记录" })).findByRole(
      "button",
      { name: /选择 .*失败/ },
    ),
  );
  fireEvent.click(btn("指定范围回补 →"));
  for (const name of ["开始参考月", "结束参考月"])
    fireEvent.change(screen.getByLabelText(name), {
      target: { value: "2024-12" },
    });
  fireEvent.click(screen.getByRole("checkbox"));
  await waitFor(() =>
    expect(btn("创建采集任务").hasAttribute("disabled")).toBe(false),
  );
  fireEvent.click(btn("创建采集任务"));
  await screen.findByText(/意图已持久接收/);
  expect(
    screen.getByRole("tab", { name: "执行记录" }).getAttribute("aria-selected"),
  ).toBe("true");
  expect(api.effects.source).toBe(before);
  expect(api.requests.filter((r) => r.method === "POST")).toHaveLength(1);
  expect(api.requests.find((r) => r.method === "POST")?.path).toBe(
    "/api/r5/cpi/plans/workflow-plan/backfill",
  );
});
it.each<Record<string, string>>([
  { asset: "missing" },
  { asset_revision: "wrong-revision" },
  { asset_revision: "" },
  { operation: "missing" },
  { operation: "market_data_foundation.domain_contracts.kd:KD_B043" },
])(
  "preserves exact-link identity gates beyond catalog budget: %j",
  async (changes) => {
    const api = asgi("over_budget");
    location.hash = new URLSearchParams({
      page: "5",
      asset: CPI.identity,
      asset_revision: CPI.revision,
      ...query,
      version: "p6-cpi-live:version",
      ...changes,
    }).toString();
    render(<App transport={api.transport} />);
    if (changes.asset_revision === "") {
      expect(screen.getByRole("alert").textContent).toContain("无法定位");
      expect(api.requests).toEqual([]);
    } else await ready();
    expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
    expect(api.requests.some((r) => r.path.includes("/read?"))).toBe(false);
  },
);
it("preserves exact read identity/version/query through canonical navigation, refresh and reload", async () => {
  const api = asgi("available");
  const fields = {
    page: "5",
    asset: CPI.identity,
    asset_revision: CPI.revision,
    ...query,
    version: "p6-cpi-live:version",
  };
  location.hash = new URLSearchParams(fields).toString();
  const view = render(<App transport={api.transport} />);
  await ready();
  nav("数据资产");
  await ready();
  nav("数据资产");
  await ready();
  expect(new URLSearchParams(location.hash.slice(1)).get("version")).toBe(
    fields.version,
  );
  fireEvent.click(btn("刷新"));
  await ready();
  fireEvent.click(btn("读取本地数据"));
  await screen.findByText(/读取成功/);
  expect(
    new URL(
      api.requests.filter((r) => r.path.includes("/read?")).at(-1)!.path,
      "http://test",
    ).searchParams.get("version_identity"),
  ).toBe(fields.version);
  view.unmount();
  render(<App transport={api.transport} />);
  await ready();
  expect((screen.getByLabelText("保存版本") as HTMLInputElement).value).toBe(
    fields.version,
  );
});
it("source directory remains cached across navigation and changing interface clears conflicting asset/version", async () => {
  const api = asgi();
  render(<App transport={api.transport} />);
  nav("数据源管理");
  await ready();
  select("注册接口", /选择 CPI .*2026-09 公布窗口/);
  fireEvent.click(btn("查看关联资产 →"));
  await ready();
  nav("数据源管理");
  select("注册接口", /选择 PPI /);
  const fields = new URLSearchParams(location.hash.slice(1));
  expect(fields.has("asset")).toBe(false);
  expect(fields.has("version")).toBe(false);
  fireEvent.click(btn("查看关联资产 →"));
  await ready();
  expect(screen.getByRole("heading", { name: "PPI · 最终需求" })).toBeTruthy();
  expect(
    api.requests.filter((r) => r.path.startsWith("/api/r5/sources")),
  ).toHaveLength(1);
});
it("F: actual Management instance records show runtime type, ownership and local expiry without a probe or family rows", async () => {
  const api = asgi("fresh", "r3_ui_exchange.py");
  location.hash = "page=7";
  render(<App transport={api.transport} />);
  const row = await within(
    screen.getByRole("region", { name: "运行实例" }),
  ).findByRole("button", { name: /选择 fixture.r3.owner.instance/ });
  fireEvent.click(row);
  expect(screen.getByRole("heading", { name: "KD 外部实例" })).toBeTruthy();
  expect(screen.getByText("Owner 独立管理；仅观察")).toBeTruthy();
  const timestamp = screen.getByText(
    "2026-10-24 07:59:50 北京时间",
  ).textContent;
  const count = api.requests.length;
  vi.spyOn(performance, "now").mockReturnValue(performance.now() + 61000);
  await waitFor(
    () =>
      expect(screen.getAllByText("观测待确认 / 已过期").length).toBeGreaterThan(
        0,
      ),
    { timeout: 2500 },
  );
  expect(screen.getByText(timestamp!)).toBeTruthy();
  expect(api.requests).toHaveLength(count);
  expect(
    screen.queryByRole("button", { name: /控制实例|启动|停止/ }),
  ).toBeNull();
});
it("back/forward between exact links updates the named version in-place without stale read results or automatic requests", async () => {
  const api = asgi("available");
  const fields = {
    page: "5",
    asset: CPI.identity,
    asset_revision: CPI.revision,
    ...query,
    version: "p6-cpi-live:version",
  };
  location.hash = new URLSearchParams(fields).toString();
  render(<App transport={api.transport} />);
  await ready();
  fireEvent.click(btn("读取本地数据"));
  await screen.findByText(/读取成功/);
  const count = api.requests.length;
  await hash({ ...fields, version: "absent-version" });
  expect((screen.getByLabelText("保存版本") as HTMLInputElement).value).toBe(
    "absent-version",
  );
  expect(screen.queryByText(/读取成功/)).toBeNull();
  expect(api.requests).toHaveLength(count);
  fireEvent.click(btn("读取本地数据"));
  await screen.findByText("version_not_found");
  await hash(fields);
  expect((screen.getByLabelText("保存版本") as HTMLInputElement).value).toBe(
    fields.version,
  );
});
it("F: instance type remains grounded in its own Management record when the separate interface catalog request fails", async () => {
  const api = asgi("fresh", "r3_ui_exchange.py");
  const transport: Transport = (input, init) =>
    String(input).startsWith("/api/r5/sources")
      ? Promise.reject(new Error("private-network-detail"))
      : api.transport(input, init);
  location.hash = "page=7";
  render(<App transport={transport} />);
  const row = await within(
    screen.getByRole("region", { name: "运行实例" }),
  ).findByRole("button", { name: /选择 fixture.r3.owner.instance/ });
  fireEvent.click(row);
  expect(screen.getByRole("heading", { name: "KD 外部实例" })).toBeTruthy();
  expect(screen.getByText("目录暂不可读取。")).toBeTruthy();
  expect(screen.queryByText("Macro 接入运行实例")).toBeNull();
  expect(screen.queryByText(/private-network-detail/)).toBeNull();
});
it("E: a failed source-directory request cannot bypass an explicit source route gate even when local CPI service evidence is available", async () => {
  const api = asgi("available");
  const transport: Transport = (input, init) =>
    String(input).startsWith("/api/r5/sources")
      ? Promise.reject(new Error("source-catalog-unreachable"))
      : api.transport(input, init);
  location.hash = new URLSearchParams({
    page: "5",
    asset: CPI.identity,
    asset_revision: CPI.revision,
    operation: MANUAL_OPERATION,
    ...query,
  }).toString();
  render(<App transport={transport} />);
  await ready();
  expect(screen.getByText("目录暂不可读取。")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
  expect(api.requests.some((r) => r.path.includes("/read?"))).toBe(false);
});

it.each(["reference", "as_of"])(
  "F1: rejects a %s-only deep link on initial load, same-tab change, history traversal and reload without catalog/read requests",
  async (field) => {
    const api = asgi("available");
    const complete = {
      ui: "r5",
      view: "services",
      asset: CPI.identity,
      asset_revision: CPI.revision,
      ...query,
      version: "p6-cpi-live:version",
    };
    const partial = {
      ...complete,
      [field]: captureQuery[field as keyof typeof captureQuery],
    };
    delete (partial as Record<string, string>)[
      field === "reference" ? "as_of" : "reference"
    ];
    history.replaceState(null, "", `#${new URLSearchParams(partial)}`);
    let view = render(<App transport={api.transport} initialQuery={query} />);
    expect(screen.getByRole("alert").textContent).toContain("无法定位");
    expect(api.requests).toEqual([]);
    view.unmount();
    history.replaceState(null, "", `#${new URLSearchParams(complete)}`);
    view = render(<App transport={api.transport} />);
    await ready();
    fireEvent.click(btn("读取本地数据"));
    await screen.findByText(/读取成功/);
    const count = api.requests.length;
    await hash(partial);
    expect(screen.getByRole("alert").textContent).toContain("无法定位");
    expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
    expect(api.requests).toHaveLength(count);
    await act(async () => {
      history.back();
    });
    await waitFor(() =>
      expect(new URLSearchParams(location.hash.slice(1)).get("reference")).toBe(
        query.reference,
      ),
    );
    await ready();
    expect((screen.getByLabelText("参考月") as HTMLInputElement).value).toBe(
      query.reference,
    );
    expect(api.requests.filter((r) => r.path.includes("/read?"))).toHaveLength(
      1,
    );
    const beforeForward = api.requests.length;
    await act(async () => {
      history.forward();
    });
    await screen.findByText(/无法定位此链接/);
    expect(api.requests).toHaveLength(beforeForward);
    view.unmount();
    render(<App transport={api.transport} initialQuery={captureQuery} />);
    expect(screen.getByRole("alert").textContent).toContain("无法定位");
    expect(api.requests).toHaveLength(beforeForward);
  },
);
it("F1: complete changed query emits exact link params through navigation, refresh, history and reload; nonexistent version is denied", async () => {
  const api = asgi("available");
  const base = {
    ui: "r5",
    view: "services",
    asset: CPI.identity,
    asset_revision: CPI.revision,
    ...query,
    version: "p6-cpi-live:version",
  };
  history.replaceState(null, "", `#${new URLSearchParams(base)}`);
  let view = render(<App transport={api.transport} />);
  await ready();
  fireEvent.click(btn("读取本地数据"));
  await screen.findByText(/读取成功/);
  const changed = { ...base, ...captureQuery, version: "absent-version" };
  await hash(changed);
  await ready();
  async function readChanged() {
    const count = api.requests.filter((r) => r.path.includes("/read?")).length;
    expect((screen.getByLabelText("参考月") as HTMLInputElement).value).toBe(
      changed.reference,
    );
    expect(
      (
        screen.getByLabelText(
          "数据可用截至时间（北京时间）",
        ) as HTMLInputElement
      ).value,
    ).toBe("2026-10-14T21:00:00");
    fireEvent.click(btn("读取本地数据"));
    await screen.findByText("version_not_found");
    const reads = api.requests.filter((r) => r.path.includes("/read?"));
    expect(reads).toHaveLength(count + 1);
    expect(
      Object.fromEntries(
        new URL(reads.at(-1)!.path, "http://test").searchParams,
      ),
    ).toEqual({
      ...captureQuery,
      as_of: "2026-10-14T21:00:00+08:00",
      mode: "exact_version",
      version_identity: "absent-version",
    });
  }
  expect(api.requests.filter((r) => r.path.includes("/read?"))).toHaveLength(1);
  await readChanged();
  await act(async () => {
    history.back();
  });
  await waitFor(() =>
    expect(new URLSearchParams(location.hash.slice(1)).get("reference")).toBe(
      query.reference,
    ),
  );
  await ready();
  await act(async () => {
    history.forward();
  });
  await waitFor(() =>
    expect(new URLSearchParams(location.hash.slice(1)).get("reference")).toBe(
      captureQuery.reference,
    ),
  );
  await ready();
  expect(api.requests.filter((r) => r.path.includes("/read?"))).toHaveLength(2);
  await readChanged();
  nav("数据资产");
  await ready();
  nav("数据资产");
  await ready();
  fireEvent.click(btn("刷新"));
  await ready();
  expect(api.requests.filter((r) => r.path.includes("/read?"))).toHaveLength(3);
  await readChanged();
  view.unmount();
  view = render(<App transport={api.transport} initialQuery={query} />);
  await ready();
  expect(api.requests.filter((r) => r.path.includes("/read?"))).toHaveLength(4);
  await readChanged();
  expect(api.requests.every((r) => r.method === "GET")).toBe(true);
});
it.each(["periodic", "executions"])(
  "F2: direct %s entry discovers actual ASGI plan/job and links its DTO asset without render POST",
  async (tab) => {
    const api = asgi("", "r5_schedules_ui_exchange.py");
    const target = (await api.exchange({ path: "/api/r5/cpi/jobs" })).body;
    expect(
      (
        await api.exchange({
          path: "/api/r5/cpi/plans",
          method: "POST",
          body: {
            plan_identity: "f2-plan",
            name: "f2-plan",
            asset_identity: target.asset_identity,
            asset_revision: target.asset_revision,
            start: "2024-12",
            end: "2024-12",
            timezone: "UTC",
            window_start: "00:00",
            window_end: "23:59",
            first_due: "2028-03-10T12:00:00+00:00",
            interval_seconds: 86400,
            cycles: 1,
          },
        })
      ).status,
    ).toBe(202);
    // Existing offline fixture control only; creates the record before UI rendering.
    await api.exchange({ control: "tick", plan: "f2-plan", failure: true });
    const effects = { ...api.effects };
    location.hash = `ui=r5&view=collection&tab=${tab}`;
    render(<App transport={api.transport} initialQuery={query} />);
    const region = tab === "periodic" ? "周期任务" : "执行记录";
    const row = await within(
      screen.getByRole("region", { name: region }),
    ).findByRole("button", {
      name: tab === "periodic" ? /选择 f2-plan/ : /选择 .*失败/,
    });
    if (tab === "executions")
      await waitFor(() => expect(row.textContent).toBe("CPI · 自动阶段"));
    else expect(row.closest("tr")!.textContent).toContain("CPI · 四项核心指标");
    expect(new URLSearchParams(location.hash.slice(1)).has("asset")).toBe(
      false,
    );
    fireEvent.click(row);
    await screen.findByRole("heading", {
      name: tab === "periodic" ? "f2-plan" : "执行详情",
    });
    expect(new URLSearchParams(location.hash.slice(1)).get("asset")).toBe(
      tab === "periodic" ? target.asset_identity : null,
    );
    expect(
      new URLSearchParams(location.hash.slice(1)).get("asset_revision"),
    ).toBe(tab === "periodic" ? target.asset_revision : null);
    if (tab === "executions")
      expect(new URLSearchParams(location.hash.slice(1)).has("operation")).toBe(
        false,
      );
    if (tab === "periodic") {
      fireEvent.click(btn("查看执行记录 →"));
      fireEvent.click(
        await within(
          screen.getByRole("region", { name: "执行记录" }),
        ).findByRole("button", { name: /选择 .*失败/ }),
      );
      await screen.findByRole("heading", { name: "执行详情" });
    }
    expect(await screen.findByText("f2-plan")).toBeTruthy();
    fireEvent.click(btn("查看关联资产 →"));
    await screen.findByRole("heading", { name: "CPI · 四项核心指标" });
    expect(new URLSearchParams(location.hash.slice(1)).get("asset")).toBe(
      target.asset_identity,
    );
    expect(
      new URLSearchParams(location.hash.slice(1)).get("asset_revision"),
    ).toBe(target.asset_revision);
    expect(
      api.requests.some((r) => r.path.startsWith("/api/r5/cpi/jobs")),
    ).toBe(true);
    expect(
      api.requests.some((r) => r.path.startsWith("/api/r5/cpi/plans")),
    ).toBe(true);
    expect(api.requests.every((r) => r.method === "GET")).toBe(true);
    expect(api.requests.some((r) => r.path.includes("/read?"))).toBe(false);
    expect(api.effects).toEqual(effects);
  },
);
it.each(["periodic", "executions"])(
  "F2: direct %s distinguishes qualified empty, unconfigured, UNKNOWN and transport failure",
  async (tab) => {
    for (const state of ["empty", "unconfigured", "unknown", "failure"]) {
      const api =
        state === "empty" ? asgi("", "r5_schedules_ui_exchange.py") : asgi();
      const transport: Transport = async (input, init) => {
        if (/cpi\/(jobs|plans)/.test(String(input))) {
          if (state === "failure") throw new Error("private-transport-detail");
          if (state === "unknown")
            return {
              ok: true,
              json: async () => ({
                status: "UNKNOWN",
                evidence_mode: "OFFLINE_FIXTURE",
                classification: "job_query_budget_exceeded",
                jobs: [],
                plans: [],
              }),
            } as Response;
        }
        return api.transport(input, init);
      };
      location.hash = `ui=r5&view=collection&tab=${tab}`;
      const view = render(<App transport={transport} />);
      await screen.findByText(
        state === "empty"
          ? tab === "periodic"
            ? "当前已查询范围暂无周期任务。"
            : "当前已查询范围没有执行记录。"
          : state === "unconfigured"
            ? "读取范围未配置，记录是否存在尚未核实。"
            : state === "unknown"
              ? "读取范围或记录状态尚未核实。"
              : "采集记录读取失败，记录是否存在尚未核实。",
      );
      expect(screen.queryByText(/private-transport-detail/)).toBeNull();
      expect(api.requests.every((r) => r.method === "GET")).toBe(true);
      if (state !== "empty")
        expect(
          screen.queryByText(/当前已查询范围暂无|当前已查询范围没有/),
        ).toBeNull();
      view.unmount();
    }
  },
);
it.each(["PPI", "TDX"])(
  "F2: direct record tabs with %s scope never query CPI jobs, plans or Reader",
  async (family) => {
    const api = asgi();
    const directory = (await api.exchange({ path: "/api/r5/sources" }))
      .body as Directory;
    const entry = directory.entries.find(
      (e) =>
        e.asset &&
        (family === "TDX"
          ? e.domain === "TDX"
          : e.asset.identity === "mdf.macro.bls.ppi.final-demand"),
    )!;
    for (const tab of ["periodic", "executions"]) {
      location.hash = new URLSearchParams({
        ui: "r5",
        view: "collection",
        tab,
        asset: entry.asset!.identity,
        asset_revision: entry.asset!.revision,
        operation: entry.identity,
      }).toString();
      const view = render(<App transport={api.transport} />);
      await ready();
      expect(screen.getByText(/尚未查询/)).toBeTruthy();
      expect(new URLSearchParams(location.hash.slice(1)).get("asset")).toBe(
        entry.asset!.identity,
      );
      expect(
        api.requests.some((r) => /cpi\/(jobs|plans|read)/.test(r.path)),
      ).toBe(false);
      expect(api.requests.every((r) => r.method === "GET")).toBe(true);
      view.unmount();
    }
  },
);

it.each([
  ["", false],
  ["", true],
  [".123456", false],
  [".123456", true],
])(
  "legacy Z links admit the same instant and submit Beijing offsets with precision %s (normalized: %s)",
  async (fraction, normalized) => {
    const legacy = `2026-10-31T20:30:05${fraction}Z`;
    const beijing = `2026-11-01T04:30:05${fraction}+08:00`;
    const setQuery = vi.fn();
    const transport = vi.fn<Transport>(async () =>
      Response.json({
        status: "OK",
        evidence_mode: "OFFLINE_FIXTURE",
        measurements: [],
        version_identity: "legacy-version",
        publication_identity: "legacy-publication",
      }),
    );
    render(
      <LocalReader
        transport={transport}
        query={{ reference: "2026-10", as_of: normalized ? beijing : legacy }}
        setQuery={setQuery}
        route={{
          view: "assets",
          invalid: false,
          fields: new URLSearchParams({
            asset: CPI.identity,
            asset_revision: CPI.revision,
            reference: "2026-10",
            as_of: legacy,
            version: "legacy-version",
          }),
        }}
        catalog={null}
        generation={0}
      />,
    );
    expect(
      (
        screen.getByLabelText(
          "数据可用截至时间（北京时间）",
        ) as HTMLInputElement
      ).value,
    ).toBe("2026-11-01T04:30:05");
    expect((screen.getByLabelText("参考月") as HTMLInputElement).value).toBe(
      "2026-10",
    );
    expect((btn("读取本地数据") as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(btn("读取本地数据"));
    await screen.findByText(/读取成功/);
    const params = new URL(
      String(transport.mock.calls[0][0]),
      "http://fixture.invalid",
    ).searchParams;
    expect(params.get("as_of")).toBe(beijing);
    expect(Date.parse(params.get("as_of")!)).toBe(Date.parse(legacy));
    expect(params.get("reference")).toBe("2026-10");
    expect(setQuery).toHaveBeenCalledWith(
      { reference: "2026-10", as_of: beijing },
      "legacy-version",
    );
  },
);
