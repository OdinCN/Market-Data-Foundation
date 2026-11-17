import { Fact } from "./WorkflowTable";
import { formatBeijingTime } from "./beijingTime";
export type RosterStatus = {
  state: string;
  classification?: string;
  snapshot_id: string | null;
  observed_at?: string;
  available_at?: string;
  valid_through?: string;
  default_query_day?: string;
  collect?: boolean;
  total?: number;
  directory_observed_day?: string | null;
  directory_snapshot_id?: string | null;
  directory_observed_at?: string | null;
  directory_available_at?: string | null;
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
      <Fact name="TDX 目录观测日">
        {value.directory_observed_day ?? "未记录目录刷新凭据"}
      </Fact>
      {value.directory_snapshot_id &&
        value.directory_snapshot_id !== value.snapshot_id && (
          <>
            <Fact name="TDX 目录快照">{value.directory_snapshot_id}</Fact>
            <Fact name="TDX 目录实际可用时间">
              {formatBeijingTime(value.directory_available_at, "未记录")}
            </Fact>
          </>
        )}
      <Fact name="交易所上市/退市证据截至日">
        {value.valid_through ?? "未取得有据日期"}
      </Fact>
      {value.classification && <Fact name="分类">{value.classification}</Fact>}
    </dl>
  );
}
