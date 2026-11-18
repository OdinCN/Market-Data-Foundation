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
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import App from "./App";
import type { FamilyProps } from "./RuntimeFamily";
import type { Transport } from "./api";
import { TdxRosterQuery } from "./TdxRosterFamily";
import { asgi, closeBridges } from "./workflowTestBridge";

let root = "";
afterEach(() => {
  cleanup();
  closeBridges();
  if (root) rmSync(root, { recursive: true, force: true });
  root = "";
  location.hash = "";
  sessionStorage.clear();
});
const props: Omit<FamilyProps, "transport"> = {
  route: { view: "assets", invalid: false, fields: new URLSearchParams() },
  navigate: () => {},
  generation: 0,
};
const dateInput = () => screen.getByLabelText("名册日期") as HTMLInputElement;
const submit = () => screen.getByRole("button", { name: "查询数据" });
const queries = (backend: ReturnType<typeof asgi>) =>
  backend.requests.filter((r) =>
    r.path.startsWith("/api/r5/tdx/roster/query?"),
  );
async function ready(day: string) {
  await waitFor(() => expect(dateInput().value).toBe(day));
  expect((submit() as HTMLButtonElement).disabled).toBe(false);
}

function fixture(scenario = "morning") {
  const backend = asgi(scenario, "tdx_roster_query_fixture.py");
  const transport: Transport = async (path, init) => {
    const response = await backend.transport(path, init);
    // The exchange bridge exposes json(); query's raw HTTP viewer also needs
    // the actual Response.text() contract, as in the existing daily UI rig.
    return Response.json(await response.json(), { status: response.status });
  };
  return { ...backend, transport };
}

it("opens the real catalog query with the published morning date and separates exchange membership from the directory", async () => {
  root = mkdtempSync(join(tmpdir(), "mdf-d01-roster-query-date-"));
  const directory = asgi("financial_manual", "d01_ui_exchange.py", {
    store_dir: root,
  });
  expect(
    (
      await directory.exchange({
        method: "PUT",
        path: "/api/r5/packages/tdx",
        body: {
          expected_revision: null,
          enabled_capability_ids: ["stock_roster"],
          source_changes: [],
        },
      })
    ).status,
  ).toBe(200);
  const backend = fixture();
  const transport: Transport = async (path, init) => {
    if ((init?.method ?? "GET") !== "GET")
      throw Error("navigation_must_not_write");
    return String(path).startsWith("/api/r5/tdx/roster/")
      ? backend.transport(path, init)
      : directory.transport(path, init);
  };
  location.hash = "ui=r5&view=assets";
  render(<App transport={transport} />);
  fireEvent.click(
    await screen.findByRole("button", { name: /选择 沪深北 A 股基础名册/ }),
  );
  fireEvent.click(await screen.findByRole("tab", { name: "查询数据" }));
  await ready("2026-11-17");
  expect(queries(backend)).toHaveLength(0);
  const search = screen.getByPlaceholderText("代码、简称、曾用名、全拼或简拼");
  fireEvent.change(search, { target: { value: "000002" } });
  expect(screen.getByLabelText("请求 URL").textContent).not.toContain("day=");
  fireEvent.click(submit());
  const table = await screen.findByRole("region", { name: "名册结果" });
  expect(within(table).getByText("000002")).toBeTruthy();
  expect(within(table).getByText("万科Ａ")).toBeTruthy();
  expect(within(table).getByText("名册成员状态")).toBeTruthy();
  expect(within(table).getByText("未取得该日逐只核验")).toBeTruthy();
  expect(within(table).queryByText("交易所确认在市")).toBeNull();
  for (const label of screen.getAllByText("TDX 目录观测日"))
    expect(label.nextElementSibling?.textContent).toBe("2026-11-17");
  for (const label of screen.getAllByText("交易所上市/退市证据截至日"))
    expect(label.nextElementSibling?.textContent).toBe("2026-11-13");
  fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
  expect(screen.getByLabelText("原始 HTTP 正文").textContent).toContain(
    '"membership_state":"UNKNOWN"',
  );
  expect(queries(backend)).toHaveLength(1);
  expect(backend.requests.every((r) => r.method === "GET")).toBe(true);
  expect((await backend.exchange({ control: "stats" })).body).toMatchObject({
    unchanged: true,
    provider_requests: 0,
    jobs: 0,
  });
});

it("resolves the automatic historical date at the microsecond cutoff while preserving an explicit later day", async () => {
  const backend = fixture();
  render(<TdxRosterQuery {...props} transport={backend.transport} />);
  await ready("2026-11-17");
  fireEvent.click(screen.getByRole("checkbox", { name: "历史截止查询" }));
  fireEvent.change(screen.getByLabelText("截止时间（北京时间）"), {
    target: { value: "2026-11-17T09:00:31.999999" },
  });
  await ready("2026-11-13");
  fireEvent.change(screen.getByLabelText("代码或名称"), {
    target: { value: "000002" },
  });
  fireEvent.click(submit());
  const table = await screen.findByRole("region", { name: "名册结果" });
  expect(within(table).getByText("未取得该日逐只核验")).toBeTruthy();
  fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
  expect(
    JSON.parse(screen.getByLabelText("原始 HTTP 正文").textContent!).rows[0]
      .membership_state,
  ).toBe("ACTIVE");
  fireEvent.click(screen.getByRole("tab", { name: "表格" }));
  const first = new URL(queries(backend)[0].path, "http://fixture");
  expect(first.searchParams.has("day")).toBe(false);
  expect(first.searchParams.get("as_of")).toBe(
    "2026-11-17T09:00:31.999999+08:00",
  );
  fireEvent.change(dateInput(), { target: { value: "2026-11-17" } });
  fireEvent.click(submit());
  const later = await screen.findByRole("region", { name: "名册结果" });
  expect(within(later).getByText("未取得该日逐只核验")).toBeTruthy();
  fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
  expect(
    JSON.parse(screen.getByLabelText("原始 HTTP 正文").textContent!).rows[0]
      .membership_state,
  ).toBe("UNKNOWN");
  fireEvent.click(screen.getByRole("tab", { name: "表格" }));
  expect(
    new URL(queries(backend)[1].path, "http://fixture").searchParams.get("day"),
  ).toBe("2026-11-17");
  fireEvent.click(screen.getByRole("checkbox", { name: "历史截止查询" }));
  await ready("2026-11-17");
  expect(backend.requests.every((r) => r.method === "GET")).toBe(true);
  expect((await backend.exchange({ control: "stats" })).body.unchanged).toBe(
    true,
  );
});

it("pins the automatic result day, snapshot and search cutoff through server paging after another publication", async () => {
  const backend = fixture();
  render(<TdxRosterQuery {...props} transport={backend.transport} />);
  await ready("2026-11-17");
  fireEvent.click(submit());
  await screen.findByRole("region", { name: "名册结果" });
  await backend.exchange({ control: "publish" });
  fireEvent.click(
    within(screen.getByLabelText("TDX 名册 结果分页")).getByRole("button", {
      name: "下一页",
    }),
  );
  await screen.findByText("分页样本99");
  expect(screen.queryByText("新版不可混页")).toBeNull();
  const page = new URL(queries(backend)[1].path, "http://fixture");
  expect(page.searchParams.get("snapshot_id")).toBe("directory-17");
  expect(page.searchParams.get("day")).toBe("2026-11-17");
  expect(page.searchParams.get("as_of")).toBe("2026-11-17T01:05:00+00:00");
  expect(page.searchParams.get("offset")).toBe("100");
  expect(queries(backend)).toHaveLength(2);
  expect((await backend.exchange({ control: "stats" })).body.unchanged).toBe(
    true,
  );
});

it.each(["prepublish", "legacy"])(
  "uses only the prior evidence for %s rather than an invented current directory date",
  async (scenario) => {
    const backend = fixture(scenario);
    render(<TdxRosterQuery {...props} transport={backend.transport} />);
    await ready("2026-11-13");
    fireEvent.change(screen.getByLabelText("代码或名称"), {
      target: { value: "000002" },
    });
    fireEvent.click(submit());
    const table = await screen.findByRole("region", { name: "名册结果" });
    expect(within(table).getByText("未取得该日逐只核验")).toBeTruthy();
    if (scenario === "legacy")
      expect(screen.getAllByText("未记录目录刷新凭据")).toHaveLength(2);
    expect(queries(backend)).toHaveLength(1);
    expect((await backend.exchange({ control: "stats" })).body.unchanged).toBe(
      true,
    );
  },
);

it.each(["08:00:00", "18:00:00"])(
  "uses the same visible exchange originals for default and explicit Nov 13 at Nov 14 %s",
  async (time) => {
    const backend = fixture("late");
    render(<TdxRosterQuery {...props} transport={backend.transport} />);
    await ready("2026-11-17");
    fireEvent.click(screen.getByRole("checkbox", { name: "历史截止查询" }));
    fireEvent.change(screen.getByLabelText("截止时间（北京时间）"), {
      target: { value: `2026-11-14T${time}` },
    });
    await ready("2026-11-13");
    fireEvent.change(screen.getByLabelText("代码或名称"), {
      target: { value: "000002" },
    });
    fireEvent.click(submit());
    const first = await screen.findByRole("region", { name: "名册结果" });
    expect(within(first).getByText("未取得该日逐只核验")).toBeTruthy();
    expect(
      new URL(queries(backend)[0].path, "http://fixture").searchParams.has(
        "day",
      ),
    ).toBe(false);
    for (const label of screen.getAllByText("发布快照"))
      expect(label.nextElementSibling?.textContent).toBe("exchange-13");
    for (const label of screen.getAllByText("TDX 目录快照"))
      expect(label.nextElementSibling?.textContent).toBe("directory-13");
    for (const label of screen.getAllByText("TDX 目录观测日"))
      expect(label.nextElementSibling?.textContent).toBe("2026-11-13");
    for (const label of screen.getAllByText("TDX 目录实际可用时间"))
      expect(label.nextElementSibling?.textContent).toBe(
        "2026-11-13 09:00:32 北京时间",
      );
    fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
    const implicit = JSON.parse(
      screen.getByLabelText("原始 HTTP 正文").textContent!,
    );
    expect(implicit.rows[0].membership_state).toBe("ACTIVE");
    expect(implicit.rows[0].roster_membership_state).toBe("UNVERIFIED");
    expect(implicit.sources[0].url).toBe("https://fixture.invalid/exchange-13");
    // Change away and back through the actual control to make the date explicit.
    fireEvent.change(dateInput(), { target: { value: "2026-11-12" } });
    fireEvent.change(dateInput(), { target: { value: "2026-11-13" } });
    fireEvent.click(submit());
    await screen.findByLabelText("原始 HTTP 正文");
    const explicit = JSON.parse(
      screen.getByLabelText("原始 HTTP 正文").textContent!,
    );
    expect(explicit.snapshot_id).toBe(implicit.snapshot_id);
    expect(explicit.rows).toEqual(implicit.rows);
    expect(explicit.directory_snapshot_id).toBe(implicit.directory_snapshot_id);
    expect(explicit.sources).toEqual(implicit.sources);
    expect(
      new URL(queries(backend)[1].path, "http://fixture").searchParams.get(
        "day",
      ),
    ).toBe("2026-11-13");
    expect(queries(backend)).toHaveLength(2);
    expect(backend.requests.every((r) => r.method === "GET")).toBe(true);
    expect((await backend.exchange({ control: "stats" })).body.unchanged).toBe(
      true,
    );
  },
);

it("pages the actual exchange snapshot and cutoff while retaining separately dated directory evidence", async () => {
  const backend = fixture("late");
  render(<TdxRosterQuery {...props} transport={backend.transport} />);
  await ready("2026-11-17");
  fireEvent.click(screen.getByRole("checkbox", { name: "历史截止查询" }));
  fireEvent.change(screen.getByLabelText("截止时间（北京时间）"), {
    target: { value: "2026-11-14T08:00:00" },
  });
  await ready("2026-11-13");
  fireEvent.click(submit());
  await screen.findByRole("region", { name: "名册结果" });
  await backend.exchange({ control: "publish" });
  fireEvent.click(
    within(screen.getByLabelText("TDX 名册 结果分页")).getByRole("button", {
      name: "下一页",
    }),
  );
  await screen.findByText("分页样本99");
  expect(screen.queryByText("新版不可混页")).toBeNull();
  const page = new URL(queries(backend)[1].path, "http://fixture");
  expect(page.searchParams.get("snapshot_id")).toBe("exchange-13");
  expect(page.searchParams.get("as_of")).toBe("2026-11-14T08:00:00+08:00");
  expect(page.searchParams.get("day")).toBe("2026-11-13");
  for (const label of screen.getAllByText("TDX 目录快照"))
    expect(label.nextElementSibling?.textContent).toBe("directory-13");
  expect(queries(backend)).toHaveLength(2);
  expect(backend.requests.every((r) => r.method === "GET")).toBe(true);
  expect((await backend.exchange({ control: "stats" })).body.unchanged).toBe(
    true,
  );
});
