import { useEffect, useRef, useState } from "react";
import type { Transport } from "./api";
import {
  financialCall,
  FinancialRequestError,
  type FinancialJob,
} from "./FinancialDailyWorkbench";
import { requestId } from "./CpiTimeSeriesCollection";
import { useVisiblePolling } from "./useVisiblePolling";

export const financialIntentKey = "mdf.financial.timeseries.pending.v1";
type Command = { action: string; start: string; end: string };
type Intent = {
  request_id: string;
  command?: Command;
  resume?: true;
  minimum_resume_count?: number;
  previous_time?: number;
};
function stored(): Intent | null {
  const raw = localStorage.getItem(financialIntentKey);
  if (!raw) return null;
  const v = JSON.parse(raw) as Intent;
  if (
    !v ||
    !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(
      v.request_id,
    ) ||
    !(
      v.resume === true ||
      (v.command &&
        ["init", "update"].includes(v.command.action) &&
        typeof v.command.start === "string" &&
        typeof v.command.end === "string")
    )
  )
    throw Error("intent_unreadable");
  return v;
}
const stamp = (job: FinancialJob) =>
  Math.max(
    ...[job.updated_at, job.finished_at, job.started_at, job.created_at]
      .map((v) => (v ? Date.parse(v) : -Infinity))
      .filter(Number.isFinite),
  );
function matches(job: FinancialJob, intent: Intent) {
  return (
    job?.request_id === intent.request_id &&
    [
      "pending",
      "running",
      "complete",
      "partial",
      "failed",
      "unknown",
      "outcome_unknown",
    ].includes(job.state) &&
    !!job.command &&
    ["init", "update"].includes(job.command.action) &&
    Array.isArray(job.steps) &&
    Number.isFinite(Date.parse(job.created_at)) &&
    (!intent.command ||
      ["action", "start", "end"].every(
        (k) =>
          job.command[k as keyof Command] ===
          intent.command![k as keyof Command],
      )) &&
    (!intent.minimum_resume_count ||
      (job.resume_count ?? 0) >= intent.minimum_resume_count ||
      (job.state === "complete" &&
        stamp(job) > (intent.previous_time ?? Infinity)))
  );
}
const terminal = (job: FinancialJob | null) =>
  !!job &&
  ["complete", "failed", "partial"].includes(job.state) &&
  !job.steps.some((s) =>
    ["running", "pending", "unknown", "outcome_unknown"].includes(s.state),
  );
/** Same lifecycle as the macro collection form: reserve -> receipt -> poll ->
 * explicit new intent. No automatic receipt clears storage or navigates. */
export function useFinancialSubmission(
  transport: Transport,
  scope: string,
  onReceipt: (job: FinancialJob) => void,
  active = true,
) {
  const [initial] = useState(() => {
    try {
      return { intent: stored(), failed: false };
    } catch {
      return { intent: null, failed: true };
    }
  });
  const [pending, setPending] = useState<Intent | null>(initial.intent);
  const [storageFailed, setStorageFailed] = useState(initial.failed);
  const [busy, setBusy] = useState(false),
    [failure, setFailure] = useState("");
  const [receipt, setReceipt] = useState<FinancialJob | null>(null),
    [rejected, setRejected] = useState(false);
  const writing = useRef(false),
    lifecycle = useRef(0),
    latest = useRef<FinancialJob | null>(null);
  const callback = useRef(onReceipt);
  callback.current = onReceipt;
  useEffect(() => {
    lifecycle.current += 1;
    setBusy(false);
    setFailure("");
    setReceipt(null);
    latest.current = null;
    setRejected(false);
    const update = () => {
      try {
        setPending(stored());
      } catch {
        setStorageFailed(true);
      }
    };
    update();
    window.addEventListener("storage", update);
    return () => {
      lifecycle.current += 1;
      window.removeEventListener("storage", update);
    };
  }, [transport, scope, active]);
  const tracking = useVisiblePolling(
    async (signal) => {
      const intent = pending!;
      const r = await financialCall<{ job: FinancialJob }>(
        transport,
        `/jobs/${intent.request_id}`,
        signal,
      );
      if (!matches(r.job, intent)) throw Error("receipt_mismatch");
      const previous = latest.current;
      if (
        previous?.request_id === r.job.request_id &&
        (stamp(r.job) < stamp(previous) ||
          (terminal(previous) &&
            !terminal(r.job) &&
            stamp(r.job) <= stamp(previous)))
      )
        throw Error("stale_receipt");
      return r.job;
    },
    [transport, scope, pending?.request_id, pending?.minimum_resume_count],
    active && !!pending,
    () => !writing.current && !rejected,
  );
  useEffect(() => {
    if (tracking.data && pending && matches(tracking.data, pending)) {
      latest.current = tracking.data;
      setReceipt(tracking.data);
      setFailure("");
    }
  }, [tracking.data, pending]);
  const uncertain =
    !!pending &&
    !rejected &&
    (!receipt ||
      ["unknown", "outcome_unknown"].includes(receipt.state) ||
      tracking.stale);
  const canRelease =
    !!pending &&
    !busy &&
    !storageFailed &&
    (rejected || (!tracking.stale && terminal(receipt)));
  function clear(intent: Intent) {
    const current = stored();
    if (!current || JSON.stringify(current) !== JSON.stringify(intent))
      throw Error("intent_changed");
    localStorage.removeItem(financialIntentKey);
  }
  function newIntent() {
    if (!pending || !canRelease || writing.current) return;
    try {
      clear(pending);
      tracking.invalidate();
      setPending(null);
      setReceipt(null);
      latest.current = null;
      setRejected(false);
      setFailure("");
    } catch {
      setStorageFailed(true);
    }
  }
  function verify() {
    if (!writing.current && !storageFailed && pending) {
      setFailure("");
      tracking.refresh();
    }
  }
  async function submit(
    command: Command | { resume: string; job: FinancialJob },
  ) {
    if (!active || writing.current || storageFailed) return;
    const resuming = "resume" in command;
    if (
      pending &&
      !(resuming && command.resume === pending.request_id && canRelease)
    )
      return;
    const intent: Intent = resuming
      ? {
          request_id: command.resume,
          resume: true,
          command: command.job.command,
          minimum_resume_count: (command.job.resume_count ?? 0) + 1,
          previous_time: stamp(command.job),
        }
      : { request_id: requestId(), command };
    const version = lifecycle.current;
    writing.current = true;
    setBusy(true);
    setFailure("");
    setRejected(false);
    try {
      const current = stored();
      if (current && JSON.stringify(current) !== JSON.stringify(pending))
        throw Error("existing_pending_intent");
      localStorage.setItem(financialIntentKey, JSON.stringify(intent));
      if (JSON.stringify(stored()) !== JSON.stringify(intent))
        throw Error("intent_not_saved");
    } catch {
      writing.current = false;
      setBusy(false);
      setStorageFailed(true);
      setFailure("本机请求身份无法可靠保存，已阻止提交；请先核实本机记录。");
      return;
    }
    tracking.invalidate();
    latest.current = null;
    setReceipt(null);
    setPending(intent);
    try {
      const r = await financialCall<{ job: FinancialJob }>(
        transport,
        intent.resume ? "/jobs/resume" : "/jobs",
        undefined,
        intent.resume
          ? { request_id: intent.request_id }
          : { request_id: intent.request_id, ...intent.command },
      );
      if (!matches(r.job, intent)) throw Error("receipt_mismatch");
      if (version === lifecycle.current) {
        latest.current = r.job;
        setReceipt(r.job);
        callback.current(r.job);
      }
    } catch (e) {
      if (version === lifecycle.current) {
        const definite =
          e instanceof FinancialRequestError &&
          [400, 403, 422].includes(e.status) &&
          [
            "financial_job_range_invalid",
            "financial_invalid_job_request",
            "financial_invalid_request_id",
            "financial_invalid_job_action",
            "financial_capability_not_enabled",
            "operator_not_authorized",
          ].includes(e.message);
        setRejected(definite);
        setFailure(
          definite
            ? e.message === "financial_job_range_invalid"
              ? "日期区间无效或超过可采范围；服务已拒绝本次请求，未创建任务。"
              : `服务已拒绝本次采集，未创建任务（${e.message}）。`
            : "采集回执不明，不能判断是否已执行。将自动跟踪原请求，请勿重复提交。",
        );
      }
    } finally {
      writing.current = false;
      if (version === lifecycle.current) {
        setBusy(false);
        tracking.refresh();
      }
    }
  }
  return {
    pending,
    busy,
    failure,
    storageFailed,
    receipt,
    uncertain,
    canRelease,
    rejected,
    submit,
    verify,
    newIntent,
    stale: tracking.stale,
  };
}
