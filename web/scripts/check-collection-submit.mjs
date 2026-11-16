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
  if (row.resumePath) {
    await resume.waitFor();
    const mustConfirm = failed.getByRole("checkbox", {
      name: "我确认按原请求恢复采集",
    });
    if (await mustConfirm.count()) await mustConfirm.check();
    const resumed = page.waitForResponse(
      (r) =>
        r.request().method() === "POST" &&
        new URL(r.url()).pathname === row.resumePath,
    );
    await resume.click();
    assert((await resumed).ok());
    const last = writes().at(-1);
    assert.equal(last.body.request_id, secondId);
    assert.equal(last.path, row.resumePath);
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
    locked: true,
    reopened: true,
    selectedAfterSwitch: true,
    roundTrip: true,
  };
}

// The two records deliberately share a UUID. The chosen asset must continue to
// own the detail, selected row and actions after the older response arrives.
async function exerciseCrossAsset(page, baseRow, other, id, pause) {
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
  const route = new URL(page.url());
  route.hash = new URLSearchParams({
    ui: "r5",
    view: "collection",
    tab: "executions",
    execution_asset: "all",
    execution_schedule: "all",
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
        let posted = false,
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
          };
          calls.push(call);
          let out;
          if (/^\/api\/r5\/(sources|packages)/.test(url.pathname)) {
            assert.equal(method, "GET");
            out = await directory.exchange(call);
          } else if (url.pathname.startsWith(row.path.replace(/\/jobs$/, ""))) {
            if (
              posted &&
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
          if (method === "POST" && out.status >= 200 && out.status < 300) {
            posted = true;
            if (lost) return route.abort("failed");
          }
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
        await submit.click();
        const briefs = page.getByRole("complementary", { name: "执行简报" });
        if (lost) {
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
