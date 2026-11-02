import { Fact } from "./WorkflowTable";

export function gdpExecutionState(
  state?: string,
  result?: Record<string, unknown> | null,
) {
  if (state !== "complete") return null;
  if (result?.result && typeof result.result === "object")
    return gdpExecutionState(state, result.result as Record<string, unknown>);
  const readback = result?.readback as { verified?: boolean } | undefined;
  if (readback?.verified !== true) return "结果未知（回读未确认）";
  if (
    result?.completion === "source_coverage_partial" ||
    (Array.isArray(result?.source_missing) && result.source_missing.length > 0)
  )
    return "已完成（来源有缺口）";
  const coverage = result?.coverage as { missing?: number } | undefined;
  if (!coverage) return "结果未知（覆盖未记录）";
  return coverage.missing ? "已完成（存在缺口）" : "已完成（回读已验证）";
}
export function GdpCollectionDetails({
  result,
}: {
  result: Record<string, unknown> | null | undefined;
}) {
  if (!result) return null;
  if (
    result.result &&
    typeof result.result === "object" &&
    !Array.isArray(result.result)
  )
    return (
      <GdpCollectionDetails result={result.result as Record<string, unknown>} />
    );
  const readback = result.readback as { verified?: boolean } | undefined;
  const coverage = result.coverage as
    | {
        expected: number;
        available: number;
        official_null: number;
        missing: number;
      }
    | undefined;
  const gaps = result.source_missing as
    | { series_id: string; quarter: string; reason?: string }[]
    | undefined;
  return (
    <section aria-label="GDP季度回读覆盖">
      <dl>
        <Fact name="本地回读">
          {readback?.verified === true ? "已验证（按可用时间回读）" : "未确认"}
        </Fact>
        {coverage && (
          <>
            <Fact name="目标观测">{coverage.expected}</Fact>
            <Fact name="可用数值">{coverage.available}</Fact>
            <Fact name="官方空值">{coverage.official_null}</Fact>
            <Fact name="缺失观测">{coverage.missing}</Fact>
          </>
        )}
      </dl>
      {gaps?.length ? (
        <>
          <p>本次来源未完整返回；已有本地值不代表本次刷新成功。</p>
          <ul>
            {[...gaps]
              .sort(
                (a, b) =>
                  b.quarter.localeCompare(a.quarter) ||
                  a.series_id.localeCompare(b.series_id),
              )
              .map((gap, i) => (
                <li key={`${gap.quarter}:${gap.series_id}:${i}`}>
                  {gap.quarter} · {gap.series_id} · {gap.reason ?? "来源缺值"}
                </li>
              ))}
          </ul>
        </>
      ) : null}
      <p className="small">
        数据已入库不代表已确认其所属的官方发布版本。当前连续值与发布材料的版本关联尚未确认。
      </p>
    </section>
  );
}
