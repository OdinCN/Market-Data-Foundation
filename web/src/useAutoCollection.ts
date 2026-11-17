import {
  autoCollectionDiagnosis,
  type DiagnosisEntry,
  type PlanSnapshot,
} from "./autoCollectionDiagnosis";
import { useEffect, useMemo, useRef, useState } from "react";
import type { Transport } from "./api";
import type { MacroProfile } from "./macroTimeSeriesProfile";
import {
  cpiRequest,
  CpiServiceFailure,
  readCpiStrategy,
  saveCpiStrategy,
  type CpiAvailability,
  type CpiStrategy,
} from "./cpiTimeSeriesApi";
import { useVisiblePolling } from "./useVisiblePolling";
export type WorkerObservation = {
  registered: boolean;
  online: boolean;
  reason: string;
  process_id: number | null;
  service_state: string;
  expires_at?: string;
  worker: { state: string; last_heartbeat_at: string | null };
};
function checked(value: WorkerObservation) {
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
  worker_service_not_started: "此资产服务尚未启动",
  worker_service_stopped: "此资产服务已停止",
  worker_heartbeat_stale: "服务心跳过期",
  worker_process_not_confirmed: "服务进程与心跳尚未对齐",
  worker_loop_error: "采集循环异常",
  worker_unit_not_registered: "固定服务尚未装配",
  worker_active_collection_in_progress:
    "任务正在采集，暂不重启；请待其完成后重试",
  worker_unmanaged_process_present: "发现独立进程，已阻止重复启动",
  worker_service_action_failed: "服务启动失败，请核对技术详情后重试",
  worker_service_control_unavailable: "服务控制暂不可用，请稍后核实",
  dev_window_expired: "DEV窗口已到期",
};
export function useAutoCollection(
  transport: Transport,
  generation: number,
  profile: MacroProfile,
  availability: CpiAvailability,
  visible: boolean,
) {
  const [saving, setSaving] = useState(false),
    [uncertain, setUncertain] = useState(false),
    [notice, setNotice] = useState<string | null>(null);
  const uncertainFrom = useRef<unknown>(null);
  const writing = useRef(false),
    mounted = useRef(true),
    target = useRef<boolean | null>(null);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  async function read(
    signal = new AbortController().signal,
    publish: (
      value: CpiStrategy & {
        control: WorkerObservation | null;
        schedule: PlanSnapshot | null;
      },
    ) => void = () => {},
  ) {
    const strategy = await readCpiStrategy(transport, signal, profile, true);
    publish({ ...strategy, control: null, schedule: null });
    const [control, schedule] = await Promise.all([
      cpiRequest<WorkerObservation>(
        transport,
        "/worker",
        { signal },
        15000,
        profile,
      )
        .then(checked)
        .catch(() => null),
      cpiRequest<PlanSnapshot>(
        transport,
        "/schedule",
        { signal },
        15000,
        profile,
      )
        .then((value) =>
          value &&
          typeof value.calendar_status === "string" &&
          Array.isArray(value.targets)
            ? value
            : null,
        )
        .catch(() => null),
    ]);
    return { ...strategy, control, schedule };
  }
  const resource = useVisiblePolling(
    read,
    [transport, generation, profile.id],
    !availability.legacy &&
      visible &&
      (!!availability.data || !!availability.discover),
    () => !writing.current,
  );
  const facts = useVisiblePolling(
    (signal) => readCpiStrategy(transport, signal, profile),
    [
      transport,
      generation,
      profile.id,
      // The selected asset's live status and policy receipt requalify these
      // optional facts; unchanged heads need no second aggregate every tick.
      JSON.stringify([
        availability.failure,
        availability.data?.batch_count,
        availability.data?.observation_count,
        availability.data?.revision_count,
        availability.data?.latest_months,
        availability.data?.latest_quarters,
        availability.data?.latest_weeks,
        resource.data?.last_result,
      ]),
    ],
    !!availability.data && !availability.failure && !!resource.data && visible,
    undefined,
    false,
  );
  const data = useMemo(
    () =>
      resource.data
        ? {
            ...resource.data,
            collection_facts_status:
              availability.failure ||
              facts.failure ||
              facts.data?.collection_facts_classification
                ? "failed"
                : facts.data
                  ? "verified"
                  : "pending",
            ...(facts.data
              ? {
                  last_successful_collection_at:
                    facts.data.last_successful_collection_at,
                  latest_data_period: facts.data.latest_data_period,
                  latest_data_periods: facts.data.latest_data_periods,
                  collection_facts_classification:
                    facts.data.collection_facts_classification,
                }
              : {}),
          }
        : null,
    [resource.data, facts.data, facts.failure, availability.failure],
  );
  async function act(recover = false) {
    if (
      !data ||
      writing.current ||
      uncertain ||
      resource.stale ||
      resource.busy
    )
      return;
    const enabled = recover || !data.enabled;
    if (
      enabled &&
      (!availability.data?.web_capabilities.collect ||
        (profile.id === "gdp" && !!availability.failure))
    ) {
      setNotice(
        `来源尚未满足采集条件：${availability.data?.web_capabilities.reason ?? "请检查来源配置与授权"}`,
      );
      return;
    }
    writing.current = true;
    target.current = enabled;
    resource.invalidate();
    setSaving(true);
    setNotice(null);
    try {
      // Only an explicit action can start a worker. A paused policy remains paused
      // until its service has been confirmed; GET/polling never run this branch.
      if (enabled) {
        const current = checked(
          await cpiRequest<WorkerObservation>(
            transport,
            "/worker",
            {},
            15000,
            profile,
          ),
        );
        if (!current.online) {
          const result = checked(
            await cpiRequest<WorkerObservation>(
              transport,
              "/worker",
              {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  action:
                    current.service_state === "active" ? "recover" : "start",
                }),
              },
              20000,
              profile,
            ),
          );
          if (!result.online) throw new Error("worker_awaiting_heartbeat");
        }
      }
      if (!recover || !data.enabled)
        await saveCpiStrategy(transport, enabled, undefined, profile);
      const value = await read();
      if (value.enabled !== enabled || (enabled && !value.control?.online))
        throw new Error("automatic_collection_unconfirmed");
      if (mounted.current) {
        resource.commit(value);
        target.current = null;
        setNotice(
          enabled
            ? "已回读确认自动采集已开启；等待官方发布与计划执行，采集成果另列。"
            : "已回读确认暂停后续自动触发；当前任务继续收尾，手动采集不受影响。",
        );
      }
    } catch (error) {
      if (mounted.current) {
        const known =
          error instanceof CpiServiceFailure && reasons[error.classification];
        setNotice(
          known ||
            "操作结果待核实，正在独立读取策略与实际服务；不会自动重复启动。若受来源配置阻塞，请检查来源与凭据入口。",
        );
        uncertainFrom.current = data;
        setUncertain(true);
      }
    } finally {
      writing.current = false;
      if (mounted.current) {
        setSaving(false);
        resource.refresh();
      }
    }
  }
  useEffect(() => {
    if (
      !data ||
      data === uncertainFrom.current ||
      !uncertain ||
      resource.stale ||
      !data.control ||
      writing.current
    )
      return;
    const control = data.control;
    const confirmed =
      data.enabled === target.current &&
      (!target.current || data.control.online);
    setNotice((old) =>
      confirmed
        ? "已独立回读确认当前自动采集状态；未重复提交启动。"
        : `${old ?? "操作未确认"} 当前策略${data.enabled ? "已开启" : "已暂停"}，${control.online ? "服务在线" : (reasons[control.reason] ?? "服务尚未确认在线")}；可核对后重试。`,
    );
    target.current = null;
    setUncertain(false);
  }, [data, uncertain, resource.stale]);
  const diagnosis = autoCollectionDiagnosis({
    data,
    saving,
    uncertain,
    resource,
    availability,
  });
  return {
    resource,
    data,
    saving,
    uncertain,
    notice,
    diagnosis,
    reason: diagnosis.listReason,
    toggle: () => act(),
    recover: () => act(true),
  };
}
export function autoCollectionState(entry: DiagnosisEntry) {
  return autoCollectionDiagnosis(entry).state;
}
