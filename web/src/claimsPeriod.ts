import { formatBeijingTime } from "./beijingTime";
import { weekValid } from "./claimsTimeSeriesAdapter";
type Obj = Record<string, unknown>;
const obj = (v: unknown): Obj =>
  v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : {};
export const CLAIMS_IDS = [
  "ICSA",
  "ICNSA",
  "CCSA",
  "CCNSA",
  "IC4WSA",
  "CC4WSA",
];
export function claimsResult(v: unknown): Obj {
  const r = obj(v);
  return r.result ? obj(r.result) : r;
}
export function claimsActualWeeks(v: unknown): Record<string, string> {
  const r = claimsResult(v),
    m = obj(r.material),
    a = obj(m.actual_weeks);
  if (
    m.status !== "available" ||
    m.readback_verified !== true ||
    !/^dol-claims-\d{4}-\d{2}-\d{2}$/.test(String(m.event_id)) ||
    Object.keys(a).length !== 6 ||
    !CLAIMS_IDS.every((s) => weekValid(a[s]))
  )
    return {};
  return a as Record<string, string>;
}
export function claimsActualsVerified(v: unknown) {
  const r = claimsResult(v),
    a = claimsActualWeeks(r),
    b = obj(r.readback),
    held = obj(b.actual_weeks);
  return (
    Object.keys(a).length === 6 &&
    b.verified === true &&
    b.as_of_basis === "available" &&
    b.actuals_verified === true &&
    Object.keys(held).length === 6 &&
    CLAIMS_IDS.every((s) => a[s] === held[s])
  );
}
const group = (weeks: Record<string, string>, prefix: string) => {
  const values = [
    ...new Set(
      Object.entries(weeks)
        .filter(([s]) => s.startsWith(prefix))
        .map(([, w]) => w),
    ),
  ];
  return values.length === 1 ? values[0] : "待报告确认";
};
export function claimsWeeksText(weeks: Record<string, string>) {
  return `初请参考周 ${group(weeks, "IC")}；续请参考周 ${group(weeks, "CC")}`;
}
export function claimsExecutionPeriod(v: unknown) {
  const r = claimsResult(v),
    m = obj(r.material);
  return /^dol-claims-\d{4}-\d{2}-\d{2}$/.test(String(m.event_id))
    ? `发布 ${String(m.event_id).slice(11)} · ${claimsWeeksText(claimsActualWeeks(r))}`
    : "发布事件及参考周待报告确认";
}
export function claimsPlanPeriod(t: Obj) {
  let weeks: Record<string, string> = {};
  for (const name of ["target", "history"]) {
    const r = claimsResult(obj(t[name]).result),
      m = obj(r.material);
    if (m.event_id === t.event_id) {
      const held = claimsActualWeeks(r);
      if (Object.keys(held).length) weeks = held;
    }
  }
  const date =
    typeof t.source_date === "string"
      ? t.source_date
      : /^dol-claims-\d{4}-\d{2}-\d{2}$/.test(String(t.event_id))
        ? String(t.event_id).slice(11)
        : formatBeijingTime(
            typeof t.release_at === "string" ? t.release_at : null,
            "计划日期待确认",
          ).slice(0, 10);
  return `发布 ${date} · ${claimsWeeksText(weeks)}`;
}

export function claimsLatestDataText(
  periods: Record<string, string | null> | undefined,
) {
  const latest = (prefix: string) => {
    const values = [
      ...new Set(
        Object.entries(periods ?? {})
          .filter(([s, w]) => s.startsWith(prefix) && weekValid(w))
          .map(([, w]) => w!),
      ),
    ].sort();
    return !values.length
      ? "待核实"
      : values.length === 1
        ? values[0]
        : `${values[0]} 至 ${values.at(-1)}（序列不同）`;
  };
  return `初请 ${latest("IC")}；续请 ${latest("CC")}`;
}
