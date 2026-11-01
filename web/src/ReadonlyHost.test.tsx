import "./offlineTestSetup";
import { afterEach, expect, it } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import App from "./App";
import { CPI, PCE } from "./workflowModel";
import { asgi, closeBridges } from "./workflowTestBridge";

afterEach(() => {
  cleanup();
  closeBridges();
  location.hash = "";
});
const button = (name: string | RegExp) =>
  screen.getByRole("button", { name }) as HTMLButtonElement;
function open(h: ReturnType<typeof asgi>, asset = CPI, tab = "") {
  location.hash = new URLSearchParams({
    ui: "r5",
    view: "assets",
    package: "macro",
    asset: asset.identity,
    asset_revision: asset.revision,
    ...(tab ? { tab } : {}),
  }).toString();
  return render(
    <App
      transport={h.transport}
      initialQuery={{
        reference: asset.identity === PCE.identity ? "2026-07" : "2026-08",
        as_of: "2026-10-27T00:00:00Z",
      }}
    />,
  );
}
const zeros = (h: ReturnType<typeof asgi>) => {
  expect(h.requests.every((r) => r.method === "GET")).toBe(true);
  expect(h.effects.source).toBe(0);
  expect(h.effects.jobs).toBe(0);
};
it("disabled CPI with retained history can query the real readonly Host without a source or config write", async () => {
  const h = asgi("default", "readonly_ui_exchange.py");
  open(h);
  await screen.findByRole("region", { name: "普通查询" });
  await waitFor(() => expect(button("刷新").disabled).toBe(false));
  expect(h.requests.some((r) => r.path.startsWith("/api/r5/cpi/query"))).toBe(
    false,
  );
  fireEvent.click(button("查询数据"));
  const result = await screen.findByRole("region", { name: "普通查询结果" });
  expect(within(result).getByText("返回渠道：本地已发布数据。")).toBeTruthy();
  expect(within(result).getAllByRole("row")).toHaveLength(5);
  expect(screen.getByText("明确持有")).toBeTruthy();
  zeros(h);
});
it("retained PCE uses the reused exact/as-of domain Reader and never advertises a qualified source", async () => {
  const h = asgi("default", "readonly_ui_exchange.py");
  open(h, PCE, "local");
  await screen.findByRole("region", { name: "本地读取" });
  await waitFor(() => expect(button("读取本地数据").disabled).toBe(false));
  fireEvent.click(button("读取本地数据"));
  const result = await screen.findByRole("region", { name: "读取结果" });
  expect(within(result).getAllByRole("row")).toHaveLength(5);
  const read = h.requests.find((r) => r.path.startsWith("/api/pce/read?"));
  expect(read?.status).toBe(200);
  expect(
    new URL(read!.path, "http://offline.invalid").searchParams.get("reference"),
  ).toBe("2026-07");
  zeros(h);
});
it("the new Host keeps source management readonly and offers no replacement control", async () => {
  const h = asgi("default", "readonly_ui_exchange.py");
  location.hash = "ui=r5&view=access&package=macro";
  render(
    <App
      transport={h.transport}
      initialQuery={{ reference: "2026-08", as_of: "2026-10-27T00:00:00Z" }}
    />,
  );
  fireEvent.click(await screen.findByRole("button", { name: "接入此包" }));
  await screen.findByRole("checkbox", { name: "美国 CPI" });
  expect(
    (
      screen
        .getByRole("checkbox", { name: "美国 CPI" })
        .closest("fieldset") as HTMLFieldSetElement
    ).disabled,
  ).toBe(true);
  expect(button("保存配置").disabled).toBe(true);
  expect(
    screen.getAllByText("当前服务仅支持查看，保存配置尚未开放。").length,
  ).toBeGreaterThan(0);
  fireEvent.click(screen.getByRole("tab", { name: "来源配置" }));
  expect(screen.queryByRole("button", { name: "替换 BLS 凭据" })).toBeNull();
  expect(document.body.textContent).not.toContain("synthetic-bls-key");
  zeros(h);
});

it("groups the CPI provider, hides candidate implementations and selects a sole definition when opening an asset", async () => {
  const h = asgi("default", "readonly_ui_exchange.py");
  location.hash = "ui=r5&view=assets&package=macro";
  render(
    <App
      transport={h.transport}
      initialQuery={{ reference: "2026-08", as_of: "2026-10-27T00:00:00Z" }}
    />,
  );
  fireEvent.click(await screen.findByRole("button", { name: /选择 美国 CPI/ }));
  await screen.findByRole("region", { name: "普通查询" });
  expect(
    new URLSearchParams(location.hash.slice(1)).get("asset_revision"),
  ).toBe(CPI.revision);
  expect(screen.queryByLabelText("资产修订")).toBeNull();
  expect(screen.queryByText("来源变体")).toBeNull();
  expect(screen.getByText("数据提供方：BLS")).toBeTruthy();
  expect(screen.queryByRole("button", { name: /MDD/ })).toBeNull();
  expect(screen.queryByText("技术详情")).toBeNull();
  zeros(h);
});

it("applies a changed historical time and reads PCE in one action while preserving the link scope", async () => {
  const h = asgi("default", "readonly_ui_exchange.py");
  open(h, PCE, "local");
  await screen.findByRole("region", { name: "本地读取" });
  await waitFor(() => expect(button("读取本地数据").disabled).toBe(false));
  expect(screen.queryByRole("button", { name: "应用查询条件" })).toBeNull();
  fireEvent.change(screen.getByLabelText("数据可用截至时间（北京时间）"), {
    target: { value: "2026-10-28T00:00:00" },
  });
  fireEvent.click(button("读取本地数据"));
  const result = await screen.findByRole("region", { name: "读取结果" });
  expect(within(result).getAllByRole("row")).toHaveLength(5);
  expect(new URLSearchParams(location.hash.slice(1)).get("as_of")).toBe(
    "2026-10-28T00:00:00+08:00",
  );
  expect(screen.queryByText("技术详情")).toBeNull();
  expect(screen.getAllByText("数据来源与版本")).toHaveLength(1);
  zeros(h);
});

it("the explicit config Host saves all three capabilities and cancelling a later draft does not save", async () => {
  const h = asgi("config_save", "readonly_ui_exchange.py");
  location.hash = "ui=r5&view=access&package=macro";
  render(
    <App
      transport={h.transport}
      initialQuery={{ reference: "2026-08", as_of: "2026-10-27T00:00:00Z" }}
    />,
  );
  fireEvent.click(await screen.findByRole("button", { name: "接入此包" }));
  for (const name of ["美国 CPI", "美国 PPI", "美国 PCE价格指数"])
    fireEvent.click(await screen.findByRole("checkbox", { name }));
  expect(button("保存配置").disabled).toBe(false);
  fireEvent.click(button("保存配置"));
  await screen.findByText("配置已保存；尚未发起来源请求或采集。");
  expect(h.requests.filter((r) => r.method === "PUT")).toHaveLength(1);
  expect(screen.queryByText("技术详情")).toBeNull();
  fireEvent.click(screen.getByRole("checkbox", { name: "美国 CPI" }));
  fireEvent.click(button("取消"));
  fireEvent.click(await screen.findByRole("button", { name: "管理此包" }));
  await waitFor(() =>
    expect(
      (screen.getByRole("checkbox", { name: "美国 CPI" }) as HTMLInputElement)
        .checked,
    ).toBe(true),
  );
  expect(h.requests.filter((r) => r.method === "PUT")).toHaveLength(1);
  expect(h.effects.source).toBe(0);
  expect(h.effects.jobs).toBe(0);
});
it("a storage failure stays explicit and an ordinary query cannot transmit", async () => {
  const h = asgi("storage_failure", "readonly_ui_exchange.py");
  open(h);
  await screen.findByRole("region", { name: "普通查询" });
  await waitFor(() => expect(button("刷新").disabled).toBe(false));
  fireEvent.click(button("查询数据"));
  const query = screen.getByRole("region", { name: "普通查询" });
  await within(query).findByRole("alert");
  expect(query.textContent).toContain("storage_unavailable");
  expect(screen.queryByRole("region", { name: "普通查询结果" })).toBeNull();
  zeros(h);
});

it("enabling CPI does not offer unavailable implementation choices when the Host has no source binding", async () => {
  const h = asgi("config_save", "readonly_ui_exchange.py");
  location.hash = "ui=r5&view=access&package=macro";
  render(
    <App
      transport={h.transport}
      initialQuery={{ reference: "2026-08", as_of: "2026-10-27T00:00:00Z" }}
    />,
  );
  fireEvent.click(await screen.findByRole("button", { name: "接入此包" }));
  fireEvent.click(await screen.findByRole("checkbox", { name: "美国 CPI" }));
  fireEvent.click(button("保存配置"));
  await screen.findByText("配置已保存；尚未发起来源请求或采集。");
  fireEvent.click(button("取消"));
  fireEvent.click(button("数据资产"));
  fireEvent.click(await screen.findByRole("button", { name: /选择 美国 CPI/ }));
  await screen.findByRole("region", { name: "普通查询" });
  fireEvent.change(screen.getByLabelText("查询月份"), {
    target: { value: "2026-09" },
  });
  fireEvent.click(button("查询数据"));
  await screen.findByText("来源查询尚未接通，本次仅能读取已保存的数据。");
  expect(screen.queryByLabelText("来源与取数方式")).toBeNull();
  expect(h.effects.source).toBe(0);
  expect(h.effects.jobs).toBe(0);
});

it("an unreadable partial CPI still appears as retained history when its capability is disabled", async () => {
  const h = asgi("partial", "readonly_ui_exchange.py");
  open(h);
  await screen.findByRole("button", { name: /选择 美国 CPI/ });
  expect(await screen.findByText("明确持有")).toBeTruthy();
  expect(screen.getByText(/当前内容读取不可用/)).toBeTruthy();
  zeros(h);
});
