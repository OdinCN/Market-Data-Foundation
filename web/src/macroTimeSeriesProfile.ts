import { CPI, PPI, type Key } from "./workflowModel";

export type MacroProfile = Readonly<{
  id: "cpi" | "ppi";
  label: string;
  asset: Key;
  basePath: string;
  series: readonly (readonly [string, string, string, string])[];
  historyNotice?: string;
}>;
export const CPI_PROFILE: MacroProfile = Object.freeze({
  id: "cpi",
  label: "CPI",
  asset: CPI,
  basePath: "/api/r5/cpi/timeseries",
  series: [
    ["CUSR0000SA0", "总体 · 季调", "headline", "sa"],
    ["CUUR0000SA0", "总体 · 未季调", "headline", "nsa"],
    ["CUSR0000SA0L1E", "核心 · 季调", "core", "sa"],
    ["CUUR0000SA0L1E", "核心 · 未季调", "core", "nsa"],
  ] as const,
});
export const PPI_PROFILE: MacroProfile = Object.freeze({
  id: "ppi",
  label: "PPI",
  asset: PPI,
  basePath: "/api/r5/ppi/timeseries",
  series: [
    ["WPSFD4", "总量 · 季调", "headline", "sa"],
    ["WPUFD4", "总量 · 未季调", "headline", "nsa"],
    ["WPSFD49104", "核心 · 季调", "core", "sa"],
    ["WPUFD49104", "核心 · 未季调", "core", "nsa"],
  ] as const,
  historyNotice:
    "核心指剔除食品和能源的最终需求。初始化范围可修改，实际历史覆盖以来源返回为准。",
});
