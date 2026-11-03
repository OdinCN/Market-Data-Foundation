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
export function M3ComparisonTable({ rows }: { rows: M3Comparison[] }) {
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
        1月环比＝（本月/上月－1）×100，同阶段、同可用截止，保留6位小数；缺前值或零分母为空。
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
              <th>耐用品先行报告（Advance）</th>
              <th>制造业完整报告</th>
              <th>先行报告1月环比（%）</th>
              <th>完整报告1月环比（%）</th>
              <th>来源快照差额</th>
              <th>缺口说明</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={`${row.month}:${row.series_id}`}>
                <td>{row.month}</td>
                <td>{names.get(row.series_id) ?? row.series_id}</td>
                <td>{display(row.advm3_value)}</td>
                <td>{display(row.m3_value)}</td>
                <td title={row.advm3_mom_reason ?? undefined}>
                  {display(row.advm3_mom_percent ?? null)}
                </td>
                <td title={row.m3_mom_reason ?? undefined}>
                  {display(row.m3_mom_percent ?? null)}
                </td>
                <td>{display(row.difference)}</td>
                <td>{row.reason ? "一侧或两侧缺值" : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
