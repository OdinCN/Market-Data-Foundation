/** Read-only deployed-page acceptance. No endpoint mocks, no mutation requests.
 * MDF_PLAYWRIGHT_MODULE + MDF_BROWSER_EXECUTABLE select installed browser tools.
 * MDF_WORKBENCH_CASES may name a JSON array of the same case schema for new families.
 * Usage: node scripts/check-live-workbench.mjs <origin> <evidence-dir>
 */
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [origin = "http://192.168.10.20:18766", output] = process.argv.slice(2);
assert(output, "An evidence directory is required");
const { chromium } = await import(
  pathToFileURL(process.env.MDF_PLAYWRIGHT_MODULE).href
);
const cases = process.env.MDF_WORKBENCH_CASES
  ? JSON.parse(await fs.readFile(process.env.MDF_WORKBENCH_CASES, "utf8"))
  : [
      {
        name: "金融日频",
        row: "金融条件日频（十项）",
        asset: "mdf.macro.financial-conditions.daily@r1",
        prefix: "/api/r5/financial/timeseries",
        dates: { 开始日期: "2026-11-04", 结束日期: "2026-11-10" },
        requirePaging: true,
        history: true,
        result: "金融条件日频 范围数据",
        paging: "金融条件日频",
        collection: true,
      },
      {
        name: "CPI",
        rowProperty: "points",
        valueProperty: "index_value",
        row: "美国 CPI",
        asset: "mdf.macro.bls.cpi.headlines@p3-r1",
        prefix: "/api/r5/cpi/timeseries",
        history: true,
        collection: true,
      },
      ...(origin.endsWith(":18766")
        ? [
            {
              name: "TDX",
              row: "沪深北 A 股原生日线",
              asset: "mdf.market.tdx.stock.daily.native@r1",
              prefix: "/api/r5/tdx/daily",
              result: "日线结果",
              paging: "TDX 日线",
              collection: true,
              requirePaging: true,
            },
          ]
        : []),
    ];
await fs.mkdir(output, { recursive: true });
const report = {
  origin,
  started: new Date().toISOString(),
  cases: [],
  errors: [],
  mutations: [],
  responses: [],
};
const browser = await chromium.launch({
  executablePath: process.env.MDF_BROWSER_EXECUTABLE,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});
const context = await browser.newContext({
  viewport: { width: 1440, height: 1040 },
});
await context.route("**/*", (route) => {
  if (["GET", "HEAD"].includes(route.request().method()))
    return route.continue();
  report.mutations.push({
    url: route.request().url(),
    method: route.request().method(),
  });
  return route.abort();
});
const page = await context.newPage();
page.setDefaultTimeout(60000);
page.on("pageerror", (error) => report.errors.push(error.message));
page.on("response", (response) => {
  if (response.url().includes("/api/"))
    report.responses.push({ url: response.url(), status: response.status() });
});
async function snapshot(name) {
  await page.screenshot({
    path: path.join(output, name + ".png"),
    fullPage: true,
  });
  await fs.writeFile(
    path.join(output, name + ".txt"),
    await page.locator("main").innerText(),
  );
}
async function query(c) {
  assert(
    await page
      .getByRole("button", { name: "查询数据", exact: true })
      .evaluate((button) => button.form?.checkValidity() ?? true),
    c.name + ": invalid form",
  );
  // Do not filter for 200: a real 4xx must fail, rather than time out or pass.
  const [response] = await Promise.all([
    page.waitForResponse(
      (r) => new URL(r.url()).pathname === c.prefix + "/query",
      { timeout: 90000 },
    ),
    page.getByRole("button", { name: "查询数据", exact: true }).click(),
  ]);
  assert.equal(
    response.status(),
    200,
    `${c.name} query: HTTP ${response.status()}`,
  );
  const data = await response.json();
  await fs.writeFile(
    path.join(output, c.name + "-query-response.json"),
    JSON.stringify(data),
  );
  const rows = data[c.rowProperty ?? "rows"];
  assert(
    Array.isArray(rows) && rows.length > 0,
    `${c.name}: expected nonempty real rows`,
  );
  assert(
    rows.some(
      (row) =>
        row[c.valueProperty ?? (c.name === "TDX" ? "close" : "value")] != null,
    ),
    `${c.name}: expected actual values, not missing placeholders`,
  );
  assert.equal(data.provider_requests, 0, `${c.name}: expected local data`);
  const table = c.result
    ? page
        .getByRole("region", { name: c.result, exact: true })
        .or(page.getByRole("table", { name: c.result, exact: true }))
    : page.locator(".cpi-timeseries table:visible").last();
  await table.locator("tbody tr").first().waitFor();
  assert(
    (await table.locator("tbody tr").count()) > 0,
    `${c.name}: form without result rows`,
  );
  return {
    url: response.url(),
    rows: rows.length,
    total: data.total,
    provider_requests: data.provider_requests,
  };
}
try {
  for (const c of cases) {
    const item = { name: c.name, asset: c.asset, checks: [] };
    report.cases.push(item);
    console.log("Checking " + c.name + " at " + origin);
    await page.goto(origin + "/pce#ui=r5&view=assets", {
      waitUntil: "domcontentloaded",
    });
    await page
      .getByRole("button", { name: new RegExp("^选择 " + c.row) })
      .click();
    await page.getByRole("button", { name: "查询数据", exact: true }).waitFor();
    assert(
      await page
        .getByRole("heading", { name: "数据资产目录", exact: true })
        .count(),
      "Asset must use the normal catalog renderer",
    );
    for (const [label, value] of Object.entries(c.dates ?? {}))
      await page.getByLabel(label, { exact: true }).fill(value);
    if (c.history) {
      await page
        .getByRole("checkbox", { name: "历史截止查询", exact: true })
        .check();
      await page
        .getByLabel("截止时间（北京时间）", { exact: true })
        .fill(
          c.cutoff ??
            (c.name === "CPI" || origin.endsWith(":18761")
              ? "2026-11-13T00:00:00"
              : "2026-11-13T00:00:00.123456"),
        );
    }
    await page.getByRole("heading", { name: /范围查询/ }).click();
    item.query = await query(c);
    item.checks.push("catalog -> real nonempty local query");
    await page.getByRole("tab", { name: "原始响应", exact: true }).click();
    assert(
      (
        await page.getByLabel("原始 HTTP 正文", { exact: true }).innerText()
      ).includes(JSON.stringify(c.rowProperty ?? "rows")),
      "Raw response must be actual JSON",
    );
    await page.getByRole("tab", { name: "表格", exact: true }).click();
    if (c.requirePaging) {
      const pager = page.getByLabel(c.paging + " 结果分页", { exact: true });
      await pager.waitFor();
      assert(
        await pager
          .getByRole("button", { name: "上一页", exact: true })
          .isDisabled(),
      );
      assert.equal(
        await page
          .getByRole("button", { name: "后一页记录", exact: true })
          .count(),
        0,
        "No second client pager",
      );
      const [response] = await Promise.all([
        page.waitForResponse(
          (r) => new URL(r.url()).pathname === c.prefix + "/query",
        ),
        pager.getByRole("button", { name: "下一页", exact: true }).click(),
      ]);
      assert.equal(response.status(), 200);
      assert((await response.json())[c.rowProperty ?? "rows"].length > 0);
      const [previous] = await Promise.all([
        page.waitForResponse(
          (r) => new URL(r.url()).pathname === c.prefix + "/query",
        ),
        pager.getByRole("button", { name: "上一页", exact: true }).click(),
      ]);
      assert.equal(previous.status(), 200);
      await page.waitForFunction(
        (label) =>
          Array.from(document.querySelectorAll("[aria-label]"))
            .find((el) => el.getAttribute("aria-label") === label)
            ?.querySelector("button")?.disabled === true,
        c.paging + " 结果分页",
      );
      item.checks.push("one server pager, forwards and backwards");
    }
    await snapshot(c.name + "-query");
    await page.getByRole("tab", { name: "API说明", exact: true }).click();
    assert(
      (await page.locator("main").innerText()).includes(c.prefix + "/query"),
    );
    item.checks.push("query -> actual API help");
    if (c.collection) {
      await page.getByRole("button", { name: "采集数据", exact: true }).click();
      assert.equal(
        await page.getByLabel("采集目标", { exact: true }).inputValue(),
        c.asset,
      );
      await page.getByRole("combobox", { name: /^采集方式/ }).waitFor();
      const briefs = page.getByRole("complementary", {
        name: "执行简报",
        exact: true,
      });
      await briefs.getByRole("button").first().waitFor();
      await snapshot(c.name + "-collection");
      await briefs.getByRole("button").first().click();
      const detail = page.getByRole("complementary", {
        name: "执行记录详情",
        exact: true,
      });
      await detail.waitFor();
      assert(await detail.innerText(), "Real job details required");
      const link = page.url();
      assert(
        new URLSearchParams(new URL(link).hash.slice(1)).get("job"),
        "Named job deep link required",
      );
      await page.reload();
      await detail.waitFor();
      assert.equal(page.url(), link);
      await snapshot(c.name + "-execution");
      item.checks.push(
        "normal collection target -> real job brief -> global details -> reload",
      );
      await page.getByRole("tab", { name: "周期任务", exact: true }).click();
      await page
        .getByLabel("周期资产筛选", { exact: true })
        .selectOption(c.asset);
      const periodic = page.getByRole("region", {
        name: "周期任务",
        exact: true,
      });
      await periodic
        .getByRole("button", { name: /^选择 / })
        .first()
        .click();
      await page
        .getByRole("complementary", { name: "周期任务详情", exact: true })
        .waitFor();
      await snapshot(c.name + "-periodic");
      item.checks.push(
        "global periodic filter -> actual details (no switches changed)",
      );
    }
    item.queryCount = report.responses.filter(
      (r) => new URL(r.url).pathname === c.prefix + "/query",
    ).length;
    assert.equal(
      item.queryCount,
      c.requirePaging ? 3 : 1,
      c.name + ": duplicate or implicit queries",
    );
    item.passed = true;
  }
  assert.equal(report.errors.length, 0, "No browser exceptions");
  assert.equal(report.mutations.length, 0, "No attempted business mutations");
  report.passed = true;
} catch (error) {
  report.passed = false;
  report.failure = String(error);
  await snapshot("failure").catch(() => {});
  process.exitCode = 1;
} finally {
  report.ended = new Date().toISOString();
  await fs.writeFile(
    path.join(output, "receipt.json"),
    JSON.stringify(report, null, 2),
  );
  console.log(JSON.stringify(report, null, 2));
  await browser.close();
}
