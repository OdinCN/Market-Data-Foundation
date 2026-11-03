import "./offlineTestSetup";
import { afterEach, expect, it } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import App from "./App";
import type { Transport } from "./api";
import { getPackage, putPackage } from "./packageApi";
import { asgi, closeBridges } from "./workflowTestBridge";

const KEY = "synthetic-ui-secret-bls";
const roots: string[] = [];
afterEach(() => {
  cleanup();
  closeBridges();
  roots
    .splice(0)
    .forEach((root) => rmSync(root, { recursive: true, force: true }));
  location.hash = "";
});
function host(scenario = "default") {
  const store_dir = mkdtempSync(join(tmpdir(), "mdf-d01-credentials-"));
  roots.push(store_dir);
  const packages = asgi(scenario, "d01_ui_exchange.py", { store_dir });
  const existing = asgi("unconfigured");
  const transport: Transport = (input, init) =>
    String(input).startsWith("/api/r5/packages")
      ? packages.transport(input, init)
      : existing.transport(input, init);
  return { packages, transport, store_dir };
}
function open(transport: Transport) {
  location.hash = "ui=r5&view=access&package=macro";
  return render(
    <App
      transport={transport}
      initialQuery={{ reference: "2026-09", as_of: "2026-10-27T00:00:00Z" }}
    />,
  );
}
const button = (name: string | RegExp) =>
  screen.getByRole("button", { name }) as HTMLButtonElement;
const click = (name: string | RegExp) => fireEvent.click(button(name));
const tab = (name: string) =>
  fireEvent.click(screen.getByRole("tab", { name }));
const choose = (name: string) =>
  fireEvent.click(screen.getByRole("checkbox", { name }));
async function ready() {
  if (!screen.queryByRole("dialog"))
    fireEvent.click(
      await screen.findByRole("button", { name: /^(接入此包|管理此包)$/ }),
    );
  const capabilities = await screen.findByRole("tab", { name: "能力配置" });
  fireEvent.click(capabilities);
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
}
function enterKey(source = "BLS", value = KEY) {
  tab("来源配置");
  click(`替换 ${source} 凭据`);
  fireEvent.change(screen.getByLabelText(`${source} 新凭据`), {
    target: { value },
  });
}
const signal = () => new AbortController().signal;

it("closing or cancelling password drafts sends no PUT and reopening has no stored input", async () => {
  const h = host();
  open(h.transport);
  await ready();
  choose("美国 CPI");
  enterKey();
  expect((screen.getByLabelText("BLS 新凭据") as HTMLInputElement).type).toBe(
    "password",
  );
  expect(document.body.textContent).not.toContain(KEY);
  click("取消");
  await ready();
  choose("美国 CPI");
  tab("来源配置");
  expect(screen.queryByLabelText("BLS 新凭据")).toBeNull();
  enterKey();
  click("关闭包配置");
  await ready();
  choose("美国 CPI");
  tab("来源配置");
  expect(screen.queryByLabelText("BLS 新凭据")).toBeNull();
  expect(h.packages.requests.every((request) => request.method === "GET")).toBe(
    true,
  );
});

it("one real PUT saves capabilities and replacement, then GET exposes state only across restart", async () => {
  const h = host();
  const view = open(h.transport);
  await ready();
  choose("美国 CPI");
  choose("美国 PCE价格指数");
  enterKey();
  expect(screen.queryByLabelText("BEA 新凭据")).toBeNull();
  click("保存配置");
  await screen.findByText(/配置已保存/);
  expect(screen.queryByLabelText("BLS 新凭据")).toBeNull();
  const writes = h.packages.requests.filter(
    (request) => request.method === "PUT",
  );
  expect(writes).toHaveLength(1);
  expect(writes[0].body).toEqual({
    expected_revision: null,
    enabled_capability_ids: ["cpi", "pce"],
    source_changes: [
      { source_id: "BEA", credential_action: "keep" },
      { source_id: "BLS", credential_action: "replace", credential_value: KEY },
    ],
  });
  const actual = await getPackage(h.transport, "macro", signal());
  expect(actual.config_revision).toBe("r1");
  expect(actual.enabled_capability_ids).toEqual(["cpi", "pce"]);
  expect(JSON.stringify(actual)).not.toContain(KEY);
  expect(JSON.stringify(actual)).not.toContain("credential_refs");
  expect(
    actual.credential_states.find((state) => state.source_id === "BLS")
      ?.authentication,
  ).toBe("unknown");
  const configuration = JSON.parse(
    readFileSync(join(h.store_dir, "configuration/packages.json"), "utf8"),
  );
  expect(JSON.stringify(configuration)).not.toContain(KEY);
  const reference = configuration.records[0].credential_refs.BLS;
  expect(
    readFileSync(
      join(h.store_dir, "configuration/credentials", reference),
      "utf8",
    ),
  ).toBe(KEY);
  view.unmount();
  open(h.transport);
  await ready();
  tab("来源配置");
  expect(screen.queryByLabelText("BLS 新凭据")).toBeNull();
  expect(document.body.textContent).not.toContain(KEY);
  closeBridges();
  const reopened = asgi("default", "d01_ui_exchange.py", {
    store_dir: h.store_dir,
  });
  const persisted = await getPackage(reopened.transport, "macro", signal());
  expect(persisted.config_revision).toBe("r1");
  expect(
    persisted.credential_states.find((state) => state.source_id === "BLS")
      ?.credential_state,
  ).toBe("configured");
});

it("removing the last capability for a source discards that source's pending replacement", async () => {
  const h = host();
  open(h.transport);
  await ready();
  choose("美国 CPI");
  enterKey();
  tab("能力配置");
  choose("美国 CPI");
  choose("美国 PCE价格指数");
  tab("来源配置");
  expect(screen.queryByRole("button", { name: /BLS 凭据/ })).toBeNull();
  tab("能力配置");
  choose("美国 CPI");
  tab("来源配置");
  expect(screen.queryByLabelText("BLS 新凭据")).toBeNull();
  click("保存配置");
  await screen.findByText(/配置已保存/);
  const write = h.packages.requests.find(
    (request) => request.method === "PUT",
  )!;
  expect(
    write.body.source_changes.every(
      (change: { credential_action: string }) =>
        change.credential_action === "keep",
    ),
  ).toBe(true);
  expect(JSON.stringify(write.body)).not.toContain(KEY);
});

it("blank replacement blocks save and cancelling replacement returns to keep semantics", async () => {
  const h = host();
  open(h.transport);
  await ready();
  choose("美国 CPI");
  tab("来源配置");
  click("替换 BLS 凭据");
  expect(button("保存配置").disabled).toBe(true);
  expect(screen.getByText("请填写新凭据，或取消本次替换。")).toBeTruthy();
  click("取消替换 BLS 凭据");
  expect(button("保存配置").disabled).toBe(false);
  click("保存配置");
  await screen.findByText(/配置已保存/);
  expect(
    h.packages.requests.find((request) => request.method === "PUT")?.body
      .source_changes,
  ).toEqual([{ source_id: "BLS", credential_action: "keep" }]);
});

it("real CAS conflict does not publish password and reread requires fresh input", async () => {
  const h = host();
  open(h.transport);
  await ready();
  choose("美国 CPI");
  enterKey();
  await putPackage(h.transport, "macro", {
    expected_revision: null,
    enabled_capability_ids: ["pce"],
    source_changes: [],
  });
  click("保存配置");
  await screen.findByText(/配置已被其他操作更新/);
  expect(button("保存配置").disabled).toBe(true);
  expect(screen.queryByLabelText("BLS 新凭据")).toBeNull();
  click("重读核对配置");
  await ready();
  expect(
    (
      screen.getByRole("checkbox", {
        name: "美国 PCE价格指数",
      }) as HTMLInputElement
    ).checked,
  ).toBe(true);
  expect(
    JSON.parse(
      readFileSync(join(h.store_dir, "configuration/packages.json"), "utf8"),
    ).records[0].credential_refs,
  ).toEqual({});
  choose("美国 CPI");
  tab("来源配置");
  expect(screen.queryByLabelText("BLS 新凭据")).toBeNull();
});

it("lost replacement receipt survives closing; reread never claims to verify the entered value or replays it", async () => {
  const h = host();
  open(h.transport);
  await ready();
  choose("美国 CPI");
  enterKey();
  h.packages.loseNext();
  click("保存配置");
  click("保存配置");
  await screen.findByText(/提交结果未知，配置可能已生效/);
  expect(screen.queryByLabelText("BLS 新凭据")).toBeNull();
  expect(button("保存配置").disabled).toBe(true);
  click("取消");
  await ready();
  await screen.findByText(/不能据此确认刚才输入的值/);
  tab("来源配置");
  expect(screen.queryByLabelText("BLS 新凭据")).toBeNull();
  expect(
    h.packages.requests.filter((request) => request.method === "PUT"),
  ).toHaveLength(1);
  expect(
    (await getPackage(h.transport, "macro", signal())).config_revision,
  ).toBe("r1");
  expect(
    readdirSync(join(h.store_dir, "configuration/credentials")),
  ).toHaveLength(1);
});

it("route changes while a replacement is in flight cannot enable a second save or resurrect a password", async () => {
  const h = host();
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let reached = false;
  const transport: Transport = async (input, init) => {
    const result = await h.transport(input, init);
    if (init?.method === "PUT") {
      reached = true;
      await gate;
    }
    return result;
  };
  open(transport);
  await ready();
  choose("美国 CPI");
  enterKey();
  click("保存配置");
  await waitFor(() => expect(reached).toBe(true));
  await act(async () => {
    location.hash = "ui=r5&view=access&package=tdx";
    window.dispatchEvent(new Event("hashchange"));
  });
  await screen.findByRole("button", { name: /^(接入此包|管理此包)$/ });
  await act(async () => {
    location.hash = "ui=r5&view=access&package=macro";
    window.dispatchEvent(new Event("hashchange"));
  });
  click(/^(接入此包|管理此包)$/);
  expect(screen.queryByLabelText("BLS 新凭据")).toBeNull();
  expect(screen.queryByRole("button", { name: "保存配置" })).toBeNull();
  await act(async () => release());
  await ready();
  expect(
    (screen.getByRole("checkbox", { name: "美国 CPI" }) as HTMLInputElement)
      .checked,
  ).toBe(true);
  tab("来源配置");
  expect(screen.queryByLabelText("BLS 新凭据")).toBeNull();
  expect(
    h.packages.requests.filter((request) => request.method === "PUT"),
  ).toHaveLength(1);
});

it("a confirmed rejection drops submitted password and never automatically retries", async () => {
  const h = host();
  let writes = 0;
  const transport: Transport = async (input, init) => {
    if (init?.method === "PUT") {
      writes++;
      return {
        ok: false,
        status: 403,
        json: async () => ({
          status: "REJECTED",
          classification: "package_write_not_authorized",
          effect: "NONE",
        }),
      } as Response;
    }
    return h.transport(input, init);
  };
  open(transport);
  await ready();
  choose("美国 CPI");
  enterKey();
  click("保存配置");
  await screen.findByText(/保存被拒绝或确认未提交/);
  expect((screen.getByLabelText("BLS 新凭据") as HTMLInputElement).value).toBe(
    "",
  );
  expect(writes).toBe(1);
  expect(button("保存配置").disabled).toBe(true);
  expect(
    (await getPackage(h.transport, "macro", signal())).config_revision,
  ).toBeNull();
  click("取消替换 BLS 凭据");
  expect(screen.queryByLabelText("BLS 新凭据")).toBeNull();
});

it.each(["read_only", "write_unknown"])(
  "%s cannot edit or submit a secret",
  async (scenario) => {
    const h = host(scenario);
    open(h.transport);
    await ready();
    choose("美国 CPI");
    tab("来源配置");
    const replace = screen.queryByRole("button", {
      name: "替换 BLS 凭据",
    }) as HTMLButtonElement | null;
    if (replace) {
      expect(replace.disabled).toBe(true);
      fireEvent.click(replace);
    }
    expect(screen.queryByLabelText("BLS 新凭据")).toBeNull();
    expect(
      h.packages.requests.every((request) => request.method === "GET"),
    ).toBe(true);
  },
);

it("FRED replacement can be cancelled, and verification requires explicit saved intent", async () => {
  const h = host();
  await putPackage(h.transport, "macro", {
    expected_revision: null,
    enabled_capability_ids: ["claims"],
    source_changes: [],
  });
  let requests = 0;
  let release: (() => void) | undefined;
  const transport: Transport = async (input, init) => {
    if (String(input).endsWith("/sources/FRED/verify")) {
      requests++;
      expect(JSON.parse(String(init?.body))).toEqual({
        expected_revision: "r1",
      });
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      const body = await getPackage(h.transport, "macro", signal());
      body.source_verifications = [
        {
          source_id: "FRED",
          checked_at: "2026-11-03T11:00:00Z",
          current: true,
          request_status: "failed",
          authentication: "unknown",
          reason: "http_forbidden",
          http_status: 403,
        },
      ];
      return new Response(JSON.stringify(body), { status: 200 });
    }
    return h.transport(input, init);
  };
  open(transport);
  await ready();
  tab("来源配置");
  expect(screen.getByRole("region", { name: "能力与来源关系" })).toBeTruthy();
  expect(screen.getByText(/DOL 官方周报日历/)).toBeTruthy();
  expect(screen.getByText(/CSV 备用数值通路未装配/)).toBeTruthy();
  enterKey("FRED", "synthetic-ui-fred");
  expect(button("验证已保存的 FRED 配置").disabled).toBe(true);
  click("取消替换 FRED 凭据");
  expect(requests).toBe(0);
  click("验证已保存的 FRED 配置");
  expect(button("正在验证…").disabled).toBe(true);
  click("正在验证…");
  expect(requests).toBe(1);
  await act(async () => release?.());
  await screen.findByText(/HTTP 403：访问被拒绝/);
  expect(screen.getByText("请求失败")).toBeTruthy();
  expect(screen.getByText("2026-11-03 19:00:00 北京时间")).toBeTruthy();
  expect(
    screen.getByText(/美国初请与续请失业金 · 数值来源：FRED/),
  ).toBeTruthy();
  expect(screen.queryByText("FRED · FRED")).toBeNull();
  expect(h.packages.requests.filter((r) => r.method === "PUT")).toHaveLength(1);
});

it("a verification configuration conflict clears old authentication success and requires reread", async () => {
  const h = host();
  await putPackage(h.transport, "macro", {
    expected_revision: null,
    enabled_capability_ids: ["claims"],
    source_changes: [],
  });
  const transport: Transport = async (input, init) => {
    if (String(input).endsWith("/sources/FRED/verify"))
      return new Response(
        JSON.stringify({
          status: "CONFLICT",
          classification: "verification_configuration_changed",
          effect: "NONE",
        }),
        { status: 409 },
      );
    const response = await h.transport(input, init);
    if (String(input) === "/api/r5/packages/macro" && init?.method === "GET") {
      const body = await response.json();
      body.source_verifications = [
        {
          source_id: "FRED",
          current: true,
          checked_at: "2026-11-03T11:00:00Z",
          request_status: "succeeded",
          authentication: "confirmed",
          reason: "request_succeeded",
          http_status: 200,
        },
      ];
      return new Response(JSON.stringify(body), { status: 200 });
    }
    return response;
  };
  open(transport);
  await ready();
  tab("来源配置");
  expect(screen.getByText("认证已确认")).toBeTruthy();
  click("验证已保存的 FRED 配置");
  await screen.findByText("配置或凭据已变化，请重新验证");
  expect(screen.queryByText("认证已确认")).toBeNull();
  expect(button("验证已保存的 FRED 配置").disabled).toBe(true);
  expect(button("重读核对配置").disabled).toBe(false);
});
