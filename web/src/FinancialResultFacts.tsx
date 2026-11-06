import type { FinancialJob } from "./FinancialDailyWorkbench";
import { Fact } from "./WorkflowTable";

function sumResult(
  job: FinancialJob,
  field: "saved_points" | "saved_revisions",
) {
  const values = job.steps.map((s) => s.result?.[field]);
  if (
    !values.length ||
    values.some(
      (v) => typeof v !== "number" || !Number.isSafeInteger(v) || v < 0,
    )
  )
    return "未完整记录";
  return values.reduce<number>((total, v) => total + v!, 0);
}
export function FinancialResultFacts({ job }: { job: FinancialJob }) {
  const covered = job.coverage?.series;
  return (
    <dl aria-label="执行结果统计">
      <Fact name="新增观测版本（含修订）">
        {sumResult(job, "saved_points")}
      </Fact>
      <Fact name="新增修订">{sumResult(job, "saved_revisions")}</Fact>
      <Fact name="来源请求（累计）">
        {typeof job.provider_requests === "number"
          ? job.provider_requests
          : "未记录"}
      </Fact>
      <Fact name="来源尝试（累计）">未记录；原件批次尝试见逐段</Fact>
      <Fact name="本地回读">未记录独立回读校验</Fact>
      <Fact name="覆盖快照有效值">
        {covered ? covered.reduce((n, s) => n + s.valid_rows, 0) : "未记录"}
      </Fact>
      <Fact name="覆盖快照明确缺值">
        {covered
          ? covered.reduce((n, s) => n + s.explicit_missing, 0)
          : "未记录"}
      </Fact>
      <Fact name="覆盖快照未返回日历项">
        {covered
          ? covered.reduce((n, s) => n + s.calendar_days_without_row, 0)
          : "未记录"}
      </Fact>
    </dl>
  );
}
