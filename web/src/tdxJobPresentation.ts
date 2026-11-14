import type { TdxJob } from "./TdxDailyWorkbench";

export function tdxJobOrigin(job: TdxJob) {
  if (job.origin === "manual") return "手动采集";
  if (job.origin === "periodic") return "自动策略";
  return "触发来源待核实";
}

export function rosterJobScope(job: TdxJob) {
  const markets = job.command.markets ?? [
    ...new Set(job.steps?.map((s) => s.market)),
  ];
  const names = ["深市", "沪市", "北交所"];
  return markets.length
    ? markets.map((m) => names[m] ?? String(m)).join("、") +
        " A 股名册（来源目录快照）"
    : "来源目录快照（市场范围未留存）";
}
