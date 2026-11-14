/** Built App + real ASGI job routers, isolated stores, no live HTTP/PG/provider.
 * node web/scripts/check-collection-submit.mjs <matrix.json> <evidence-dir>
 * Matrix input is generated from the actual product registrations by CI.
 */
import assert from "node:assert/strict";
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
  pathToFileURL(process.env.MDF_PLAYWRIGHT_MODULE).href
);
const browser = await chromium.launch({
  executablePath: process.env.MDF_BROWSER_EXECUTABLE,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});
const proofs = [];
try {
  for (const row of inventory.registrations.filter(
    (r) => !r.protocol.startsWith("legacy"),
  )) {
    for (const lost of [false, true]) {
      const backend = bridge(row.profile ?? row.protocol);
      const store = await fs.mkdtemp(
        path.join(tmpdir(), "mdf-d01-submit-browser-"),
      );
      const directory = bridge("financial_manual", "d01_ui_exchange.py", {
        scenario: "financial_manual",
        store_dir: store,
      });
      const page = await browser.newPage();
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
        let posted = false,
          allowRead = !lost;
        const calls = [];
        await page.route("**/*", async (route) => {
          const request = route.request(),
            url = new URL(request.url()),
            method = request.method();
          if (!url.pathname.startsWith("/api/")) {
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
                body: await fs.readFile(path.join(root, "web/dist", file)),
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
          } else {
            assert.equal(method, "GET", "unregistered write");
            out = { status: 404, body: { classification: "not_enabled" } };
          }
          call.status = out.status;
          if (method === "POST" && out.status >= 200 && out.status < 300) {
            posted = true;
            if (lost) return route.abort("failed");
          }
          return route.fulfill({ status: out.status, json: out.body });
        });
        const ready = page.waitForResponse(
          (r) =>
            new URL(r.url()).pathname ===
              row.path.replace(/\/jobs$/, "/status") && r.status() === 200,
        );
        await page.goto(
          "http://mdf-isolated.invalid/pce#" +
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
          await input.blur();
        }
        await form.getByRole("checkbox").check();
        await submit.click();
        const briefs = page.getByRole("complementary", { name: "执行简报" });
        if (lost) {
          const recall = page.getByRole("button", {
            name: /核实原请求|回查同一任务|回查原名册任务/,
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
        await card.click();
        const detail = page.getByRole("complementary", {
          name: "执行记录详情",
        });
        await detail.getByText(id, { exact: false }).last().waitFor();
        assert.equal(
          new URLSearchParams(new URL(page.url()).hash.slice(1)).get("tab"),
          "executions",
        );
        assert.equal(calls.filter((c) => c.method === "POST").length, 1);
        const stored = await backend.exchange({ control: "stored" });
        assert(stored.body.records.some((j) => j.request_id === id));
        proofs.push({
          asset: row.asset,
          variant: row.variant,
          lost,
          id,
          postCount: 1,
          stay: true,
          explicitDetail: true,
          calls,
        });
        await fs.writeFile(
          path.join(output, "browser.json"),
          JSON.stringify({ isolated: true, proofs }, null, 2),
        );
      } finally {
        await page.close();
        backend.close();
        directory.close();
        await fs.rm(store, { recursive: true, force: true });
      }
    }
  }
  assert.equal(
    proofs.length,
    inventory.registrations.filter((r) => !r.protocol.startsWith("legacy"))
      .length * 2,
  );
} finally {
  await browser.close();
}
console.log(JSON.stringify({ scenarios: proofs.length, isolated: true }));
