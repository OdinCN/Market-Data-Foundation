import { retailDecimalText } from "./decimalValue";
import { M3_PROFILE } from "./macroTimeSeriesProfile";
export type M3Comparison = {
  month: string;
  series_id: string;
  m3_mom_percent?: string | null;
  advm3_mom_percent?: string | null;
  m3_mom_reason?: string | null;
  advm3_mom_reason?: string | null;
  m3_value: string | null;
  advm3_value: string | null;
  difference: string | null;
  reason: string | null;
  comparison_basis: string;
  difference_direction: string;
  official_event_revision: null;
};
export function m3MomReason(reason: string | null | undefined): string {
  return reason === "zero_denominator"
    ? "上月值为零，无法计算环比"
    : reason === "missing_denominator"
      ? "缺少同报告上月值"
      : reason === "current_missing"
        ? "本月值缺失"
        : reason
          ? "环比不可用"
          : "—";
}
export function m3MomText(
  value: string | null | undefined,
  reason?: string | null,
): string {
  if (value === null || value === undefined) return m3MomReason(reason);
  // The API sends a fixed six-place Decimal. Round digits without binary floats.
  const match = /^([+-]?)([0-9]+)(?:\.([0-9]+))?$/.exec(value);
  if (!match) return "环比不可用";
  const fraction = (match[3] ?? "").padEnd(3, "0");
  let cents = BigInt(match[2] + fraction.slice(0, 2));
  const discarded = fraction.slice(2);
  if (
    discarded[0] > "5" ||
    (discarded[0] === "5" &&
      (/[1-9]/.test(discarded.slice(1)) || cents % 2n !== 0n))
  )
    cents += 1n;
  return `${match[1] === "-" && cents !== 0n ? "-" : ""}${cents / 100n}.${String(cents % 100n).padStart(2, "0")}%`;
}
export function M3ComparisonTable({
  rows,
  columns = ["value", "mom_percent"],
}: {
  rows: M3Comparison[];
  columns?: string[];
}) {
  const levels = columns.includes("value"),
    mom = columns.includes("mom_percent");
  const names = new Map(M3_PROFILE.series.map(([id, label]) => [id, label]));
  const display = (value: string | null) =>
    value === null ? "—" : retailDecimalText(value);
  return (
    <section aria-label="两阶段并排比较">
      <p>
        耐用品先行报告（Advance）与制造业完整报告独立保留。金额及差额为百万当期美元。
      </p>
      <p>
        差额＝完整报告－先行报告，是来源快照差额；历史查询使用所选可用截止；未绑定官方事件，不代表官方当次修订或
        surprise。缺一边不计算差额。
      </p>
      <p>
        1月环比＝（本月/上月－1）×100，同阶段、同可用截止，API保留6位小数，页面显示两位百分比；缺前值或零分母为空。
      </p>
      <div className="table-wrap">
        <table
          className="macro-result-table m3-comparison-table"
          aria-label="两报告金额及差额"
        >
          <thead>
            <tr>
              <th>月份</th>
              <th>口径</th>
              {levels && <th>耐用品先行报告（Advance）</th>}
              {levels && <th>制造业完整报告</th>}
              {mom && <th>先行报告1月环比（%）</th>}
              {mom && <th>完整报告1月环比（%）</th>}
              {levels && <th>来源快照差额</th>}
              <th>缺口说明</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={`${row.month}:${row.series_id}`}>
                <td>{row.month}</td>
                <td>{names.get(row.series_id) ?? row.series_id}</td>
                {levels && <td>{display(row.advm3_value)}</td>}
                {levels && <td>{display(row.m3_value)}</td>}
                {mom && (
                  <td>
                    {m3MomText(row.advm3_mom_percent, row.advm3_mom_reason)}
                  </td>
                )}
                {mom && (
                  <td>{m3MomText(row.m3_mom_percent, row.m3_mom_reason)}</td>
                )}
                {levels && <td>{display(row.difference)}</td>}
                <td>{row.reason ? "一侧或两侧缺值" : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
