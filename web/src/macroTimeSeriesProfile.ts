import { CPI, PPI, PCE, type Key } from "./workflowModel";

export type MacroProfile = Readonly<{
  id: "cpi" | "ppi" | "pce";
  label: string;
  provider: "BLS" | "BEA";
  asset: Key;
  basePath: string;
  series: readonly (readonly [string, string, string, string])[];
  historyNotice?: string;
}>;
export const CPI_PROFILE: MacroProfile = Object.freeze({
  id: "cpi",
  label: "CPI",
  provider: "BLS",
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
  provider: "BLS",
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
export const PCE_PROFILE: MacroProfile = Object.freeze({
  id: "pce",
  label: "PCE",
  provider: "BEA",
  asset: PCE,
  basePath: "/api/r5/pce/timeseries",
  series: [
    ["DPCERG", "总体 · 季调", "headline", "sa"],
    ["DPCCRG", "核心 · 季调", "core", "sa"],
  ] as const,
  historyNotice:
    "BEA NIPA T20804 月度指数，仅总体与核心季调两系列。初始化默认240个月，可修改；实际覆盖以来源返回为准。1/3/6/12月变化由同一截止时点的指数端点计算，需前12个月缓冲。入库时间不代表官方发布时间；发布事件以官方日历证据为准，未知不推算。",
});
export const profileScopeText = (profile: MacroProfile) =>
  `${profile.series.map(([, title]) => title).join("、")}，共${profile.series.length}种口径。`;
