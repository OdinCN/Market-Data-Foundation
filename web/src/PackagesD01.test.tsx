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
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import App from "./App";
import type { Transport } from "./api";
import { asgi, closeBridges } from "./workflowTestBridge";
import { getPackage, getPackages, putPackage } from "./packageApi";
import { menus, route } from "./workflowRoute";
const roots: string[] = [];
afterEach(() => {
  cleanup();
  closeBridges();
  roots.splice(0).forEach((p) => rmSync(p, { recursive: true, force: true }));
  location.hash = "";
});
function host(scenario = "default", existingScenario = "unconfigured") {
  const store_dir = mkdtempSync(join(tmpdir(), "mdf-d01-ui-"));
  roots.push(store_dir);
  const packages = asgi(scenario, "d01_ui_exchange.py", { store_dir });
  const existing = asgi(existingScenario);
  const transport: Transport = (input, init) =>
    String(input).startsWith("/api/r5/packages")
      ? packages.transport(input, init)
      : existing.transport(input, init);
  return { packages, existing, transport };
}
const btn = (name: string | RegExp) =>
  screen.getByRole("button", { name }) as HTMLButtonElement;
const choose = (name: string) =>
  fireEvent.click(screen.getByRole("checkbox", { name }));
const enterEditor = async () => {
  if (!screen.queryByRole("dialog"))
    fireEvent.click(
      await screen.findByRole("button", { name: /^(接入此包|管理此包)$/ }),
    );
};
const ready = async () => {
  await enterEditor();
  await screen.findByRole("checkbox", { name: "美国 CPI" });
  await waitFor(() =>
    expect(
      (
        screen.getByRole("button", {
          name: "重读核对配置",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false),
  );
};
function open(transport: Transport, fields: Record<string, string> = {}) {
  location.hash = new URLSearchParams({
    ui: "r5",
    view: "access",
    package: "macro",
    ...fields,
  }).toString();
  return render(
    <App
      transport={transport}
      initialQuery={{ reference: "2026-09", as_of: "2026-10-27T00:00:00Z" }}
    />,
  );
}
async function move(fields: Record<string, string>) {
  await act(async () => {
    location.hash = new URLSearchParams({ ui: "r5", ...fields }).toString();
    window.dispatchEvent(new Event("hashchange"));
  });
}
const signal = () => new AbortController().signal;
it("has exactly five entries and normalizes old service/interface/page deep links without dropping exact scope", () => {
  expect(menus.map((m) => m[1])).toEqual([
    "运行总览",
    "数据源管理",
    "数据资产",
    "采集任务",
    "平台管理",
  ]);
  for (const view of ["services", "interfaces"]) {
    location.hash = `ui=r5&view=${view}&asset=mdf.example&asset_revision=r1&version=v1&reference=2026-09&as_of=2026-10-27T00:00:00Z`;
    const result = route();
    expect(result.invalid).toBe(false);
    expect(result.view).toBe(view === "services" ? "assets" : "platform");
    expect(result.fields.get("version")).toBe("v1");
  }
  for (let page = 0; page < 9; page++) {
    location.hash = `page=${page}`;
    expect(route().invalid).toBe(false);
    expect(menus.some((m) => m[0] === route().view)).toBe(true);
  }
});
it("single package editor shares two tabs; cancel never writes and uses declared sources only", async () => {
  const h = host();
  open(h.transport);
  await ready();
  choose("美国 PPI");
  fireEvent.click(screen.getByRole("tab", { name: "来源配置" }));
  expect(
    screen.getByText("本次选择所需来源：BLS。切换能力不会访问来源。"),
  ).toBeTruthy();
  expect(screen.queryByRole("textbox", { name: /凭据/ })).toBeNull();
  expect(screen.getByText(/保存后不会回显完整凭据/)).toBeTruthy();
  fireEvent.click(btn("取消"));
  await ready();
  fireEvent.click(screen.getByRole("tab", { name: "能力配置" }));
  expect(
    (screen.getByRole("checkbox", { name: "美国 PPI" }) as HTMLInputElement)
      .checked,
  ).toBe(false);
  expect(h.packages.requests.every((r) => r.method === "GET")).toBe(true);
});
it("saves PPI only, reloads actual storage and projects only its enabled asset", async () => {
  const h = host("history_unknown");
  const view = open(h.transport);
  await ready();
  choose("美国 PPI");
  fireEvent.click(btn("保存配置"));
  await screen.findByText(/配置已保存/);
  expect(h.packages.requests.filter((r) => r.method === "PUT")).toHaveLength(1);
  expect(h.packages.requests.find((r) => r.method === "PUT")?.body).toEqual({
    expected_revision: null,
    enabled_capability_ids: ["ppi"],
    source_changes: [{ source_id: "BLS", credential_action: "keep" }],
  });
  view.unmount();
  open(h.transport);
  await ready();
  expect(
    (screen.getByRole("checkbox", { name: "美国 PPI" }) as HTMLInputElement)
      .checked,
  ).toBe(true);
  fireEvent.click(btn("查看本包资产 →"));
  const table = await screen.findByRole("region", { name: "数据资产" });
  await within(table).findByRole("button", { name: /选择 美国 PPI/ });
  expect(within(table).queryByText("美国 CPI")).toBeNull();
  expect(within(table).queryByText("美国 PCE")).toBeNull();
  expect(screen.getByText(/部分接入包的本地数据尚未核实/)).toBeTruthy();
});
it("all eight Macro enablement combinations use server projection, retaining CAS revisions", async () => {
  const h = host("history_unknown");
  let current = await putPackage(h.transport, "macro", {
    expected_revision: null,
    enabled_capability_ids: ["cpi"],
    source_changes: [],
  });
  for (let mask = 0; mask < 8; mask++) {
    const ids = ["cpi", "ppi", "pce"].filter((_, bit) => mask & (1 << bit));
    current = await putPackage(h.transport, "macro", {
      expected_revision: current.config_revision,
      enabled_capability_ids: ids,
      source_changes: [],
    });
    const directory = await getPackages(h.transport, signal());
    const p = directory.packages.find((p) => p.package_id === "macro")!;
    expect(p.config_revision).toBe(current.config_revision);
    expect([...p.enabled_capability_ids].sort()).toEqual([...ids].sort());
    const view = open(h.transport, { view: "assets" });
    await waitFor(() =>
      expect(screen.queryByText("正在读取运行资产目录…")).toBeNull(),
    );
    const table = screen.getByRole("region", { name: "数据资产" });
    expect(within(table).getAllByRole("row")).toHaveLength(ids.length + 1);
    for (const c of p.capabilities)
      expect(within(table).queryByText(c.title) !== null).toBe(
        ids.includes(c.capability_id),
      );
    view.unmount();
  }
});
it("allows complete disable after first setup and retains unreadable history in the directory", async () => {
  const h = host("read_denied");
  await putPackage(h.transport, "macro", {
    expected_revision: null,
    enabled_capability_ids: ["ppi"],
    source_changes: [],
  });
  open(h.transport);
  await ready();
  choose("美国 PPI");
  fireEvent.click(btn("保存配置"));
  await screen.findByText(/配置已保存/);
  expect(
    (await getPackage(h.transport, "macro", signal())).enabled_capability_ids,
  ).toEqual([]);
  fireEvent.click(btn("查看本包资产 →"));
  await screen.findByRole("button", { name: /选择 美国 PPI/ });
  fireEvent.click(btn(/选择 美国 PPI/));
  // Use the actual declared revision; the fixture may evolve independently.
  expect(screen.queryByLabelText("资产修订")).toBeNull();
  expect(screen.getByText("明确持有")).toBeTruthy();
  expect(screen.getByText(/当前内容读取不可用/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
});
it.each(["read_only", "write_unknown"])(
  "%s disables save with a visible reason",
  async (scenario) => {
    const h = host(scenario);
    open(h.transport);
    await ready();
    choose("美国 CPI");
    expect(btn("保存配置").disabled).toBe(true);
    expect(screen.getByText(/当前账号无法保存配置/)).toBeTruthy();
  },
);
it("real CAS conflict preserves draft and blocks saving until explicit reread", async () => {
  const h = host();
  open(h.transport);
  await ready();
  choose("美国 CPI");
  await putPackage(h.transport, "macro", {
    expected_revision: null,
    enabled_capability_ids: ["pce"],
    source_changes: [],
  });
  fireEvent.click(btn("保存配置"));
  await screen.findByText(/配置已被其他操作更新/);
  expect(btn("保存配置").disabled).toBe(true);
  expect(
    (screen.getByRole("checkbox", { name: "美国 CPI" }) as HTMLInputElement)
      .checked,
  ).toBe(true);
  fireEvent.click(btn("重读核对配置"));
  await ready();
  expect(
    (
      screen.getByRole("checkbox", {
        name: "美国 PCE价格指数",
      }) as HTMLInputElement
    ).checked,
  ).toBe(true);
});
it("lost PUT receipt remains unknown and rereads the committed revision without a second PUT", async () => {
  const h = host();
  open(h.transport);
  await ready();
  choose("美国 PPI");
  h.packages.loseNext();
  fireEvent.click(btn("保存配置"));
  fireEvent.click(btn("保存配置"));
  await screen.findByText(/提交结果未知，配置可能已生效/);
  expect(btn("保存配置").disabled).toBe(true);
  fireEvent.click(btn("重读核对配置"));
  await screen.findByText(/当前启用集合与待核对内容一致/);
  expect(h.packages.requests.filter((r) => r.method === "PUT")).toHaveLength(1);
  expect(
    (await getPackage(h.transport, "macro", signal())).config_revision,
  ).toBe("r1");
});
it("package filter persists in URL, isolates TDX/KD and clears visibly", async () => {
  const h = host();
  await putPackage(h.transport, "tdx", {
    expected_revision: null,
    enabled_capability_ids: ["daily_raw"],
    source_changes: [],
  });
  await putPackage(h.transport, "kd", {
    expected_revision: null,
    enabled_capability_ids: ["current_rank"],
    source_changes: [],
  });
  open(h.transport, { view: "assets", package: "tdx" });
  await screen.findByRole("button", { name: /选择 TDX · 原始日线/ });
  expect(
    screen.queryByRole("button", { name: /选择 当前排名|选择 美国 PPI/ }),
  ).toBeNull();
  fireEvent.change(screen.getByLabelText("接入包筛选"), {
    target: { value: "kd" },
  });
  await screen.findByRole("button", { name: /选择 KD · 板块当前排名/ });
  expect(location.hash).toContain("package=kd");
  expect(
    screen.queryByRole("button", { name: /选择 TDX · 原始日线/ }),
  ).toBeNull();
  fireEvent.click(btn("清除包筛选"));
  await screen.findByRole("button", { name: /选择 TDX · 原始日线/ });
  expect(new URLSearchParams(location.hash.slice(1)).has("package")).toBe(
    false,
  );
});
it("catalog authorization failure never becomes an empty successful directory", async () => {
  const h = host("catalog_denied");
  open(h.transport, { view: "assets" });
  await screen.findByText(/数据资产读取失败/);
  expect(screen.queryByRole("region", { name: "数据资产" })).toBeNull();
  expect(screen.queryByText(/当前筛选下没有可见资产/)).toBeNull();
});
it("late package response after switching packages never replaces the new editor", async () => {
  const h = host();
  let release: (response: Response) => void = () => {};
  let held = false;
  const transport: Transport = async (input, init) => {
    const response = await h.transport(input, init);
    if (String(input) === "/api/r5/packages/macro" && !held) {
      held = true;
      return new Promise<Response>((resolve) => {
        release = resolve;
        void response;
        releaseResponse = response;
      });
    }
    return response;
  };
  let releaseResponse: Response;
  open(transport);
  await enterEditor();
  await waitFor(() => expect(held).toBe(true));
  await move({ view: "access", package: "tdx" });
  await enterEditor();
  await screen.findByRole("checkbox", { name: "原始日线" });
  choose("原始日线");
  await act(async () => release(releaseResponse));
  expect(screen.queryByRole("checkbox", { name: "美国 CPI" })).toBeNull();
  expect(
    (screen.getByRole("checkbox", { name: "原始日线" }) as HTMLInputElement)
      .checked,
  ).toBe(true);
});
it("refresh does not overwrite an unsaved capability draft", async () => {
  const h = host();
  open(h.transport);
  await ready();
  choose("美国 CPI");
  await waitFor(() => expect(btn("刷新").disabled).toBe(false));
  fireEvent.click(btn("刷新"));
  await ready();
  expect(
    (screen.getByRole("checkbox", { name: "美国 CPI" }) as HTMLInputElement)
      .checked,
  ).toBe(true);
  expect(h.packages.requests.filter((r) => r.method === "PUT")).toHaveLength(0);
});
it("pending save stays reserved across unmount and reconciles a late lost receipt before editing", async () => {
  const h = host();
  let release = () => {};
  let submitted = false;
  const transport: Transport = async (input, init) => {
    const response = await h.transport(input, init);
    if (init?.method === "PUT") {
      submitted = true;
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      throw new Error("synthetic_lost_receipt");
    }
    return response;
  };
  const first = open(transport);
  await ready();
  choose("美国 CPI");
  fireEvent.click(btn("保存配置"));
  await waitFor(() => expect(submitted).toBe(true));
  first.unmount();
  open(transport);
  await enterEditor();
  await screen.findByText("正在读取包配置…");
  expect(screen.queryByRole("button", { name: "保存配置" })).toBeNull();
  await act(async () => release());
  await screen.findByText(/当前启用集合与待核对内容一致/);
  expect(h.packages.requests.filter((r) => r.method === "PUT")).toHaveLength(1);
  expect(
    (screen.getByRole("checkbox", { name: "美国 CPI" }) as HTMLInputElement)
      .checked,
  ).toBe(true);
});
it.each(["metadata_denied", "metadata_unknown"])(
  "%s never reveals retained holdings or claims a complete empty history",
  async (scenario) => {
    const h = host(scenario);
    open(h.transport, { view: "assets" });
    await screen.findByText(/部分接入包的本地数据尚未核实/);
    const table = screen.getByRole("region", { name: "数据资产" });
    expect(
      within(table).queryByRole("button", { name: /选择 美国 PPI/ }),
    ).toBeNull();
    expect(screen.queryByText("明确持有")).toBeNull();
    expect(screen.getByText(/当前没有可确认的目录条目/)).toBeTruthy();
  },
);
it("failed reread after a lost receipt keeps saving blocked until a successful server reread", async () => {
  const h = host();
  let failRead = false;
  const transport: Transport = (input, init) =>
    failRead &&
    String(input) === "/api/r5/packages/macro" &&
    init?.method === "GET"
      ? Promise.reject(new Error("synthetic_unreachable"))
      : h.transport(input, init);
  open(transport);
  await ready();
  choose("美国 CPI");
  h.packages.loseNext();
  fireEvent.click(btn("保存配置"));
  await screen.findByText(/提交结果未知，配置可能已生效/);
  failRead = true;
  fireEvent.click(btn("重读核对配置"));
  await screen.findByText(/提交结果未知，配置可能已生效/);
  expect(btn("保存配置").disabled).toBe(true);
  failRead = false;
  fireEvent.click(btn("重读核对配置"));
  await screen.findByText(/当前启用集合与待核对内容一致/);
  expect(h.packages.requests.filter((r) => r.method === "PUT")).toHaveLength(1);
});
it.each(["history_unknown", "read_denied"])(
  "%s keeps named local Reader assessment separate and never overrides an explicit denial",
  async (scenario) => {
    const h = host(scenario, "available");
    await putPackage(h.transport, "macro", {
      expected_revision: null,
      enabled_capability_ids: ["cpi"],
      source_changes: [],
    });
    open(h.transport, {
      view: "assets",
      asset: "mdf.macro.bls.cpi.headlines",
      asset_revision: "p3-r1",
      tab: "local",
      version: "p6-cpi-live:version",
      reference: "2026-08",
      as_of: "2026-10-06T00:00:10+00:00",
    });
    await screen.findByText(
      scenario === "history_unknown"
        ? /当前内容读取资格尚未确认/
        : /当前内容读取不可用/,
    );
    if (scenario === "read_denied") {
      expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
    } else {
      const read = await screen.findByRole("button", { name: "读取本地数据" });
      await waitFor(() =>
        expect((read as HTMLButtonElement).disabled).toBe(false),
      );
      fireEvent.click(read);
      await screen.findByText(/读取成功/);
      fireEvent.change(screen.getByLabelText("读取方式"), {
        target: { value: "as_of" },
      });
      fireEvent.click(btn("读取本地数据"));
      await screen.findByText(/读取成功/);
      expect(
        h.existing.requests
          .filter((r) => r.path.includes("/read?"))
          .map((r) =>
            new URL(r.path, "http://offline").searchParams.get("mode"),
          ),
      ).toEqual(["exact_version", "as_of"]);
    }
    expect(h.existing.effects).toEqual({ source: 0, raw: 0, jobs: 0 });
  },
);

it("frozen package management opens the unified modal", async () => {
  const h = host();
  open(h.transport);
  await ready();
  expect(screen.getByRole("dialog", { name: /接入包配置/ })).toBeTruthy();
});

it.each(["取消", "关闭包配置", "Escape"])(
  "%s closes the frozen two-tab dialog and discards an unsaved draft without a PUT",
  async (action) => {
    const h = host();
    open(h.transport);
    await ready();
    const dialog = screen.getByRole("dialog", { name: /接入包配置/ });
    expect(within(dialog).getAllByRole("tab")).toHaveLength(2);
    expect(within(dialog).getByRole("tab", { name: "能力配置" })).toBeTruthy();
    choose("美国 CPI");
    choose("美国 PCE价格指数");
    fireEvent.click(within(dialog).getByRole("tab", { name: "来源配置" }));
    expect(
      within(dialog).getByText(/本次选择所需来源：BEA \/ BLS/),
    ).toBeTruthy();
    if (action === "Escape") fireEvent.keyDown(dialog, { key: "Escape" });
    else fireEvent.click(within(dialog).getByRole("button", { name: action }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(h.packages.requests.filter((r) => r.method === "PUT")).toHaveLength(
      0,
    );
    expect(btn("接入此包")).toBeTruthy();
    await ready();
    fireEvent.click(screen.getByRole("tab", { name: "能力配置" }));
    for (const name of ["美国 CPI", "美国 PPI", "美国 PCE价格指数"])
      expect(
        (screen.getByRole("checkbox", { name }) as HTMLInputElement).checked,
      ).toBe(false);
  },
);
it("configured package has one management action using the same dialog", async () => {
  const h = host();
  await putPackage(h.transport, "macro", {
    expected_revision: null,
    enabled_capability_ids: ["ppi"],
    source_changes: [],
  });
  open(h.transport);
  await screen.findByRole("button", { name: "管理此包" });
  expect(screen.queryByRole("button", { name: "接入此包" })).toBeNull();
  expect(document.querySelectorAll("[data-package-action]")).toHaveLength(1);
  await ready();
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
  choose("美国 CPI");
  fireEvent.click(btn("保存配置"));
  await screen.findByText(/配置已保存/);
  fireEvent.click(btn("取消"));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(
    [
      ...(await getPackage(h.transport, "macro", signal()))
        .enabled_capability_ids,
    ].sort(),
  ).toEqual(["cpi", "ppi"]);
});
it("directory refresh failure after a confirmed save keeps the editor and its confirmed snapshot", async () => {
  const h = host();
  let directories = 0;
  const transport: Transport = async (input, init) => {
    if (String(input) === "/api/r5/packages" && ++directories > 1)
      return {
        ok: false,
        status: 503,
        json: async () => ({
          status: "UNAVAILABLE",
          classification: "package_host_unavailable",
          effect: "NONE",
        }),
      } as Response;
    return h.transport(input, init);
  };
  open(transport);
  await ready();
  choose("美国 PPI");
  fireEvent.click(btn("保存配置"));
  await screen.findByText(/接入包读取失败/);
  expect(screen.getByRole("dialog")).toBeTruthy();
  expect(screen.getByText(/配置已保存/)).toBeTruthy();
  expect(
    (screen.getByRole("checkbox", { name: "美国 PPI" }) as HTMLInputElement)
      .checked,
  ).toBe(true);
  expect(h.packages.requests.filter((r) => r.method === "PUT")).toHaveLength(1);
});
