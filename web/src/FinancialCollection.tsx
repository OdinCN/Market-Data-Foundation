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
  financialJobOrigin,
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
  const history =
    jobs.data?.jobs
      .filter(
        (j) =>
          financialJobOrigin(j) === "手动采集" &&
          j.request_id !== pending?.request_id,
      )
      .slice(0, 5) ?? [];
  // A late poll effect after newIntent may still retain the previous receipt.
  // Only the active intent owns a receipt card; history already owns released IDs.
  const currentReceipt =
    pending?.request_id === receipt?.request_id ? receipt : null;
  const briefs = [...(currentReceipt ? [currentReceipt] : []), ...history];
  function newIntent() {
    if (!canRelease) return;
    submission.newIntent();
    setConfirmed(false);
  }
  return (
    <CollectionColumns
      briefs={
        <aside className="detail-pane execution-briefs" aria-label="执行简报">
          <h3>执行简报</h3>
          <p className="small">
            时间基准：北京时间（UTC+08:00）。以下为手动采集；自动作业见周期任务与执行记录。
          </p>
          {jobs.busy && !jobs.data && <p role="status">正在读取执行简报…</p>}
          {jobs.failure && (
            <p role="alert">执行简报读取失败，已有记录待核实。</p>
          )}
          {jobs.data && !briefs.length && !pending && (
            <p>暂无手动采集记录；尚未提交本次采集。</p>
          )}
          <ol className="execution-brief-list">
            {pending && !receipt && (
              <li>
                <div className="execution-brief-card" role="status">
                  <strong>
                    金融条件日频 ·{" "}
                    {pending.command?.action === "init"
                      ? "初始化采集"
                      : "更新指定范围"}
                  </strong>
                  <dl>
                    <div>
                      <dt>请求编号</dt>
                      <dd className="request-id">{pending.request_id}</dd>
                    </div>
                    <div>
                      <dt>创建时间</dt>
                      <dd>未核实（本机保留意图）</dd>
                    </div>
                    <div>
                      <dt>采集范围</dt>
                      <dd>
                        {pending.command?.start ?? "待核实"} 至{" "}
                        {pending.command?.end ?? "待核实"}
                      </dd>
                    </div>
                    <div>
                      <dt>结果</dt>
                      <dd>
                        {busy
                          ? "正在提交，尚未确认任务创建。"
                          : rejected
                            ? failure
                            : "结果未知，保留请求并自动跟踪，请勿重复提交；尚未确认任务创建。"}
                      </dd>
                    </div>
                  </dl>
                </div>
                {canRelease && (
                  <button disabled={busy || storageFailed} onClick={newIntent}>
                    开始另一项采集
                  </button>
                )}
              </li>
            )}
            {pending && uncertain && !busy && (
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
                  onSelect={() => onExecution(j.request_id)}
                />

                {pending?.request_id === j.request_id && canRelease && (
                  <button disabled={busy || storageFailed} onClick={newIntent}>
                    开始另一项采集
                  </button>
                )}
              </li>
            ))}
          </ol>
        </aside>
      }
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!error && confirmed && status && e.currentTarget.checkValidity())
            void submission.submit({ action, ...range });
        }}
      >
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
            !status ||
            !confirmed ||
            !!error ||
            busy ||
            !!pending ||
            storageFailed
          }
        >
          {busy ? "正在提交…" : "提交 金融条件日频 采集"}
        </button>
      </form>
      {failure && <p role="alert">{failure}</p>}
      {storageFailed && <p role="alert">本机请求记录无法核实，已阻止提交。</p>}
    </CollectionColumns>
  );
}
