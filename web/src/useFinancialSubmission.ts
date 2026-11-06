import { useEffect, useRef, useState } from "react";
import type { Transport } from "./api";
import {
  financialCall,
  FinancialRequestError,
  type FinancialJob,
} from "./FinancialDailyWorkbench";
import { requestId } from "./CpiTimeSeriesCollection";

export const financialIntentKey = "mdf.financial.timeseries.pending.v1";
type Command = { action: string; start: string; end: string };
type Intent = { request_id: string; command?: Command; resume?: true };
function stored(): Intent | null {
  const raw = localStorage.getItem(financialIntentKey);
  if (!raw) return null;
  const v = JSON.parse(raw) as Intent;
  if (
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
function matches(job: FinancialJob, intent: Intent) {
  return (
    job?.request_id === intent.request_id &&
    typeof job.state === "string" &&
    (!intent.command ||
      ["action", "start", "end"].every(
        (k) =>
          job.command?.[k as keyof Command] ===
          intent.command![k as keyof Command],
      ))
  );
}
/** An outbox is written BEFORE a mutation. Only a matching durable receipt clears it.
 * Switching pages invalidates UI effects, never the original request identity. */
export function useFinancialSubmission(
  transport: Transport,
  scope: string,
  onReceipt: (job: FinancialJob) => void,
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
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const writing = useRef(false),
    lifecycle = useRef(0);
  const rejected = useRef<Intent | null>(null);
  const callback = useRef(onReceipt);
  callback.current = onReceipt;
  useEffect(() => {
    lifecycle.current += 1;
    setBusy(false);
    setFailure("");
    const update = () => {
      try {
        setPending(stored());
      } catch {
        setStorageFailed(true);
      }
    };
    window.addEventListener("storage", update);
    return () => {
      lifecycle.current += 1;
      window.removeEventListener("storage", update);
    };
  }, [transport, scope]);
  function clear(intent: Intent) {
    if (stored()?.request_id === intent.request_id)
      localStorage.removeItem(financialIntentKey);
  }
  async function verify() {
    if (!pending || writing.current || storageFailed) return;
    const version = lifecycle.current,
      intent = pending;
    writing.current = true;
    setBusy(true);
    try {
      const r = await financialCall<{ job: FinancialJob }>(
        transport,
        `/jobs/${intent.request_id}`,
      );
      if (!matches(r.job, intent)) throw Error("receipt_mismatch");
      clear(intent);
      if (version === lifecycle.current) {
        setPending(null);
        setFailure("");
        callback.current(r.job);
      }
    } catch {
      if (version === lifecycle.current)
        setFailure(
          "同一请求尚未取得匹配回执；保留请求身份，请稍后回查，不会自动重复提交。",
        );
    } finally {
      writing.current = false;
      if (version === lifecycle.current) setBusy(false);
    }
  }
  async function submit(command: Command | { resume: string }) {
    if (writing.current || pending || storageFailed) return;
    const version = lifecycle.current;
    const intent: Intent =
      "resume" in command
        ? { request_id: command.resume, resume: true }
        : rejected.current?.command &&
            JSON.stringify(rejected.current.command) === JSON.stringify(command)
          ? rejected.current
          : { request_id: requestId(), command };
    writing.current = true;
    setBusy(true);
    setFailure("");
    try {
      if (stored()) throw Error("existing_pending_intent");
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
      clear(intent);
      rejected.current = null;
      if (version === lifecycle.current) {
        setPending(null);
        callback.current(r.job);
      }
    } catch (e) {
      // A definite 4xx rejection has no accepted receipt. An explicit unchanged
      // retry retains its request_id; an unknown/5xx result is GET-only until verified.
      const definite =
        e instanceof FinancialRequestError &&
        e.status >= 400 &&
        e.status < 500 &&
        e.status !== 409;
      if (definite) {
        clear(intent);
        rejected.current = intent;
      }
      if (version === lifecycle.current) {
        if (definite) setPending(null);
        setFailure(
          definite
            ? e.message === "financial_job_range_invalid"
              ? "日期区间无效或超过可采范围，请核对日期后重新确认。"
              : `请求被拒绝（${e.message}），请核对范围或来源准入后重新确认。`
            : "提交结果尚未确认，请先核实原请求；不会自动重复提交。",
        );
      }
    } finally {
      writing.current = false;
      if (version === lifecycle.current) setBusy(false);
    }
  }
  return { pending, busy, failure, storageFailed, submit, verify };
}
