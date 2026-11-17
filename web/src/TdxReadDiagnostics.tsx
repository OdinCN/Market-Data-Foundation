import type { RecordReadError } from "./RuntimeFamily";
import type { TdxJob } from "./TdxDailyWorkbench";
import { Fact, Technical } from "./WorkflowTable";
import { formatBeijingTime } from "./beijingTime";

export type TdxJobList = { jobs: TdxJob[]; read_errors?: RecordReadError[] };
export function readableTdxJobs(
  jobs: TdxJob[],
  errors: RecordReadError[] = [],
) {
  return jobs.filter(
    (job) => !errors.some((error) => error.request_id === job.request_id),
  );
}
export type TdxStrategy = {
  enabled: boolean;
  worker_online: boolean;
  heartbeat_at?: string;
  calendar_state?: string;
  blocked?: {
    day: string;
    request_id: string;
    classification: string;
    cause_classification?: string;
    observed_at?: string;
  } | null;
};

export function TdxStrategyBlock({ strategy }: { strategy: TdxStrategy }) {
  const block = strategy.blocked;
  if (!block) return null;
  return (
    <div role="alert" aria-label="共享日线与名册策略阻断">
      <p>日线与名册共用策略存在待核实阻断。</p>
      <dl>
        <Fact name="阻断目标日">{block.day}</Fact>
        <Fact name="阻断原号">
          <Technical>{block.request_id}</Technical>
        </Fact>
        <Fact name="阻断原因">
          {block.classification}
          {block.cause_classification && ` · ${block.cause_classification}`}
        </Fact>
        <Fact name="发现时间（北京时间）">
          {formatBeijingTime(block.observed_at, "未记录")}
        </Fact>
      </dl>
    </div>
  );
}
