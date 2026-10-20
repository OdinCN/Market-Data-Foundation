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
import type { Catalog, Directory, Transport } from "./api";

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
  it.each([
    ["over_budget", query.reference, "p6-cpi-live:version"],
    ["history_over_budget", "2025-09", "r5-cpi-history-budget-128-a1:version"],
  ])(
    "reads a named exact link despite the bounded catalog exceeding 128 versions: %s",
    async (scenario, reference, identity) => {
      location.hash = new URLSearchParams({
        page: "5",
        asset: "mdf.macro.bls.cpi.headlines",
        asset_revision: "p3-r1",
        reference,
        as_of: query.as_of,
        version: identity,
      }).toString();
      const api = bridge(scenario);
      render(<App transport={api.transport} />);
      await screen.findByText(
        /没有满足当前条件的数据服务/,
        {},
        { timeout: 10000 },
      );
      expect(
        screen.getAllByText(/operator_query_budget_exceeded/).length,
      ).toBeGreaterThan(0);
      expect(screen.getByText("没有可核实的版本")).toBeTruthy();
      expect(api.requests.some((path) => path.includes("/read?"))).toBe(false);
      const read = button("读取本地数据");
      expect(read.hasAttribute("disabled")).toBe(false);
      fireEvent.click(read);
      await screen.findByText(/读取成功/);
      expect(screen.getByText(identity)).toBeTruthy();
      expect(screen.getByText(/没有满足当前条件的数据服务/)).toBeTruthy();
      fireEvent.change(screen.getByLabelText("读取方式"), {
        target: { value: "as_of" },
      });
      expect(screen.queryByText(/读取成功/)).toBeNull();
      expect(read.hasAttribute("disabled")).toBe(true);
      fireEvent.change(screen.getByLabelText("读取方式"), {
        target: { value: "exact_version" },
      });
      expect(read.hasAttribute("disabled")).toBe(false);
      fireEvent.change(screen.getByLabelText("版本标识"), {
        target: { value: identity.slice(0, -1) },
      });
      expect(read.hasAttribute("disabled")).toBe(true);
      expect(
        api.requests.filter((path) => path.includes("/read?")),
      ).toHaveLength(1);
    },
  );

  it.each<Record<string, string>>([
    { asset: "missing" },
    { asset_revision: "wrong-revision" },
    { asset_revision: "" },
    { operation: "missing" },
    { operation: "market_data_foundation.domain_contracts.kd:KD_B043" },
  ])(
    "keeps exact-link identity gates when the catalog exceeds budget: %j",
    async (changes) => {
      location.hash = new URLSearchParams({
        page: "5",
        asset: "mdf.macro.bls.cpi.headlines",
        asset_revision: "p3-r1",
        ...query,
        version: "p6-cpi-live:version",
        ...changes,
      }).toString();
      const api = bridge("over_budget");
      render(<App transport={api.transport} />);
      await waitFor(
        () => expect(button("刷新").hasAttribute("disabled")).toBe(false),
        { timeout: 10000 },
      );
      expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
      expect(api.requests.some((path) => path.includes("/read?"))).toBe(false);
    },
  );

  it("leaves a nonexistent named version to the Reader without claiming service availability", async () => {
    location.hash = new URLSearchParams({
      page: "5",
      asset: "mdf.macro.bls.cpi.headlines",
      asset_revision: "p3-r1",
      ...query,
      version: "absent-version",
    }).toString();
    const api = bridge("over_budget");
    render(<App transport={api.transport} />);
    const read = await screen.findByRole(
      "button",
      { name: "读取本地数据" },
      { timeout: 10000 },
    );
    fireEvent.click(read);
    await screen.findByText(/NOT_AVAILABLE · version_not_found/);
    expect(screen.queryByText(/读取成功/)).toBeNull();
    expect(screen.getByText(/没有满足当前条件的数据服务/)).toBeTruthy();
    expect(api.requests.filter((path) => path.includes("/read?"))).toHaveLength(
      1,
    );
  });

  it("keeps page 06 controls usable while editing an exact link without an as-of service", async () => {
    location.hash = new URLSearchParams({
      page: "5",
      asset: "mdf.macro.bls.cpi.headlines",
      asset_revision: "p3-r1",
      reference: query.reference,
      as_of: query.as_of,
      version: "p6-cpi-live:version",
    }).toString();
    // Isolate React mounting and action gates from the separate TestClient bridge.
    const catalog: Catalog = {
      status: "NOT_AVAILABLE",
      classification: "as_of_ambiguous",
      evidence_mode: "OFFLINE_FIXTURE",
      ...query,
      definition: {
        identity: "mdf.macro.bls.cpi.headlines",
        revision: "p3-r1",
        title: "Synthetic CPI",
        registration: "CANONICAL_CONTRACT",
        source_qualified_contract: true,
        source_health: "UNKNOWN",
        binding_activation: "UNKNOWN",
      },
      asset: {
        identity: "mdf.macro.bls.cpi.headlines",
        definition_revision: "p3-r1",
        local_holding: "UNKNOWN",
        qualification: "as_of_ambiguous",
        versions: ["p6-cpi-live:version", "synthetic-second-version"].map(
          (identity) => ({
            identity,
            frozen_at: query.as_of,
            completeness: "synthetic-complete",
            selection_state: "eligible",
            binding_identity: null,
            binding_revision: null,
            publications: [],
          }),
        ),
      },
      service: null,
    };
    const directory: Directory = {
      status: "OK",
      evidence_mode: "CODE_CONTRACTS_ONLY",
      scope: "NAMED_R5_I2_ALLOWLIST",
      entries: [],
    };
    const requests: string[] = [];
    const api = {
      requests,
      transport: (async (input, init) => {
        expect(init?.method).toBe("GET");
        const path = String(input);
        requests.push(path);
        const isRead = path.startsWith("/api/r5/cpi/read?");
        return {
          ok: !isRead,
          json: async () =>
            isRead
              ? { status: "NOT_AVAILABLE", classification: "version_not_found" }
              : path.startsWith("/api/r5/sources?")
                ? directory
                : catalog,
        } as Response;
      }) as Transport,
    };
    render(<App transport={api.transport} />);
    await screen.findByRole("heading", { name: "本地服务读取" });
    expect(screen.getByText(/没有满足当前条件的数据服务/)).toBeTruthy();
    const mode = screen.getByLabelText("读取方式");
    const version = screen.getByLabelText("版本标识");
    const read = button("读取本地数据");
    expect(read.hasAttribute("disabled")).toBe(false);
    fireEvent.click(read);
    await screen.findByRole("alert");
    const before = api.requests.length;
    for (const value of ["p6-cpi-live:versio", ""]) {
      fireEvent.change(version, { target: { value } });
      expect(screen.getByLabelText("版本标识")).toBe(version);
      expect(screen.getByLabelText("读取方式")).toBe(mode);
      expect(read.hasAttribute("disabled")).toBe(true);
      expect(screen.queryByText(/读取成功/)).toBeNull();
      expect(screen.queryByRole("alert")).toBeNull();
      fireEvent.click(read);
    }
    fireEvent.change(version, { target: { value: "p6-cpi-live:version" } });
    expect(read.hasAttribute("disabled")).toBe(false);
    fireEvent.change(mode, { target: { value: "as_of" } });
    expect(screen.getByLabelText("读取方式")).toBe(mode);
    expect(read.hasAttribute("disabled")).toBe(true);
    fireEvent.click(read);
    expect(api.requests).toHaveLength(before);
    fireEvent.change(mode, { target: { value: "exact_version" } });
    expect(screen.getByLabelText("读取方式")).toBe(mode);
    expect(read.hasAttribute("disabled")).toBe(false);
    fireEvent.click(read);
    await screen.findByRole("alert");
    expect(screen.getByText(/没有满足当前条件的数据服务/)).toBeTruthy();
    expect(api.requests).toHaveLength(before + 1);
  });

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
    expect(api.requests.some((path) => path.includes("/plans"))).toBe(false);
    go("采集任务");
    expect(
      screen.getByRole("heading", { name: "CPI 人工一次性初始化" }),
    ).toBeTruthy();
    expect(screen.getAllByRole("tab")).toHaveLength(3);
    expect(button("持久提交一次性意图").hasAttribute("disabled")).toBe(true);
    expect(screen.queryByText(/读取成功/)).toBeNull();
    // The task page mounts both the legacy job view and history-plan form.
    // Its plan GET reads eligibility/state; it must never execute a plan.
    await waitFor(() => expect(api.requests).toHaveLength(6));
    expect(
      api.requests
        .map((path) => new URL(path, "http://fixture").pathname)
        .sort(),
    ).toEqual([
      "/api/r5/cpi",
      "/api/r5/cpi/jobs",
      "/api/r5/cpi/plans",
      "/api/r5/cpi/read",
      "/api/r5/cpi/read",
      "/api/r5/sources",
    ]);
    fireEvent.click(screen.getByRole("tab", { name: "周期任务" }));
    await screen.findByRole("heading", { name: "CPI 有界周期计划" });
    expect(button("持久提交历史计划").hasAttribute("disabled")).toBe(true);
    expect(api.requests).toHaveLength(6);
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
    if (noAsset) {
      expect(screen.queryByRole("button", { name: "读取本地数据" })).toBeNull();
    } else {
      expect(button("读取本地数据").hasAttribute("disabled")).toBe(true);
      expect(screen.getByLabelText("读取方式")).toBeTruthy();
    }
    expect(api.requests.every((path) => !path.includes("/read?"))).toBe(true);
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
      await within(screen.getByLabelText("运行观测")).findByText(
        "UNCONFIGURED · UNKNOWN",
        { selector: '[role="status"]' },
      );
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
