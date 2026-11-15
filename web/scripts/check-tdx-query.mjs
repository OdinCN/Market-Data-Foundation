/** Actual 18766 UI + read-only real data, and explicitly simulated intraday ASGI.
 * No POST/PUT/DELETE, no collector, no artificial PG writes.
 * node web/scripts/check-tdx-query.mjs <origin> <evidence-dir>
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { createHash } from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
const [origin = "http://192.168.10.20:18766", output] = process.argv.slice(2);
assert(output);
await fs.mkdir(output, { recursive: true });
const root = process.cwd(),
  dist = process.env.MDF_QUERY_DIST ?? path.join(root, "web/dist");
const today = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
}).format(new Date());
const { chromium } = await import(
  pathToFileURL(process.env.MDF_PLAYWRIGHT_MODULE).href
);
const browser = await chromium.launch({
  executablePath: process.env.MDF_BROWSER_EXECUTABLE,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});
const report = {
  origin,
  started: new Date().toISOString(),
  realIntraday: false,
  cases: [],
  mutations: [],
  bundles: {},
};
function bridge(scenario) {
  const child = spawn(
    path.join(root, ".venv/bin/python"),
    ["-u", "tests/tdx_query_ui_exchange.py", scenario],
    {
      cwd: root,
      env: {
        ...process.env,
        PYTHONDONTWRITEBYTECODE: "1",
        PYTHONPATH: `${root}/src:${root}/tests:${root}`,
      },
    },
  );
  const queue = [];
  let errors = "";
  child.stderr.on("data", (b) => (errors += b));
  createInterface({ input: child.stdout }).on("line", (l) =>
    queue.shift()?.resolve(JSON.parse(l)),
  );
  child.on("exit", (code) =>
    queue
      .splice(0)
      .forEach((p) => p.reject(Error(`fixture ${code}: ${errors}`))),
  );
  return {
    exchange: (message) =>
      new Promise((resolve, reject) => {
        queue.push({ resolve, reject });
        child.stdin.write(JSON.stringify(message) + "\n");
      }),
    close: () => {
      child.stdin.end();
      child.kill();
    },
  };
}
async function save() {
  await fs.writeFile(
    path.join(output, "browser.json"),
    JSON.stringify(report, null, 2),
  );
}
async function open(name, scenario) {
  const backend = scenario ? bridge(scenario) : null;
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1100 },
  });
  page.setDefaultTimeout(60000);
  const calls = [];
  await page.route("**/*", async (route) => {
    const r = route.request(),
      u = new URL(r.url());
    if (!["GET", "HEAD"].includes(r.method())) {
      report.mutations.push({ url: r.url(), method: r.method() });
      return route.abort();
    }
    if (u.pathname.startsWith("/api/"))
      calls.push({ path: u.pathname + u.search, method: r.method() });
    if (backend && u.pathname.startsWith("/api/r5/tdx/daily/")) {
      const out = await backend.exchange({ path: u.pathname + u.search });
      return route.fulfill({ status: out.status, json: out.body });
    }
    return route.continue();
  });
  page.on("response", async (r) => {
    const name = new URL(r.url()).pathname;
    if (
      name.startsWith("/assets/") &&
      name.endsWith(".js") &&
      r.status() === 200
    )
      report.bundles[name] = createHash("sha256")
        .update(await r.body())
        .digest("hex");
  });
  await page.goto(origin + "/pce#ui=r5&view=assets&package=tdx");
  await page.getByRole("button", { name: /选择 沪深北 A 股原生日线/ }).click();
  await page.getByRole("button", { name: "查询数据", exact: true }).waitFor();
  return { page, backend, calls, name, scenario };
}
async function dateField(page, label, value) {
  const input = page.getByLabel(label, { exact: true });
  await input.fill(value);
  await input.blur();
}
async function query(h) {
  const response = h.page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === "/api/r5/tdx/daily/query" &&
      r.request().method() === "GET",
  );
  await h.page.getByRole("button", { name: "查询数据", exact: true }).click();
  const r = await response;
  assert.equal(r.status(), 200);
  const body = await r.json();
  await h.page.getByRole("region", { name: "日线结果", exact: true }).waitFor();
  assert(
    Number.isInteger(body.provider_requests) && body.provider_requests >= 0,
  );
  const keys = body.rows.map(
    (x) => `${x.trading_day}:${x.market}:${x.instrument}`,
  );
  assert.equal(keys.length, new Set(keys).size);
  return body;
}
async function capture(h, body) {
  const entry = {
    name: h.name,
    simulation: !!h.scenario,
    scenario: h.scenario,
    actualWallClock: new Date().toISOString(),
    body,
    requests: h.calls,
  };
  if (h.backend) {
    entry.fixture = (await h.backend.exchange({ control: "stats" })).body;
    assert(entry.fixture.store_unchanged);
    assert.equal(entry.fixture.job_files, 0);
    assert(entry.fixture.all_sources_closed);
  }
  await h.page.screenshot({
    path: path.join(output, h.name + ".png"),
    fullPage: true,
  });
  await fs.writeFile(
    path.join(output, h.name + ".txt"),
    await h.page.locator("main").innerText(),
  );
  report.cases.push(entry);
  await save();
}
async function close(h) {
  await h.page.close();
  h.backend?.close();
}
try {
  for (const mode of ["history", "day"]) {
    const h = await open("real-" + mode);
    try {
      if (mode === "history") {
        await dateField(h.page, "开始日期", "2026-11-13");
        await dateField(h.page, "结束日期", "2026-11-13");
      } else {
        await h.page.getByLabel("查询方向").selectOption("day");
        await dateField(h.page, "交易日", "2026-11-13");
      }
      const body = await query(h);
      assert(
        body.rows.length > 0,
        "known persisted sample must show real rows",
      );
      assert(body.rows.every((r) => r.trading_day === "2026-11-13"));
      assert(body.rows.some((r) => Number.isInteger(r.close) && r.close > 0));
      await capture(h, body);
    } finally {
      await close(h);
    }
  }
  const current = await open("real-current-cross-section");
  try {
    await current.page.getByLabel("查询方向").selectOption("day");
    await dateField(current.page, "交易日", today);
    const body = await query(current);
    assert(body.rows.every((r) => r.trading_day === today));
    if (
      ["pre_open", "market_closed", "future_day"].includes(
        body.query_coverage?.session,
      )
    ) {
      assert.equal(body.provider_requests, 0);
      assert.equal(body.rows.length, 0);
    } else if (
      body.rows.some((r) => r.origin === "upstream" && r.is_complete === false)
    )
      report.realIntraday = true;
    await capture(current, body);
  } finally {
    await close(current);
  }
  for (const mode of ["history", "day"]) {
    const h = await open("simulated-intraday-" + mode, "intraday");
    try {
      if (mode === "history") {
        await dateField(h.page, "开始日期", "2026-11-13");
        await dateField(h.page, "结束日期", "2026-11-16");
      } else {
        await h.page.getByLabel("查询方向").selectOption("day");
        await h.page.getByLabel("交易所").selectOption("all");
        await dateField(h.page, "交易日", "2026-11-16");
      }
      const first = await query(h);
      const rows = first.rows.filter((r) => r.trading_day === "2026-11-16");
      assert.equal(rows.length, mode === "day" ? 3 : 1);
      assert(
        rows.every(
          (r) =>
            r.persisted === false &&
            r.is_complete === false &&
            r.available_at === null &&
            r.observed_at,
        ),
      );
      assert.equal(first.provider_requests, mode === "day" ? 3 : 1);
      await h.page
        .getByText(/临时 TDX（未入库）/)
        .first()
        .waitFor();
      const second = await query(h);
      const changed = second.rows.find(
        (r) => r.trading_day === "2026-11-16" && r.instrument === "600519",
      );
      const old = rows.find((r) => r.instrument === "600519");
      assert(changed.close > old.close);
      assert.notEqual(changed.observed_at, old.observed_at);
      await h.page.getByRole("tab", { name: "原始响应", exact: true }).click();
      assert(
        (await h.page.getByLabel("原始 HTTP 正文").innerText()).includes(
          "intraday_incomplete",
        ),
      );
      await h.page.getByRole("tab", { name: "表格", exact: true }).click();
      await h.page.getByRole("tab", { name: "API说明", exact: true }).click();
      await h.page.getByText(/查询与采集解耦/).waitFor();
      await h.page.getByRole("tab", { name: "查询数据", exact: true }).click();
      await capture(h, second);
      const stats = (await h.backend.exchange({ control: "stats" })).body;
      assert.equal(stats.source_requests, mode === "day" ? 6 : 2);
    } finally {
      await close(h);
    }
  }
  const paging = await open("simulated-empty-candidate-pagination", "paging");
  try {
    await paging.page.getByLabel("查询方向").selectOption("day");
    await paging.page.getByLabel("交易所").selectOption("all");
    await dateField(paging.page, "交易日", "2026-11-16");
    const first = await query(paging);
    assert.equal(first.next_offset, 2);
    assert.equal(first.total, 4);
    assert.equal(first.rows.length, 1);
    const response = paging.page.waitForResponse((r) =>
      new URL(r.url()).pathname.endsWith("/tdx/daily/query"),
    );
    await paging.page
      .getByLabel("TDX 日线 结果分页")
      .getByRole("button", { name: "下一页" })
      .click();
    const second = await (await response).json();
    assert.equal(second.offset, 2);
    assert.deepEqual(
      second.rows.map((r) => r.instrument),
      ["000003", "000004"],
    );
    const previous = paging.page.waitForResponse((r) =>
      new URL(r.url()).pathname.endsWith("/tdx/daily/query"),
    );
    await paging.page
      .getByLabel("TDX 日线 结果分页")
      .getByRole("button", { name: "上一页" })
      .click();
    const back = await (await previous).json();
    assert.equal(back.offset, 0);
    assert.equal(back.snapshot_id, first.snapshot_id);
    await capture(paging, back);
  } finally {
    await close(paging);
  }
  assert.equal(report.mutations.length, 0);
  assert(report.bundles && Object.keys(report.bundles).length);
  for (const [name, sha] of Object.entries(report.bundles))
    assert.equal(
      sha,
      createHash("sha256")
        .update(await fs.readFile(path.join(dist, name.slice(1))))
        .digest("hex"),
      "18766 must serve the candidate build",
    );
  report.finished = new Date().toISOString();
  report.passed = true;
  await save();
  console.log(
    JSON.stringify({
      passed: true,
      cases: report.cases.length,
      realIntraday: report.realIntraday,
      mutations: report.mutations,
    }),
  );
} finally {
  await browser.close();
}
