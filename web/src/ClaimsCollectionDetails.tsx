import { retailDecimalText } from "./decimalValue";
import { CLAIMS_PROFILE } from "./macroTimeSeriesProfile";
import { releaseLink } from "./macroReleaseLinks";
export function ClaimsCollectionDetails({
  result,
}: {
  result: Record<string, unknown> | null | undefined;
}) {
  if (!result) return null;
  const data = (
    result.result && typeof result.result === "object" ? result.result : result
  ) as Record<string, unknown>;
  const coverage = data.coverage as Record<string, number> | undefined;
  const series = data.series_coverage as
    | Record<string, Record<string, unknown>>
    | undefined;
  const material = data.material as
    | {
        status?: string;
        classification?: string;
        event_id?: string;
        readback_verified?: boolean;
      }
    | undefined;
  const display = (v: unknown) =>
    typeof v === "number"
      ? retailDecimalText(v)
      : typeof v === "string"
        ? v
        : "未知";
  const materialStatus: Record<string, string> = {
    available: "已取得并独立回读",
    unbound: "材料能力未绑定",
    unavailable: "材料取得或回读失败",
    source_not_updated: "来源仍为旧报告，等待更新",
  };
  return (
    <div aria-label="Claims周频回读覆盖">
      {coverage && (
        <p>
          周频覆盖：目标 {display(coverage.expected)}，有值{" "}
          {display(coverage.available)}，本地缺行{" "}
          {display(coverage.missing_rows)}，FRED提供方缺值{" "}
          {display(coverage.provider_missing_values)}。各系列末端周独立。
        </p>
      )}
      {series &&
        Object.entries(series).map(([id, v]) => (
          <details key={id}>
            <summary>
              {CLAIMS_PROFILE.series.find(([sid]) => sid === id)?.[1] ?? id}
              ：最近有值截止周 {display(v.latest_week)}
            </summary>
            <p>
              有值 {display(v.available)} / 目标 {display(v.expected)}；缺周共{" "}
              {display(v.missing_weeks_total)}，末端缺周共{" "}
              {display(v.trailing_missing_weeks_total)}。
            </p>
            <p>
              缺周样本：
              {Array.isArray(v.missing_weeks)
                ? v.missing_weeks.join("、") || "无"
                : "未知"}
              {v.missing_weeks_truncated === true ? "（已截断，最多16个）" : ""}
              ；末端缺周样本：
              {Array.isArray(v.trailing_missing_weeks)
                ? v.trailing_missing_weeks.join("、") || "无"
                : "未知"}
              {v.trailing_missing_weeks_truncated === true
                ? "（已截断，最多16个）"
                : ""}
              。
            </p>
          </details>
        ))}
      {material && (
        <p>
          发布材料：{materialStatus[material.status ?? ""] ?? "尚未核实"}
          {material.classification ? `（${material.classification}）` : ""}
          。连续数值回读与材料状态分别核实。
          {material.event_id && (
            <a href={releaseLink(CLAIMS_PROFILE, material.event_id)}>
              查看本Claims发布材料
            </a>
          )}
        </p>
      )}
    </div>
  );
}
