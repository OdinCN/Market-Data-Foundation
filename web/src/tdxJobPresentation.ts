import type { TdxJob } from "./TdxDailyWorkbench";

export function tdxJobOrigin(job: TdxJob) {
  if (job.origin === "manual") return "手动采集";
  if (job.origin === "periodic") return "自动策略";
  return "触发来源待核实";
}
