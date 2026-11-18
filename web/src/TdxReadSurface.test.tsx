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
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import App from "./App";
import { TDX_DAILY } from "./TdxDailyWorkbench";
import { TDX_ROSTER } from "./TdxRosterFamily";
import { keyOf } from "./workflowModel";
import { asgi, closeBridges } from "./workflowTestBridge";

let directory = "";
afterEach(() => {
  cleanup();
  closeBridges();
  if (directory) rmSync(directory, { recursive: true, force: true });
  directory = "";
  location.hash = "";
  sessionStorage.clear();
  localStorage.clear();
});
function prepare(mode: "surface" | "surface-repair", root: string) {
  const repo = resolve(process.cwd(), "..");
  const result = spawnSync(
    join(repo, ".venv/bin/python"),
    ["tests/prepare_tdx_job_read_fixture.py", root, mode],
    {
      cwd: repo,
      encoding: "utf8",
      env: { ...process.env, PYTHONPATH: `${repo}/src:${repo}` },
    },
  );
  expect(result.status, result.stderr).toBe(0);
}
function fixture() {
  directory = mkdtempSync(join(tmpdir(), "mdf-tdx-surface-"));
  const root = join(directory, "surface");
  prepare("surface", root);
  const ids: { daily: string; roster: string; corrupt: string; day: string } =
    JSON.parse(readFileSync(join(root, "fixture-only.json"), "utf8"));
  return { root, ids, bridge: asgi(root, "tdx_job_read_fixture.py") };
}
const families = [
  { asset: TDX_DAILY, field: "daily" as const, strategy: "tdx-daily-auto" },
  { asset: TDX_ROSTER, field: "roster" as const, strategy: "tdx-roster-auto" },
];
for (const { asset, field, strategy } of families) {
  it.each(["all", keyOf(asset)])(
    `keeps healthy ${field} rows clickable, shows shared blocking evidence and clears after repair (%s)`,
    async (filter) => {
      const f = fixture();
      location.hash = new URLSearchParams({
        ui: "r5",
        view: "collection",
        tab: "executions",
        asset: asset.identity,
        asset_revision: asset.revision,
        execution_asset: filter,
      }).toString();
      render(<App transport={f.bridge.transport} />);
      const warning = await screen.findByRole("alert", {
        name: "采集记录局部读取失败",
      });
      expect(within(warning).getAllByText(f.ids.corrupt)).toHaveLength(1);
      expect(warning.textContent).toContain("tdx_job_unreadable");
      expect(warning.textContent).toContain("JSONDecodeError");
      expect(warning.textContent).toContain("资产、来源和状态尚未核实");
      const table = screen.getByRole("region", { name: "执行记录" });
      expect(
        table.querySelector(`tr[data-execution-id="${f.ids.corrupt}"]`),
      ).toBeNull();
      const row = table.querySelector<HTMLElement>(
        `tr[data-execution-id="${f.ids[field]}"]`,
      )!;
      expect(row).toBeTruthy();
      fireEvent.click(within(row).getByRole("button"));
      let shell = await screen.findByRole("region", { name: "执行详情主体" });
      expect(shell.dataset.executionId).toBe(f.ids[field]);
      expect(shell.dataset.executionAsset).toBe(keyOf(asset));
      expect(
        table
          .querySelector('tr[aria-selected="true"]')
          ?.getAttribute("data-execution-id"),
      ).toBe(f.ids[field]);
      expect(within(shell).getByText("手动采集", { exact: true })).toBeTruthy();
      fireEvent.click(screen.getByRole("tab", { name: "新建采集" }));
      await waitFor(() => {
        const briefs = screen.getByRole("complementary", { name: "执行简报" });
        expect(
          within(briefs).getByRole("alert", { name: "采集记录局部读取失败" }),
        ).toBeTruthy();
        expect(
          within(briefs).getByRole("button", {
            name: new RegExp(f.ids[field]),
          }),
        ).toBeTruthy();
      });
      // A formerly verified receipt must lose its card/actions when unreadable.
      const healthyPath = join(f.root, "journal", f.ids[field] + ".json");
      const healthyBytes = readFileSync(healthyPath);
      writeFileSync(healthyPath, "{");
      fireEvent.click(screen.getByRole("button", { name: "刷新" }));
      await waitFor(() => {
        expect(
          within(
            screen.getByRole("complementary", { name: "执行简报" }),
          ).getByRole("alert", { name: "采集记录局部读取失败" }).textContent,
        ).toContain(f.ids[field]);
        expect(
          screen.queryByRole("button", { name: new RegExp(f.ids[field]) }),
        ).toBeNull();
      });
      writeFileSync(healthyPath, healthyBytes);
      fireEvent.click(screen.getByRole("button", { name: "刷新" }));
      const card = await waitFor(() =>
        within(
          screen.getByRole("complementary", { name: "执行简报" }),
        ).getByRole("button", { name: new RegExp(f.ids[field]) }),
      );
      fireEvent.click(card);
      shell = await screen.findByRole("region", { name: "执行详情主体" });
      expect(shell.dataset.executionId).toBe(f.ids[field]);
      fireEvent.click(
        within(shell).getByRole("button", { name: "查看周期任务" }),
      );
      await waitFor(() => {
        expect(
          screen.getByLabelText("周期操作").getAttribute("data-periodic-id"),
        ).toBe(strategy);
      });
      const block = await screen.findByRole("alert", {
        name: "共享日线与名册策略阻断",
      });
      expect(within(block).getByText(f.ids.corrupt)).toBeTruthy();
      expect(within(block).getByText(f.ids.day)).toBeTruthy();
      expect(block.textContent).toContain("tdx_periodic_roster_job_unreadable");
      expect(block.textContent).toContain("JSONDecodeError");
      expect(
        within(block).getByText("发现时间（北京时间）").closest(".fact")
          ?.textContent,
      ).not.toContain("未记录");
      expect(within(block).queryByRole("button")).toBeNull();
      prepare("surface-repair", f.root);
      fireEvent.click(screen.getByRole("button", { name: "刷新" }));
      await waitFor(() => {
        expect(
          screen.getByLabelText("周期操作").getAttribute("data-periodic-id"),
        ).toBe(strategy);
        expect(
          screen.queryByRole("alert", { name: "共享日线与名册策略阻断" }),
        ).toBeNull();
      });
      fireEvent.click(screen.getByRole("button", { name: "返回此执行记录" }));
      shell = await screen.findByRole("region", { name: "执行详情主体" });
      expect(shell.dataset.executionId).toBe(f.ids[field]);
      await waitFor(() => {
        expect(
          screen
            .getByRole("region", { name: "执行记录" })
            .querySelector('tr[aria-selected="true"]')
            ?.getAttribute("data-execution-id"),
        ).toBe(f.ids[field]);
        expect(
          screen.queryByRole("alert", { name: "采集记录局部读取失败" }),
        ).toBeNull();
      });
      expect(new URLSearchParams(location.hash.slice(1)).get("job")).toBe(
        f.ids[field],
      );
      expect(f.bridge.requests.every((r) => r.method === "GET")).toBe(true);
      expect(
        f.bridge.requests.filter((r) =>
          r.path.includes("/jobs/" + f.ids.corrupt),
        ),
      ).toHaveLength(0);
      expect(
        readFileSync(join(f.root, "journal", f.ids.corrupt + ".json")),
      ).toEqual(readFileSync(join(f.root, "repair-header.bin")));
    },
  );
  it(`keeps a corrupt ${field} deep link unverified without borrowing another record or offering actions`, async () => {
    const f = fixture();
    location.hash = new URLSearchParams({
      ui: "r5",
      view: "collection",
      tab: "executions",
      asset: asset.identity,
      asset_revision: asset.revision,
      execution_asset: keyOf(asset),
      job: f.ids.corrupt,
    }).toString();
    render(<App transport={f.bridge.transport} />);
    const failure = await screen.findByText(
      "请求的 TDX 执行记录当前不可核实，未改选其他任务。",
    );
    expect(failure.closest('[role="alert"]')?.textContent).toContain(
      f.ids.corrupt,
    );
    expect(screen.queryByRole("region", { name: "执行详情主体" })).toBeNull();
    expect(screen.queryByRole("button", { name: "查看资产数据" })).toBeNull();
    expect(
      screen.queryByRole("button", {
        name: /^(续跑未完成分片|按原请求恢复名册刷新)$/,
      }),
    ).toBeNull();
    expect(new URLSearchParams(location.hash.slice(1)).get("job")).toBe(
      f.ids.corrupt,
    );
    expect(f.bridge.requests.every((r) => r.method === "GET")).toBe(true);
  });
}
