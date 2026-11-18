/** Exercise both deployed forms using isolated real ASGI job routes. All other writes are blocked.
 * Run from the repo root: node web/scripts/check-tdx-submit.mjs <evidence-dir>
 */
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
const output = process.argv[2];
assert(output);
await fs.mkdir(output, { recursive: true });
const state = await fs.mkdtemp(path.join(output, "jobs-"));
const origin = "http://192.168.10.20:18766";
const { chromium } = await import(
  pathToFileURL(process.env.MDF_PLAYWRIGHT_MODULE).href
);
const browser = await chromium.launch({
  executablePath: process.env.MDF_BROWSER_EXECUTABLE,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});
const scenarios = [];
const loseReceipt = process.argv.includes("--lost-receipt");
try {
  for (const [family, asset, confirm, submit] of [
    [
      "daily",
      "mdf.market.tdx.stock.daily.native@r1",
      "我确认将以上范围采集到开发测试库",
      "提交 TDX 日线 采集",
    ],
    [
      "roster",
      "mdf.market.tdx.stock.roster@r1",
      "我确认刷新三市名册到开发测试库",
      "提交采集任务",
    ],
  ]) {
    const page = await browser.newPage({ serviceWorkers: "block" });
    const posts = [],
      reads = [],
      unexpected = [];
    let allowRecall = !loseReceipt;
    await page.route("**/*", async (route) => {
      const r = route.request(),
        url = new URL(r.url()),
        method = r.method();
      if (
        url.origin === origin &&
        /^\/api\/r5\/tdx\/(daily|roster)\/jobs(?:\/[a-f0-9-]{36})?$/.test(
          url.pathname,
        )
      ) {
        const body = r.postDataJSON();
        if (method === "POST") posts.push(body.request_id);
        else reads.push(url.pathname);
        const out = JSON.parse(
          execFileSync(
            ".venv/bin/python",
            ["-m", "tests.tdx_submit_browser_fixture", state],
            {
              input: JSON.stringify({ path: url.pathname, method, body }),
              encoding: "utf8",
            },
          ),
        );
        if (
          loseReceipt &&
          (method === "POST" ||
            (!allowRecall && url.pathname.endsWith("/" + posts[0])))
        )
          return route.abort("failed");
        return route.fulfill({ status: out.status, json: out.body });
      }
      if (method === "GET" || method === "HEAD") return route.continue();
      unexpected.push(url.pathname);
      return route.abort();
    });
    const [id, rev] = asset.split("@");
    await page.goto(
      origin +
        "/pce#" +
        new URLSearchParams({
          ui: "r5",
          view: "collection",
          tab: "new",
          asset: id,
          asset_revision: rev,
        }),
    );
    const button = page.getByRole("button", { name: submit, exact: true });
    await page.getByRole("checkbox", { name: confirm, exact: true }).check();
    await button.click();
    if (loseReceipt) {
      await page
        .getByRole("alert")
        .filter({ hasText: /回执未核实/ })
        .waitFor();
      await page.reload();
      await page.locator("code").filter({ hasText: posts[0] }).waitFor();
      assert(await button.isDisabled());
      allowRecall = true;
      await page.getByRole("button", { name: "核实原请求" }).click();
    }
    const briefs = page.getByRole("complementary", { name: "执行简报" });
    const card = briefs.locator("button[aria-current=true]");
    await card.waitFor();
    assert.equal(posts.length, 1);
    assert((await card.innerText()).includes(posts[0]));
    assert.equal(
      new URLSearchParams(new URL(page.url()).hash.slice(1)).get("tab"),
      "new",
    );
    await page.screenshot({
      path: path.join(output, family + "-accepted.png"),
    });
    await card.click();
    const detail = page.getByRole("complementary", { name: "执行记录详情" });
    await detail.getByText(posts[0], { exact: false }).last().waitFor();
    await page.reload();
    await detail.getByText(posts[0], { exact: false }).last().waitFor();
    assert.equal(
      new URLSearchParams(new URL(page.url()).hash.slice(1)).get("job"),
      posts[0],
    );
    assert(reads.some((url) => url.endsWith("/" + posts[0])));
    assert.equal(posts.length, 1);
    assert.deepEqual(unexpected, []);
    scenarios.push({
      family,
      lost_receipt_and_reload: loseReceipt,
      request_id: posts[0],
      stored_detail: page.url(),
      posts: posts.length,
    });
    await page.close();
  }
  await fs.writeFile(
    path.join(output, "receipt.json"),
    JSON.stringify(scenarios, null, 2),
  );
  console.log(JSON.stringify(scenarios));
} finally {
  await browser.close();
}
