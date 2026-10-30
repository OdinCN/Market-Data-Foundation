import { useState } from "react";
import { monthValid, shiftMonth } from "./cpiTimeSeriesApi";

type Data = Record<string, unknown>;
type Point = { series_id: string; month: string };
type Range = { start?: string | null; end?: string | null };
const object = (value: unknown): Data =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Data)
    : {};
const key = (point: Point) => `${point.series_id}:${point.month}`;
const points = (value: unknown): Point[] =>
  Array.isArray(value)
    ? value.filter(
        (p): p is Point =>
          typeof p?.series_id === "string" && monthValid(p?.month),
      )
    : [];

// Stage summaries can wrap a result; retain evidence from every level.
export function coverageEvidence(result: Data | null | undefined) {
  const layers: Data[] = [];
  const readbacks: Data[] = [];
  let current = object(result);
  for (let depth = 0; depth < 5 && Object.keys(current).length; depth++) {
    const readback = object(current.readback);
    readbacks.push(readback);
    layers.push(current, readback);
    current = object(current.result);
  }
  const data = Object.assign({}, ...layers) as Data;
  const readback = Object.assign({}, ...readbacks) as Data;
  const collect = (name: string) => [
    ...new Map(
      layers.flatMap((l) => points(l[name])).map((p) => [key(p), p]),
    ).values(),
  ];
  return {
    data,
    readback,
    missingKnown: Array.isArray(readback.missing),
    missing: collect("missing"),
    absent: collect("source_missing"),
    nulls: collect("source_unavailable"),
  };
}

export function MacroSourceCoverage({
  result,
  start,
  end,
}: { result: Data | null | undefined } & Range) {
  const [limit, setLimit] = useState(50);
  if (!result) return null;
  const evidence = coverageEvidence(result);
  const { data, readback, missing, absent, nulls } = evidence;
  const rangeKnown = monthValid(start) && monthValid(end) && start <= end;
  const inRange = (p: Point) =>
    !!rangeKnown && p.month >= start! && p.month <= end!;
  const nullKeys = new Set(nulls.map(key));
  const absentKeys = new Set(
    absent.filter((p) => !nullKeys.has(key(p))).map(key),
  );
  const missingKeys = new Set(missing.map(key));
  const all = [
    ...new Map(
      [...missing, ...absent, ...nulls].map((p) => [key(p), p]),
    ).values(),
  ].sort(
    (a, b) =>
      a.month.localeCompare(b.month) || a.series_id.localeCompare(b.series_id),
  );
  const scopedMissing = missing.filter(inRange);
  const expected =
    typeof readback.expected_points === "number" &&
    Number.isInteger(readback.expected_points) &&
    readback.expected_points >= 0
      ? readback.expected_points
      : null;
  // point_count and saved_points include buffers or write counts, never coverage.
  const measured =
    rangeKnown &&
    evidence.missingKnown &&
    expected !== null &&
    scopedMissing.length <= expected;
  const scopedAbsent = all.filter((p) => inRange(p) && absentKeys.has(key(p)));
  const scopedNulls = nulls.filter(inRange);
  const localGaps = scopedMissing.filter((p) => !absentKeys.has(key(p)));
  const reasons = [
    scopedAbsent.length ? `来源未返回 ${scopedAbsent.length} 条` : "",
    scopedNulls.length ? `来源空值 ${scopedNulls.length} 条` : "",
    localGaps.length
      ? `本地缺口 ${localGaps.length} 条（来源缺席未证实，待核实）`
      : "",
  ].filter(Boolean);
  const buffer = all.filter(
    (p) => rangeKnown && p.month < start! && p.month >= shiftMonth(start!, -12),
  );
  const request = all.filter(inRange);
  const outside = all.filter((p) => !inRange(p) && !buffer.includes(p));
  const groups = rangeKnown
    ? ([
        ["请求范围", request],
        ["计算缓冲期（请求前 12 个月，不计入范围覆盖）", buffer],
        ["范围外记录（用途未核实）", outside],
      ] as const)
    : ([["范围未记录，无法区分请求范围与计算缓冲期", all]] as const);
  const describe = (p: Point) => {
    if (nullKeys.has(key(p)))
      return missingKeys.has(key(p)) ? "来源空值；本地缺口待核实" : "来源空值";
    if (absentKeys.has(key(p))) return "来源未返回";
    return "本地缺口（来源缺席未证实，待核实）";
  };
  const unknownNullCount =
    !nulls.length &&
    typeof data.source_unavailable_count === "number" &&
    data.source_unavailable_count > 0
      ? data.source_unavailable_count
      : null;
  const unmeasuredCoverage =
    data.coverage_complete === true
      ? "完整（数量未记录）"
      : data.coverage_complete === false
        ? "有缺口（数量未记录）"
        : "未知（缺少范围或回读明细）";
  return (
    <>
      <p>
        {measured
          ? `范围覆盖：${expected! - scopedMissing.length} / ${expected} 条${reasons.length ? `，${reasons.join("，")}` : ""}。`
          : `范围覆盖：${unmeasuredCoverage}${reasons.length ? `，${reasons.join("，")}` : ""}。`}
      </p>
      {(all.length > 0 || unknownNullCount !== null) && (
        <details>
          <summary>查看缺口明细</summary>
          {groups.map(
            ([label, entries]) =>
              entries.length > 0 && (
                <section key={label}>
                  <p>
                    {label}：{entries.length} 条
                  </p>
                  <ul>
                    {entries.slice(0, limit).map((p) => (
                      <li key={key(p)}>
                        {p.series_id} · {p.month}
                        <span> — {describe(p)}</span>
                      </li>
                    ))}
                  </ul>
                </section>
              ),
          )}
          {unknownNullCount !== null && (
            <p>来源空值：{unknownNullCount} 条（逐项明细与所属范围未知）</p>
          )}
          {groups.some(([, entries]) => entries.length > limit) && (
            <button
              type="button"
              onClick={() => setLimit((value) => value + 50)}
            >
              再显示 50 条
            </button>
          )}
        </details>
      )}
    </>
  );
}
