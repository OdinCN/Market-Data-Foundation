import { afterEach, expect, it } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { asgi, closeBridges } from "./workflowTestBridge";
import { MacroReleaseRecords } from "./MacroReleaseRecords";
import { CpiTimeSeries } from "./CpiTimeSeries";
import { EMPLOYMENT_PROFILE as profile } from "./macroTimeSeriesProfile";
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
  expect(list.textContent).toContain("2024-10-04 · date");
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
  expect(values.textContent).toContain("123 thousand_jobs");
  expect(values.textContent).toContain("上期值（本次修订后）");
  expect(values.textContent).toContain("38 thousand_jobs");
  expect(detail.textContent).toContain("预期差未计算");
  const raw = f.text();
  fireEvent.click(screen.getByRole("button", { name: "实际响应" }));
  expect(screen.getByLabelText("发布记录实际HTTP正文").textContent).toBe(raw);
  expect(f.bridge.effects.source).toBe(0);
});

it("unknown event remains unknown and late archive evidence is absent before MDF availability", async () => {
  const f = fixture("unknown");
  render(<MacroReleaseRecords profile={profile} transport={f.transport} />);
  const list = await screen.findByRole("table", { name: "发布事件列表" });
  expect(list.textContent).toContain("unknown");
  fireEvent.change(screen.getByLabelText("发布证据截止"), {
    target: { value: "2024-10-04T12:00:00Z" },
  });
  fireEvent.click(screen.getByRole("button", { name: "读取发布记录" }));
  await screen.findByText(
    "缺项：official_release_evidence_not_available_at_cutoff",
  );
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
    expect(detail.textContent).toContain("29 thousand_jobs");
    expect(detail.textContent).toContain("133 thousand_jobs");
    expect(detail.textContent).toContain("非完整原始HTTP字节");
    expect(detail.textContent).toContain("official_summary_projection");
    expect(f.bridge.effects.source).toBe(0);
  },
);
