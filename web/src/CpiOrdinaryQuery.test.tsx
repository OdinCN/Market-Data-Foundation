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
import type { Transport } from "./api";
import { CPI } from "./workflowModel";
import { asgi, closeBridges } from "./workflowTestBridge";

afterEach(() => {
  cleanup();
  closeBridges();
  location.hash = "";
});
const queryRequests = (h: ReturnType<typeof asgi>) =>
  h.requests.filter((request) => request.path.startsWith("/api/r5/cpi/query?"));
const button = (name: string | RegExp) =>
  screen.getByRole("button", { name }) as HTMLButtonElement;
function open(transport: Transport, extra: Record<string, string> = {}) {
  location.hash = new URLSearchParams({
    ui: "r5",
    view: "assets",
    package: "macro",
    asset: CPI.identity,
    asset_revision: CPI.revision,
    ...extra,
  }).toString();
  return render(
    <App
      transport={transport}
      initialQuery={{ reference: "2026-08", as_of: "2026-10-27T00:00:00Z" }}
    />,
  );
}
async function ready() {
  await screen.findByRole("region", { name: "普通查询" });
  await waitFor(() => expect(button("刷新").disabled).toBe(false));
}
async function direct(h: ReturnType<typeof asgi>, suffix = "bls_cpi_direct") {
  const directory = (await h.exchange({ path: "/api/r5/sources" })).body;
  const entry = directory.entries.find((e: { component_identity: string }) =>
    e.component_identity.endsWith(suffix),
  );
  expect(entry).toBeTruthy();
  await act(async () => {
    const fields = new URLSearchParams(location.hash.slice(1));
    fields.set("operation", entry.identity);
    location.hash = fields.toString();
    window.dispatchEvent(new Event("hashchange"));
  });
}
const submit = () => fireEvent.click(button("查询数据"));
const zeros = (h: ReturnType<typeof asgi>, source: number) => {
  expect(h.effects.source).toBe(source);
  expect(h.effects.jobs).toBe(0);
  expect(h.effects.versions).toBe(0);
  expect(h.effects.publications).toBe(0);
  expect(h.requests.every((request) => request.method === "GET")).toBe(true);
};
it("local query is explicit, carries only ordinary parameters and keeps the actual local version", async () => {
  const h = asgi("local_hit", "d02_ui_exchange.py");
  open(h.transport);
  await ready();
  expect(queryRequests(h)).toHaveLength(0);
  submit();
  const result = await screen.findByRole("region", { name: "普通查询结果" });
  expect(within(result).getByText("返回渠道：本地已发布数据。")).toBeTruthy();
  expect(within(result).getAllByRole("row")).toHaveLength(5);
  const url = new URL(queryRequests(h)[0].path, "http://offline.invalid");
  expect([...url.searchParams.keys()]).toEqual(["reference"]);
  expect(url.searchParams.get("reference")).toBe("2026-08");
  zeros(h, 0);
  fireEvent.click(button("精确读取本次本地版本 →"));
  const scope = new URLSearchParams(location.hash.slice(1));
  expect(scope.get("version")).toBeTruthy();
  expect(scope.get("reference")).toBe("2026-08");
  expect(scope.get("as_of")).toBe("2026-10-27T08:00:00Z");
  expect(scope.get("tab")).toBe("local");
  expect(queryRequests(h)).toHaveLength(1);
});
it("CPI groups the BLS provider and reveals explicit implementation choices only after a local miss", async () => {
  const h = asgi("miss", "d02_ui_exchange.py");
  open(h.transport);
  await ready();
  expect(screen.getByText("数据提供方：BLS")).toBeTruthy();
  expect(screen.queryByText("来源变体")).toBeNull();
  expect(screen.queryByRole("button", { name: /MDD/ })).toBeNull();
  expect(screen.queryByLabelText("来源与取数方式")).toBeNull();
  submit();
  const methods = await screen.findByLabelText("来源与取数方式");
  const options = within(methods).getAllByRole("option");
  expect(options.map((o) => o.textContent)).toEqual(
    expect.arrayContaining([
      "BLS · 直接计算值",
      "BLS · 2026-09 公布窗口",
      "BLS · 历史指数推导",
    ]),
  );
  const directOption = options.find(
    (o) => o.textContent === "BLS · 直接计算值",
  ) as HTMLOptionElement;
  fireEvent.change(methods, { target: { value: directOption.value } });
  expect(queryRequests(h)).toHaveLength(1);
  zeros(h, 0);
});
it("missing local data needs an explicit named source and never chooses the first one", async () => {
  const h = asgi("miss", "d02_ui_exchange.py");
  open(h.transport);
  await ready();
  submit();
  const query = screen.getByRole("region", { name: "普通查询" });
  await within(query).findByRole("alert");
  expect(query.textContent).toContain("source_selection_required");
  zeros(h, 0);
  const methods = screen.getByLabelText("来源与取数方式");
  const option = within(methods).getByRole("option", {
    name: "BLS · 直接计算值",
  }) as HTMLOptionElement;
  fireEvent.change(methods, { target: { value: option.value } });
  submit();
  const result = await screen.findByRole("region", { name: "普通查询结果" });
  expect(
    within(result).getByText("返回渠道：来源透传。本次结果未持久化。"),
  ).toBeTruthy();
  expect(
    within(result).queryByRole("button", { name: "精确读取本次本地版本 →" }),
  ).toBeNull();
  const request = new URL(
    queryRequests(h).at(-1)!.path,
    "http://offline.invalid",
  );
  expect(request.searchParams.get("operation_id")).toContain(
    "bls-v2.cpi.direct@",
  );
  expect([...request.searchParams.keys()].sort()).toEqual([
    "operation_id",
    "reference",
  ]);
  zeros(h, 1);
});
it.each(["partial", "stale"])(
  "%s uses a whole named-source result with no persistence",
  async (scenario) => {
    const h = asgi(scenario, "d02_ui_exchange.py");
    open(h.transport);
    await ready();
    await direct(h);
    await ready();
    submit();
    const result = await screen.findByRole("region", { name: "普通查询结果" });
    expect(result.textContent).toContain("来源透传");
    expect(within(result).getAllByRole("row")).toHaveLength(5);
    expect(result.textContent).toContain(
      scenario === "partial"
        ? "本地数据覆盖不全"
        : "本地数据未满足本次更新要求",
    );
    zeros(h, 1);
  },
);
it.each([
  ["storage_failure", "storage_unavailable"],
  ["content_invalid", "cpi_payload_invalid_or_mismatched"],
  ["read_denied", "read_permission_denied"],
  ["source_unknown", "source_qualification_unknown"],
  ["credential_missing", "credential_missing"],
  ["source_scope_denied", "source_scope_not_admitted"],
  ["unconfigured", "query_host_not_configured"],
])(
  "%s stays a classified failure without querying a source",
  async (scenario, classification) => {
    const h = asgi(scenario, "d02_ui_exchange.py");
    open(h.transport);
    await ready();
    await direct(h);
    await ready();
    submit();
    const query = screen.getByRole("region", { name: "普通查询" });
    await within(query).findByRole("alert");
    expect(query.textContent).toContain(classification);
    expect(screen.queryByRole("region", { name: "普通查询结果" })).toBeNull();
    zeros(h, 0);
  },
);
it.each(["source_failure", "source_partial"])(
  "%s does not silently switch source or retain a false successful result",
  async (scenario) => {
    const h = asgi(scenario, "d02_ui_exchange.py");
    open(h.transport);
    await ready();
    await direct(h);
    await ready();
    submit();
    await within(screen.getByRole("region", { name: "普通查询" })).findByRole(
      "alert",
    );
    expect(screen.queryByRole("region", { name: "普通查询结果" })).toBeNull();
    expect(queryRequests(h)).toHaveLength(1);
    zeros(h, 1);
  },
);
it("disabled source still permits an existing qualified local result", async () => {
  const h = asgi("source_disabled", "d02_ui_exchange.py");
  open(h.transport);
  await ready();
  submit();
  await screen.findByText("返回渠道：本地已发布数据。");
  zeros(h, 0);
  fireEvent.change(screen.getByLabelText("查询月份"), {
    target: { value: "2026-09" },
  });
  expect(screen.queryByRole("region", { name: "普通查询结果" })).toBeNull();
  submit();
  const query = screen.getByRole("region", { name: "普通查询" });
  await within(query).findByRole("alert");
  expect(query.textContent).toContain("source_disabled");
  zeros(h, 0);
});
it("a pending query cannot be submitted twice", async () => {
  const h = asgi("miss", "d02_ui_exchange.py");
  let release: (response: Response) => void = () => {};
  let held: Response | null = null;
  const transport: Transport = async (input, init) => {
    const response = await h.transport(input, init);
    if (String(input).startsWith("/api/r5/cpi/query?"))
      return new Promise<Response>((resolve) => {
        held = response;
        release = resolve;
      });
    return response;
  };
  open(transport);
  await ready();
  await direct(h);
  await ready();
  submit();
  await waitFor(() => expect(held).not.toBeNull());
  fireEvent.submit(button("查询数据").closest("form")!);
  expect(queryRequests(h)).toHaveLength(1);
  await act(async () => release(held!));
  await screen.findByRole("region", { name: "普通查询结果" });
  zeros(h, 1);
});
it.each(["reference", "source"])(
  "late response cannot restore an old %s scope",
  async (change) => {
    const h = asgi("miss", "d02_ui_exchange.py");
    let release: (response: Response) => void = () => {};
    let held: Response | null = null;
    const transport: Transport = async (input, init) => {
      const response = await h.transport(input, init);
      if (String(input).startsWith("/api/r5/cpi/query?"))
        return new Promise<Response>((resolve) => {
          held = response;
          release = resolve;
        });
      return response;
    };
    open(transport);
    await ready();
    await direct(h);
    await ready();
    submit();
    await waitFor(() => expect(held).not.toBeNull());
    if (change === "reference") {
      fireEvent.change(screen.getByLabelText("查询月份"), {
        target: { value: "2026-09" },
      });
    } else await direct(h, "cpi_history");
    await act(async () => release(held!));
    expect(screen.queryByRole("region", { name: "普通查询结果" })).toBeNull();
    expect(queryRequests(h)).toHaveLength(1);
  },
);
it.each(["scope", "durability"])(
  "invalid returned %s is not shown as a successful query",
  async (damage) => {
    const h = asgi("miss", "d02_ui_exchange.py");
    const transport: Transport = async (input, init) => {
      const response = await h.transport(input, init);
      if (!String(input).startsWith("/api/r5/cpi/query?")) return response;
      const body = await response.json();
      if (damage === "scope") body.reference = "2026-09";
      else body.durable = true;
      return { ok: true, status: 200, json: async () => body } as Response;
    };
    open(transport);
    await ready();
    await direct(h);
    await ready();
    submit();
    const query = screen.getByRole("region", { name: "普通查询" });
    await within(query).findByRole("alert");
    expect(query.textContent).toContain("query_response_unavailable");
    expect(screen.queryByRole("region", { name: "普通查询结果" })).toBeNull();
    zeros(h, 1);
  },
);
