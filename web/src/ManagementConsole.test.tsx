import { formatBeijingTime } from "./beijingTime";
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
import { resolve } from "node:path";
import { createInterface } from "node:readline";
import App from "./App";
import { ManagementConsole } from "./ManagementConsole";
import type { Management, Transport } from "./api";

const children: ChildProcessWithoutNullStreams[] = [];
const originalWidth = window.innerWidth;
function bridge(scenario = "catalog") {
  const root = resolve(process.cwd(), "..");
  const child = spawn(
    `${root}/.venv/bin/python`,
    ["-u", "tests/r3_ui_exchange.py", scenario],
    {
      cwd: root,
      env: {
        ...process.env,
        PYTHONPATH: `${root}/src:${root}:${process.env.PYTHONPATH ?? ""}`,
      },
    },
  );
  children.push(child);
  const pending: {
    resolve: (value: unknown) => void;
    reject: (error: Error) => void;
  }[] = [];
  let stderr = "";
  child.stderr.on("data", (value) => {
    stderr += value;
  });
  createInterface({ input: child.stdout }).on("line", (line) =>
    pending.shift()?.resolve(JSON.parse(line)),
  );
  child.on("exit", (code) => {
    if (code)
      pending
        .splice(0)
        .forEach((item) =>
          item.reject(new Error(`ASGI bridge ${code}: ${stderr}`)),
        );
  });
  const api = {
    requests: [] as string[],
    management: null as Management | null,
  };
  const transport = (async (
    input: string | URL | Request,
    init?: RequestInit,
  ) => {
    expect(init?.method).toBe("GET");
    const path = String(input);
    expect(path.startsWith("/api/")).toBe(true);
    api.requests.push(path);
    const result = (await new Promise<unknown>((resolve, reject) => {
      pending.push({ resolve, reject });
      child.stdin.write(JSON.stringify({ path }) + "\n");
    })) as { status: number; body: unknown };
    if (path.startsWith("/api/r3/management"))
      api.management = result.body as Management;
    return {
      ok: result.status === 200,
      status: result.status,
      json: async () => result.body,
    } as Response;
  }) as Transport;
  return { api, transport };
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  children.splice(0).forEach((child) => {
    child.stdin.end();
    child.kill();
  });
  location.hash = "";
  window.innerWidth = originalWidth;
});

it.each([320, 390, 960, 1440])(
  "preserves navigation focus, empty instance semantics and three tabs at %ipx (DOM only)",
  async (width) => {
    window.innerWidth = width;
    location.hash = "page=7";
    const { api, transport } = bridge();
    render(<App transport={transport} />);
    await screen.findByText(/实例读取范围未配置/);
    const navigation = within(screen.getByRole("navigation"));
    const buttons = navigation.getAllByRole("button");
    expect(buttons).toHaveLength(5);
    for (const button of buttons) {
      expect(button.getAttribute("aria-label")).toBe(
        button.querySelector(".nav-label")?.textContent,
      );
      button.focus();
      expect(document.activeElement).toBe(button);
    }
    expect(
      navigation
        .getByRole("button", { name: "平台管理" })
        .getAttribute("aria-current"),
    ).toBe("page");
    expect(screen.queryByRole("group", { name: "Macro 九族" })).toBeNull();
    expect(screen.getAllByRole("tab")).toHaveLength(3);
    expect(api.management?.instances).toEqual([]);
    expect(api.management?.entries).toHaveLength(28);
    expect(
      api.management?.entries.filter(
        (e) => e.asset?.identity === "mdf.macro.financial-conditions.daily",
      ),
    ).toHaveLength(6);
    expect(
      api.management?.entries.some(
        (e) => e.asset?.identity === "mdf.macro.census.durable-capital-goods",
      ),
    ).toBe(true);
    expect(
      api.management?.entries.some(
        (e) => e.asset?.identity === "mdf.macro.dol.claims",
      ),
    ).toBe(true);
    expect(
      api.management?.entries.every(
        (e) =>
          e.source_qualification === "UNKNOWN" &&
          e.current_binding === "UNKNOWN",
      ),
    ).toBe(true);
    expect(
      api.requests.every(
        (p) => !["/read?", "provider", "probe"].some((x) => p.includes(x)),
      ),
    ).toBe(true);
  },
);
it("groups the six registered financial paths without claiming qualification or acquiring data", async () => {
  const { api, transport } = bridge();
  const onSelect = vi.fn();
  render(
    <ManagementConsole
      transport={transport}
      generation={0}
      selectedIdentity={null}
      onSelect={onSelect}
    />,
  );
  const family = await screen.findByRole("button", {
    name: /金融条件日频（十项）/,
  });
  expect(family.textContent).toContain("6 合同 · UNKNOWN");
  fireEvent.click(family);
  const select = screen.getByLabelText("选择来源与能力");
  const identities = [
    "archive",
    "board",
    "derived",
    "fred",
    "nyfed",
    "treasury",
  ].map((path) => `mdf.source.financial-conditions.manual.${path}@r1`);
  expect(
    within(select)
      .getAllByRole("option")
      .map((option) => (option as HTMLOptionElement).value)
      .filter(Boolean)
      .sort(),
  ).toEqual(identities);
  fireEvent.change(select, { target: { value: identities[1] } });
  expect(onSelect).toHaveBeenCalledOnce();
  expect(onSelect.mock.calls[0][0]).toMatchObject({
    identity: identities[1],
    source_name: "Federal Reserve Board",
    source_qualification: "UNKNOWN",
    current_binding: "UNKNOWN",
  });
  expect(api.requests).toEqual(["/api/r3/management?"]);
});
it("normalizes the legacy code directory page into platform diagnostics without an extra menu", async () => {
  location.hash = "page=8";
  const { api, transport } = bridge();
  render(<App transport={transport} />);
  expect(
    screen
      .getByRole("tab", { name: "实时 / Socket / WS" })
      .getAttribute("aria-selected"),
  ).toBe("false");
  expect(
    within(screen.getByRole("navigation"))
      .getByRole("button", { name: "平台管理" })
      .getAttribute("aria-current"),
  ).toBe("page");
  expect(
    within(screen.getByRole("navigation")).getAllByRole("button"),
  ).toHaveLength(5);
  await screen.findByRole("button", { name: "刷新" });
  expect(location.hash).toBe("#page=8");
  expect(
    api.requests.every((p) => !p.includes("/read?") && !p.includes("jobs")),
  ).toBe(true);
});
it("component-filtered interface retains canonical identity and links platform records without acquisition", async () => {
  location.hash = "ui=r5&view=platform&tab=diagnostics&component=Macro";
  const { api, transport } = bridge();
  render(<App transport={transport} />);
  await screen.findByRole("region", { name: "注册接口" });
  expect(location.hash).toContain("component=Macro");
  await waitFor(() => expect(api.management).not.toBeNull());
  const entry = api.management!.entries.find(
    (e) => e.source_contract && e.web_read === "CPI_REQUEST_QUALIFIED",
  )!;
  fireEvent.click(
    within(screen.getByRole("region", { name: "注册接口" })).getByRole(
      "button",
      {
        name: new RegExp(entry.identity.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")),
      },
    ),
  );
  expect(location.hash).toContain(encodeURIComponent(entry.identity));
  fireEvent.click(
    within(screen.getByRole("navigation")).getByRole("button", {
      name: "平台管理",
    }),
  );
  await screen.findByText(/实例读取范围未配置/);
  expect(location.hash).toContain(encodeURIComponent(entry.identity));
  expect(api.requests.some((p) => /cpi\/(read|jobs|plans)/.test(p))).toBe(
    false,
  );
});

it.each(["fresh", "expired"])(
  "displays %s fixture observations and owner command restrictions without changing source qualification",
  async (scenario) => {
    location.hash = "page=7";
    const { api, transport } = bridge(scenario);
    const response = await transport("/api/r3/management", { method: "GET" });
    const data = await response.json();
    const record = data.instances[0].record;
    render(
      <ManagementConsole
        transport={transport}
        generation={0}
        selectedIdentity={record.catalog_identity}
        onSelect={() => {}}
      />,
    );
    await screen.findByText(/OFFLINE_FIXTURE · TRUSTED_READ/);
    const instance = within(
      screen.getByRole("region", { name: `实例 ${record.identity}` }),
    );
    expect(instance.getByText(/Owner 独立管理/)).toBeTruthy();
    expect(
      instance.getByText(
        scenario === "expired"
          ? "UNKNOWN · EXPIRED · expired"
          : "AVAILABLE · FRESH · observed",
      ),
    ).toBeTruthy();
    expect(
      instance
        .getByRole("button", { name: `控制实例 ${record.identity}` })
        .hasAttribute("disabled"),
    ).toBe(true);
    expect(screen.getByText("UNKNOWN / UNKNOWN")).toBeTruthy();
  },
);

it("expires an open observation without refreshing, probing or changing its timestamp", async () => {
  const { transport } = bridge("fresh");
  const response = await transport("/api/r3/management", { method: "GET" });
  const data = (await response.json()) as Management;
  const record = data.instances[0].record;
  const local = vi.fn(
    async () => ({ ok: true, json: async () => data }) as Response,
  );
  vi.useFakeTimers();
  await act(async () => {
    render(
      <ManagementConsole
        transport={local as Transport}
        generation={0}
        selectedIdentity={record.catalog_identity}
        onSelect={() => {}}
      />,
    );
  });
  const instance = within(
    screen.getByRole("region", { name: `实例 ${record.identity}` }),
  );
  expect(instance.getByText("AVAILABLE · FRESH · observed")).toBeTruthy();
  act(() => {
    vi.advanceTimersByTime(60000);
  });
  expect(instance.getByText("UNKNOWN · EXPIRED · expired")).toBeTruthy();
  expect(
    instance.getByText(
      `${formatBeijingTime(record.observed_at)} / ${formatBeijingTime(record.expires_at)}`,
    ),
  ).toBeTruthy();
  expect(local).toHaveBeenCalledTimes(1);
});

it("rejects a catalog operation linked to a mismatched asset revision", async () => {
  const { transport } = bridge();
  const response = await transport("/api/r3/management", { method: "GET" });
  const data = (await response.json()) as Management;
  const entry = data.entries.find((item) => item.asset)!;
  const snapshot = (async () =>
    ({ ok: true, json: async () => data }) as Response) as Transport;
  render(
    <ManagementConsole
      transport={snapshot}
      generation={0}
      selectedIdentity={entry.identity}
      selectedAsset={entry.asset!.identity}
      selectedRevision="wrong-revision"
      onSelect={() => {}}
    />,
  );
  await screen.findByText(/目录身份不匹配/);
  expect(screen.queryByRole("region", { name: "接入链详情" })).toBeNull();
  expect(
    (screen.getByLabelText("选择来源与能力") as HTMLSelectElement).value,
  ).toBe("");
});
