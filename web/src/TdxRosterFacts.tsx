import { Fact } from "./WorkflowTable";
import { formatBeijingTime } from "./beijingTime";
export type RosterStatus = {
  state: string;
  classification?: string;
  snapshot_id: string | null;
  observed_at?: string;
  available_at?: string;
  valid_through?: string;
  collect?: boolean;
  total?: number;
  directory_observed_day?: string;
};
const stateText: Record<string, string> = {
  MISSING: "无已发布名册",
  INCOMPLETE: "名册不完整",
  STALE: "官方证据已过期",
  DATED: "有据日期名册",
  CURRENT_DIRECTORY_ONLY: "仅当前来源目录，历史未核实",
};
export function TdxRosterFacts({ value }: { value: RosterStatus }) {
  return (
    <dl>
      <Fact name="名册状态">{stateText[value.state] ?? value.state}</Fact>
      <Fact name="发布快照">{value.snapshot_id ?? "无"}</Fact>
      <Fact name="实际可用时间">
        {formatBeijingTime(value.available_at, "未记录")}
      </Fact>
      {value.directory_observed_day && (
        <Fact name="行情目录观测日">{value.directory_observed_day}</Fact>
      )}
      <Fact name="交易所名册有效至">
        {value.valid_through ?? "未取得有据日期"}
      </Fact>
      {value.classification && <Fact name="分类">{value.classification}</Fact>}
    </dl>
  );
}
