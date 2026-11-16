import { openSubmittedBrief } from "./openSubmittedBrief.testHelper";
import "./offlineTestSetup";
import { afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import App from "./App";
import { asgi, closeBridges } from "./workflowTestBridge";
import { CPI, MANUAL_OPERATION, HISTORY_OPERATION } from "./workflowModel";
import type { Transport } from "./api";

const query = { reference: "2026-09", as_of: "2026-10-14T13:00:00+00:00" };
afterEach(() => {
  cleanup();
  closeBridges();
  location.hash = "";
  vi.restoreAllMocks();
  sessionStorage.clear();
});
function route(operation = MANUAL_OPERATION) {
  location.hash = new URLSearchParams({
    page: "4",
    asset: CPI.identity,
    asset_revision: CPI.revision,
    operation,
  }).toString();
}
const button = () =>
  screen.getByRole("button", { name: "创建采集任务" }) as HTMLButtonElement;
function months(value: string) {
  for (const name of ["开始参考月", "结束参考月"])
    fireEvent.change(screen.getByLabelText(name), { target: { value } });
}

// The Host keeps returning the pre-submit empty list. A transport failure cannot
// establish whether the POST reached it, so this must never authorize a retry.
function staleHost(postReply?: () => Promise<Response>) {
  const api = asgi("dev", "b3_dev_ui_exchange.py");
  const posts: string[] = [];
  const gets: string[] = [];
  let intent: string | undefined;
  const transport: Transport = async (input, init) => {
    if (init?.method === "POST") {
      posts.push(JSON.parse(String(init.body)).job_identity);
      if (postReply) return postReply();
      throw new Error("synthetic_lost_receipt");
    }
    gets.push(String(input));
    const response = await api.transport(input, init);
    const body = await response.json();
    return {
      ...response,
      json: async () =>
        String(input).startsWith("/api/r5/cpi/jobs") && intent
          ? { ...body, intent_identity: intent }
          : body,
    } as Response;
  };
  return {
    transport,
    posts,
    gets,
    setIntent: (value: string) => (intent = value),
  };
}

async function openCandidate(transport: Transport) {
  route();
  const view = render(<App transport={transport} initialQuery={query} />);
  await screen.findByText(/Operator：ao-dev-cpi-05/);
  months("2026-09");
  fireEvent.click(screen.getByRole("checkbox"));
  return view;
}

it("lost receipt survives mode changes and reconfirmation with only original-ID GETs and no second POST", async () => {
  const host = staleHost();
  await openCandidate(host.transport);
  fireEvent.click(button());
  await screen.findByText(/提交回执未核实/);
  const gets = host.gets.length;
  fireEvent.click(screen.getByRole("button", { name: "指定范围回补" }));
  fireEvent.click(screen.getByRole("button", { name: "一次性初始化" }));
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(button());
  expect(host.posts).toHaveLength(1);
  for (const path of host.gets.slice(gets)) {
    const read = new URL(path, "http://fixture.invalid");
    expect(read.pathname).toBe("/api/r5/cpi/jobs");
    expect(read.searchParams.get("job_identity")).toBe(host.posts[0]);
  }
  expect(button().disabled).toBe(true);
});

it("lost receipt survives date changes, reset, tab navigation and remount with empty jobs", async () => {
  const host = staleHost();
  const view = await openCandidate(host.transport);
  fireEvent.click(button());
  await screen.findByText(/提交回执未核实/);
  months("2026-08");
  months("2026-09");
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(button());
  expect(button().disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "重置表单" }));
  months("2026-09");
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(button());
  expect(button().disabled).toBe(true);
  fireEvent.click(screen.getByRole("tab", { name: "执行记录" }));
  fireEvent.click(screen.getByRole("tab", { name: "新建采集" }));
  fireEvent.click(button());
  expect(button().disabled).toBe(true);
  view.unmount();
  await openCandidate(host.transport);
  fireEvent.click(button());
  expect(button().disabled).toBe(true);
  expect(host.posts).toHaveLength(1);
});

it.each(["lost", "pending"])(
  "%s receipt survives SPA module reload using only session storage and empty Host reads",
  async (state) => {
    const host = staleHost(
      state === "pending" ? () => new Promise<Response>(() => {}) : undefined,
    );
    const view = await openCandidate(host.transport);
    fireEvent.click(button());
    if (state === "lost") await screen.findByText(/提交回执未核实/);
    expect(host.posts).toHaveLength(1);
    view.unmount();
    // Discard the application module graph and all component state, preserving
    // only the browser session's storage, as a same-tab reload does.
    vi.resetModules();
    const { default: ReloadedApp } = await import("./App");
    route();
    render(<ReloadedApp transport={host.transport} initialQuery={query} />);
    await screen.findByText(/Operator：ao-dev-cpi-05/);
    months("2026-09");
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(button());
    expect(button().disabled).toBe(true);
    expect(host.posts).toHaveLength(1);
    // No persisted record exists, so use the original-ID read-only recall,
    // rather than a disabled navigation button inside the locked form.
    const recall = await screen.findByRole("button", { name: "核实原请求" });
    const before = host.gets.length;
    fireEvent.click(recall);
    await waitFor(() =>
      expect(
        host.gets
          .slice(before)
          .some((path) => path.includes(`job_identity=${host.posts[0]}`)),
      ).toBe(true),
    );
    expect(host.posts).toHaveLength(1);
  },
);

it("reserves each named intent independently and preserves the earlier UNKNOWN lock", async () => {
  const host = staleHost();
  await openCandidate(host.transport);
  fireEvent.click(button());
  await screen.findByText(/提交回执未核实/);
  const first = host.posts[0];
  host.setIntent("cpi-other-named-intent");
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  await screen.findByText(/Operator：ao-dev-cpi-05/);
  months("2026-09");
  fireEvent.click(screen.getByRole("checkbox"));
  expect(button().disabled).toBe(false);
  fireEvent.click(button());
  await screen.findByText(/提交回执未核实/);
  expect(host.posts).toEqual([first, "cpi-other-named-intent"]);
  host.setIntent(first);
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  await screen.findByText(/Operator：ao-dev-cpi-05/);
  months("2026-09");
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(button());
  expect(button().disabled).toBe(true);
  expect(host.posts).toHaveLength(2);
});

it.each(["read", "write", "silent write", "invalid JSON", "invalid shape"])(
  "fails closed with %s session storage failure before any POST",
  async (failure) => {
    const host = staleHost();
    if (failure.startsWith("invalid"))
      sessionStorage.setItem(
        "mdf.cpi.dev.manual-intents.v1",
        failure === "invalid JSON" ? "{" : "{}",
      );
    if (failure === "read")
      vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
        throw new Error("unavailable");
      });
    if (failure === "write")
      vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
        throw new Error("unavailable");
      });
    if (failure === "silent write")
      vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {});
    await openCandidate(host.transport);
    fireEvent.click(button());
    await waitFor(() => expect(button().disabled).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "指定范围回补" }));
    fireEvent.click(screen.getByRole("button", { name: "一次性初始化" }));
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(button());
    expect(button().disabled).toBe(true);
    expect(host.posts).toHaveLength(0);
  },
);

it("enables only the exact DEV candidate with confirmation, then reads the new version in 06", async () => {
  const api = asgi("dev", "b3_dev_ui_exchange.py");
  route();
  render(<App transport={api.transport} initialQuery={query} />);
  await screen.findByText(/Operator：ao-dev-cpi-05/);
  expect(button().disabled).toBe(true);
  months("2026-08");
  fireEvent.click(screen.getByRole("checkbox"));
  expect(button().disabled).toBe(true);
  months("2026-09");
  expect(button().disabled).toBe(true);
  fireEvent.click(screen.getByRole("checkbox"));
  expect(button().disabled).toBe(false);
  fireEvent.click(button());
  await openSubmittedBrief();
  const posts = api.requests.filter((r) => r.method === "POST");
  expect(posts).toHaveLength(1);
  expect(posts[0].body).toMatchObject({
    source_operation_ref: MANUAL_OPERATION,
    target_schema: "mdf_b3_cpi_05_dev_r1",
    confirmed: true,
  });
  expect(posts[0].body.operator_identity).toBeUndefined();
  expect(api.effects).toEqual({ source: 0, raw: 0, jobs: 1 });
  await act(async () => {
    await api.exchange({ control: "run" });
  });
  fireEvent(document, new Event("visibilitychange"));
  await waitFor(() => {
    const detail = within(
      screen.getByRole("complementary", { name: "执行记录详情" }),
    );
    expect(
      detail.getByText("执行结果").parentElement?.querySelector("dd")
        ?.textContent,
    ).toBe("成功");
    expect(
      detail.getByText("执行阶段回报").parentElement?.querySelector("dd")
        ?.textContent,
    ).toBe("local_read_verified · 无错误分类");
  });
  fireEvent.click(screen.getByRole("button", { name: "精确读取结果 →" }));
  await waitFor(() =>
    expect(
      (
        screen.getByRole("button", {
          name: "读取本地数据",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false),
  );
  fireEvent.click(screen.getByRole("button", { name: "读取本地数据" }));
  await screen.findByText(/读取成功/);
  expect(api.effects).toEqual({ source: 1, raw: 1, jobs: 1 });
});

it("lost receipt blocks retry and reopening observes the durable registration", async () => {
  const api = asgi("dev", "b3_dev_ui_exchange.py");
  route();
  const view = render(<App transport={api.transport} initialQuery={query} />);
  await screen.findByText(/Operator：ao-dev-cpi-05/);
  months("2026-09");
  fireEvent.click(screen.getByRole("checkbox"));
  api.loseNext();
  fireEvent.click(button());
  await screen.findByText("已接收", { exact: true });
  expect(button().disabled).toBe(true);
  view.unmount();
  render(<App transport={api.transport} initialQuery={query} />);
  await screen.findByText(/此 DEV 候选已登记/);
  months("2026-09");
  fireEvent.click(screen.getByRole("checkbox"));
  expect(button().disabled).toBe(true);
  expect(api.requests.filter((r) => r.method === "POST")).toHaveLength(1);
});

it("other CPI sources and periodic collection stay disabled", async () => {
  const api = asgi("dev", "b3_dev_ui_exchange.py");
  route(HISTORY_OPERATION);
  render(<App transport={api.transport} initialQuery={query} />);
  await screen.findByText(/Operator：ao-dev-cpi-05/);
  months("2026-09");
  fireEvent.click(screen.getByRole("checkbox"));
  expect(button().disabled).toBe(true);
  expect(screen.queryByRole("button", { name: "周期采集" })).toBeNull();
  fireEvent.click(screen.getByRole("tab", { name: "周期任务" }));
  await screen.findByRole("region", { name: "周期任务" });
  expect(screen.queryByRole("button", { name: "保存周期任务" })).toBeNull();
  expect(api.requests.filter((r) => r.method === "POST")).toHaveLength(0);
});
