import {
  CollectionBriefs,
  CollectionFormLock,
  collectionRecent,
  useCollectionRecords,
} from "./CollectionLifecycle";
import {
  DailyDateRange,
  CollectionColumns,
  ExecutionBriefCard,
  dailyRangeError,
  rangeWithDefaults,
} from "./DailyFormControls";
import { useEffect, useRef, useState } from "react";
import { beijingToday } from "./beijingTime";
import {
  financialJobResult,
  financialJobTitle,
} from "./financialJobPresentation";
import {
  financialCall,
  type FinancialJob,
  type FinancialStatus,
} from "./FinancialDailyWorkbench";
import { useVisiblePolling } from "./useVisiblePolling";
import { useFinancialSubmission } from "./useFinancialSubmission";
import type { Transport } from "./api";

type Range = { start: string; end: string };
export { dailyRangeError as financialRangeError } from "./DailyFormControls";
export function FinancialCollection({
  transport,
  generation,
  status,
  onExecution,
}: {
  transport: Transport;
  generation: number;
  status: FinancialStatus | null;
  onExecution: (id: string) => void;
}) {
  const [action, setAction] = useState<"update" | "init">("update");
  const [drafts, setDrafts] = useState<Record<"update" | "init", Range>>({
    update: { start: "", end: "" },
    init: { start: "", end: "" },
  });
  const [confirmed, setConfirmed] = useState(false);
  const edited = useRef({
    update: { start: false, end: false },
    init: { start: false, end: false },
  });
  const jobs = useVisiblePolling(
    (signal) =>
      financialCall<{ jobs: FinancialJob[] }>(transport, "/jobs", signal),
    [transport, generation],
  );
  const submission = useFinancialSubmission(transport, "new", () =>
    jobs.refresh(),
  );
  const {
    pending,
    busy,
    failure,
    storageFailed,
    receipt,
    uncertain,
    canRelease,
    rejected,
  } = submission;
  useEffect(() => {
    if (!status) return;
    const end = status.defaults.end;
    // Compatibility for an older status DTO: same documented 14-day maintenance window.
    const start = new Date(Date.parse(end + "T00:00:00Z") - 13 * 86400000)
      .toISOString()
      .slice(0, 10);
    setDrafts((old) => ({
      update: rangeWithDefaults(
        old.update,
        status.update_defaults ?? { start, end },
        edited.current.update,
      ),
      init: rangeWithDefaults(old.init, status.defaults, edited.current.init),
    }));
  }, [status]);
  useEffect(() => {
    const command = pending?.command;
    if (!command || !["init", "update"].includes(command.action)) return;
    const mode = command.action as "init" | "update";
    edited.current[mode] = { start: true, end: true };
    setAction(mode);
    setConfirmed(false);
    setDrafts((old) => ({
      ...old,
      [mode]: { start: command.start, end: command.end },
    }));
  }, [pending]);
  const range = drafts[action];
  useEffect(() => setConfirmed(false), [action, range.start, range.end]);
  const canCollect = status?.web_capabilities?.collect === true;
  const maxDays = status?.collection_limits?.max_calendar_days ?? 366 * 30;
  const latest = status?.collection_limits?.latest_end ?? beijingToday();
  const error = dailyRangeError(range, maxDays, latest);
  function change(key: keyof Range, value: string) {
    edited.current[action][key] = true;
    setConfirmed(false);
    setDrafts((old) => ({
      ...old,
      [action]: { ...old[action], [key]: value },
    }));
  }
  const verified = useCollectionRecords([
    ...(receipt ? [receipt] : []),
    ...(jobs.data?.jobs ?? []),
  ]);
  const tracked = verified.find((j) => j.request_id === pending?.request_id);
  const briefs = collectionRecent(verified, pending?.request_id);
  function newIntent() {
    if (!canRelease) return;
    submission.newIntent();
    setConfirmed(false);
  }
  return (
    <CollectionColumns
      briefs={
        <CollectionBriefs>
          <p className="small">近期真实执行；触发来源以各记录为准。</p>
          {jobs.busy && !jobs.data && <p role="status">正在读取执行简报…</p>}
          {jobs.failure && (
            <p role="alert">执行简报读取失败，已有记录待核实。</p>
          )}
          {jobs.data && !briefs.length && !pending && (
            <p>暂无执行记录；尚未提交本次采集。</p>
          )}
          <ol className="execution-brief-list">
            {pending && !tracked && (
              <li>
                <ExecutionBriefCard
                  id={pending.request_id}
                  current
                  disabled
                  title="金融条件日频 · 待核实请求"
                  createdAt=""
                  start={pending.command?.start}
                  end={pending.command?.end}
                  state={rejected ? "failed" : "unknown"}
                  canReopen={canRelease}
                  onReopen={newIntent}
                  reopenDisabled={busy || storageFailed}
                  result={
                    busy
                      ? "正在提交，尚未确认任务创建。"
                      : rejected
                        ? failure
                        : "结果未知，保留请求并自动跟踪，请勿重复提交；尚未确认任务创建。"
                  }
                  onSelect={() => {}}
                />
              </li>
            )}
            {pending && !tracked && uncertain && !busy && (
              <li>
                <p role="status">
                  同一请求的回执尚未核实或已陈旧，保留请求并自动跟踪，请勿重复提交。
                </p>
                <button
                  disabled={busy || storageFailed}
                  onClick={() => submission.verify()}
                >
                  核实原请求
                </button>
              </li>
            )}
            {briefs.map((j) => (
              <li
                key={j.request_id}
                aria-current={
                  pending?.request_id === j.request_id ? "true" : undefined
                }
              >
                <ExecutionBriefCard
                  id={j.request_id}
                  title={financialJobTitle(j)}
                  createdAt={j.created_at}
                  start={j.command.start}
                  end={j.command.end}
                  result={financialJobResult(j)}
                  current={pending?.request_id === j.request_id}
                  state={j.state}
                  canReopen={canRelease}
                  onReopen={newIntent}
                  reopenDisabled={busy || storageFailed}
                  onSelect={() => onExecution(j.request_id)}
                />
              </li>
            ))}
          </ol>
        </CollectionBriefs>
      }
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (
            !error &&
            confirmed &&
            canCollect &&
            e.currentTarget.checkValidity()
          )
            void submission.submit({ action, ...range });
        }}
      >
        <CollectionFormLock locked={busy || !!pending}>
          {!canCollect && (
            <p role="status">
              当前未开放金融日频采集：
              {status?.web_capabilities?.reason ?? "采集准入尚未核实"}
            </p>
          )}
          <label>
            采集方式
            <select
              aria-label="采集方式"
              value={action}
              disabled={busy || !!pending}
              onChange={(e) => {
                setConfirmed(false);
                setAction(e.target.value as "init" | "update");
              }}
            >
              <option value="update">更新指定范围</option>
              <option value="init">初始化采集</option>
            </select>
          </label>
          <div className="field-grid">
            <DailyDateRange
              key={action}
              collection
              start={range.start}
              end={range.end}
              onStart={(v) => change("start", v)}
              onEnd={(v) => change("end", v)}
              disabled={busy || !!pending}
              max={latest}
            />
          </div>
          {action === "init" ? (
            <>
              <p>初始化服务预置范围：20日历年，可自行修改</p>
              <p>
                服务预置日期：
                {status
                  ? `${status.defaults.start} 至 ${status.defaults.end}`
                  : "正在读取…"}
              </p>
            </>
          ) : (
            <p className="small">
              更新默认范围：
              {status?.update_default_basis ??
                "回看近14个日历日，与周期维护范围一致，可编辑。"}{" "}
              不沿用初始化草稿。
            </p>
          )}
          <p className="small">
            范围依据：美国东部当前观测日 {status?.defaults.end ?? "正在读取…"}
            ，不代表来源已经公布。尾部未公布独立留缺，已有历史仍可取得；当前最多{" "}
            {maxDays} 个日历日。
          </p>
          {error && range.start && range.end && <p role="alert">{error}</p>}
          <label className="cpi-collection-confirm">
            <input
              type="checkbox"
              checked={confirmed}
              disabled={busy || !!pending}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            <span>我确认将以上范围采集到开发测试库</span>
          </label>
          <button
            type="submit"
            className="primary"
            disabled={
              !canCollect ||
              !confirmed ||
              !!error ||
              busy ||
              !!pending ||
              storageFailed
            }
          >
            {busy ? "正在提交…" : "提交 金融条件日频 采集"}
          </button>
        </CollectionFormLock>
      </form>
      {failure && <p role="alert">{failure}</p>}
      {storageFailed && <p role="alert">本机请求记录无法核实，已阻止提交。</p>}
    </CollectionColumns>
  );
}
