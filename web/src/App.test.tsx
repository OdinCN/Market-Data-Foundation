import { afterEach, describe, expect, it } from "vitest";
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
import type { Transport } from "./api";

const query = { reference: "2026-08", as_of: "2026-10-06T00:00:10+00:00" };
const bridges: ChildProcessWithoutNullStreams[] = [];

// A persistent Python process runs TestClient against the actual FastAPI app,
// rebuilding admitted canonical fixtures. No TCP socket or frontend mock JSON.
function bridge(scenario = "available") {
  const root = resolve(process.cwd(), "..") + "/";
  const child = spawn(
    `${root}.venv/bin/python`,
    ["-u", "tests/r5_ui_exchange.py", scenario],
    {
      cwd: root,
      env: { ...process.env, PYTHONPATH: `${root}src:${root}` },
    },
  );
  bridges.push(child);
  const pending: {
    resolve: (value: unknown) => void;
    reject: (error: Error) => void;
  }[] = [];
  const requests: string[] = [];
  const lines = createInterface({ input: child.stdout });
  lines.on("line", (line) => pending.shift()?.resolve(JSON.parse(line)));
  let errors = "";
  child.stderr.on("data", (chunk) => {
    errors += String(chunk);
  });
  child.on("exit", (code) => {
    if (code !== 0)
      pending
        .splice(0)
        .forEach((item) =>
          item.reject(new Error(`ASGI bridge exited ${code}: ${errors}`)),
        );
  });
  const transport = (async (
    input: string | URL | Request,
    init?: RequestInit,
  ) => {
    expect(init?.method).toBe("GET");
    const path = String(input);
    requests.push(path);
    const value = (await new Promise<unknown>((resolve, reject) => {
      pending.push({ resolve, reject });
      child.stdin.write(JSON.stringify({ path }) + "\n");
    })) as { status: number; body: unknown };
    return {
      ok: value.status >= 200 && value.status < 300,
      status: value.status,
      json: async () => value.body,
    } as Response;
  }) as Transport;
  return { transport, requests };
}

afterEach(() => {
  cleanup();
  bridges.splice(0).forEach((child) => {
    child.stdin.end();
    child.kill();
  });
  location.hash = "";
});

async function available() {
  await screen.findByText(/离线合成 fixture/);
  await screen.findByRole("button", { name: "查看关联资产" });
}
function button(name: string) {
  return screen.getByRole("button", { name });
}
function go(name: string) {
  fireEvent.click(
    within(screen.getByRole("navigation")).getByRole("button", { name }),
  );
}

describe("CPI UI → ASGI → canonical reader", () => {
  it("keeps seven entries, identity through 03→04→06, both readers and themes", async () => {
    const api = bridge();
    render(<App transport={api.transport} initialQuery={query} />);
    await available();
    expect(
      within(screen.getByRole("navigation")).getAllByRole("button"),
    ).toHaveLength(7);
    fireEvent.click(button("查看关联资产"));
    expect(screen.getByTestId("asset-identity").textContent).toBe(
      "mdf.macro.bls.cpi.headlines",
    );
    expect(location.hash).toContain("asset=mdf.macro.bls.cpi.headlines");
    await screen.findByRole("heading", { name: "本地数据预览" });
    fireEvent.click(button("查看数据服务"));
    expect(screen.getByRole("heading", { name: "本地服务读取" })).toBeTruthy();
    expect(screen.getByTestId("asset-identity").textContent).toBe(
      "mdf.macro.bls.cpi.headlines",
    );
    fireEvent.click(button("读取本地数据"));
    await screen.findByText(/读取成功/);
    expect(screen.getByText("CPI 同比")).toBeTruthy();
    expect(screen.getByText("3.4")).toBeTruthy();
    expect(screen.getByText("2.4")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("读取方式"), {
      target: { value: "exact_version" },
    });
    expect(screen.queryByText(/读取成功/)).toBeNull();
    fireEvent.click(button("读取本地数据"));
    await screen.findByText(/读取成功/);
    expect(
      api.requests.some(
        (path) =>
          path.includes("mode=exact_version") &&
          path.includes("version_identity=p6-cpi-live"),
      ),
    ).toBe(true);
    // Browser history/hash changes clear prior reads just like business links.
    act(() => {
      location.hash = "page=3&asset=mdf.macro.bls.cpi.headlines";
    });
    await screen.findByRole("heading", { name: "本地数据预览" });
    expect(screen.queryByText(/读取成功/)).toBeNull();
    fireEvent.click(button("切换主题"));
    expect(document.querySelector(".shell.dark")).toBeTruthy();
    fireEvent.click(button("收起侧栏"));
    expect(button("展开侧栏")).toBeTruthy();
    go("采集任务");
    expect(
      screen.getByRole("heading", { name: "本模块尚未实现" }),
    ).toBeTruthy();
    expect(screen.getByText("新建采集 / 周期任务 / 执行记录")).toBeTruthy();
    expect(screen.queryByText(/读取成功/)).toBeNull();
    expect(api.requests).toHaveLength(4);
    expect(
      api.requests.filter((path) => path.startsWith("/api/r5/sources")),
    ).toHaveLength(1);
  });

  it.each([
    "unconfigured",
    "empty",
    "missing_content",
    "withdrawn",
    "ambiguous",
    "partial",
    "over_budget",
    "content_storage_fault",
    "scope_exit_fault",
  ])("does not fabricate a service: %s", async (scenario) => {
    const api = bridge(scenario);
    render(<App transport={api.transport} initialQuery={query} />);
    await waitFor(() =>
      expect(button("刷新").hasAttribute("disabled")).toBe(false),
    );
    const noAsset = [
      "unconfigured",
      "over_budget",
      "content_storage_fault",
      "scope_exit_fault",
    ].includes(scenario);
    expect(button("查看关联资产").hasAttribute("disabled")).toBe(noAsset);
    go("数据资产");
    if (noAsset) {
      expect(screen.getByText(/关联资产不可核实/)).toBeTruthy();
      expect(screen.getByText("没有可核实的版本")).toBeTruthy();
      expect(screen.queryByText("p6-cpi-live:version")).toBeNull();
      expect(screen.queryByText(/private-sensitive/)).toBeNull();
      expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
    } else {
      expect(button("查看数据服务").hasAttribute("disabled")).toBe(true);
    }
    go("数据服务");
    expect(screen.getByText(/没有满足当前条件的数据服务/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
  });

  it.each(["content_storage_fault", "scope_exit_fault"])(
    "clears verified asset, publication and preview after refresh encounters %s",
    async (scenario) => {
      const healthy = bridge();
      const failing = bridge(scenario);
      let fault = false;
      const transport = ((...args: Parameters<Transport>) =>
        (fault ? failing : healthy).transport(...args)) as Transport;
      render(<App transport={transport} initialQuery={query} />);
      await available();
      fireEvent.click(button("查看关联资产"));
      fireEvent.click(button("读取本地数据"));
      await screen.findByText(/读取成功/);
      expect(screen.getAllByText("p6-cpi-live:version").length).toBeGreaterThan(
        0,
      );
      fault = true;
      fireEvent.click(button("刷新"));
      await screen.findByText(/关联资产不可核实 · storage_unavailable/);
      expect(screen.queryByText(/读取成功/)).toBeNull();
      expect(screen.queryByText("p6-cpi-live:version")).toBeNull();
      expect(screen.getByText("没有可核实的版本")).toBeTruthy();
      expect(button("查看数据服务").hasAttribute("disabled")).toBe(true);
      expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
      go("接口注册");
      expect(button("查看关联资产").hasAttribute("disabled")).toBe(true);
      go("数据服务");
      expect(screen.getByText(/没有满足当前条件的数据服务/)).toBeTruthy();
      expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
    },
  );

  it("shows stale reference and exact miss; applying conditions/refresh clears old results", async () => {
    const api = bridge();
    render(<App transport={api.transport} initialQuery={query} />);
    await available();
    fireEvent.click(button("查看关联资产"));
    fireEvent.click(button("读取本地数据"));
    await screen.findByText(/读取成功/);
    fireEvent.change(screen.getByLabelText("读取方式"), {
      target: { value: "exact_version" },
    });
    fireEvent.change(screen.getByLabelText("版本标识"), {
      target: { value: "absent" },
    });
    fireEvent.click(button("读取本地数据"));
    await screen.findByRole("alert");
    expect(screen.getByRole("alert").textContent).toContain(
      "version_not_found",
    );
    fireEvent.change(screen.getByLabelText("报告月份"), {
      target: { value: "2026-09" },
    });
    fireEvent.click(button("应用读取条件"));
    await screen.findByText("reference_stale_or_mismatched");
    expect(button("查看数据服务").hasAttribute("disabled")).toBe(true);
    expect(screen.queryByText(/读取成功/)).toBeNull();
    fireEvent.click(button("刷新"));
    await screen.findByText("reference_stale_or_mismatched");
    expect(api.requests.every((path) => !path.includes("jobs"))).toBe(true);
  });

  it("rejects missing cross-page identities and makes unfinished modules explicit", async () => {
    location.hash = "page=3&asset=missing";
    const api = bridge();
    render(<App transport={api.transport} initialQuery={query} />);
    await screen.findByRole("alert");
    expect(screen.getByRole("alert").textContent).toContain("关联对象缺失");
    expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
    for (const name of ["运行总览", "平台管理"]) {
      go(name);
      await screen.findByText(/UNCONFIGURED · UNKNOWN/);
      expect(screen.getByLabelText("运行观测")).toBeTruthy();
    }
  });

  it("discards delayed results after refresh", async () => {
    const api = bridge();
    let release: (() => void) | undefined;
    const transport = (async (...args: Parameters<Transport>) => {
      const response = await api.transport(...args);
      if (String(args[0]).includes("/read?"))
        await new Promise<void>((resolve) => {
          release = resolve;
        });
      return response;
    }) as Transport;
    render(<App transport={transport} initialQuery={query} />);
    await available();
    fireEvent.click(button("查看关联资产"));
    fireEvent.click(button("读取本地数据"));
    await waitFor(() => expect(release).toBeTypeOf("function"));
    fireEvent.click(button("刷新"));
    await screen.findByText(/离线合成 fixture/);
    await act(async () => {
      release!();
    });
    expect(screen.queryByText(/读取成功/)).toBeNull();
    expect(button("读取本地数据").hasAttribute("disabled")).toBe(false);
  });

  it("uses one KD B043 identity across access, interfaces, assets and services without admission", async () => {
    location.hash = "page=1";
    const api = bridge();
    render(<App transport={api.transport} initialQuery={query} />);
    const select = await screen.findByRole("button", {
      name: /选择 sector-analysis.current_rank/,
    });
    fireEvent.click(select);
    const identity = screen.getByTestId("operation-identity").textContent;
    expect(identity).toContain("KD_B043");
    expect(screen.getByText("kd_runtime_descriptor_required")).toBeTruthy();
    expect(screen.getByText("sampled_current_state_history_only")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
    go("接口注册");
    expect(screen.getByTestId("operation-identity").textContent).toBe(identity);
    fireEvent.click(button("查看关联资产"));
    expect(location.hash).toContain(
      "asset=mdf.kd.sector-analysis.current-rank",
    );
    expect(screen.getByTestId("directory-asset-identity").textContent).toBe(
      "mdf.kd.sector-analysis.current-rank@p3-r1",
    );
    expect(button("查看数据服务").hasAttribute("disabled")).toBe(true);
    go("数据服务");
    expect(screen.getByTestId("operation-identity").textContent).toBe(identity);
    expect(
      screen.getByText(/独立 Macro \/ TDX 合同与 KD 候选不能冒充已开放服务/),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
    expect(api.requests.every((path) => !path.includes("/read?"))).toBe(true);
  });

  it("keeps B044 unadmitted, without a fabricated Source or asset", async () => {
    const api = bridge();
    render(<App transport={api.transport} initialQuery={query} />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: /选择 market-stock-ranking.current_a_share/,
      }),
    );
    const detail = within(screen.getByRole("region", { name: "具名操作详情" }));
    expect(detail.getByText(/缺少 SourceOperationRef/)).toBeTruthy();
    expect(detail.getByText(/无已 admitted Definition/)).toBeTruthy();
    expect(button("查看关联资产").hasAttribute("disabled")).toBe(true);
    expect(detail.getByText(/NOT_ADMITTED_SELF_BLOCKED/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
  });

  it.each([/选择 timeseries.data:PPI-final-demand/, /选择 bars.daily/])(
    "preserves independent Macro/TDX mapping boundaries: %s",
    async (name) => {
      const api = bridge();
      render(<App transport={api.transport} initialQuery={query} />);
      fireEvent.click(await screen.findByRole("button", { name }));
      const identity = screen.getByTestId("operation-identity").textContent;
      const asset = screen.getByTestId("directory-asset-identity").textContent;
      fireEvent.click(button("查看关联资产"));
      expect(screen.getByTestId("operation-identity").textContent).toBe(
        identity,
      );
      expect(screen.getByTestId("directory-asset-identity").textContent).toBe(
        asset,
      );
      expect(button("查看数据服务").hasAttribute("disabled")).toBe(true);
      expect(screen.getByText("DOMAIN_SPECIFIC_ONLY")).toBeTruthy();
      expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
    },
  );

  it("links a direct CPI contract to the existing request-qualified asset and service", async () => {
    const api = bridge();
    render(<App transport={api.transport} initialQuery={query} />);
    const choices = await screen.findAllByRole("button", {
      name: /选择 timeseries.data:CPI/,
    });
    fireEvent.click(choices[0]);
    await available();
    const operation = screen.getByTestId("operation-identity").textContent;
    fireEvent.click(button("查看关联资产"));
    expect(screen.getByTestId("asset-identity").textContent).toBe(
      "mdf.macro.bls.cpi.headlines",
    );
    expect(screen.getByTestId("operation-identity").textContent).toBe(
      operation,
    );
    fireEvent.click(button("查看数据服务"));
    expect(screen.getByTestId("operation-identity").textContent).toBe(
      operation,
    );
    expect(screen.getByText(/Version 血缘为准/)).toBeTruthy();
    fireEvent.click(button("读取本地数据"));
    await screen.findByText(/读取成功/);
  });

  it.each(["missing", "market_data_foundation.domain_contracts.kd:KD_B043"])(
    "fails closed for an unknown or mismatched operation route: %s",
    async (operation) => {
      location.hash = `page=3&asset=mdf.macro.bls.cpi.headlines&operation=${encodeURIComponent(operation)}`;
      const api = bridge();
      render(<App transport={api.transport} initialQuery={query} />);
      await screen.findByText(/关联对象缺失：未找到请求的具名来源操作或资产/);
      expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
      expect(screen.queryByTestId("operation-identity")).toBeNull();
    },
  );

  it("clears directory details when refresh fails", async () => {
    const api = bridge();
    let fault = false;
    const transport = (async (...args: Parameters<Transport>) => {
      if (fault && String(args[0]).startsWith("/api/r5/sources")) {
        return {
          ok: false,
          json: async () => ({
            status: "UNKNOWN",
            classification: "storage_unavailable",
          }),
        } as Response;
      }
      return api.transport(...args);
    }) as Transport;
    render(<App transport={transport} initialQuery={query} />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: /选择 sector-analysis.current_rank/,
      }),
    );
    expect(screen.getByTestId("operation-identity")).toBeTruthy();
    await waitFor(() =>
      expect(button("刷新").hasAttribute("disabled")).toBe(false),
    );
    fault = true;
    fireEvent.click(button("刷新"));
    await screen.findByText(/目录不可核实 · storage_unavailable/);
    expect(screen.queryByTestId("operation-identity")).toBeNull();
    expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
  });
});
