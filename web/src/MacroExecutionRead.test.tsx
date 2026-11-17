import { afterEach, expect, it } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import App from "./App";
import type { Transport } from "./api";
import { CPI_PROFILE } from "./macroTimeSeriesProfile";
import { collectionRegistrations } from "./collectionRegistry";
import { RUNTIME_FAMILIES } from "./runtimeFamilies";
import { expectedPolicy } from "../scripts/collection-protocol-evidence";
import { readExecutionProtocol } from "./cpiTimeSeriesApi";
import { keyOf } from "./workflowModel";
import { asgi, closeBridges } from "./workflowTestBridge";

let root = "";
afterEach(() => {
  cleanup();
  closeBridges();
  if (root) rmSync(root, { recursive: true, force: true });
  root = "";
  location.hash = "";
  sessionStorage.clear();
  localStorage.clear();
});
const pending = () => new Promise<Response>(() => {});

it.each(
  collectionRegistrations(RUNTIME_FAMILIES).filter(
    (row) => row.protocol === "macro" && !!row.profile,
  ),
)(
  "$profile.id opens failed original details and the real policy before aggregates or optional observations",
  async (row) => {
    const profile = row.profile!;
    root = mkdtempSync(join(tmpdir(), "mdf-d01-macro-read-"));
    const directory = asgi("financial_manual", "d01_ui_exchange.py", {
      store_dir: root,
    });
    const backend = asgi(profile.id, "collection_submit_fixture.py");
    const expected = await expectedPolicy(row, backend.exchange);
    expect(row.strategy).toEqual(expected);
    const inventory = (await backend.exchange({ control: "inventory" })).body;
    const capability = inventory.find(
      (c: any) => keyOf(c.asset) === keyOf(profile.asset),
    );
    expect(capability).toBeTruthy();
    expect(
      (
        await directory.exchange({
          method: "PUT",
          path: "/api/r5/packages/macro",
          body: {
            expected_revision: null,
            enabled_capability_ids: [capability.capability],
            source_changes: [],
          },
        })
      ).status,
    ).toBe(200);
    const id = "00000000-0000-4000-8000-000000000717";
    const period =
      profile.id === "gdp"
        ? "2026-Q3"
        : profile.id === "claims"
          ? "2026-10-31"
          : "2026-09";
    const job = {
      id,
      job_id: id,
      request_id: id,
      asset: profile.id,
      origin: profile.id === "claims" ? "schedule" : "manual",
      action: "update",
      start: period,
      end: period,
      state: "failed",
      created_at: "2026-11-16T00:00:00Z",
      started_at: "2026-11-16T00:00:00Z",
      finished_at: "2026-11-16T00:01:00Z",
      classification: "source_temporarily_unavailable",
      result: null,
    };
    const calls: { path: string; method: string; minimal: boolean }[] = [];
    const transport: Transport = async (input, init) => {
      const path = String(input),
        minimal = new Headers(init?.headers).get("Prefer") === "return=minimal";
      calls.push({ path, method: init?.method ?? "GET", minimal });
      if (/^\/api\/r5\/(sources|packages)/.test(path))
        return directory.transport(input, init);
      if (path.endsWith("/status")) return pending();
      if (path === profile.basePath + "/executions")
        return Response.json({ executions: [job] });
      if (path === profile.basePath + "/jobs/" + id) return Response.json(job);
      if (path === profile.basePath + "/jobs")
        return Response.json({ jobs: [job] });
      if (path === profile.basePath + "/strategy")
        return minimal ? backend.transport(input, init) : pending();
      if (
        path === profile.basePath + "/worker" ||
        path === profile.basePath + "/schedule"
      )
        return pending();
      return Response.json({ classification: "not_enabled" }, { status: 404 });
    };
    location.hash = new URLSearchParams({
      ui: "r5",
      view: "collection",
      tab: "executions",
      asset: profile.asset.identity,
      asset_revision: profile.asset.revision,
      execution_asset: "all",
      job: id,
    }).toString();
    render(<App transport={transport} />);
    const shell = await screen.findByRole("region", { name: "执行详情主体" });
    expect(shell.dataset.executionId).toBe(id);
    expect(shell.dataset.executionAsset).toBe(keyOf(profile.asset));
    expect(within(shell).getByText("失败", { exact: true })).toBeTruthy();
    if (expected) {
      fireEvent.click(
        await within(shell).findByRole("button", {
          name: "查看周期任务",
        }),
      );
      const policy = await screen.findByLabelText("周期操作");
      expect(policy.dataset.periodicId).toBe(expected.id);
      const route = new URLSearchParams(location.hash.slice(1));
      expect(route.get("periodic_asset")).toBe(keyOf(profile.asset));
      expect(route.get("schedule")).toBe(expected.id);
      fireEvent.click(screen.getByRole("button", { name: "返回此执行记录" }));
      await act(async () => {});
      expect(
        screen.getByRole("region", { name: "执行详情主体" }).dataset
          .executionId,
      ).toBe(id);
    } else {
      await act(async () => {});
      expect(
        within(shell).queryByRole("button", { name: "查看周期任务" }),
      ).toBeNull();
    }
    expect(
      calls
        .filter((c) => c.path.endsWith("/timeseries/status"))
        .every((c) => c.path === profile.basePath + "/status"),
    ).toBe(true);
    expect(
      calls.some((c) => c.path === profile.basePath + "/strategy" && c.minimal),
    ).toBe(!!expected);
    expect(calls.every((c) => c.method === "GET")).toBe(true);
  },
);

it("a transient execution read failure cannot masquerade as an empty legacy list", async () => {
  await expect(
    readExecutionProtocol(
      async () =>
        Response.json(
          { classification: "storage_read_failed" },
          { status: 503 },
        ),
      new AbortController().signal,
      CPI_PROFILE,
    ),
  ).rejects.toMatchObject({
    status: 503,
    classification: "storage_read_failed",
  });
});
