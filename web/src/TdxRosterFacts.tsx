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
  directory_verification?: {
    state: string;
    classification?: string;
    cause_classification?: string;
    applies_to_query_day?: boolean;
  };
  latest_refresh?: {
    request_id: string;
    state: string;
    classification?: string | null;
    finished_at?: string | null;
  } | null;
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
    <>
      {value.latest_refresh && value.latest_refresh.state !== "COMPLETE" && (
        <p role="alert">
          最近一次名册刷新未完成：{value.latest_refresh.request_id}，
          {value.latest_refresh.state}
          {value.latest_refresh.classification
            ? `（${value.latest_refresh.classification}）`
            : ""}
          ；
          {formatBeijingTime(
            value.latest_refresh.finished_at,
            "尚未记录完成时间",
          )}
          。 当前继续显示已有完整发布，不将失败时间作为核验时间。
        </p>
      )}
      <dl>
        <Fact name="名册核验">
          {value.directory_verification?.state === "VERIFIED" &&
          value.directory_verification.applies_to_query_day !== false
            ? "目录已逐只核验"
            : "该日逐只核验资料不可用"}
        </Fact>
        {value.directory_verification?.state === "UNVERIFIED" && (
          <Fact name="核验资料原因">
            {value.directory_verification.cause_classification ??
              value.directory_verification.classification ??
              "未取得可核实的逐只凭据"}
          </Fact>
        )}
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
      </dl>
      <details>
        <summary>交易所上市/退市资料</summary>
        <dl>
          <Fact name="交易所资料状态">
            {stateText[value.state] ?? value.state}
          </Fact>
          <Fact name="交易所上市/退市证据截至日">
            {value.valid_through ?? "未取得有据日期"}
          </Fact>
          {value.classification && (
            <Fact name="分类">{value.classification}</Fact>
          )}
        </dl>
      </details>
    </>
  );
}
