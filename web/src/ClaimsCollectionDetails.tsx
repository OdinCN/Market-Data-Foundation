import { claimsExecutionPeriod, claimsActualWeeks } from "./claimsPeriod";
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
  const actuals = claimsActualWeeks(data);
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
  const readback = data.readback as
    | { verified?: boolean; actuals_verified?: boolean }
    | undefined;
  const materialStatus: Record<string, string> = {
    available:
      material?.readback_verified === true
        ? "获取成功、解析成功、独立回读已核实"
        : "已保存材料，独立回读尚未核实",
    unbound: "材料能力未绑定，未执行获取、解析与回读",
    unavailable: "获取、解析或回读流程未完成",
    source_not_updated: "获取和解析成功，来源仍为旧报告，未完成目标事件回读",
  };
  const reason = (code: string) => {
    if (code.includes("source_not_updated"))
      return "材料尚未更新到目标发布事件";
    if (code.includes("unbound")) return "未配置材料获取能力";
    if (code.includes("readback")) return "保存后的材料回读未通过核对";
    if (
      code.includes("parse") ||
      code.includes("mismatch") ||
      code.includes("header") ||
      code.includes("layout")
    )
      return "材料解析或内容核对未通过";
    if (code.includes("timeout")) return "获取材料超时";
    if (
      code.includes("http") ||
      code.includes("network") ||
      code.includes("connect")
    )
      return "来源请求未成功";
    if (
      code.includes("size") ||
      code.includes("encoding") ||
      code.includes("invalid")
    )
      return "材料格式或大小未通过校验";
    return "材料流程未完成，具体分类见技术详情";
  };
  return (
    <div aria-label="Claims周频回读覆盖">
      <p>{claimsExecutionPeriod(data)}。发布日期与参考周分别列示。</p>
      {data.completion === "existing_history_readback_verified" && (
        <p>历史核实复用目标报告，只读检查本地覆盖，未发起新的来源请求。</p>
      )}
      <p>
        本次回读范围：{display(data.start_date)} 至 {display(data.end_date)}
        。数值回读：{readback?.verified === true ? "已核实" : "尚未核实"}
        ；发布材料对应周数值：
        {readback?.actuals_verified === true ? "已逐项核实" : "尚未逐项核实"}。
      </p>
      {coverage && (
        <p>
          周频覆盖：目标 {display(coverage.expected)}，有值{" "}
          {display(coverage.available)}，区间本地缺行（含尚未发布周）{" "}
          {display(coverage.missing_rows)}，FRED提供方缺值{" "}
          {display(coverage.provider_missing_values)}。各系列末端周独立。
        </p>
      )}
      {series &&
        Object.entries(series).map(([id, v]) => (
          <details key={id}>
            <summary>
              {CLAIMS_PROFILE.series.find(([sid]) => sid === id)?.[1] ?? id}
              ：范围内最新有值截止周 {display(v.latest_week)}
              <br />
              有值 {display(v.available)} / 目标 {display(v.expected)}；缺周共{" "}
              {display(v.missing_weeks_total)}，末端缺周共{" "}
              {display(v.trailing_missing_weeks_total)}。展开查看有限样本。
            </summary>
            <p>
              本次报告参考周：{actuals[id] ?? "待报告确认"}。
              {actuals[id] && Array.isArray(v.missing_weeks) && (
                <span>
                  报告参考周之后的区间缺行{" "}
                  {
                    v.missing_weeks.filter((w) => String(w) > actuals[id])
                      .length
                  }{" "}
                  个，不作为本次发布漏采；各序列按自己的参考周核实。
                </span>
              )}
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
          {material.classification
            ? `。原因：${reason(material.classification)}`
            : ""}
          。连续数值回读与材料状态分别核实。
          {material.event_id && (
            <a href={releaseLink(CLAIMS_PROFILE, material.event_id)}>
              查看本Claims发布材料
            </a>
          )}
        </p>
      )}
      <details>
        <summary>执行回读技术详情</summary>
        <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {JSON.stringify(
            { coverage, series_coverage: series, material, readback },
            null,
            2,
          )}
        </pre>
      </details>
    </div>
  );
}
