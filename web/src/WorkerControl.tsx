import { useRef, useState } from "react";
import type { Transport } from "./api";
import type { MacroProfile } from "./macroTimeSeriesProfile";
import { cpiRequest, CpiServiceFailure } from "./cpiTimeSeriesApi";
import { useVisiblePolling } from "./useVisiblePolling";
import { formatBeijingTime } from "./beijingTime";

type WorkerState = {
  registered: boolean;
  online: boolean;
  reason: string;
  process_id: number | null;
  service_state: string;
  worker: { state: string; last_heartbeat_at: string | null };
  operation?: string;
};
function checked(value: WorkerState): WorkerState {
  if (
    !value ||
    typeof value.online !== "boolean" ||
    typeof value.registered !== "boolean" ||
    !value.worker ||
    typeof value.worker.state !== "string"
  )
    throw new Error("worker_status_unknown");
  return value;
}
const reasons: Record<string, string> = {
  worker_online: "执行器在线，进程和心跳已核对",
  worker_service_not_started: "可控服务已装配，本窗口尚未启动；可按需单独启动",
  worker_service_stopped: "服务已停止；可单独启动此资产执行器",
  worker_heartbeat_stale: "心跳过期；请核对并恢复此执行器",
  worker_process_not_confirmed: "服务进程与心跳尚未对齐，等待刷新核实",
  worker_loop_error: "进程存在，但采集循环异常，可重试恢复",
  worker_unit_not_registered: "此资产的固定服务尚未装配",
};
export function WorkerControl({
  transport,
  profile,
  onUpdated,
}: {
  transport: Transport;
  profile: MacroProfile;
  onUpdated: () => void;
}) {
  const [busy, setBusy] = useState(false),
    [message, setMessage] = useState<string | null>(null);
  const writing = useRef(false);
  const resource = useVisiblePolling(
    (signal) =>
      cpiRequest<WorkerState>(
        transport,
        "/worker",
        { signal },
        15000,
        profile,
      ).then(checked),
    [transport, profile.id],
    true,
    () => !writing.current,
  );
  async function act(action: "start" | "recover") {
    if (writing.current) return;
    writing.current = true;
    setBusy(true);
    setMessage(null);
    resource.invalidate();
    try {
      const value = await cpiRequest<WorkerState>(
        transport,
        "/worker",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action }),
        },
        20000,
        profile,
      );
      resource.commit(checked(value));
      setMessage(
        value.online
          ? "已回读确认执行器在线。"
          : "动作已提交，尚未确认在线，请刷新核对；当前不会报告恢复成功。",
      );
    } catch (error) {
      const known: Record<string, string> = {
        worker_active_collection_in_progress:
          "当前资产正在采集，为避免中断写入，本次未重启。请待任务结束后重试。",
        worker_unmanaged_process_present:
          "发现已有独立执行器进程，本次未重复启动。请先核对该进程。",
        dev_window_expired: "DEV运行窗口已到期，不能启动或恢复。",
        worker_unit_not_registered:
          "当前固定版本服务未装配，请刷新并核对运行版本。",
      };
      setMessage(
        error instanceof CpiServiceFailure && known[error.classification]
          ? known[error.classification]
          : "启动或恢复结果未确认。请刷新核对实际服务；若仍停止或异常，可重试。当前未报告修复成功。",
      );
    } finally {
      writing.current = false;
      setBusy(false);
      resource.refresh();
      onUpdated();
    }
  }
  const value = resource.data;
  return (
    <section
      className="worker-control"
      aria-label={`${profile.label}执行器控制`}
    >
      <div className="toolbar">
        <h4>执行器</h4>
        <button
          disabled={busy}
          onClick={() => {
            resource.refresh();
            onUpdated();
          }}
        >
          刷新执行器状态
        </button>
      </div>
      <p>
        {value
          ? (reasons[value.reason] ?? "执行器状态待核对")
          : resource.failure
            ? "执行器控制读取失败，请刷新重试。"
            : "正在核对实际服务与心跳…"}
      </p>
      {value && (
        <p className="small">
          服务：{value.service_state} · 进程：{value.process_id ?? "未运行"} ·
          心跳：{formatBeijingTime(value.worker.last_heartbeat_at, "未记录")}
        </p>
      )}
      {value?.registered && !value.online && (
        <div className="actions">
          <button
            className="primary"
            disabled={busy}
            onClick={() =>
              void act(value.service_state === "active" ? "recover" : "start")
            }
          >
            {busy
              ? "核对处理中…"
              : value.service_state === "active"
                ? "恢复执行器"
                : "启动执行器"}
          </button>
        </div>
      )}
      {message && <p role="status">{message}</p>}
      {resource.stale && (
        <p role="status">当前观测已陈旧，刷新后再确认运行情况。</p>
      )}
      <p className="small">
        仅控制当前资产的 DEV
        执行器，沿用原截止；策略开关独立，在线不代表采集成功。
      </p>
    </section>
  );
}
