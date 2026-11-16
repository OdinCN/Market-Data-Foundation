import "./offlineTestSetup";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import App from "./App";
import type { Transport } from "./api";
import {
  collectionRegistrations,
  readonlyCollections,
  type CollectionRegistration,
} from "./collectionRegistry";
import { RUNTIME_FAMILIES } from "./runtimeFamilies";
import {
  HISTORY_OPERATION,
  MANUAL_OPERATION,
  PCE_OPERATION,
  keyOf,
} from "./workflowModel";
import { asgi, closeBridges } from "./workflowTestBridge";

const registrations = collectionRegistrations(RUNTIME_FAMILIES);
type Bridge = ReturnType<typeof asgi>;
type Driver = { bridge: () => Bridge; legacy?: true };
// Protocol adapters, not an asset whitelist. Every registration MUST get every scenario.
const drivers: Record<CollectionRegistration["protocol"], Driver> = {
  macro: {
    bridge: () => {
      throw Error("profile required");
    },
  },
  financial: {
    bridge: () => asgi("financial", "collection_submit_fixture.py"),
  },
  tdx: { bridge: () => asgi("tdx", "collection_submit_fixture.py") },
  roster: { bridge: () => asgi("roster", "collection_submit_fixture.py") },
  "legacy-cpi": {
    bridge: () => asgi("bound", "r5_jobs_ui_exchange.py"),
    legacy: true,
  },
  "legacy-pce": {
    bridge: () => asgi("once", "pce_native_ui_exchange.py"),
    legacy: true,
  },
  "legacy-plan": {
    bridge: () => asgi("default", "r5_schedules_ui_exchange.py"),
    legacy: true,
  },
};
export function requireScenarios(
  rows: readonly CollectionRegistration[],
  available = drivers,
) {
  for (const row of rows)
    if (!available[row.protocol])
      throw Error(
        `missing submit scenario: ${keyOf(row.asset)} ${row.variant}`,
      );
}
let roots: string[] = [];
type Proof = {
  asset: string;
  variant: string;
  scenario: string;
  [key: string]: unknown;
};
const proofs: Proof[] = [];
let directoryEntries: {
  asset: { identity: string; revision: string } | null;
}[] = [];
function requireEvidence(
  rows: readonly CollectionRegistration[],
  evidence: Proof[],
) {
  const missing: string[] = [];
  for (const row of rows)
    for (const scenario of ["accepted", "lost", "denied", "lifecycle"])
      if (
        !evidence.some(
          (p) =>
            p.asset === keyOf(row.asset) &&
            p.variant === row.variant &&
            p.scenario === scenario,
        )
      )
        missing.push(`${keyOf(row.asset)} ${row.variant} ${scenario}`);
  if (missing.length)
    throw Error(`missing completed scenario: ${missing.join("; ")}`);
}
let capabilities: {
  asset: { identity: string; revision: string };
  capability: string;
  package: string;
}[];
beforeAll(async () => {
  const inventory = asgi("cpi", "collection_submit_fixture.py");
  capabilities = (await inventory.exchange({ control: "inventory" })).body;
  directoryEntries = (await inventory.exchange({ control: "directory" })).body
    .entries;
  requireScenarios(registrations);
});
afterEach(() => {
  cleanup();
  closeBridges();
  localStorage.clear();
  sessionStorage.clear();
  location.hash = "";
  roots.forEach((p) => rmSync(p, { recursive: true, force: true }));
  roots = [];
});
afterAll(() => {
  if (process.env.MDF_SUBMIT_EVIDENCE)
    writeFileSync(
      process.env.MDF_SUBMIT_EVIDENCE,
      JSON.stringify(
        {
          capabilities,
          directoryEntries,
          readonly: readonlyCollections,
          registrations: registrations.map(({ profile, ...r }) => ({
            ...r,
            profile: profile?.id,
          })),
          proofs,
        },
        null,
        2,
      ),
    );
  requireEvidence(registrations, proofs);
});
const submitButton = () =>
  screen.getByRole("button", {
    name: /^(提交 .+ 采集|提交采集任务|创建采集任务)$/,
  }) as HTMLButtonElement;
const tab = () => new URLSearchParams(location.hash.slice(1)).get("tab");
async function selectedShell(row: CollectionRegistration, id: string) {
  await waitFor(
    () => {
      const shell = screen.getByRole("region", { name: "执行详情主体" });
      expect(shell.dataset.executionId).toBe(id);
      expect(shell.dataset.executionAsset).toBe(keyOf(row.asset));
      const selected = screen
        .getByRole("region", { name: "执行记录" })
        .querySelector<HTMLElement>('tr[aria-selected="true"]');
      expect(selected?.dataset.executionId).toBe(id);
      expect(selected?.dataset.executionAsset).toBe(keyOf(row.asset));
      const route = new URLSearchParams(location.hash.slice(1));
      expect(route.get("job")).toBe(id);
      expect(route.get("asset")).toBe(row.asset.identity);
    },
    { timeout: 15000 },
  );
  return screen.getByRole("region", { name: "执行详情主体" });
}
async function setup(
  row: CollectionRegistration,
  lost = false,
  denied = false,
) {
  let backend =
    row.protocol === "macro"
      ? asgi(row.profile!.id, "collection_submit_fixture.py")
      : drivers[row.protocol].bridge();
  if (row.protocol === "legacy-cpi" && row.variant === "legacy-backfill")
    backend = asgi("ready", "cpi_r8_ui_exchange.py");
  if (row.protocol === "legacy-pce" && row.variant === "legacy-backfill")
    backend = asgi("backfill", "pce_native_ui_exchange.py");
  const fields: Record<string, string> = {
    ui: "r5",
    view: "collection",
    tab: "new",
    asset: row.asset.identity,
    asset_revision: row.asset.revision,
  };
  let reference = "2026-09";
  if (row.protocol.startsWith("legacy")) {
    fields.operation =
      row.protocol === "legacy-pce"
        ? PCE_OPERATION
        : row.protocol === "legacy-plan"
          ? HISTORY_OPERATION
          : MANUAL_OPERATION;
    if (row.protocol === "legacy-plan") {
      const created = await backend.exchange({
        method: "POST",
        path: "/api/r5/cpi/plans",
        body: {
          plan_identity: "submit-plan",
          asset_identity: row.asset.identity,
          asset_revision: row.asset.revision,
          name: "submit-plan",
          start: "2024-12",
          end: "2025-01",
          timezone: "Asia/Shanghai",
          window_start: "19:00",
          window_end: "22:00",
          first_due: "2028-03-10T12:00:00+00:00",
          interval_seconds: 86400,
          cycles: 1,
        },
      });
      expect(created.status).toBe(202);
      await backend.exchange({
        control: "tick",
        plan: "submit-plan",
        failure: true,
      });
      const plans = await backend.exchange({ path: "/api/r5/cpi/plans" });
      const child = plans.body.plans[0].batches
        .flatMap((b: any) => b.jobs)
        .find((j: any) => j.state === "failed");
      expect(child).toBeTruthy();
      fields.job = child.job_identity;
      fields.mode = "backfill";
      reference = child.reference ?? "2024-12";
    } else if (row.variant === "legacy-backfill") {
      const data = await backend.exchange({
        path:
          row.protocol === "legacy-pce" ? "/api/pce/jobs" : "/api/r5/cpi/jobs",
      });
      fields.job = data.body.backfill.submission.parent_job_id;
      fields.mode = "backfill";
      reference =
        row.protocol === "legacy-pce" ? "2026-07" : data.body.reference;
    } else if (row.protocol === "legacy-cpi") {
      reference = (await backend.exchange({ path: "/api/r5/cpi/jobs" })).body
        .reference;
    } else reference = "2026-07";
  }
  let directory: Bridge | undefined;
  if (!row.protocol.startsWith("legacy")) {
    const root = mkdtempSync(join(tmpdir(), "mdf-d01-submit-"));
    roots.push(root);
    directory = asgi("financial_manual", "d01_ui_exchange.py", {
      store_dir: root,
    });
    const cap = capabilities.find((c) => keyOf(c.asset) === keyOf(row.asset));
    expect(cap).toBeTruthy();
    const saved = await directory.exchange({
      method: "PUT",
      path: `/api/r5/packages/${cap!.package}`,
      body: {
        expected_revision: null,
        enabled_capability_ids: [cap!.capability],
        source_changes: [],
      },
    });
    expect(saved.status).toBe(200);
  }
  const calls: { url: string; method: string; body?: any; status?: number }[] =
    [];
  let posted = false,
    allowRead = !lost;
  const transport: Transport = async (input, init) => {
    const url = String(input),
      method = init?.method ?? "GET",
      body = init?.body ? JSON.parse(String(init.body)) : undefined;
    const call = { url, method, body, status: undefined as number | undefined };
    calls.push(call);
    if (row.protocol.startsWith("legacy") && /\/timeseries\/status$/.test(url))
      return Response.json({ classification: "not_enabled" }, { status: 404 });
    let response: Response;
    if (directory && /^\/api\/r5\/(packages|sources)/.test(url))
      response = await directory.transport(input, init);
    else if (directory && !url.startsWith(row.path.replace(/\/jobs$/, "")))
      return Response.json({ classification: "not_enabled" }, { status: 404 });
    else {
      if (
        posted &&
        !allowRead &&
        method === "GET" &&
        /\/jobs(?:\?|\/|$)|\/plans/.test(url)
      )
        return Response.json(
          { classification: "fixture_read_delayed" },
          { status: 503 },
        );
      response = await backend.transport(input, init);
    }
    if (
      denied &&
      method === "GET" &&
      /\/(status|jobs|plans|assets|catalog)(?:\?|$)/.test(url)
    ) {
      const data = await response.json();
      if (data.web_capabilities) data.web_capabilities.collect = false;
      data.collect = false;
      data.write_eligible = false;
      if (data.backfill) data.backfill.eligible = false;
      response = Response.json(data, { status: response.status });
    }
    call.status = response.status;
    if (method === "POST" && response.ok) {
      posted = true;
      if (lost) throw Error("fixture_lost_after_durable_write");
    }
    return response;
  };
  location.hash = new URLSearchParams(fields).toString();
  const view = render(
    <App
      transport={transport}
      initialQuery={{ reference, as_of: "2026-11-14T00:00:00Z" }}
    />,
  );
  const button = await screen.findByRole("button", {
    name: /^(提交 .+ 采集|提交采集任务|创建采集任务)$/,
  });
  await waitFor(() =>
    expect((screen.getByLabelText("采集目标") as HTMLSelectElement).value).toBe(
      keyOf(row.asset),
    ),
  );
  if (!row.protocol.startsWith("legacy"))
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.url === row.path.replace(/\/jobs$/, "/status") &&
            c.status === 200,
        ),
      ).toBe(true),
    );
  else
    await waitFor(() =>
      expect(
        calls.some((c) => /\/jobs(?:\?|$)/.test(c.url) && c.status === 200),
      ).toBe(true),
    );
  await act(async () => {});
  return {
    backend,
    transport,
    calls,
    reference,
    view,
    button,
    release: () => {
      allowRead = true;
    },
  };
}
function fillForm(reference: string, alternate = false) {
  const button = submitButton(),
    form = button.closest("form")!;
  for (const input of Array.from(
    form.querySelectorAll<HTMLInputElement>("input"),
  )) {
    const label =
      input.getAttribute("aria-label") ??
      Array.from(input.labels ?? [])
        .map((l) => l.textContent)
        .join(" ");
    if (!/开始|结束/.test(label)) continue;
    const value = label.includes("季度")
      ? alternate
        ? "2026-Q2"
        : "2026-Q3"
      : input.type === "date" || input.placeholder.includes("DD")
        ? alternate
          ? "2026-10-24"
          : "2026-10-31"
        : label.includes("参考月")
          ? reference
          : alternate
            ? "2026-08"
            : "2026-09";
    fireEvent.change(input, { target: { value } });
  }
}

it("covers the actual capability catalog and requires a scenario for every production registration", () => {
  for (const cap of [
    ...capabilities,
    ...directoryEntries
      .filter((e) => e.asset)
      .map((e) => ({ asset: e.asset!, capability: keyOf(e.asset!) })),
  ])
    expect(
      [...registrations, ...readonlyCollections].some(
        (r) => keyOf(r.asset) === keyOf(cap.asset),
      ),
      cap.capability,
    ).toBe(true);
  const omitted = { ...drivers };
  delete (omitted as Partial<typeof drivers>).roster;
  expect(() => requireScenarios(registrations, omitted)).toThrow(
    /missing submit scenario/,
  );
  const complete = registrations.flatMap((r) =>
    ["accepted", "lost", "denied", "lifecycle"].map((scenario) => ({
      asset: keyOf(r.asset),
      variant: r.variant,
      scenario,
    })),
  );
  expect(() => requireEvidence(registrations, complete.slice(1))).toThrow(
    /missing completed scenario/,
  );
  expect(() => requireEvidence(registrations, complete)).not.toThrow();
});

describe.each(registrations)("$asset.identity $variant", (row) => {
  it("honors actual disabled collection capability, including forced submit", async () => {
    const context = await setup(row, false, true);
    await waitFor(() =>
      expect(
        (screen.getByLabelText("采集目标") as HTMLSelectElement).value,
      ).toBe(keyOf(row.asset)),
    );
    fillForm(context.reference);
    await act(async () => {});
    const form = submitButton().closest("form")!;
    fireEvent.click(within(form).getByRole("checkbox"));
    await waitFor(() => expect(submitButton().disabled).toBe(true));
    fireEvent.submit(form);
    await act(async () => {});
    expect(context.calls.filter((c) => c.method === "POST")).toHaveLength(0);
    proofs.push({
      asset: keyOf(row.asset),
      variant: row.variant,
      scenario: "denied",
      requests: context.calls,
    });
  }, 90000);
  it("keeps the accepted intent, tracks its ID, reopens explicitly and uses one detail shell", async () => {
    const context = await setup(row);
    fillForm(context.reference);
    await act(async () => {});
    const form = submitButton().closest("form")!;
    fireEvent.click(within(form).getByRole("checkbox"));
    await waitFor(() => expect(submitButton().disabled).toBe(false));
    fireEvent.click(submitButton());
    await waitFor(() =>
      expect(context.calls.filter((c) => c.method === "POST")).toHaveLength(1),
    );
    const post = context.calls.find((c) => c.method === "POST")!;
    const id =
      post.body.request_id ??
      post.body.job_identity ??
      post.url.split("/").at(-2)!;
    const briefs = () =>
      screen.getByRole("complementary", { name: "执行简报" });
    const card = (requestId = id) =>
      within(briefs()).getByRole("button", {
        name: new RegExp(requestId),
      }) as HTMLButtonElement;
    await waitFor(() => expect(card().disabled).toBe(false));
    expect(tab()).toBe("new");
    expect(
      within(briefs()).queryByRole("button", {
        name: /回查同一任务|核实原请求/,
      }),
    ).toBeNull();
    if (row.intent === "server-offer") {
      // Compatibility contracts offer a bounded server-owned ID. Never invent
      // a client UUID or release the durable reservation to pretend to reopen.
      expect(
        within(briefs()).queryByRole("button", { name: "开始另一项采集" }),
      ).toBeNull();
      expect(
        briefs().querySelector('[data-collection-exception="server-offer"]'),
      ).toBeTruthy();
      fireEvent.submit(form);
      expect(context.calls.filter((c) => c.method === "POST")).toHaveLength(1);
      proofs.push({
        asset: keyOf(row.asset),
        variant: row.variant,
        scenario: "lifecycle",
        exception: "server-offer",
        id,
      });
      return;
    }
    const track = async (phase: "active" | "unknown" | "complete") => {
      const changed = await context.backend.exchange({
        control: "phase",
        id,
        phase,
      });
      expect(changed.status).toBe(200);
      const visibleState = {
        active: /执行中|采集中/,
        unknown: /未知|待核实|commit_outcome_unknown/,
        complete: /完成/,
      }[phase];
      // Another GET may finish before the polling response for this job arrives.
      await waitFor(
        () => {
          const label = within(card()).getByText("结果", { selector: "dt" });
          expect(label.nextElementSibling?.textContent).toMatch(visibleState);
        },
        { timeout: 15000 },
      );
    };
    for (const phase of ["active", "unknown"] as const) {
      await track(phase);
      await waitFor(() =>
        expect(
          within(briefs()).queryByRole("button", { name: "开始另一项采集" }),
        ).toBeNull(),
      );
      expect(form.querySelector("fieldset")?.disabled).toBe(true);
      fireEvent.submit(form);
      expect(context.calls.filter((c) => c.method === "POST")).toHaveLength(1);
    }
    await track("complete");
    const reopen = await within(briefs()).findByRole(
      "button",
      {
        name: "开始另一项采集",
      },
      { timeout: 12000 },
    );
    fireEvent.click(reopen);
    await waitFor(() =>
      expect(form.querySelector("fieldset")?.disabled).toBe(false),
    );
    expect(
      (within(form).getByRole("checkbox") as HTMLInputElement).checked,
    ).toBe(false);
    expect(card().disabled).toBe(false); // released history remains available
    fireEvent.submit(form);
    expect(context.calls.filter((c) => c.method === "POST")).toHaveLength(1);
    fillForm(context.reference, true);
    fireEvent.click(within(form).getByRole("checkbox"));
    await waitFor(() => expect(submitButton().disabled).toBe(false));
    fireEvent.click(submitButton());
    await waitFor(() =>
      expect(context.calls.filter((c) => c.method === "POST")).toHaveLength(2),
    );
    const next = context.calls.filter((c) => c.method === "POST")[1];
    expect(next.url).toBe(post.url);
    expect(next.body.request_id).not.toBe(id);
    if (typeof post.body.start === "string")
      expect(next.body.start).not.toBe(post.body.start);
    expect(tab()).toBe("new");
    const nextId = next.body.request_id;
    await context.backend.exchange({
      control: "phase",
      id: nextId,
      phase: "failed",
    });
    // The provisional card already has this ID while its receipt is in flight.
    await waitFor(() => expect(card(nextId).disabled).toBe(false), {
      timeout: 15000,
    });
    fireEvent.click(card(nextId));
    const failedShell = await selectedShell(row, nextId);
    expect(failedShell.dataset.executionId).toBe(nextId);
    let expectedPosts = 2;
    if (row.resumePath) {
      const resume = (await within(failedShell).findByRole(
        "button",
        { name: /续跑|按原请求恢复/ },
        { timeout: 15000 },
      )) as HTMLButtonElement;
      const confirm = within(failedShell).queryByRole("checkbox", {
        name: "我确认按原请求恢复采集",
      });
      if (confirm) fireEvent.click(confirm);
      await waitFor(() => expect(resume.disabled).toBe(false));
      fireEvent.click(resume);
      await waitFor(() =>
        expect(context.calls.filter((c) => c.method === "POST")).toHaveLength(
          3,
        ),
      );
      const recovery = context.calls.filter((c) => c.method === "POST")[2];
      expect(recovery.url).toBe(row.resumePath);
      expect(recovery.body.request_id).toBe(nextId);
      expect(new URLSearchParams(location.hash.slice(1)).get("job")).toBe(
        nextId,
      );
      expectedPosts = 3;
    } else
      expect(
        within(failedShell).queryByRole("button", {
          name: /续跑|按原请求恢复/,
        }),
      ).toBeNull();
    const priorRow = Array.from(
      screen
        .getByRole("region", { name: "执行记录" })
        .querySelectorAll<HTMLElement>("tr[data-record-id]"),
    ).find((r) => r.dataset.recordId?.endsWith(id))!;
    expect(priorRow).toBeTruthy();
    fireEvent.click(within(priorRow).getByRole("button"));
    const shell = await selectedShell(row, id);
    expect(shell.dataset.executionId).toBe(id);
    expect(shell.dataset.executionAsset).toBe(keyOf(row.asset));
    const scope = within(shell).getByText("范围").parentElement!.textContent;
    if (row.protocol === "roster") {
      for (const market of ["深市", "沪市", "北交所"])
        expect(scope).toContain(market);
    } else {
      expect(scope).toContain(post.body.start);
      expect(scope).toContain(post.body.end);
    }

    expect(shell.querySelector("h3")?.textContent).toBe("执行详情");
    expect(
      within(shell).queryByRole("button", { name: /续跑|恢复采集|恢复名册/ }),
    ).toBeNull();
    expect(
      within(shell).queryByRole("button", { name: /回查当前任务/ }),
    ).toBeNull();
    const selected = screen
      .getByRole("region", { name: "执行记录" })
      .querySelector('tr[aria-selected="true"]') as HTMLElement;
    expect(selected.dataset.recordId?.endsWith(id)).toBe(true);
    context.view.unmount();
    render(
      <App
        transport={context.transport}
        initialQuery={{
          reference: context.reference,
          as_of: "2026-11-14T00:00:00Z",
        }}
      />,
    );
    const reloaded = await selectedShell(row, id);
    expect(reloaded.dataset.executionId).toBe(id);
    expect(reloaded.dataset.executionAsset).toBe(keyOf(row.asset));
    fireEvent.click(
      within(reloaded).getByRole("button", { name: "查看资产数据" }),
    );
    await waitFor(() =>
      expect(new URLSearchParams(location.hash.slice(1)).get("view")).toBe(
        "assets",
      ),
    );
    expect(new URLSearchParams(location.hash.slice(1)).get("job")).toBe(id);
    expect(new URLSearchParams(location.hash.slice(1)).get("asset")).toBe(
      row.asset.identity,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "返回此执行记录" }),
    );
    const returned = await screen.findByRole("region", {
      name: "执行详情主体",
    });
    expect(returned.dataset.executionId).toBe(id);
    expect(returned.dataset.executionAsset).toBe(keyOf(row.asset));
    expect(context.calls.filter((c) => c.method === "POST")).toHaveLength(
      expectedPosts,
    );
    proofs.push({
      asset: keyOf(row.asset),
      variant: row.variant,
      scenario: "lifecycle",
      id,
      next: next.body.request_id,
      locked: true,
      unknownBlocks: true,
      samePageReopen: true,
      resultAndReturn: true,
    });
  }, 90000);
  it.each([false, true])(
    "same POST, stay, same-ID brief and explicit detail; lost=%s",
    async (lost) => {
      const context = await setup(row, lost);
      await waitFor(() =>
        expect(
          (screen.getByLabelText("采集目标") as HTMLSelectElement).value,
        ).toBe(keyOf(row.asset)),
      );
      const form = submitButton().closest("form")!;
      // Discover modes from the real form; no second list of assets or commands.
      const mode =
        form.querySelector<HTMLSelectElement>(
          'select[aria-label="采集方式"]',
        ) ??
        (within(form).queryByRole("combobox", {
          name: "采集方式",
        }) as HTMLSelectElement | null);
      const selectedMode = mode?.value;
      fillForm(context.reference);
      await act(async () => {});
      fireEvent.click(within(form).getByRole("checkbox"));
      await waitFor(() => expect(submitButton().disabled).toBe(false), {
        onTimeout: () =>
          Error(
            JSON.stringify({
              form: form.textContent,
              inputs: Array.from(form.querySelectorAll("input")).map((e) => [
                e.type,
                e.value,
                e.checked,
              ]),
              calls: context.calls,
            }),
          ),
      });
      fireEvent.click(submitButton());
      fireEvent.submit(form); // synchronous duplicate gesture must not create another POST
      const briefs = await screen.findByRole("complementary", {
        name: "执行简报",
      });
      await waitFor(() =>
        expect(context.calls.filter((c) => c.method === "POST")).toHaveLength(
          1,
        ),
      );
      const post = context.calls.find((c) => c.method === "POST")!;
      const responseIds = [post.body.request_id, post.body.job_identity].filter(
        Boolean,
      );
      const id = responseIds[0] ?? post.url.split("/").at(-2)!;
      await within(briefs).findByText(id, { exact: false });
      expect(tab()).toBe("new");
      expect(new URLSearchParams(location.hash.slice(1)).get("asset")).toBe(
        row.asset.identity,
      );
      expect(post.url).toBe(row.path.replace("{plan}", id));
      if (selectedMode && post.body.action !== undefined)
        expect(post.body.action).toBe(
          ["gdp", "claims"].includes(row.profile?.id ?? "") &&
            selectedMode === "initialize"
            ? "init"
            : selectedMode,
        );
      if (row.protocol === "roster")
        expect(Object.keys(post.body)).toEqual(["request_id"]);
      if (row.protocol.startsWith("legacy")) {
        const start =
          post.body.start ?? post.body.reference ?? post.body.binding.reference;
        const end = post.body.end ?? start;
        // Editing the draft cannot rewrite the accepted/unknown receipt's range,
        // or make a plan recovery look up a different batch.
        fireEvent.change(screen.getByLabelText("开始参考月"), {
          target: { value: "2020-01" },
        });
        expect(
          within(briefs).getByRole("button", { name: new RegExp(id) })
            .textContent,
        ).toContain(`${start} 至 ${end}`);
      }
      if (lost) {
        const recall = await screen.findByRole("button", {
          name: "核实原请求",
        });
        context.release();
        fireEvent.click(recall);
      }
      const currentCard = () =>
        within(
          screen.getByRole("complementary", { name: "执行简报" }),
        ).getByRole("button", { name: new RegExp(id) }) as HTMLButtonElement;
      await waitFor(() =>
        expect(
          context.calls.some(
            (c) => c.method === "GET" && c.url.includes("/jobs"),
          ),
        ).toBe(true),
      );
      expect(tab()).toBe("new");
      expect(context.calls.filter((c) => c.method === "POST")).toHaveLength(1);
      await waitFor(() => expect(currentCard().disabled).toBe(false));
      fireEvent.click(currentCard());
      const detailTab =
        row.protocol === "legacy-plan" ? "periodic" : "executions";
      await waitFor(() => expect(tab()).toBe(detailTab));
      const detail = await screen.findByRole("complementary", {
        name: row.protocol === "legacy-plan" ? "周期任务详情" : "执行记录详情",
      });
      await within(detail).findAllByText(id, { exact: false });
      expect(context.calls.filter((c) => c.method === "POST")).toHaveLength(1);
      if (!row.protocol.startsWith("legacy")) {
        const stored = await context.backend.exchange({ control: "stored" });
        expect(stored.body.records.some((r: any) => r.request_id === id)).toBe(
          true,
        );
      }
      proofs.push({
        asset: keyOf(row.asset),
        variant: row.variant,
        protocol: row.protocol,
        lost,
        scenario: lost ? "lost" : "accepted",
        path: post.url,
        identity: id,
        action: post.body.action,
        postCount: 1,
        stay: true,
        detailAfterClick: true,
        detailKind: row.protocol === "legacy-plan" ? "plan" : "execution",
        requests: context.calls,
      });
    },
    90000,
  );
});

it.each(readonlyCollections)(
  "keeps $asset.identity read only at its normal entry",
  async (row) => {
    const root = mkdtempSync(join(tmpdir(), "mdf-d01-submit-"));
    roots.push(root);
    const directory = asgi("default", "d01_ui_exchange.py", {
      store_dir: root,
    });
    const calls: string[] = [];
    const transport: Transport = async (url, init) => {
      if (init?.method && init.method !== "GET") calls.push(init.method);
      return /^\/api\/r5\/(packages|sources)/.test(String(url))
        ? directory.transport(url, init)
        : Response.json({ classification: "not_enabled" }, { status: 404 });
    };
    location.hash = new URLSearchParams({
      ui: "r5",
      view: "collection",
      tab: "new",
      asset: row.asset.identity,
      asset_revision: row.asset.revision,
    }).toString();
    render(<App transport={transport} />);
    await screen.findByText(row.reason);
    expect(submitButton().disabled).toBe(true);
    fireEvent.submit(submitButton().closest("form")!);
    await act(async () => {});
    expect(calls).toEqual([]);
  },
  90000,
);

it("keeps list selection and details on the same asset/ID when an old family read returns late", async () => {
  const chosen = [
    registrations.find((r) => r.profile?.id === "consumption")!,
    registrations.find((r) => r.profile?.id === "cpi")!,
    registrations.find((r) => r.protocol === "financial")!,
    registrations.find((r) => r.protocol === "tdx")!,
  ];
  const identity = "73117ab1-6301-48fa-b96e-81b25e789abb";
  const sources = new Map<string, Bridge>();
  for (const row of chosen) {
    const b = row.profile
      ? asgi(row.profile.id, "collection_submit_fixture.py")
      : drivers[row.protocol].bridge();
    sources.set(row.path.replace(/\/jobs$/, ""), b);
    if (row.protocol === "tdx")
      await b.exchange({
        method: "POST",
        path: row.path,
        body: {
          request_id: "411d48ab-952c-4099-aad0-6df9d01b75da",
          action: "universe",
        },
      });
    const command = row.profile
      ? {
          request_id: identity,
          action: "update",
          start: "2026-09",
          end: "2026-09",
        }
      : {
          request_id: identity,
          action: "update",
          start: "2026-11-09",
          end: "2026-11-12",
          ...(row.protocol === "tdx" ? { markets: [0, 1, 2] } : {}),
        };
    const saved = await b.exchange({
      method: "POST",
      path: row.path,
      body: command,
    });
    expect([200, 202]).toContain(saved.status);
    await b.exchange({ control: "phase", id: identity, phase: "complete" });
  }
  const root = mkdtempSync(join(tmpdir(), "mdf-d01-cross-details-"));
  roots.push(root);
  const directory = asgi("financial_manual", "d01_ui_exchange.py", {
    store_dir: root,
  });
  const groups = new Map<string, string[]>();
  for (const row of chosen) {
    const cap = capabilities.find((c) => keyOf(c.asset) === keyOf(row.asset))!;
    groups.set(cap.package, [
      ...(groups.get(cap.package) ?? []),
      cap.capability,
    ]);
  }
  for (const [pkg, enabled] of groups)
    expect(
      (
        await directory.exchange({
          method: "PUT",
          path: `/api/r5/packages/${pkg}`,
          body: {
            expected_revision: null,
            enabled_capability_ids: enabled,
            source_changes: [],
          },
        })
      ).status,
    ).toBe(200);
  let pending = false,
    delay = true;
  let release = () => {};
  const tdx = chosen[3];
  const transport: Transport = async (input, init) => {
    const url = String(input);
    if (/^\/api\/r5\/(packages|sources)/.test(url))
      return directory.transport(input, init);
    const match = [...sources].find(([prefix]) => url.startsWith(prefix));
    if (!match)
      return Response.json(
        { classification: "fixture_not_enabled" },
        { status: 404 },
      );
    const response = await match[1].transport(input, init);
    if (delay && url === tdx.path + "/" + identity) {
      delay = false;
      pending = true;
      await new Promise<void>((resolve) => {
        release = resolve;
      });
    }
    return response;
  };
  location.hash = new URLSearchParams({
    ui: "r5",
    view: "collection",
    tab: "executions",
    execution_asset: "all",
    package: "macro",
    execution_schedule: "all",
    asset: chosen[0].asset.identity,
    asset_revision: chosen[0].asset.revision,
    job: identity,
  }).toString();
  render(<App transport={transport} />);
  async function select(row: CollectionRegistration) {
    const table = await screen.findByRole("region", { name: "执行记录" });
    let found: HTMLElement | undefined;
    await waitFor(() => {
      found = [
        ...table.querySelectorAll<HTMLElement>("tr[data-record-id]"),
      ].find(
        (r) =>
          r.dataset.executionId === identity &&
          r.dataset.executionAsset === keyOf(row.asset),
      );
      expect(found).toBeTruthy();
    });
    fireEvent.click(within(found!).getByRole("button"));
  }
  async function same(row: CollectionRegistration) {
    await waitFor(() => {
      const shell = screen.getByRole("region", { name: "执行详情主体" });
      expect(shell.dataset.executionId).toBe(identity);
      expect(shell.dataset.executionAsset).toBe(keyOf(row.asset));
      const selected = screen
        .getByRole("region", { name: "执行记录" })
        .querySelector('tr[aria-selected="true"]') as HTMLElement;
      expect(selected.dataset.executionId).toBe(identity);
      expect(selected.dataset.executionAsset).toBe(keyOf(row.asset));
    });
    expect(new URLSearchParams(location.hash.slice(1)).get("asset")).toBe(
      row.asset.identity,
    );
  }
  await same(chosen[0]);
  await select(tdx);
  await waitFor(() => expect(pending).toBe(true));
  await select(chosen[2]);
  await same(chosen[2]);
  await act(async () => {
    release();
  });
  await same(chosen[2]);
  await select(tdx);
  await same(tdx);
  await select(chosen[0]);
  await same(chosen[0]);
  await select(chosen[1]);
  await same(chosen[1]); // CPI must replace the previous Macro identity
  await select(tdx);
  await same(tdx);
  fireEvent.click(
    within(screen.getByRole("region", { name: "执行详情主体" })).getByRole(
      "button",
      { name: "查看资产数据" },
    ),
  );
  const context = await screen.findByLabelText("关联执行");
  expect(context.dataset.executionAsset).toBe(keyOf(tdx.asset));
  expect(context.dataset.executionId).toBe(identity);
  fireEvent.click(
    within(context).getByRole("button", { name: "返回此执行记录" }),
  );
  await same(tdx);
}, 90000);
