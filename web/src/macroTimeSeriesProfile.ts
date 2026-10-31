import { CPI, PPI, PCE, EMPLOYMENT, type Key } from "./workflowModel";

export type MacroProfile = Readonly<{
  id: "cpi" | "ppi" | "pce" | "employment";
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
export const EMPLOYMENT_PROFILE: MacroProfile = Object.freeze({
  id: "employment",
  label: "就业",
  provider: "BLS",
  asset: EMPLOYMENT,
  basePath: "/api/r5/employment/timeseries",
  series: [
    ["CES0000000001", "非农 · CES · 千岗位", "total_nonfarm", "sa"],
    ["LNS14000000", "失业率 · CPS · %", "unemployment_rate", "sa"],
    [
      "CES0500000003",
      "平均时薪 · CES · 美元/小时",
      "average_hourly_earnings",
      "sa",
    ],
    [
      "LNS11300000",
      "劳动参与率 · CPS · %",
      "labor_force_participation_rate",
      "sa",
    ],
    [
      "CES0500000002",
      "平均每周计薪工时 · CES · 小时",
      "average_weekly_hours",
      "sa",
    ],
  ] as const,
  historyNotice:
    "非农为千岗位及月净增，岗位不是人数；失业率、劳动参与率为百分数及百分点变化；平均每周计薪工时为小时及小时变化；平均时薪为美元/小时及环比、同比。初始化240个月可编辑，无发布证据时截至上一完整自然月，仅为预置范围。官方变化与本地计算分别保留，revision 1 是首次本地观察。",
});
export const profileScopeText = (profile: MacroProfile) =>
  `${profile.series.map(([, title]) => title).join("、")}，共${profile.series.length}种口径。`;
