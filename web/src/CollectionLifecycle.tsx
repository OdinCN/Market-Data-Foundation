import {
  useEffect,
  useRef,
  useState,
  type ComponentProps,
  type ReactNode,
} from "react";
import { ExecutionFacts } from "./ExecutionFacts";

// Unknown and in-flight states never authorize a new intent. A domain may
// further restrict terminal states (for example unresolved financial steps).
export function collectionCanReopen(state?: string) {
  return [
    "complete",
    "completed",
    "partial",
    "failed",
    "cancelled",
    "interrupted",
  ].includes((state ?? "").toLowerCase());
}

type TrackedJob = {
  request_id: string;
  state: string;
  created_at: string;
  updated_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
};
// Preserve verified receipts while lists lag or fail. An old running snapshot
// must not replace a later terminal receipt at the same or an older timestamp.
export function useCollectionRecords<T extends TrackedJob>(records: T[]) {
  const retained = useRef(new Map<string, T>());
  const stamp = (r: T) =>
    Math.max(
      ...[r.created_at, r.updated_at, r.started_at, r.finished_at]
        .map((t) => (t ? Date.parse(t) : -Infinity))
        .filter(Number.isFinite),
    );
  for (const r of records) {
    const old = retained.current.get(r.request_id);
    if (
      !old ||
      stamp(r) > stamp(old) ||
      (stamp(r) === stamp(old) &&
        (!collectionCanReopen(old.state) || collectionCanReopen(r.state)))
    )
      retained.current.set(r.request_id, r);
  }
  return [...retained.current.values()].sort(
    (a, b) => Date.parse(b.created_at) - Date.parse(a.created_at),
  );
}

export function collectionRecent<T extends { request_id: string }>(
  records: T[],
  current?: string | null,
) {
  const active = records.find((r) => r.request_id === current);
  return [
    ...(active ? [active] : []),
    ...records.filter((r) => r.request_id !== current),
  ].slice(0, 5);
}

export function CollectionFormLock({
  locked,
  children,
}: {
  locked: boolean;
  children: ReactNode;
}) {
  return (
    <fieldset className="collection-fields" disabled={locked}>
      {locked && (
        <p role="status">
          原提交已锁定，正在跟踪同一请求；请在当前简报允许时开始另一项采集。
        </p>
      )}
      {children}
    </fieldset>
  );
}

export function CollectionBriefs({ children }: { children: ReactNode }) {
  return (
    <aside className="detail-pane execution-briefs" aria-label="执行简报">
      <h3>执行简报</h3>
      <p className="small">
        时间基准：北京时间（UTC+08:00）。点击记录查看原任务；新采集须重新确认。
      </p>
      {children}
    </aside>
  );
}

export type ExecutionDetailProps = ComponentProps<typeof ExecutionFacts> & {
  assetKey: string;
  children?: ReactNode;
  onResult?: () => void;
  onPeriodic?: () => void;
  onResume?: () => void;
  resumeLabel?: string;
  actionsDisabled?: boolean;
  confirmResume?: boolean;
};
export function ExecutionDetailShell({
  assetKey,
  children,
  onResult,
  onPeriodic,
  onResume,
  resumeLabel = "按原请求恢复采集",
  actionsDisabled,
  confirmResume = false,
  ...facts
}: ExecutionDetailProps) {
  const [confirmed, setConfirmed] = useState(false);
  useEffect(() => setConfirmed(false), [facts.id, assetKey]);
  return (
    <section
      className="execution-detail-shell"
      aria-label="执行详情主体"
      data-execution-id={facts.id}
      data-execution-asset={assetKey}
    >
      <h3>执行详情</h3>
      <ExecutionFacts {...facts} />
      <div
        className="actions"
        aria-label="执行操作"
        data-execution-id={facts.id}
      >
        {onResult && <button onClick={onResult}>查看资产数据</button>}
        {onPeriodic && <button onClick={onPeriodic}>查看周期任务</button>}
        {onResume && confirmResume && (
          <label className="cpi-collection-confirm">
            <input
              type="checkbox"
              checked={confirmed}
              disabled={actionsDisabled}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            <span>我确认按原请求恢复采集</span>
          </label>
        )}
        {onResume && (
          <button
            disabled={actionsDisabled || (confirmResume && !confirmed)}
            onClick={() => {
              setConfirmed(false);
              onResume();
            }}
          >
            {resumeLabel}
          </button>
        )}
      </div>
      {onResume && actionsDisabled && (
        <p role="status">恢复操作当前不可用，请回查原记录及采集准入。</p>
      )}
      {children}
    </section>
  );
}
