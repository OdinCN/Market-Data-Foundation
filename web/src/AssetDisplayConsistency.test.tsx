import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import { CollectionWorkbench } from "./CollectionWorkbench";
import { RuntimeAssets } from "./RuntimeAssets";
import { CpiExecutionDetail } from "./CpiStrategyWorkbench";
import { project, assetTitle, keyOf } from "./workflowModel";
import {
  CPI_PROFILE,
  PPI_PROFILE,
  PCE_PROFILE,
  EMPLOYMENT_PROFILE,
  RETAIL_PROFILE,
  CONSUMPTION_PROFILE,
  GDP_PROFILE,
  macroAssetLabel,
} from "./macroTimeSeriesProfile";
import type { Directory, Transport } from "./api";
import type { Packages } from "./packageApi";
import type { CpiAvailability } from "./cpiTimeSeriesApi";
const profiles = [
  CPI_PROFILE,
  PPI_PROFILE,
  PCE_PROFILE,
  EMPLOYMENT_PROFILE,
  RETAIL_PROFILE,
  CONSUMPTION_PROFILE,
  GDP_PROFILE,
];
const names = [
  "美国 CPI",
  "美国 PPI",
  "美国 PCE价格指数",
  "美国就业",
  "美国零售销售",
  "美国消费与收入",
  "美国 GDP 与增长",
];
const availability: CpiAvailability = {
  data: {
    latest_months: {},
    observation_count: 0,
    revision_count: 0,
    batch_count: 0,
    web_capabilities: { query: true, collect: true, reason: null },
  },
  busy: false,
  failure: null,
  legacy: false,
};
const response = (value: unknown) => new Response(JSON.stringify(value));
const transport: Transport = async (path) =>
  response(
    String(path).endsWith("/strategy")
      ? {
          exists: true,
          enabled: false,
          worker: { state: "paused", last_heartbeat_at: null },
          next_due: null,
          history_start: String(path).includes("/gdp/") ? "2006-Q4" : "2006-10",
          policy: {},
        }
      : String(path).endsWith("/schedule")
        ? { targets: [] }
        : { jobs: [], executions: [] },
  );
const directory = {
  status: "OK",
  entries: [...profiles, CPI_PROFILE].map((profile, i) => ({
    identity: `display-source-${i}`,
    domain: "Macro",
    component_identity: i === 7 ? "bls_cpi_direct" : "cpi_history",
    source_name: profile.provider,
    asset: profile.asset,
    operation: "read",
    source_contract: null,
  })),
} as unknown as Directory;
afterEach(() => {
  cleanup();
  sessionStorage.clear();
  localStorage.clear();
});
it("uses the same seven asset names across collection options, strategy rows, execution details and runtime assets, including CPI source variants", async () => {
  const model = project(directory, null, null);
  expect(model.assets.map((a) => a.label)).toEqual(names);
  expect(
    model.interfaces
      .filter((i) => i.asset?.identity === CPI_PROFILE.asset.identity)
      .map((i) => i.label),
  ).toEqual([names[0], names[0]]);
  expect(profiles.map(macroAssetLabel)).toEqual(names);
  for (const tab of ["new", "periodic", "executions"]) {
    render(
      <CollectionWorkbench
        assets={model.assets}
        asset={model.assets[6]}
        source={undefined}
        invalidIdentity={false}
        route={{
          view: "collection",
          invalid: false,
          fields: new URLSearchParams({
            tab,
            asset: GDP_PROFILE.asset.identity,
            asset_revision: GDP_PROFILE.asset.revision,
            periodic_asset: "all",
            execution_asset: "all",
          }),
        }}
        navigate={vi.fn()}
        transport={transport}
        generation={0}
        cpiAvailability={availability}
        ppiAvailability={availability}
        pceAvailability={availability}
        employmentAvailability={availability}
        retailAvailability={availability}
        consumptionAvailability={availability}
        gdpAvailability={availability}
      />,
    );
    await act(async () => {});
    const select = screen.getByLabelText(
      tab === "new"
        ? "采集目标"
        : tab === "periodic"
          ? "周期资产筛选"
          : "执行资产筛选",
    );
    for (const [index, profile] of profiles.entries())
      expect(
        within(select)
          .getByRole("option", { name: names[index] })
          .getAttribute("value"),
      ).toBe(keyOf(profile.asset));
    if (tab === "periodic") {
      const rows = within(screen.getByRole("region", { name: "周期任务" }))
        .getAllByRole("row")
        .slice(1);
      expect(
        rows.map((row) => row.querySelectorAll("td")[0].textContent).sort(),
      ).toEqual([...names].sort());
    }
    cleanup();
  }
  for (const [index, profile] of profiles.entries()) {
    render(
      <CpiExecutionDetail
        profile={profile}
        execution={{
          id: "existing",
          origin: "manual",
          action: "update",
          state: "pending",
          start: null,
          end: null,
          created_at: null,
          started_at: null,
          finished_at: null,
          classification: null,
          result: null,
        }}
      />,
    );
    expect(
      screen.getByText("关联资产").parentElement!.querySelector("dd")!
        .textContent,
    ).toBe(names[index]);
    cleanup();
  }
  const packages = {
    status: "OK",
    packages: [
      {
        package_id: "macro",
        name: "宏观",
        capabilities: profiles.map((profile) => ({
          definition_ref: profile.asset,
          title: "legacy provider title",
        })),
        assets: profiles.map((profile) => ({
          identity: profile.asset.identity,
          revisions: [],
        })),
      },
    ],
  } as unknown as Packages;
  render(
    <RuntimeAssets
      data={packages}
      busy={false}
      failure={null}
      route={{ view: "assets", invalid: false, fields: new URLSearchParams() }}
      navigate={vi.fn()}
      reader={null}
      ordinary={null}
      reference={null}
    />,
  );
  const rows = within(screen.getByRole("region", { name: "数据资产" }))
    .getAllByRole("row")
    .slice(1);
  expect(rows.map((row) => row.querySelector("td")!.textContent)).toEqual(
    names,
  );
  expect(
    assetTitle({
      identity: "mdf.macro.fred-alfred.vintage-bundle",
      revision: "r1",
    }),
  ).not.toContain("美国");
  expect(
    assetTitle({ identity: "mdf.macro.ecb.sdmx.exr", revision: "r1" }),
  ).not.toContain("美国");
  expect(
    assetTitle({
      identity: "mdf.macro.estat.sdmx.prc_hicp_minr",
      revision: "r1",
    }),
  ).not.toContain("美国");
});

it("a GDP periodic deep link without an all-assets filter reads only its exact asset version", async () => {
  const model = project(directory, null, null);
  const calls: string[] = [];
  render(
    <CollectionWorkbench
      assets={model.assets}
      asset={model.assets[6]}
      source={undefined}
      invalidIdentity={false}
      route={{
        view: "collection",
        invalid: false,
        fields: new URLSearchParams({
          tab: "periodic",
          asset: GDP_PROFILE.asset.identity,
          asset_revision: GDP_PROFILE.asset.revision,
        }),
      }}
      navigate={vi.fn()}
      transport={(path, init) => {
        expect(init?.method ?? "GET").toBe("GET");
        calls.push(String(path));
        return transport(path, init);
      }}
      generation={0}
      cpiAvailability={availability}
      ppiAvailability={availability}
      pceAvailability={availability}
      employmentAvailability={availability}
      retailAvailability={availability}
      consumptionAvailability={availability}
      gdpAvailability={availability}
    />,
  );
  await act(async () => {});
  expect(
    (screen.getByLabelText("周期资产筛选") as HTMLSelectElement).value,
  ).toBe(keyOf(GDP_PROFILE.asset));
  const rows = within(screen.getByRole("region", { name: "周期任务" }))
    .getAllByRole("row")
    .slice(1);
  expect(rows.map((row) => row.querySelector("td")!.textContent)).toEqual([
    macroAssetLabel(GDP_PROFILE),
  ]);
  expect(calls.length).toBeGreaterThan(0);
  expect(calls.every((path) => path.startsWith(GDP_PROFILE.basePath))).toBe(
    true,
  );
});

it.each([
  [
    true,
    ["ENABLED_MAPPING", "RETAINED_HISTORY", "REGISTERED_MANUAL"],
    true,
    "能力已启用",
  ],
  [true, ["REGISTERED_MANUAL"], false, "能力已启用"],
  [false, ["RETAINED_HISTORY", "REGISTERED_MANUAL"], true, "保留历史"],
  [false, ["RETAINED_HISTORY"], false, "保留历史"],
  [false, ["REGISTERED_MANUAL"], true, "已登记人工通路"],
  [false, [], true, "可见依据待核实"],
] as const)(
  "financial visibility follows enabled=%s and evidence=%s independently of runnable=%s",
  (enabled, basis, runnable, expected) => {
    const identity = "mdf.macro.financial-conditions.daily";
    const packages = {
      status: "OK",
      packages: [
        {
          package_id: "macro",
          name: "宏观",
          capabilities: [
            {
              definition_ref: { identity, revision: "r1" },
              title: "金融条件日频",
            },
          ],
          assets: [
            {
              identity,
              revisions: [
                {
                  definition_ref: { identity, revision: "r1" },
                  capability_enabled: enabled,
                  source_entries: [],
                  visibility_basis: [...basis],
                  collect: { allowed: runnable, reasons: ["host"] },
                  plan: { allowed: runnable, reasons: ["host"] },
                },
              ],
            },
          ],
        },
      ],
    } as unknown as Packages;
    render(
      <RuntimeAssets
        data={packages}
        busy={false}
        failure={null}
        route={{
          view: "assets",
          invalid: false,
          fields: new URLSearchParams({
            asset: identity,
            asset_revision: "r1",
          }),
        }}
        navigate={vi.fn()}
        reader={null}
        ordinary={null}
        reference={null}
        financial={<p>日频查询</p>}
      />,
    );
    expect(screen.getByText(expected)).toBeTruthy();
    expect(screen.getByText(/能力不代表策略已开启或执行器在线/)).toBeTruthy();
    expect(screen.queryByText("已登记 · 人工与周期采集已接入")).toBeNull();
  },
);
