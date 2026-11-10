import type { FinancialJob } from "./FinancialDailyWorkbench";

const states: Record<string, string> = {
  pending: "等待执行",
  running: "采集中",
  complete: "已完成",
  partial: "部分完成，可续跑",
  failed: "失败",
  unknown: "结果待核实",
  outcome_unknown: "结果待核实",
  paused: "已暂停",
  cooling_down: "来源冷却中",
  waiting_publication: "等待来源公布",
  observed: "有值",
  provider_missing: "来源明确缺值",
  provider_field_absent: "原文未提供该期限字段",
  non_observation_day: "非观测日",
  not_collected: "未采集",
  not_known_at_cutoff: "截止前尚未知",
  outside_provider_range: "来源范围之外",
  not_yet_available_from_provider: "来源尚未提供",
  derived_inputs_unavailable: "派生输入缺失",
  not_returned_calendar_unconfirmed: "未返回，日历待核实",
  request_failed: "请求失败",
  commit_pending: "持久确认待恢复",
};
export const financialStateText = (state: string) =>
  states[state] ?? "状态待核实";
export function financialJobResult(job: FinancialJob) {
  if (job.state !== "partial") return financialStateText(job.state);
  if (
    job.steps.some((s) =>
      ["failed", "unknown", "outcome_unknown", "cooling_down"].includes(
        s.state,
      ),
    )
  )
    return "部分完成，可续跑";
  if (job.steps.some((s) => s.state === "waiting_publication"))
    return "部分完成，等待来源公布";
  return "部分完成，未执行步骤待核实";
}
export const financialJobOrigin = (job: Pick<FinancialJob, "origin">) =>
  job.origin === "periodic"
    ? "自动策略"
    : job.origin == null || job.origin === "manual"
      ? "手动采集"
      : "触发来源待核实";
export const financialJobTitle = (job: FinancialJob) =>
  `金融日频 · ${financialJobOrigin(job)} · ${job.command.action === "init" ? "初始化采集" : "更新指定范围"}`;

export function financialSeriesSource(id: string) {
  if (["DFEDTARL", "DFEDTARU"].includes(id)) return "联储理事会（政策投影）";
  if (["EFFR", "SOFR"].includes(id)) return "纽约联储（原始）";
  if (["DGS2", "DGS10", "DFII10"].includes(id)) return "美国财政部（原始）";
  if (id === "T10YIE") return "MDF 派生（财政部两腿）";
  if (["BAMLC0A0CM", "BAMLH0A0HYM2"].includes(id))
    return "信用归档 / FRED（ICE 指数）";
  return "来源待核实";
}
export function financialJobSources(job: FinancialJob) {
  return [
    ...new Set(
      job.steps.map((s) =>
        s.source === "archive"
          ? "固定 GitHub 信用归档（ICE 指数）"
          : s.source === "fred"
            ? "FRED（ICE 指数）"
            : ["policy", "origin", "derived"].includes(s.source)
              ? financialSeriesSource(s.series_id)
              : "来源待核实",
      ),
    ),
  ];
}
export const financialJobSearch = (job: FinancialJob) =>
  `金融条件日频 ${job.request_id} ${financialJobTitle(job)} ${financialJobSources(job).join(" ")} ${financialJobResult(job)} ${job.command.start} ${job.command.end}`;
