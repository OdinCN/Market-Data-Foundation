import type { ReactNode } from "react";
import { Fact } from "./WorkflowTable";
import { formatBeijingTime } from "./beijingTime";

/** The shared execution identity/time/result fields for macro workbenches. */
export function ExecutionFacts({
  id,
  asset,
  source,
  action,
  start,
  end,
  createdAt,
  startedAt,
  finishedAt,
  result,
  classification,
}: {
  id: string;
  asset: ReactNode;
  source: ReactNode;
  action: ReactNode;
  start?: string | null;
  end?: string | null;
  createdAt?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
  result: ReactNode;
  classification?: ReactNode;
}) {
  return (
    <dl>
      <Fact name="记录编号">{id}</Fact>
      <Fact name="关联资产">{asset}</Fact>
      <Fact name="来源">{source}</Fact>
      <Fact name="采集方式">{action}</Fact>
      <Fact name="范围">
        {start ?? "未记录"} 至 {end ?? "未记录"}
      </Fact>
      <Fact name="创建时间（北京时间）">
        {formatBeijingTime(createdAt, "未记录")}
      </Fact>
      <Fact name="开始时间（北京时间）">
        {formatBeijingTime(startedAt, "未记录")}
      </Fact>
      <Fact name="结束时间（北京时间）">
        {formatBeijingTime(finishedAt, "未记录")}
      </Fact>
      <Fact name="执行结果">{result}</Fact>
      {classification && <Fact name="结果分类">{classification}</Fact>}
    </dl>
  );
}
