/** Built App + real ASGI job routers, isolated stores, no live HTTP/PG/provider.
 * node web/scripts/check-collection-submit.mjs <matrix.json> <evidence-dir>
 * Matrix input is generated from the actual product registrations by CI.
 */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { tmpdir } from "node:os";
import {
  expectedPolicy,
  protocolExecutionOrigins,
} from "./collection-protocol-evidence.ts";
const [inventoryPath, output] = process.argv.slice(2);
assert(inventoryPath && output);
const inventory = JSON.parse(await fs.readFile(inventoryPath, "utf8"));
const root = process.cwd();
const dist = process.env.MDF_SUBMIT_DIST ?? path.join(root, "web/dist");
const origin = process.env.MDF_SUBMIT_ORIGIN ?? "http://mdf-isolated.invalid";
const deployed = !!process.env.MDF_SUBMIT_ORIGIN;
const bundles = new Map();
await fs.mkdir(output, { recursive: true });
function bridge(family, script = "collection_submit_fixture.py", fields = {}) {
  const child = spawn(
    path.join(root, ".venv/bin/python"),
    ["-u", "tests/" + script, family],
    {
      cwd: root,
      env: {
        ...process.env,
        PYTHONDONTWRITEBYTECODE: "1",
        PYTHONPATH: `${root}/src:${root}/tests:${root}`,
      },
    },
  );
  const pending = [];
  let errors = "";
  child.stderr.on("data", (b) => {
    errors += b;
  });
  createInterface({ input: child.stdout }).on("line", (l) =>
    pending.shift()?.resolve(JSON.parse(l)),
  );
  child.on("exit", (code) =>
    pending
      .splice(0)
      .forEach((p) => p.reject(Error(`fixture ${code}: ${errors}`))),
  );
  return {
    exchange: (req) =>
      new Promise((resolve, reject) => {
        pending.push({ resolve, reject });
        child.stdin.write(JSON.stringify({ ...fields, ...req }) + "\n");
      }),
    close: () => {
      child.stdin.end();
      child.kill();
    },
  };
}
const { chromium } = await import(
  process.env.MDF_PLAYWRIGHT_MODULE
    ? pathToFileURL(process.env.MDF_PLAYWRIGHT_MODULE).href
    : "playwright"
);
const browser = await chromium.launch({
  executablePath: process.env.MDF_BROWSER_EXECUTABLE,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});
function currentRoute(page) {
  return new URLSearchParams(new URL(page.url()).hash.slice(1));
}
async function selectedDetail(page, asset, id) {
  const key = asset.identity + "@" + asset.revision;
  await page.waitForFunction(
    ({ key, id }) => {
      const detail = document.querySelector('[aria-label="执行详情主体"]');
      const selected = document.querySelector('tr[aria-selected="true"]');
      return [detail, selected].every(
        (el) =>
          el?.dataset.executionId === id && el.dataset.executionAsset === key,
      );
    },
    { key, id },
  );
  assert.equal(currentRoute(page).get("job"), id);
  assert.equal(currentRoute(page).get("asset"), asset.identity);
  const detail = page.getByRole("region", { name: "执行详情主体" });
  await detail
    .getByRole("heading", { name: "执行详情", exact: true })
    .waitFor();
  assert.equal(
    await detail
      .locator('[aria-label="执行操作"]')
      .getAttribute("data-execution-id"),
    id,
  );
  const redundant = detail.getByRole("button", {
    name: /^(回查当前任务|查看关联资产 →|返回执行列表)$/,
  });
  assert.equal(await redundant.count(), 0);
  assert.equal(
    await detail.getByRole("button", { name: "查看资产数据" }).count(),
    1,
  );
  return detail;
}
async function checkBriefLayout(page, briefs) {
  // Stress the actual shared card for every registered family. Preserve full
  // long results/IDs rather than clip them or expand the document.
  const text =
    "执行完成（本次发布参考周已核实） · 发布 2026-11-12 · 初请参考周 2026-11-07；续请参考周 2026-10-31";
  const widths = [];
  for (const width of [1440, 1180]) {
    await page.setViewportSize({ width, height: 980 });
    const result = await briefs.evaluate((element, text) => {
      const card = element.querySelector(".execution-brief-card");
      const values = [...card.querySelectorAll("dd")];
      if (values.length < 2) throw Error("missing real brief facts");
      const originals = values.map((el) => el.textContent);
      values[0].textContent = "long-request-" + "0123456789abcdef".repeat(12);
      values.at(-1).textContent = text;
      const facts = {
        viewport: innerWidth,
        document: document.documentElement.scrollWidth,
        card: card.clientWidth,
        content: card.scrollWidth,
        full: values.at(-1).textContent,
      };
      values.forEach((el, i) => {
        el.textContent = originals[i];
      });
      return facts;
    }, text);
    assert.equal(result.full, text);
    assert(
      result.document <= width + 1,
      "brief overflow: " + JSON.stringify(result),
    );
    assert(
      result.content <= result.card + 1,
      "brief content overflow at " + width,
    );
    widths.push(result);
  }
  await page.setViewportSize({ width: 1440, height: 980 });
  return widths;
}
async function exercisePolicy(page, row, backend, id) {
  const layout = [];
  const policy = await expectedPolicy(row, backend.exchange);
  assert.deepEqual(
    row.strategy,
    policy,
    "registry must agree with protocol policy evidence",
  );
  let detail = await selectedDetail(page, row.asset, id);
  const action = detail.getByRole("button", { name: "查看周期任务" });
  if (!policy) {
    assert.equal(
      await action.count(),
      0,
      "policy-free API must omit navigation",
    );
    return { policy: null };
  }
  await action.click();
  async function selectedPolicy() {
    assert.equal(
      currentRoute(page).get("schedule"),
      policy.id,
      "verified policy destination",
    );
    await page
      .locator(
        '[aria-label="周期任务"] tr[aria-selected="true"][data-record-id="' +
          policy.id +
          '"]',
      )
      .waitFor();
    const operations = page.getByLabel("周期操作", { exact: true });
    assert.equal(await operations.getAttribute("data-periodic-id"), policy.id);
    assert.equal(
      await operations.getAttribute("data-periodic-asset"),
      row.asset.identity + "@" + row.asset.revision,
    );
    assert.equal(currentRoute(page).get("schedule"), policy.id);
    assert.equal(currentRoute(page).get("asset_revision"), row.asset.revision);
    if (!layout.length) {
      for (const width of [1440, 1180]) {
        await page.setViewportSize({ width, height: 980 });
        const measured = await operations.evaluate((nav) => {
          const pane = nav.closest(".detail-pane"),
            shell = nav.closest('[aria-label="周期详情主体"]');
          const heading = pane.querySelector("h3"),
            style = getComputedStyle(pane);
          return {
            width: innerWidth,
            docWidth: document.documentElement.scrollWidth,
            title: heading?.textContent,
            offset:
              heading?.getBoundingClientRect().top -
              pane.getBoundingClientRect().top,
            expected:
              parseFloat(style.paddingTop) + parseFloat(style.borderTopWidth),
            shell: !!shell,
            first: shell?.firstElementChild === heading,
            second: shell?.children[1] === nav,
            after:
              nav.getBoundingClientRect().top >=
              heading.getBoundingClientRect().bottom,
          };
        });
        assert(
          measured.shell && measured.first && measured.second && measured.after,
          "periodic heading/navigation/domain order: " +
            JSON.stringify(measured),
        );
        assert(
          Math.abs(measured.offset - measured.expected) <= 1,
          "periodic title offset: " + JSON.stringify(measured),
        );
        assert(
          measured.docWidth <= width + 1,
          "periodic overflow: " + JSON.stringify(measured),
        );
        layout.push(measured);
      }
      await page.setViewportSize({ width: 1440, height: 980 });
    }
    return operations;
  }
  let operations = await selectedPolicy();
  await page.getByRole("tab", { name: "新建采集", exact: true }).click();
  assert.equal(currentRoute(page).get("tab"), "new");
  assert.equal(currentRoute(page).get("asset"), row.asset.identity);
  assert.equal(currentRoute(page).get("asset_revision"), row.asset.revision);
  assert.equal(
    await page.getByLabel("采集目标", { exact: true }).inputValue(),
    row.asset.identity + "@" + row.asset.revision,
  );
  await page.goBack();
  operations = await selectedPolicy();
  await operations.getByRole("button", { name: "返回此执行记录" }).click();
  detail = await selectedDetail(page, row.asset, id);
  assert.match(
    await detail
      .locator(".fact")
      .filter({ has: page.locator("dt", { hasText: /^触发方式$/ }) })
      .innerText(),
    /手动采集/,
  );
  // Same asset manual and unknown records cannot leak into a strategy list.
  // PCE deliberately has no automatic record.
  const autoId = "00000000-0000-4000-8000-000000000222";
  const unknownId = "00000000-0000-4000-8000-000000000333";
  const noAutomatic = row.profile === "pce";
  const origins = protocolExecutionOrigins(row);
  const origin = origins.automatic;
  if (!noAutomatic)
    assert.equal(
      (
        await backend.exchange({
          control: "clone",
          id,
          target_id: autoId,
          origin,
        })
      ).status,
      200,
    );
  assert.equal(
    (
      await backend.exchange({
        control: "clone",
        id,
        target_id: unknownId,
        origin: origins.unknown ? null : "manual",
      })
    ).status,
    200,
  );
  await detail.getByRole("button", { name: "查看周期任务" }).click();
  operations = await selectedPolicy();
  await operations
    .getByRole("button", { name: "查看执行记录", exact: true })
    .click();
  assert.equal(
    currentRoute(page).get("execution_asset"),
    row.asset.identity + "@" + row.asset.revision,
  );
  assert.equal(currentRoute(page).get("execution_schedule"), policy.id);
  const list = page.getByRole("region", { name: "执行记录", exact: true });
  const expected = noAutomatic ? [] : [autoId];
  await page.waitForFunction((expected) => {
    const list = document.querySelector('[aria-label="执行记录"]');
    const ids = [
      ...(list?.querySelectorAll("tr[data-execution-id]") ?? []),
    ].map((el) => el.dataset.executionId);
    return (
      JSON.stringify(ids) === JSON.stringify(expected) &&
      !/正在读取|尚未查询/.test(list.textContent)
    );
  }, expected);
  if (noAutomatic) assert.match(await list.innerText(), /没有执行记录/);
  await page.goBack();
  operations = await selectedPolicy();
  await operations.getByRole("button", { name: "返回此执行记录" }).click();
  await selectedDetail(page, row.asset, id);
  return {
    policy,
    layout,
    automatic: expected,
    excluded: [id, unknownId],
    unknownOriginSupported: origins.unknown,
    manualReturn: id,
  };
}
async function exerciseLifecycle({
  page,
  row,
  backend,
  firstId,
  form,
  submit,
  briefs,
  calls,
}) {
  const firstCard = briefs.getByRole("button", { name: new RegExp(firstId) });
  const current = firstCard.locator(
    "xpath=ancestor::*[contains(concat(' ',normalize-space(@class),' '),' execution-brief ')][1]",
  );
  const reopen = current.getByRole("button", {
    name: "开始另一项采集",
    exact: true,
  });
  const confirm = form.getByRole("checkbox");
  const writes = () => calls.filter((c) => c.method === "POST");
  async function trackPhase(phase) {
    assert.equal(
      (await backend.exchange({ control: "phase", id: firstId, phase })).status,
      200,
    );
    const read = page.waitForResponse(
      (r) =>
        r.request().method() === "GET" &&
        new URL(r.url()).pathname.startsWith(row.path) &&
        r.status() === 200,
    );
    assert.equal(
      await current
        .getByRole("button", {
          name: "回查同一任务",
          exact: true,
        })
        .count(),
      0,
    );
    await read;
    await page.waitForFunction(
      ({ id, phase }) => {
        const card = [...document.querySelectorAll(".execution-brief")]
          .find((el) => el.dataset.executionId === id)
          ?.querySelector(".execution-brief-card");
        const result =
          [...(card?.querySelectorAll("dl > div") ?? [])]
            .find((el) => el.querySelector("dt")?.textContent === "结果")
            ?.querySelector("dd")?.textContent ?? "";
        const visibleState = {
          active: /执行中|采集中/,
          unknown: /未知|待核实|commit_outcome_unknown/,
          complete: /完成/,
        }[phase];
        return !!visibleState?.test(result);
      },
      { id: firstId, phase },
    );
  }
  for (const phase of ["active", "unknown"]) {
    await trackPhase(phase);
    await reopen.waitFor({ state: "hidden" });
    assert(await confirm.isDisabled(), phase + " must lock the accepted form");
    assert.equal(writes().length, 1);
  }
  await trackPhase("complete");
  const layout = await checkBriefLayout(page, briefs);
  await reopen.click();
  assert.equal(currentRoute(page).get("tab"), "new");
  assert(await firstCard.isVisible());
  assert.equal(await confirm.isChecked(), false);
  assert.equal(await confirm.isEnabled(), true);
  assert(await submit.isDisabled());
  assert.equal(writes().length, 1);
  await confirm.check();
  const accepted = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" && new URL(r.url()).pathname === row.path,
  );
  await submit.click();
  assert((await accepted).ok());
  assert.equal(writes().length, 2);
  const secondId = writes()[1].body.request_id;
  assert(secondId && secondId !== firstId);
  const secondCard = briefs.getByRole("button", { name: new RegExp(secondId) });
  await secondCard.waitFor();
  await backend.exchange({ control: "phase", id: secondId, phase: "failed" });
  await secondCard.click();
  const failed = await selectedDetail(page, row.asset, secondId);
  const resume = failed.getByRole("button", { name: /续跑|按原请求恢复/ });
  const apiContract = await backend.exchange({ path: "/openapi.json" });
  assert.equal(apiContract.status, 200);
  const resumePath = apiContract.body.paths[row.path + "/resume"]?.post
    ? row.path + "/resume"
    : undefined;
  assert.equal(
    row.resumePath,
    resumePath,
    "resume declaration must match real router contract",
  );
  if (resumePath) {
    await resume.waitFor();
    assert(
      await resume.isDisabled(),
      "recovery requires explicit confirmation",
    );
    const mustConfirm = failed.getByRole("checkbox", {
      name: "我确认按原请求恢复采集",
    });
    assert.equal(await mustConfirm.count(), 1);
    await mustConfirm.check();
    const resumed = page.waitForResponse(
      (r) =>
        r.request().method() === "POST" &&
        new URL(r.url()).pathname === resumePath,
    );
    await resume.click();
    assert((await resumed).ok());
    const last = writes().at(-1);
    assert.equal(last.body.request_id, secondId);
    assert.equal(last.path, resumePath);
  } else assert.equal(await resume.count(), 0);
  await page
    .locator('tr[data-execution-id="' + firstId + '"]')
    .getByRole("button")
    .click();
  let detail = await selectedDetail(page, row.asset, firstId);
  assert.equal(
    await detail.getByRole("button", { name: /续跑|按原请求恢复/ }).count(),
    0,
  );
  assert.equal(
    await detail
      .getByRole("button", {
        name: "回查当前任务",
        exact: true,
      })
      .count(),
    0,
  );
  await page.reload();
  detail = await selectedDetail(page, row.asset, firstId);
  const policy = await exercisePolicy(page, row, backend, firstId);
  detail = await selectedDetail(page, row.asset, firstId);
  await detail.getByRole("button", { name: "查看资产数据" }).click();
  const context = page.getByLabel("关联执行", { exact: true });
  await context.waitFor();
  assert.equal(await context.getAttribute("data-execution-id"), firstId);
  assert.equal(currentRoute(page).get("asset"), row.asset.identity);
  await context.getByRole("button", { name: "返回此执行记录" }).click();
  await selectedDetail(page, row.asset, firstId);
  assert.equal(writes().length, row.resumePath ? 3 : 2);
  return {
    firstId,
    secondId,
    resume: !!row.resumePath,
    policy,
    layout,
    locked: true,
    reopened: true,
    selectedAfterSwitch: true,
    roundTrip: true,
  };
}

// The two records deliberately share a UUID. The chosen asset must continue to
// own the detail, selected row and actions after the older response arrives.
async function exerciseCrossAsset(page, baseRow, backend, other, id, pause) {
  const response = await other.backend.exchange({
    method: "POST",
    path: other.row.path,
    body: {
      request_id: id,
      action: "update",
      start: "2026-09",
      end: "2026-09",
    },
  });
  assert([200, 202].includes(response.status));
  assert.equal(
    (await other.backend.exchange({ control: "phase", id, phase: "complete" }))
      .status,
    200,
  );
  for (const [target, count, day] of [
    [backend, 25, "2026-12-01"],
    [other.backend, 22, "2026-12-02"],
  ]) {
    for (let i = 0; i < count; i++)
      assert.equal(
        (
          await target.exchange({
            control: "clone",
            id,
            target_id: "00000000-0000-4000-9000-" + String(i).padStart(12, "0"),
            origin: "manual",
            created_at: day + "T01:00:00Z",
          })
        ).status,
        200,
      );
  }
  const route = new URL(page.url());
  route.hash = new URLSearchParams({
    ui: "r5",
    view: "collection",
    tab: "executions",
    execution_asset: "all",
    execution_schedule: "all",
    asset: baseRow.asset.identity,
    asset_revision: baseRow.asset.revision,
    job: id,
  }).toString();
  let releaseList, listStarted, listFinished;
  const listHeld = new Promise((resolve) => {
    listStarted = resolve;
  });
  const listRelease = new Promise((resolve) => {
    releaseList = resolve;
  });
  const listDelivered = new Promise((resolve) => {
    listFinished = resolve;
  });
  pause.path = other.row.path.replace(/\/jobs$/, "/executions");
  pause.once = async (send) => {
    listStarted();
    await listRelease;
    try {
      await send();
    } finally {
      listFinished();
    }
  };
  try {
    await page.goto(route.href);
    await listHeld;
    await selectedDetail(page, baseRow.asset, id);
    await page.getByText("第 2 页", { exact: true }).waitFor();
  } finally {
    releaseList();
  }
  await listDelivered;
  await selectedDetail(page, baseRow.asset, id);
  await page.getByText("第 3 页", { exact: true }).waitFor();
  await page.getByRole("button", { name: "前一页记录" }).click();
  await page.getByText("第 2 页", { exact: true }).waitFor();
  route.hash = new URLSearchParams({
    ...Object.fromEntries(currentRoute(page)),
    asset: other.row.asset.identity,
    asset_revision: other.row.asset.revision,
    job: id,
  }).toString();
  await page.goto(route.href);
  await selectedDetail(page, other.row.asset, id);
  async function select(row) {
    const key = row.asset.identity + "@" + row.asset.revision;
    await page
      .locator(
        'tr[data-execution-asset="' +
          key +
          '"][data-execution-id="' +
          id +
          '"]',
      )
      .getByRole("button")
      .click();
  }
  let resume;
  const released = new Promise((resolve) => {
    resume = resolve;
  });
  let started;
  const held = new Promise((resolve) => {
    started = resolve;
  });
  let finished;
  const delivered = new Promise((resolve) => {
    finished = resolve;
  });
  pause.path = baseRow.path + "/" + id;
  pause.once = async (send) => {
    started();
    await released;
    try {
      await send();
    } finally {
      finished();
    }
  };
  try {
    const namedRequest = page.waitForRequest(
      (r) => r.method() === "GET" && new URL(r.url()).pathname === pause.path,
    );
    await select(baseRow);
    await namedRequest;
    await held;
    await select(other.row);
    await selectedDetail(page, other.row.asset, id);
  } finally {
    resume();
  }
  await delivered;
  await selectedDetail(page, other.row.asset, id);
  await select(baseRow);
  await selectedDetail(page, baseRow.asset, id);
  return {
    asyncPagination: true,
    id,
    assets: [baseRow.asset.identity, other.row.asset.identity],
    delayed: true,
  };
}

const proofs = [];
try {
  for (const row of inventory.registrations.filter(
    (r) => !r.protocol.startsWith("legacy"),
  )) {
    for (const lost of [false, true]) {
      const backend = bridge(row.profile ?? row.protocol);
      const peer =
        row.protocol === "roster" && !lost
          ? inventory.registrations.find((r) => r.profile === "consumption")
          : null;
      const other = peer ? { row: peer, backend: bridge(peer.profile) } : null;
      const pause = { path: null, once: null };
      const store = await fs.mkdtemp(
        path.join(tmpdir(), "mdf-d01-submit-browser-"),
      );
      const directory = bridge("financial_manual", "d01_ui_exchange.py", {
        scenario: "financial_manual",
        store_dir: store,
      });
      const page = await browser.newPage({
        viewport: { width: 1440, height: 980 },
      });
      page.setDefaultTimeout(15000);
      try {
        const cap = inventory.capabilities.find(
          (c) =>
            c.asset.identity === row.asset.identity &&
            c.asset.revision === row.asset.revision,
        );
        assert(cap);
        const saved = await directory.exchange({
          method: "PUT",
          path: `/api/r5/packages/${cap.package}`,
          body: {
            expected_revision: null,
            enabled_capability_ids: [cap.capability],
            source_changes: [],
          },
        });
        assert.equal(saved.status, 200);
        if (other) {
          const peerCap = inventory.capabilities.find(
            (c) =>
              c.asset.identity === peer.asset.identity &&
              c.asset.revision === peer.asset.revision,
          );
          assert(peerCap && peerCap.package !== cap.package);
          const enabled = await directory.exchange({
            method: "PUT",
            path: "/api/r5/packages/" + peerCap.package,
            body: {
              expected_revision: null,
              enabled_capability_ids: [peerCap.capability],
              source_changes: [],
            },
          });
          assert.equal(enabled.status, 200);
        }
        let writeStarted = false,
          allowRead = !lost;
        const calls = [];
        await page.route("**/*", async (route) => {
          const request = route.request(),
            url = new URL(request.url()),
            method = request.method();
          if (!url.pathname.startsWith("/api/")) {
            assert.equal(method, "GET");
            assert.equal(url.origin, origin, "external static request");
            if (deployed) return route.continue();
            const file = ["/", "/pce"].includes(url.pathname)
              ? "index.html"
              : url.pathname.slice(1);
            assert(!file.includes(".."));
            try {
              const type = file.endsWith(".js")
                ? "text/javascript"
                : file.endsWith(".css")
                  ? "text/css"
                  : file.endsWith(".html")
                    ? "text/html"
                    : "image/png";
              return route.fulfill({
                body: await fs.readFile(path.join(dist, file)),
                contentType: type,
              });
            } catch {
              return route.fulfill({
                status: 404,
                body: "missing_static_fixture",
              });
            }
          }
          const call = {
            path: url.pathname + url.search,
            method,
            body: request.postDataJSON() ?? undefined,
            headers: request.headers()["prefer"]
              ? { Prefer: request.headers()["prefer"] }
              : {},
          };
          calls.push(call);
          let out;
          if (/^\/api\/r5\/(sources|packages)/.test(url.pathname)) {
            assert.equal(method, "GET");
            out = await directory.exchange(call);
          } else if (
            url.pathname.startsWith(row.path.replace(/\/jobs$/, "")) ||
            (row.protocol === "roster" &&
              method === "GET" &&
              url.pathname === "/api/r5/tdx/daily/strategy")
          ) {
            if (method === "POST") writeStarted = true;
            if (
              writeStarted &&
              !allowRead &&
              method === "GET" &&
              url.pathname.includes("/jobs")
            )
              return route.fulfill({
                status: 503,
                json: { classification: "fixture_read_delayed" },
              });
            out = await backend.exchange(call);
          } else if (
            other &&
            url.pathname.startsWith(other.row.path.replace(/\/jobs$/, ""))
          ) {
            assert.equal(method, "GET");
            out = await other.backend.exchange(call);
          } else {
            assert.equal(method, "GET", "unregistered write");
            out = { status: 404, body: { classification: "not_enabled" } };
          }
          call.status = out.status;
          if (
            lost &&
            method === "POST" &&
            out.status >= 200 &&
            out.status < 300
          )
            return route.abort("failed");
          const send = () =>
            route.fulfill({ status: out.status, json: out.body });
          if (method === "GET" && pause.once && url.pathname === pause.path) {
            const hold = pause.once;
            pause.once = null;
            return hold(send);
          }
          return send();
        });
        page.on("response", async (response) => {
          const name = new URL(response.url()).pathname;
          if (
            name.startsWith("/assets/") &&
            name.endsWith(".js") &&
            response.status() === 200
          )
            bundles.set(
              name,
              createHash("sha256")
                .update(await response.body())
                .digest("hex"),
            );
        });
        const ready = page.waitForResponse(
          (r) =>
            new URL(r.url()).pathname ===
              row.path.replace(/\/jobs$/, "/status") && r.status() === 200,
        );
        await page.goto(
          origin +
            "/pce#" +
            new URLSearchParams({
              ui: "r5",
              view: "collection",
              tab: "new",
              asset: row.asset.identity,
              asset_revision: row.asset.revision,
            }),
        );
        const submit = page.getByRole("button", {
          name: /^(提交 .+ 采集|提交采集任务)$/,
        });
        await ready;
        await submit.waitFor();
        const form = submit.locator("xpath=ancestor::form");
        for (const input of await form.locator("input").all()) {
          const label = await input.evaluate(
            (e) =>
              e.getAttribute("aria-label") ??
              Array.from(e.labels ?? [])
                .map((l) => l.textContent)
                .join(" "),
          );
          if (!/开始|结束/.test(label)) continue;
          const placeholder = (await input.getAttribute("placeholder")) ?? "";
          await input.fill(
            label.includes("季度")
              ? "2026-Q3"
              : placeholder.includes("DD")
                ? "2026-10-31"
                : "2026-09",
          );
          await input.press("Escape");
          await input.blur();
        }
        await form.getByRole("checkbox").check();
        const lostReceipt = lost
          ? page.waitForEvent("requestfailed", {
              predicate: (req) =>
                req.method() === "POST" &&
                new URL(req.url()).pathname === row.path,
            })
          : null;
        await submit.click();
        const briefs = page.getByRole("complementary", { name: "执行简报" });
        if (lost) {
          // A pending card can render before the POST reaches the job router.
          // Reload only after the router accepted the job and the browser lost its receipt.
          await lostReceipt;
          const accepted = calls.find(
            (c) => c.method === "POST" && c.path === row.path,
          );
          assert(accepted && accepted.status >= 200 && accepted.status < 300);
          const recall = page.getByRole("button", {
            name: "核实原请求",
          });
          await recall.waitFor();
          await page.reload();
          await recall.waitFor();
          assert(
            await submit.isDisabled(),
            "refresh must not authorize another POST",
          );
          allowRead = true;
          await recall.click();
        }
        const posts = calls.filter((c) => c.method === "POST");
        assert.equal(posts.length, 1);
        const id = posts[0].body.request_id;
        assert.equal(posts[0].path, row.path);
        const card = briefs.getByRole("button", { name: new RegExp(id) });
        await card.waitFor();
        assert.equal(
          new URLSearchParams(new URL(page.url()).hash.slice(1)).get("tab"),
          "new",
        );
        assert((await card.innerText()).includes(id));
        await page.screenshot({
          path: path.join(
            output,
            `${row.profile ?? row.protocol}-${lost ? "lost" : "accepted"}.png`,
          ),
          fullPage: true,
        });
        let lifecycle = null;
        if (lost) {
          await card.click();
          await selectedDetail(page, row.asset, id);
          assert.equal(currentRoute(page).get("tab"), "executions");
          assert.equal(calls.filter((c) => c.method === "POST").length, 1);
        } else {
          lifecycle = await exerciseLifecycle({
            page,
            row,
            backend,
            firstId: id,
            form,
            submit,
            briefs,
            calls,
          });
        }
        if (other)
          lifecycle.crossAsset = await exerciseCrossAsset(
            page,
            row,
            backend,
            other,
            id,
            pause,
          );
        const stored = await backend.exchange({ control: "stored" });
        assert(stored.body.records.some((j) => j.request_id === id));
        proofs.push({
          asset: row.asset,
          variant: row.variant,
          lost,
          id,
          postCount: calls.filter((c) => c.method === "POST").length,
          lifecycle,
          stay: true,
          explicitDetail: true,
          calls,
        });
        await fs.writeFile(
          path.join(output, "browser.json"),
          JSON.stringify(
            {
              isolated: true,
              origin,
              deployed,
              bundles: Object.fromEntries(bundles),
              proofs,
            },
            null,
            2,
          ),
        );
      } catch (error) {
        throw new Error(
          `${row.asset.identity} (${lost ? "lost" : "accepted"}) at ${page.url()}: ${error.message}`,
          { cause: error },
        );
      } finally {
        await page.close();
        backend.close();
        other?.backend.close();
        directory.close();
        await fs.rm(store, { recursive: true, force: true });
      }
    }
  }
  assert(
    proofs.some((p) => p.lifecycle?.crossAsset?.delayed),
    "missing cross-asset delayed response",
  );
  assert(bundles.size, "loaded production bundle required");
  for (const [name, sha] of bundles)
    assert.equal(
      sha,
      createHash("sha256")
        .update(await fs.readFile(path.join(dist, name.slice(1))))
        .digest("hex"),
      "served bundle must match this build",
    );
  assert.equal(
    proofs.length,
    inventory.registrations.filter((r) => !r.protocol.startsWith("legacy"))
      .length * 2,
  );
} finally {
  await browser.close();
}
console.log(JSON.stringify({ scenarios: proofs.length, isolated: true }));
