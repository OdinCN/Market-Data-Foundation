import { afterEach, expect, it } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { asgi, closeBridges } from "./workflowTestBridge";
import { MacroReleaseRecords } from "./MacroReleaseRecords";
import { CpiTimeSeries } from "./CpiTimeSeries";
import {
  EMPLOYMENT_PROFILE as profile,
  CPI_PROFILE,
  PPI_PROFILE,
  PCE_PROFILE,
  RETAIL_PROFILE,
  CONSUMPTION_PROFILE,
} from "./macroTimeSeriesProfile";
import type { Transport } from "./api";
afterEach(() => {
  cleanup();
  closeBridges();
});

function fixture(scenario = "confirmed") {
  const bridge = asgi(scenario, "macro_release_ui_exchange.py");
  let lastText = "";
  const transport: Transport = async (path, init) => {
    const wire = await bridge.exchange({
      path: String(path),
      method: init?.method ?? "GET",
    });
    lastText = wire.text;
    return new Response(wire.text, { status: wire.status });
  };
  return { bridge, transport, text: () => lastText };
}

it("renders actual HTTP archive claims, revision roles, date precision and raw wire without source requests", async () => {
  const f = fixture();
  render(
    <CpiTimeSeries
      profile={profile}
      transport={f.transport}
      availability={{ busy: false, failure: null, legacy: false, data: null }}
    />,
  );
  fireEvent.click(screen.getByRole("tab", { name: "发布记录" }));
  const list = await screen.findByRole("table", { name: "发布事件列表" });
  expect(list.textContent).toContain("2024-10-04 · 日期");
  fireEvent.click(
    within(list).getByRole("button", {
      name: "bls:employment-situation:2024-10-04",
    }),
  );
  const detail = screen.getByRole("region", { name: "发布事件详情" });
  expect(detail.textContent).toContain("未知（未获足够证据）");
  const values = within(detail).getByRole("table", {
    name: "事件公布与修订值",
  });
  expect(values.textContent).toContain("123 千岗位");
  expect(values.textContent).toContain("上期值（本次修订后）");
  expect(values.textContent).toContain("38 千岗位");
  expect(detail.textContent).toContain("预期差未计算");
  const raw = f.text();
  fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
  expect(screen.getByLabelText("原始 HTTP 正文").textContent).toBe(raw);
  expect(f.bridge.effects.source).toBe(0);
});

it("unknown event remains unknown and late archive evidence is absent before MDF availability", async () => {
  const f = fixture("unknown");
  render(<MacroReleaseRecords profile={profile} transport={f.transport} />);
  const list = await screen.findByRole("table", { name: "发布事件列表" });
  expect(list.textContent).toContain("当期值缺证据");
  fireEvent.change(screen.getByLabelText("发布证据截止"), {
    target: { value: "2024-10-04T20:00:00" },
  });
  fireEvent.click(screen.getByRole("button", { name: "读取发布记录" }));
  await screen.findByText("缺项：截止时没有可消费的官方发布材料");
  expect(
    screen.queryByRole("button", {
      name: "bls:employment-situation:2024-10-04",
    }),
  ).toBeNull();
  expect(f.bridge.effects.source).toBe(0);
});

it.skipIf(!process.env.MDF_RELEASE_RETAINED_DIR)(
  "renders root-retained official summary through real HTTP without claiming original bytes or live collection",
  async () => {
    const f = fixture("retained");
    render(<MacroReleaseRecords profile={profile} transport={f.transport} />);
    const list = await screen.findByRole("table", { name: "发布事件列表" });
    fireEvent.click(
      within(list).getByRole("button", {
        name: "bls:employment-situation:2026-10-02",
      }),
    );
    const detail = screen.getByRole("region", { name: "发布事件详情" });
    expect(detail.textContent).toContain("29 千岗位");
    expect(detail.textContent).toContain("133 千岗位");
    expect(detail.textContent).toContain("非完整原始HTTP字节");
    expect(detail.textContent).toContain("official_summary_projection");
    expect(f.bridge.effects.source).toBe(0);
  },
);

it.each([
  [CPI_PROFILE, "bls:cpi:2026-10-14", "336.940 指数点"],
  [PPI_PROFILE, "bls:ppi:2026-10-15", "1.3 %"],
  [PCE_PROFILE, "bea:personal-income-and-outlays:2026-10-29", "0.5 %"],
  [profile, "bls:employment-situation:2026-10-02", "29 千岗位"],
  [RETAIL_PROFILE, "census:advance-retail:2026-10-15", "745000.0 百万美元"],
] as const)(
  "renders retained %s material through actual HTTP into the asset pane",
  async (selectedProfile, eventId, value) => {
    const f = fixture("five-family");
    render(
      <MacroReleaseRecords
        profile={selectedProfile}
        transport={f.transport}
        initialEventId={eventId}
      />,
    );
    const values = await screen.findByRole("table", {
      name: "事件公布与修订值",
    });
    expect(values.textContent).toContain(value);
    if (selectedProfile.id === "retail") {
      expect(values.textContent).toContain("总体 · 季调 / 水平");
      expect(values.textContent).toContain("原文：745.0 十亿美元");
    }
    const raw = f.text();
    fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
    expect(screen.getByLabelText("原始 HTTP 正文").textContent).toBe(raw);
    expect(screen.getByRole("button", { name: "复制原始响应" })).toBeTruthy();
    expect(f.bridge.effects.source).toBe(0);
  },
);

it.each([
  CPI_PROFILE,
  PPI_PROFILE,
  PCE_PROFILE,
  profile,
  RETAIL_PROFILE,
  CONSUMPTION_PROFILE,
])(
  "$label release failures retain exact HTTP evidence with the shared response controls",
  async (selectedProfile) => {
    const body =
      '\n { "classification": "local_read_not_authorized", "extra": 9007199254740993 }\n';
    const transport: Transport = async () =>
      new Response(body, { status: 403 });
    render(
      <MacroReleaseRecords profile={selectedProfile} transport={transport} />,
    );
    await screen.findByRole("alert");
    const exchange = screen.getByRole("region", { name: "本次原始响应" });
    expect(exchange.textContent).toContain("403");
    expect(exchange.textContent).toContain("耗时");
    expect(exchange.textContent).toContain("完成时间");
    fireEvent.click(screen.getByRole("tab", { name: "原始响应" }));
    expect(screen.getByLabelText("原始 HTTP 正文").textContent).toBe(body);
    expect(screen.getByLabelText("本次请求 URL").textContent).toContain(
      selectedProfile.basePath + "/releases?",
    );
    expect(screen.queryByRole("table", { name: "发布事件列表" })).toBeNull();
  },
);

it("ignores a previous profile's late release response", async () => {
  let finish!: (value: Response) => void;
  const transport: Transport = async (path) => {
    if (String(path).includes("/cpi/"))
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    return new Response(
      JSON.stringify({
        events: [],
        missing: [],
        as_of_basis: "available",
        provider_requests: 0,
      }),
      { status: 200 },
    );
  };
  const view = render(
    <MacroReleaseRecords profile={CPI_PROFILE} transport={transport} />,
  );
  view.rerender(
    <MacroReleaseRecords profile={CONSUMPTION_PROFILE} transport={transport} />,
  );
  await screen.findByRole("table", { name: "发布事件列表" });
  await act(async () =>
    finish(new Response("old CPI failure", { status: 502 })),
  );
  expect(screen.getByLabelText("本次请求 URL").textContent).toContain(
    "/consumption/",
  );
  expect(screen.queryByRole("alert")).toBeNull();
});
