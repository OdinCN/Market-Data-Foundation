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
import App from "./App";
import { asgi, closeBridges } from "./workflowTestBridge";
import {
  CPI,
  PCE,
  MANUAL_OPERATION,
  PCE_OPERATION,
  keyOf,
} from "./workflowModel";
import type { Transport } from "./api";

const parent = "cpi-init-fb8e51bc93a2918b6854c5d9e743c7a38f16de53";
const child = "cpi-backfill-fee9d5cf578629516dab4ea30d11da454c6ed433";
const pceParent = "pce-init-698446184a6df3c77bfb182b4411fa73b001143c";
const query = { reference: "2026-09", as_of: "2026-10-25T12:00:00Z" };
afterEach(() => {
  cleanup();
  closeBridges();
  sessionStorage.clear();
  location.hash = "";
});
const button = (name: string) =>
  screen.getByRole("button", { name }) as HTMLButtonElement;
const tab = (name: string) =>
  fireEvent.click(screen.getByRole("tab", { name }));
function host(scenario = "ready") {
  const cpi = asgi(scenario, "cpi_r8_ui_exchange.py");
  const pce = asgi("backfill", "pce_native_ui_exchange.py");
  const transport: Transport = (input, init) =>
    String(input).startsWith("/api/pce/")
      ? pce.transport(input, init)
      : cpi.transport(input, init);
  return { cpi, pce, transport };
}
function open(transport: Transport, fields: Record<string, string> = {}) {
  location.hash = new URLSearchParams({
    ui: "r5",
    view: "collection",
    tab: "new",
    ...fields,
  }).toString();
  return render(<App transport={transport} initialQuery={query} />);
}
async function executions() {
  tab("执行记录");
  await screen.findByRole("region", { name: "执行记录" });
  await screen.findByRole("button", { name: new RegExp(parent) });
}
const row = (id: string) =>
  within(screen.getByRole("region", { name: "执行记录" })).getByRole("button", {
    name: new RegExp(id),
  });

async function chooseBackfillParent(target = CPI, job = parent) {
  await screen.findByRole("option", { name: target === CPI ? /CPI/ : /PCE/ });
  fireEvent.change(screen.getByLabelText("采集目标"), {
    target: { value: keyOf(target) },
  });
  fireEvent.click(button("指定范围回补"));
  await screen.findByRole("option", { name: new RegExp(job) });
  fireEvent.change(screen.getByLabelText("关联失败 / 异常执行"), {
    target: { value: job },
  });
  // Confirm only after the newly mounted form has loaded its parent's range.
  // A late range initialization intentionally clears any earlier confirmation.
  await waitFor(() => {
    for (const name of ["开始参考月", "结束参考月"])
      expect((screen.getByLabelText(name) as HTMLInputElement).value).toBe(
        target === PCE ? "2026-07" : "2026-09",
      );
  });
}

it.each([false, true])(
  "empty creation route binds the selected CPI failed INIT and submits once (other asset filter: %s)",
  async (filtered) => {
    const h = host();
    open(h.transport);
    expect(new URLSearchParams(location.hash.slice(1)).has("operation")).toBe(
      false,
    );
    const directory = (await h.cpi.exchange({ path: "/api/r5/sources" })).body;
    const before = { ...h.cpi.effects };
    expect(
      directory.entries.filter((e: any) => e.asset?.identity === CPI.identity),
    ).toHaveLength(4);
    if (filtered) {
      await executions();
      fireEvent.change(screen.getByLabelText("执行资产筛选"), {
        target: { value: keyOf(PCE) },
      });
      tab("新建采集");
    }
    await chooseBackfillParent();
    await waitFor(() =>
      expect(
        (screen.getByLabelText("开始参考月") as HTMLInputElement).value,
      ).toBe("2026-09"),
    );
    fireEvent.click(screen.getByRole("checkbox"));
    await waitFor(() => expect(button("创建采集任务").disabled).toBe(false));
    expect(new URLSearchParams(location.hash.slice(1)).get("operation")).toBe(
      MANUAL_OPERATION,
    );
    fireEvent.click(button("创建采集任务"));
    await screen.findByRole("heading", { name: "执行详情" });
    const detail = screen.getByRole("complementary", { name: "执行记录详情" });
    expect(within(detail).getByText(`${child} / 尚未执行`)).toBeTruthy();
    expect(
      (screen.getByLabelText("执行资产筛选") as HTMLSelectElement).value,
    ).toBe(filtered ? keyOf(PCE) : "all");
    const childRow = within(
      screen.getByRole("region", { name: "执行记录" }),
    ).queryByRole("button", { name: new RegExp(`^选择 ${child} `) });
    if (filtered) expect(childRow).toBeNull();
    else expect(childRow).toBeTruthy();
    expect(h.cpi.requests.filter((r) => r.method === "POST")).toHaveLength(1);
    expect(h.cpi.requests.find((r) => r.method === "POST")?.body).toMatchObject(
      {
        parent_job_id: parent,
        job_identity: child,
        reference: "2026-09",
      },
    );
    expect(h.cpi.effects).toEqual({ ...before, jobs: 2 });
  },
);

it("clearing a selected parent and changing assets invalidates its source, range and confirmation", async () => {
  const h = host();
  open(h.transport);
  await screen.findByRole("option", { name: /PCE/ });
  fireEvent.change(screen.getByLabelText("采集目标"), {
    target: { value: keyOf(PCE) },
  });
  expect(new URLSearchParams(location.hash.slice(1)).get("reference")).toBe(
    "2026-07",
  );
  await chooseBackfillParent();
  await waitFor(() =>
    expect(new URLSearchParams(location.hash.slice(1)).get("reference")).toBe(
      "2026-09",
    ),
  );
  fireEvent.click(screen.getByRole("checkbox"));
  await waitFor(() => expect(button("创建采集任务").disabled).toBe(false));
  fireEvent.change(screen.getByLabelText("关联失败 / 异常执行"), {
    target: { value: "" },
  });
  expect((screen.getByLabelText("开始参考月") as HTMLInputElement).value).toBe(
    "",
  );
  expect((screen.getByLabelText("结束参考月") as HTMLInputElement).value).toBe(
    "",
  );
  expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(
    false,
  );
  expect(button("创建采集任务").disabled).toBe(true);
  expect(screen.getByText("未选择具名来源变体")).toBeTruthy();
  for (const key of ["job", "operation", "reference", "as_of"])
    expect(new URLSearchParams(location.hash.slice(1)).has(key)).toBe(false);
  await screen.findByRole("option", { name: new RegExp(parent) });
  fireEvent.change(screen.getByLabelText("关联失败 / 异常执行"), {
    target: { value: parent },
  });
  fireEvent.click(screen.getByRole("checkbox"));
  await waitFor(() => expect(button("创建采集任务").disabled).toBe(false));
  for (const target of [PCE, CPI]) {
    fireEvent.change(screen.getByLabelText("采集目标"), {
      target: { value: keyOf(target) },
    });
    expect(new URLSearchParams(location.hash.slice(1)).has("job")).toBe(false);
    expect(new URLSearchParams(location.hash.slice(1)).has("operation")).toBe(
      false,
    );
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(
      false,
    );
    expect(button("创建采集任务").disabled).toBe(true);
  }
  expect((screen.getByLabelText("开始参考月") as HTMLInputElement).value).toBe(
    "",
  );
  expect(screen.getByText("未选择具名来源变体")).toBeTruthy();
  expect(h.cpi.requests.filter((r) => r.method === "POST")).toHaveLength(0);
  expect(h.pce.requests.filter((r) => r.method === "POST")).toHaveLength(0);
});

it.each([
  [PCE, parent],
  [CPI, pceParent],
] as const)(
  "ordinary details preserve the selected creation target %j",
  async (target, viewedJob) => {
    const h = host();
    open(h.transport);
    await screen.findByRole("option", { name: /PCE/ });
    fireEvent.change(screen.getByLabelText("采集目标"), {
      target: { value: keyOf(target) },
    });
    await executions();
    await waitFor(() => expect(row(viewedJob)).toBeTruthy());
    fireEvent.click(row(viewedJob));
    await screen.findByRole("heading", { name: "执行详情" });
    expect(
      within(
        screen.getByRole("complementary", { name: "执行记录详情" }),
      ).getByText(new RegExp(`${viewedJob} /`)),
    ).toBeTruthy();
    tab("新建采集");
    expect((screen.getByLabelText("采集目标") as HTMLSelectElement).value).toBe(
      keyOf(target),
    );
    expect(h.cpi.requests.filter((r) => r.method === "POST")).toHaveLength(0);
    expect(h.pce.requests.filter((r) => r.method === "POST")).toHaveLength(0);
  },
);

it.each([CPI, PCE])(
  "named existing successor for %j remains visible outside both asset and plan list filters",
  async (target) => {
    const h = host();
    const bridge = target === CPI ? h.cpi : h.pce;
    const path = target === CPI ? "/api/r5/cpi/jobs" : "/api/pce/jobs";
    const filter = keyOf(target === CPI ? PCE : CPI);
    const offer = (await bridge.exchange({ path })).body.backfill.submission;
    await bridge.exchange({
      path: `${path}/backfill`,
      method: "POST",
      body: offer,
    });
    open(h.transport, {
      execution_asset: filter,
      execution_schedule: "previous-plan",
    });
    await chooseBackfillParent(target, offer.parent_job_id);
    fireEvent.click(
      await screen.findByRole("button", { name: "查看回补任务 →" }),
    );
    await screen.findByRole("heading", { name: "执行详情" });
    expect(
      within(
        screen.getByRole("complementary", { name: "执行记录详情" }),
      ).getByText(`${offer.job_identity} / 尚未执行`),
    ).toBeTruthy();
    expect(
      (screen.getByLabelText("执行资产筛选") as HTMLSelectElement).value,
    ).toBe(filter);
    expect(screen.getByText("计划筛选：previous-plan")).toBeTruthy();
    expect(
      within(screen.getByRole("region", { name: "执行记录" })).queryByText(
        offer.job_identity,
      ),
    ).toBeNull();
    expect(bridge.requests.filter((r) => r.method === "POST")).toHaveLength(0);
  },
);

it("creation target never filters records; details, independent filters, remount and history keep visible scope", async () => {
  const h = host();
  let view = open(h.transport);
  await screen.findByLabelText("采集目标");
  for (const target of [keyOf(CPI), keyOf(PCE), ""]) {
    fireEvent.change(screen.getByLabelText("采集目标"), {
      target: { value: target },
    });
    await executions();
    await waitFor(() => expect(row(pceParent)).toBeTruthy());
    expect(
      (screen.getByLabelText("执行资产筛选") as HTMLSelectElement).value,
    ).toBe("all");
    fireEvent.click(row(parent));
    await screen.findByRole("heading", { name: "执行详情" });
    expect(row(pceParent)).toBeTruthy();
    expect(
      h.cpi.requests.filter((r) => r.path.includes("job_identity=")),
    ).toHaveLength(0);
    tab("新建采集");
  }
  await executions();
  fireEvent.change(screen.getByLabelText("执行资产筛选"), {
    target: { value: keyOf(PCE) },
  });
  await waitFor(() =>
    expect(
      within(screen.getByRole("region", { name: "执行记录" })).queryByText(
        parent,
      ),
    ).toBeNull(),
  );
  const pceFiltered = location.hash;
  tab("周期任务");
  expect(
    (screen.getByLabelText("周期资产筛选") as HTMLSelectElement).value,
  ).toBe("all");
  fireEvent.change(screen.getByLabelText("周期资产筛选"), {
    target: { value: keyOf(CPI) },
  });
  tab("执行记录");
  expect(
    (screen.getByLabelText("执行资产筛选") as HTMLSelectElement).value,
  ).toBe(keyOf(PCE));
  const saved = location.hash;
  view.unmount();
  view = render(<App transport={h.transport} initialQuery={query} />);
  await screen.findByLabelText("执行资产筛选");
  expect(
    (screen.getByLabelText("执行资产筛选") as HTMLSelectElement).value,
  ).toBe(keyOf(PCE));
  fireEvent.click(button("清除筛选"));
  await screen.findByRole("button", { name: new RegExp(parent) });
  const cleared = location.hash;
  // The same popstate/hashchange path is used by Back/Forward; restore recorded entries.
  for (const hash of [pceFiltered, saved, cleared]) {
    act(() => {
      history.replaceState(null, "", hash);
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await waitFor(() =>
      expect(
        (screen.getByLabelText("执行资产筛选") as HTMLSelectElement).value,
      ).toBe(hash === cleared ? "all" : keyOf(PCE)),
    );
  }
});

it("CPI failed INIT → only successor POST → separate worker → original 06 exact read", async () => {
  const h = host();
  open(h.transport, {
    tab: "executions",
    asset: CPI.identity,
    asset_revision: CPI.revision,
    operation: MANUAL_OPERATION,
    job: parent,
  });
  await screen.findByRole("button", { name: "指定范围回补 →" });
  fireEvent.click(button("指定范围回补 →"));
  await screen.findByLabelText("关联失败 / 异常执行");
  await waitFor(() =>
    expect(
      (screen.getByLabelText("开始参考月") as HTMLInputElement).value,
    ).toBe("2026-09"),
  );
  fireEvent.click(screen.getByRole("checkbox"));
  await waitFor(() => expect(button("创建采集任务").disabled).toBe(false));
  fireEvent.doubleClick(button("创建采集任务"));
  fireEvent.click(button("创建采集任务"));
  await screen.findByRole("heading", { name: "执行详情" });
  const writes = h.cpi.requests.filter((r) => r.method === "POST");
  expect(writes).toHaveLength(1);
  expect(writes[0].path).toBe("/api/r5/cpi/jobs/backfill");
  expect(writes[0].body.parent_job_id).toBe(parent);
  expect(writes[0].body.job_identity).toBe(child);
  expect(h.cpi.effects.jobs).toBe(2);
  expect((await h.cpi.exchange({ control: "run" })).body.state).toBe(
    "succeeded",
  );
  await act(async () => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    });
    document.dispatchEvent(new Event("visibilitychange"));
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await screen.findByRole("button", { name: "精确读取结果 →" });
  fireEvent.click(button("查看原失败执行 →"));
  await screen.findByRole("button", { name: "查看回补成功结果 →" });
  expect(screen.queryByRole("button", { name: "指定范围回补 →" })).toBeNull();
  fireEvent.click(button("查看回补成功结果 →"));
  await screen.findByRole("button", { name: "读取本地数据" });
  await waitFor(() => expect(button("读取本地数据").disabled).toBe(false));
  fireEvent.click(button("读取本地数据"));
  const readResult = await screen.findByRole("region", { name: "读取结果" });
  expect(readResult.textContent).toContain(
    `p4-bls-cpi-direct-live-${child}-a1:version`,
  );
  expect(readResult.textContent).toContain(
    `p4-bls-cpi-direct-live-${child}-a1:publication`,
  );
  expect(h.cpi.requests.filter((r) => r.method === "POST")).toHaveLength(1);
});

it.each(["migration", "readonly"])(
  "CPI %s shows next step without a create invitation",
  async (scenario) => {
    const h = host(scenario);
    open(h.transport, {
      tab: "executions",
      asset: CPI.identity,
      asset_revision: CPI.revision,
      operation: MANUAL_OPERATION,
      job: parent,
    });
    await screen.findByRole("heading", { name: "执行详情" });
    await screen.findByText(
      scenario === "migration" ? /测试库尚未准入/ : /当前为只读/,
    );
    expect(screen.queryByRole("button", { name: "指定范围回补 →" })).toBeNull();
  },
);

it.each(["lost", "late"])(
  "CPI %s receipt survives navigation and remount with one POST",
  async (scenario) => {
    const h = host();
    let release: (() => void) | undefined;
    let posted = false;
    const transport: Transport = async (input, init) => {
      const response = await h.transport(input, init);
      if (init?.method === "POST") {
        posted = true;
        if (scenario === "lost") throw new Error("synthetic lost receipt");
        await new Promise<void>((resolve) => {
          release = resolve;
        });
      }
      return response;
    };
    let view = open(transport, {
      asset: CPI.identity,
      asset_revision: CPI.revision,
      operation: MANUAL_OPERATION,
      job: parent,
      mode: "backfill",
    });
    await waitFor(() =>
      expect(
        (screen.getByLabelText("开始参考月") as HTMLInputElement).value,
      ).toBe("2026-09"),
    );
    fireEvent.click(screen.getByRole("checkbox"));
    await waitFor(() => expect(button("创建采集任务").disabled).toBe(false));
    fireEvent.click(button("创建采集任务"));
    fireEvent.click(button("创建采集任务"));
    await waitFor(() => expect(posted).toBe(true));
    view.unmount();
    view = render(<App transport={transport} initialQuery={query} />);
    await screen.findByRole("button", { name: "查看回补任务 →" });
    expect(button("创建采集任务").disabled).toBe(true);
    await act(async () => release?.());
    expect(h.cpi.requests.filter((r) => r.method === "POST")).toHaveLength(1);
    expect(h.cpi.effects.jobs).toBe(2);
  },
);

it("PCE durable succeeded successor overrides local reservation on parent and form, and reads its own result", async () => {
  const h = host();
  const offer = (await h.pce.exchange({ path: "/api/pce/jobs" })).body.backfill
    .submission;
  await h.pce.exchange({
    path: "/api/pce/jobs/backfill",
    method: "POST",
    body: offer,
  });
  expect((await h.pce.exchange({ control: "run" })).body.state).toBe(
    "succeeded",
  );
  sessionStorage.setItem(
    "mdf.cpi.dev.manual-intents.v1",
    JSON.stringify([offer.job_identity]),
  );
  let view = open(h.transport, {
    tab: "executions",
    asset: PCE.identity,
    asset_revision: PCE.revision,
    operation: PCE_OPERATION,
    job: pceParent,
  });
  await screen.findByRole("button", { name: "查看回补成功结果 →" });
  expect(screen.queryByRole("button", { name: "指定范围回补 →" })).toBeNull();
  view.unmount();
  view = open(h.transport, {
    asset: PCE.identity,
    asset_revision: PCE.revision,
    operation: PCE_OPERATION,
    job: pceParent,
    mode: "backfill",
  });
  await screen.findByRole("button", { name: "查看回补成功结果 →" });
  expect(button("创建采集任务").disabled).toBe(true);
  expect(screen.queryByText(/此回补意图已预留/)).toBeNull();
  fireEvent.click(button("查看回补成功结果 →"));
  await screen.findByRole("button", { name: "读取本地数据" });
  await waitFor(() => expect(button("读取本地数据").disabled).toBe(false));
  fireEvent.click(button("读取本地数据"));
  await screen.findByText(`${offer.job_identity}:version`);
  expect(h.pce.requests.filter((r) => r.method === "POST")).toHaveLength(0);
});
