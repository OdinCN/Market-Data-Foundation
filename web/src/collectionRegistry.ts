import { MACRO_PROFILES, type MacroProfile } from "./macroTimeSeriesProfile";
import { FINANCIAL } from "./FinancialDailyWorkbench";
import type { RuntimeFamily } from "./RuntimeFamily";
import { CPI, PCE, keyOf, sameKey, type Key } from "./workflowModel";

export type CollectionContract = {
  protocol:
    | "macro"
    | "financial"
    | "tdx"
    | "roster"
    | "legacy-cpi"
    | "legacy-pce"
    | "legacy-plan";
  path: string;
};
export type CollectionRegistration = CollectionContract & {
  asset: Key;
  variant: string;
  profile?: MacroProfile;
};
/** Used by the target selector and submission admission, and enumerated by CI.
 * Availability/source binding still decides whether a registered form can write.
 */
export function collectionRegistrations(
  families: readonly RuntimeFamily[],
): CollectionRegistration[] {
  return [
    ...MACRO_PROFILES.map((profile) => ({
      asset: profile.asset,
      variant: "timeseries",
      protocol: "macro" as const,
      path: profile.basePath + "/jobs",
      profile,
    })),
    {
      asset: FINANCIAL,
      variant: "daily",
      protocol: "financial",
      path: "/api/r5/financial/timeseries/jobs",
    },
    ...families
      .filter((f) => f.collection)
      .map((f) => ({
        asset: f.asset,
        variant: "registered",
        ...f.collection!,
      })),
    {
      asset: CPI,
      variant: "legacy-once",
      protocol: "legacy-cpi",
      path: "/api/r5/cpi/jobs",
    },
    {
      asset: CPI,
      variant: "legacy-backfill",
      protocol: "legacy-cpi",
      path: "/api/r5/cpi/jobs/backfill",
    },
    {
      asset: CPI,
      variant: "legacy-plan-backfill",
      protocol: "legacy-plan",
      path: "/api/r5/cpi/plans/{plan}/backfill",
    },
    {
      asset: PCE,
      variant: "legacy-once",
      protocol: "legacy-pce",
      path: "/api/pce/jobs",
    },
    {
      asset: PCE,
      variant: "legacy-backfill",
      protocol: "legacy-pce",
      path: "/api/pce/jobs/backfill",
    },
  ];
}
export const readonlyCollections = [
  {
    asset: { identity: "mdf.market.bars.daily.raw", revision: "p3-r1" },
    reason: "旧原始日线未绑定当前工作台采集执行器；保留原读取入口。",
  },
  {
    asset: {
      identity: "mdf.kd.sector-analysis.current-rank",
      revision: "p3-r1",
    },
    reason: "KD 当前排名运行 descriptor 尚未装配。",
  },
  ...[
    ["mdf.macro.ecb.sdmx.exr", "t08-historical-wire-r2"],
    ["mdf.macro.estat.sdmx.prc_hicp_minr", "t08-historical-wire-r2"],
    ["mdf.macro.fred-alfred.vintage-bundle", "t06-r1"],
    ["mdf.macro.fomc-statement-document", "t07-r1"],
  ].map(([identity, revision]) => ({
    asset: { identity, revision },
    reason: "该来源已登记；当前工作台未装配此资产的写入执行器。",
  })),
];
export function collectionTargets(families: readonly RuntimeFamily[]) {
  return [
    ...new Map(
      collectionRegistrations(families).map((r) => [keyOf(r.asset), r.asset]),
    ).values(),
  ];
}
export function legacyCollection(
  asset: Key | undefined,
  variant: string,
  families: readonly RuntimeFamily[],
) {
  return collectionRegistrations(families).find(
    (r) => sameKey(asset, r.asset) && r.variant === variant,
  );
}
