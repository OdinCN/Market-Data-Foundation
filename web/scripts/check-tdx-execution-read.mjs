/** Candidate only, copied journal + real read routers. Never contacts live HTTP. */
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";
import { createHash } from "node:crypto";

const [fixture, output] = process.argv.slice(2);
const dist = process.env.MDF_DETAIL_DIST;
assert(
  fixture && output && dist,
  "explicit isolated fixture/output/dist required",
);
const samples = JSON.parse(
  await fs.readFile(path.join(fixture, "fixture-only.json")),
);
assert(samples.length >= 2 && samples.every((j) => j.step_count > 5000));
await fs.mkdir(output, { recursive: true });
const child = spawn(
  path.resolve(".venv/bin/python"),
  ["-u", "tests/tdx_job_read_fixture.py", fixture],
  {
    env: {
      ...process.env,
      PYTHONDONTWRITEBYTECODE: "1",
      PYTHONPATH: path.resolve("src") + ":" + process.cwd(),
    },
  },
);
const pending = [];
let stderr = "";
child.stderr.on("data", (b) => (stderr += String(b)));
createInterface({ input: child.stdout }).on("line", (line) =>
  pending.shift()?.resolve(JSON.parse(line)),
);
child.on("exit", (code) =>
  pending
    .splice(0)
    .forEach((p) => p.reject(Error(`fixture ${code}: ${stderr}`))),
);
const exchange = (message) =>
  new Promise((resolve, reject) => {
    pending.push({ resolve, reject });
    child.stdin.write(JSON.stringify(message) + "\n");
  });
const { chromium } = await import(
  pathToFileURL(
    process.env.MDF_PLAYWRIGHT_MODULE ??
      path.resolve("web/node_modules/playwright/index.mjs"),
  ).href
);
const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.MDF_BROWSER_EXECUTABLE,
  args: ["--no-sandbox"],
});
const proofs = [];
try {
  for (const filter of ["all", "mdf.market.tdx.stock.daily.native@r1"]) {
    const context = await browser.newContext();
    const page = await context.newPage();
    const reads = [],
      writes = [],
      errors = [],
      gates = [];
    let holdSummary = true,
      holdSteps = true,
      heldStatuses = true;
    const wait = (kind) =>
      new Promise((resolve) => gates.push({ kind, resolve }));
    const release = (kind) =>
      gates.filter((g) => g.kind === kind).forEach((g) => g.resolve());
    await context.route("**/*", async (route) => {
      const request = route.request(),
        url = new URL(request.url());
      try {
        if (!["GET", "HEAD"].includes(request.method())) {
          writes.push(request.url());
          return route.abort();
        }
        if (url.pathname.startsWith("/api/")) {
          reads.push(url.pathname + url.search);
          if (url.pathname.includes("/timeseries/status")) {
            if (!url.pathname.includes("consumption") && heldStatuses)
              await wait("status");
            return route.fulfill({
              status: 503,
              contentType: "application/json",
              body: JSON.stringify({
                classification: "cpi_timeseries_storage_read_failed",
              }),
            });
          }
          if (url.pathname.endsWith("/jobs/" + samples[0].id) && holdSummary)
            await wait("summary");
          if (url.searchParams.get("view") === "steps" && holdSteps)
            await wait("steps");
          const result = await exchange({
            path: url.pathname + url.search,
            method: "GET",
          });
          return route.fulfill({
            status: result.status,
            contentType: "application/json",
            body: JSON.stringify(result.body),
          });
        }
        const relative = url.pathname.startsWith("/assets/")
          ? url.pathname.slice(1)
          : url.pathname.endsWith(".png")
            ? url.pathname.slice(1)
            : "index.html";
        const file = path.resolve(dist, relative);
        assert(file.startsWith(path.resolve(dist) + path.sep));
        return route.fulfill({
          body: await fs.readFile(file),
          contentType: file.endsWith(".js")
            ? "text/javascript"
            : file.endsWith(".css")
              ? "text/css"
              : file.endsWith(".png")
                ? "image/png"
                : "text/html",
        });
      } catch (error) {
        if (!page.isClosed() && !String(error).includes("already handled"))
          errors.push(String(error));
      }
    });
    page.on("pageerror", (e) => errors.push(e.message));
    const hash = new URLSearchParams({
      ui: "r5",
      view: "collection",
      tab: "executions",
      asset: "mdf.market.tdx.stock.daily.native",
      asset_revision: "r1",
      execution_asset: filter,
      job: samples[0].id,
    });
    const start = Date.now();
    try {
      await page.goto("http://mdf-detail-isolated.invalid/pce#" + hash);
      const shell = () =>
        page.getByRole("region", { name: "执行详情主体", exact: true });
      await shell().waitFor({ timeout: 15000 });
      assert.equal(
        await shell().getAttribute("data-execution-id"),
        samples[0].id,
      );
      assert(
        holdSummary && holdSteps,
        "facts must precede blocked detail and shard reads",
      );
      const firstFactsMs = Date.now() - start;
      await shell()
        .getByRole("button", { name: "查看周期任务", exact: true })
        .click();
      await page
        .locator('[aria-label="周期操作"][data-periodic-id="tdx-daily-auto"]')
        .waitFor();
      await page
        .getByRole("button", { name: "返回此执行记录", exact: true })
        .click();
      await shell().waitFor();
      assert.equal(
        await shell().getAttribute("data-execution-id"),
        samples[0].id,
      );
      await page
        .locator(`tr[data-execution-id="${samples[1].id}"] button`)
        .click();
      await page.waitForFunction(
        (id) =>
          document
            .querySelector('[aria-label="执行详情主体"]')
            ?.getAttribute("data-execution-id") === id,
        samples[1].id,
      );
      await shell()
        .getByRole("button", { name: "查看周期任务", exact: true })
        .click();
      await page
        .getByRole("button", { name: "返回此执行记录", exact: true })
        .click();
      await page.waitForFunction(
        (id) =>
          document
            .querySelector('[aria-label="执行详情主体"]')
            ?.getAttribute("data-execution-id") === id,
        samples[1].id,
      );
      const refresh = page.getByRole("button", { name: "刷新", exact: true });
      assert.equal(
        await refresh.isEnabled(),
        true,
        "other status reads cannot disable refresh",
      );
      await refresh.click();
      await shell().waitFor();
      assert.equal(
        await shell().getAttribute("data-execution-id"),
        samples[1].id,
      );
      holdSummary = false;
      holdSteps = false;
      release("summary");
      release("steps");
      const table = page.getByRole("region", { name: "采集分片", exact: true });
      await table.locator("tbody tr").first().waitFor();
      assert.equal(await table.locator("tbody tr").count(), 25);
      await page
        .getByRole("button", { name: "后一页分片", exact: true })
        .click();
      await page.waitForFunction(() =>
        document
          .querySelector('[aria-label="采集分片分页"]')
          ?.textContent.includes("26–50"),
      );
      await page
        .getByLabel("搜索采集分片", { exact: true })
        .fill(samples[1].sample);
      await page.getByRole("button", { name: "搜索分片", exact: true }).click();
      await table.getByText(samples[1].sample, { exact: true }).waitFor();
      assert.equal(
        await shell().getAttribute("data-execution-id"),
        samples[1].id,
      );
      assert.equal(writes.length, 0);
      assert(
        reads.some((p) => p.includes("view=steps") && p.includes("offset=25")),
      );
      proofs.push({
        filter,
        ids: samples.map((s) => s.id),
        firstFactsMs,
        blockedSummary: true,
        blockedShards: true,
        policyReturns: true,
        refresh: true,
        search: true,
        serverPageRows: 25,
        reads,
        writes,
        errors,
      });
      assert.deepEqual(errors, []);
    } finally {
      holdSummary = false;
      holdSteps = false;
      heldStatuses = false;
      for (const g of gates) g.resolve();
      await context.close();
    }
  }
  const html = await fs.readFile(path.join(dist, "index.html"), "utf8");
  const bundle = html.match(/src="([^"]+\.js)"/)[1];
  const sha256 = createHash("sha256")
    .update(await fs.readFile(path.join(dist, bundle.slice(1))))
    .digest("hex");
  await fs.writeFile(
    path.join(output, "browser.json"),
    JSON.stringify({ proofs, bundle, sha256, isolated: true }, null, 2),
  );
  console.log(
    JSON.stringify({
      cases: proofs.length,
      largeJobs: samples.length,
      isolated: true,
    }),
  );
} finally {
  await browser.close();
  child.stdin.end();
  child.kill();
}
