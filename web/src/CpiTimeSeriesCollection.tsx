import {
  CPI_PROFILE,
  profileScopeText,
  type MacroProfile,
} from "./macroTimeSeriesProfile";
import { DateField } from "./DateField";
import { useEffect, useRef, useState } from "react";
import type { Transport } from "./api";
import { useVisiblePolling } from "./useVisiblePolling";
import { CpiAvailabilityNotice } from "./CpiTimeSeries";
import {
  cpiRequest,
  CpiServiceFailure,
  latestMonth,
  monthCount,
  rangeValid,
  readCpiJobs,
  validJob,
  uuidValid,
  type CpiAvailability,
  type CpiJob,
  type CpiJobIntent,
} from "./cpiTimeSeriesApi";
import {
  CpiStrategyWorkbench,
  cpiActionText,
  cpiExecutionStateText,
} from "./CpiStrategyWorkbench";
import { formatBeijingTime } from "./beijingTime";

const profileIntentKey = (profile: MacroProfile) =>
  `mdf.${profile.id}.timeseries.pending.v1`;
function requestId() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  // HTTP LAN origins still expose getRandomValues outside a secure context.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"));
  return [
    hex.slice(0, 4),
    hex.slice(4, 6),
    hex.slice(6, 8),
    hex.slice(8, 10),
    hex.slice(10),
  ]
    .map((part) => part.join(""))
    .join("-");
}
function storedIntent(intentKey: string): CpiJobIntent | null {
  const raw = localStorage.getItem(intentKey);
  if (raw === null) return null;
  const value: CpiJobIntent = JSON.parse(raw);
  if (
    !value ||
    !uuidValid(value.request_id) ||
    !["init", "initialize", "update"].includes(value.action) ||
    !rangeValid(value.start, value.end)
  )
    throw new Error("intent_unreadable");
  return value;
}
const sameIntent = (a: CpiJobIntent, b: CpiJobIntent) =>
  a.request_id === b.request_id &&
  a.action === b.action &&
  a.start === b.start &&
  a.end === b.end;
export function CpiTimeSeriesCollection(
  props: Parameters<typeof CpiTimeSeriesCollectionContent>[0],
) {
  return (
    <CpiTimeSeriesCollectionContent
      {...props}
      key={(props.profile ?? CPI_PROFILE).id}
    />
  );
}
function CpiTimeSeriesCollectionContent({
  transport,
  profile = CPI_PROFILE,
  availability,
  tab,
  generation = 0,
  onExecution,
}: {
  transport: Transport;
  profile?: MacroProfile;
  availability: CpiAvailability;
  tab: string;
  generation?: number;
  onExecution?: (id: string) => void;
}) {
  const intentKey = profileIntentKey(profile);
  const latest = latestMonth(availability.data);
  const [start, setStart] = useState(latest);
  const [end, setEnd] = useState(latest);
  const [action, setAction] = useState<"initialize" | "update">("update");
  const [confirmed, setConfirmed] = useState(false);
  const [initial] = useState(() => {
    try {
      return { intent: storedIntent(intentKey), failed: false };
    } catch {
      return { intent: null, failed: true };
    }
  });
  const [intent, setIntent] = useState(initial.intent);
  const [storageFailed, setStorageFailed] = useState(initial.failed);
  const [receipt, setReceipt] = useState<CpiJob | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const drafts = useRef<
    Partial<Record<"initialize" | "update", { start: string; end: string }>>
  >({});
  const edited = useRef({ initialize: false, update: false }),
    writing = useRef(false),
    mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    if (latest && action === "update" && !edited.current.update && !intent) {
      setStart(latest);
      setEnd(latest);
    }
  }, [latest, action, intent]);
  useEffect(() => {
    setConfirmed(false);
  }, [start, end, action]);
  useEffect(() => {
    const update = () => {
      try {
        setIntent(storedIntent(intentKey));
      } catch {
        setStorageFailed(true);
      }
    };
    window.addEventListener("storage", update);
    return () => window.removeEventListener("storage", update);
  }, []);
  const jobs = useVisiblePolling(
    (signal) => readCpiJobs(transport, signal, profile),
    [transport, generation, profile.id],
    !!availability.data && tab === "new",
  );
  const matched = intent
    ? (jobs.data?.jobs.find((job) => sameIntent(job, intent)) ??
      (receipt && sameIntent(receipt, intent) ? receipt : null))
    : null;
  const unresolved =
    !!intent && (!matched || ["running", "unknown"].includes(matched.state));
  const enabled =
    availability.data?.web_capabilities.collect === true &&
    !availability.failure;
  const maxMonths = availability.data?.collection_scope?.max_months;
  const preset = availability.data?.initialization;
  const initializing = action === "initialize";
  const requestedStart = start;
  const requestedEnd = end;
  const validRange =
    rangeValid(start, end) &&
    (maxMonths == null || monthCount(start, end) <= maxMonths);
  useEffect(() => {
    if (
      initializing &&
      !edited.current.initialize &&
      !intent &&
      preset?.available &&
      rangeValid(preset.start ?? "", preset.end ?? "")
    ) {
      setStart(preset.start!);
      setEnd(preset.end!);
    }
  }, [initializing, preset?.start, preset?.end, preset?.available, intent]);
  useEffect(() => {
    if (!initial.intent) return;
    setAction(initial.intent.action === "update" ? "update" : "initialize");
    setStart(initial.intent.start);
    setEnd(initial.intent.end);
  }, [initial]);
  function changeAction(next: "initialize" | "update") {
    drafts.current[action] = { start, end };
    const saved = drafts.current[next];
    const defaultRange =
      next === "initialize"
        ? {
            start: preset?.available ? (preset.start ?? "") : "",
            end: preset?.available ? (preset.end ?? "") : "",
          }
        : { start: latest, end: latest };
    setStart((saved ?? defaultRange).start);
    setEnd((saved ?? defaultRange).end);
    setAction(next);
  }
  async function submit() {
    if (
      writing.current ||
      !enabled ||
      !confirmed ||
      intent ||
      storageFailed ||
      !validRange
    )
      return;
    writing.current = true;
    setBusy(true);
    setNotice("");
    let reserved: CpiJobIntent | null = null;
    try {
      const existing = storedIntent(intentKey);
      if (existing) {
        setIntent(existing);
        setNotice("已有请求待回查，请先核实原任务。");
        writing.current = false;
        setBusy(false);
        return;
      }
      reserved = {
        request_id: requestId(),
        action,
        start: requestedStart,
        end: requestedEnd,
      };
      const serialized = JSON.stringify(reserved);
      localStorage.setItem(intentKey, serialized);
      if (localStorage.getItem(intentKey) !== serialized)
        throw new Error("intent_not_saved");
      setIntent(reserved);
    } catch {
      reserved = null;
      setStorageFailed(true);
      setNotice("无法可靠保存请求编号，采集未提交。");
    }
    try {
      if (!reserved) return;
      const job = await cpiRequest<CpiJob>(
        transport,
        "/jobs",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(reserved),
        },
        20000,
        profile,
      );
      if (!validJob(job) || !sameIntent(job, reserved))
        throw new Error("receipt_unknown");
      if (mounted.current) {
        setReceipt(job);
        jobs.refresh();
        setNotice("已收到任务状态，正在自动跟踪同一请求。");
      }
    } catch (error) {
      if (mounted.current) {
        // The host refuses the execution lock before saving this request's journal.
        const jobsBusy =
          error instanceof CpiServiceFailure &&
          error.status === 409 &&
          error.classification === `${profile.id}_jobs_busy`;
        const initializationRefused =
          reserved?.action === "initialize" &&
          error instanceof CpiServiceFailure &&
          ((error.status === 409 &&
            error.classification === "initialization_anchor_unavailable") ||
            (error.status === 400 &&
              error.classification === "initialization_preset_range_required"));
        const refused =
          jobsBusy ||
          initializationRefused ||
          (error instanceof CpiServiceFailure &&
            [400, 403, 422].includes(error.status ?? 0) &&
            [
              "collection_not_enabled",
              `${profile.id}_package_credential_binding_unavailable`,
              `${profile.id}_capability_disabled`,
              "bls_credential_missing",
              "bls_credential_unknown",
              "operator_not_authorized",
              "local_read_not_authorized",
              "collection_range_exceeds_host_limit",
            ].includes(error.classification));
        if (refused && reserved) {
          try {
            const stored = storedIntent(intentKey);
            if (!stored || !sameIntent(stored, reserved))
              throw new Error("intent_changed");
            localStorage.removeItem(intentKey);
            setIntent(null);
            if (reserved.action !== "init") setAction(reserved.action);
            setStart(reserved.start);
            setEnd(reserved.end);
            setConfirmed(false);
          } catch {
            setStorageFailed(true);
          }
        }
        if (initializationRefused) availability.refresh?.();
        setNotice(
          jobsBusy
            ? `另一项 ${profile.label} 采集正在执行，本次请求未创建任务。请待任务释放后重新确认原范围。`
            : refused
              ? "服务明确拒绝本次采集，未创建任务。请核实写入资格或范围后重新确认。"
              : "采集回执不明，不能判断是否已执行。将自动跟踪原请求，请勿重复提交。",
        );
        jobs.refresh();
      }
    } finally {
      writing.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  function newIntent() {
    if (!intent || unresolved || busy || !matched) return;
    try {
      const stored = storedIntent(intentKey);
      if (!stored || !sameIntent(stored, intent))
        throw new Error("intent_changed");
      localStorage.removeItem(intentKey);
      setIntent(null);
      setReceipt(null);
      setNotice("");
      setConfirmed(false);
    } catch {
      setStorageFailed(true);
    }
  }
  const briefJobs = [
    ...(jobs.data?.jobs ?? []),
    ...(receipt &&
    !jobs.data?.jobs.some((job) => job.request_id === receipt.request_id)
      ? [receipt]
      : []),
  ].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
  async function resumeJob(job: CpiJob) {
    if (
      writing.current ||
      !enabled ||
      !confirmed ||
      start !== job.start ||
      end !== job.end ||
      (action === "update") !== (job.action === "update")
    )
      return;
    writing.current = true;
    setBusy(true);
    try {
      const next = await cpiRequest<CpiJob>(
        transport,
        "/jobs/resume",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            request_id: job.request_id,
            action: job.action,
            start: job.start,
            end: job.end,
          }),
        },
        20000,
        profile,
      );
      if (!validJob(next) || !sameIntent(next, job))
        throw new Error("retail_resume_unknown");
      setReceipt(next);
      setNotice("已按原请求编号恢复；保留已成功批次，不创建另一项采集。");
    } catch {
      setNotice("恢复回执未知或尚未到重试时间；保留原请求，请刷新核实。");
    } finally {
      writing.current = false;
      if (mounted.current) {
        setBusy(false);
        jobs.refresh();
      }
    }
  }
  if (tab === "periodic")
    return (
      <CpiStrategyWorkbench
        profile={profile}
        transport={transport}
        availability={availability}
        generation={generation}
      />
    );
  if (tab !== "new") return null;
  return (
    <section
      aria-label={`${profile.label} 受控采集`}
      className="ordinary-query cpi-timeseries"
    >
      <h3>{profile.label} 受控采集</h3>
      <CpiAvailabilityNotice profile={profile} availability={availability} />
      {profile.historyNotice && (
        <p className="small">{profile.historyNotice}</p>
      )}
      <p>
        {profileScopeText(profile)}
        一起采集。长范围由后端自动分批，无需手动拆分。
      </p>
      {maxMonths != null && (
        <p className="small">当前主机每次允许最多 {maxMonths} 个月。</p>
      )}
      {!enabled && availability.data && (
        <p role="status">当前未开放 {profile.label} 采集。</p>
      )}
      <div className="workbench cpi-collection-workbench">
        <div className="collection-input-pane">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (e.currentTarget.checkValidity()) void submit();
            }}
          >
            <label>
              采集方式
              <select
                value={action}
                onChange={(e) =>
                  changeAction(e.target.value as "initialize" | "update")
                }
              >
                <option value="update">更新指定范围</option>
                <option value="initialize">初始化采集</option>
              </select>
            </label>
            <div className="field-grid">
              <DateField
                key={`${action}-start`}
                label="采集开始月份"
                type="month"
                value={start}
                onChange={(value) => {
                  edited.current[action] = true;
                  setStart(value);
                }}
              />
              <DateField
                key={`${action}-end`}
                label="采集结束月份"
                type="month"
                value={end}
                onChange={(value) => {
                  edited.current[action] = true;
                  setEnd(value);
                }}
              />
            </div>
            {initializing && (
              <p>
                初始化服务预置范围：{preset?.months ?? "待核实"}个月，可自行修改
              </p>
            )}
            {initializing && preset?.available && (
              <p>
                服务预置月份：{preset.start} 至 {preset.end}
              </p>
            )}
            {initializing && !preset?.available && (
              <p role="status">
                服务暂无默认范围（{preset?.reason || "尚未确认"}
                ），请自行填写合法月份范围。
              </p>
            )}
            {start && end && !validRange && (
              <p role="alert">起止月份无效或超过服务允许的采集范围。</p>
            )}
            <label className="cpi-collection-confirm">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              <span>我确认将以上范围采集到开发测试库</span>
            </label>
            <button
              className="primary"
              disabled={
                !enabled ||
                !confirmed ||
                !!intent ||
                storageFailed ||
                busy ||
                !validRange
              }
            >
              {busy ? "正在提交…" : `提交 ${profile.label} 采集`}
            </button>
          </form>
          {storageFailed && (
            <p role="alert">本机请求记录无法核实，已阻止提交。</p>
          )}
        </div>
        <aside className="detail-pane execution-briefs" aria-label="执行简报">
          <h3>执行简报</h3>
          {notice && <p role="status">{notice}</p>}
          <p className="small">时间基准：北京时间（UTC+08:00）</p>
          <ol className="execution-brief-list">
            {intent && !briefJobs.some((job) => sameIntent(job, intent)) && (
              <li>
                <div className="execution-brief-card" role="status">
                  <strong>
                    美国 {profile.label} · {cpiActionText(intent.action)}
                  </strong>
                  <dl>
                    <div>
                      <dt>请求编号</dt>
                      <dd className="request-id">{intent.request_id}</dd>
                    </div>
                    <div>
                      <dt>创建时间</dt>
                      <dd>未核实（本机保留意图）</dd>
                    </div>
                    <div>
                      <dt>采集范围</dt>
                      <dd>
                        {intent.start} 至 {intent.end}
                      </dd>
                    </div>
                    <div>
                      <dt>结果</dt>
                      <dd>
                        {busy
                          ? "正在提交，尚未确认任务创建。"
                          : "结果未知，保留请求并自动跟踪，请勿重复提交；尚未确认任务创建。"}
                      </dd>
                    </div>
                  </dl>
                </div>
              </li>
            )}
            {briefJobs.map((job) => (
              <li key={job.request_id}>
                <button
                  type="button"
                  className="execution-brief-card"
                  onClick={() => onExecution?.(job.job_id)}
                >
                  <strong>
                    美国 {profile.label} · {cpiActionText(job.action)}
                  </strong>
                  <dl>
                    <div>
                      <dt>请求编号</dt>
                      <dd className="request-id">{job.request_id}</dd>
                    </div>
                    <div>
                      <dt>创建时间</dt>
                      <dd>
                        <time>
                          {formatBeijingTime(job.created_at).replace(
                            " 北京时间",
                            "",
                          )}
                        </time>
                      </dd>
                    </div>
                    <div>
                      <dt>采集范围</dt>
                      <dd>
                        {job.start} 至 {job.end}
                      </dd>
                    </div>
                    <div>
                      <dt>结果</dt>
                      <dd>
                        {cpiExecutionStateText(
                          job.state,
                          job.result,
                          profile,
                          job,
                        )}
                        {job.classification ? ` · ${job.classification}` : ""}
                      </dd>
                    </div>
                  </dl>
                </button>
                {profile.id === "retail" &&
                  ["failed", "unknown"].includes(job.state) && (
                    <button
                      disabled={busy || !enabled || !confirmed}
                      onClick={() => void resumeJob(job)}
                    >
                      按原请求恢复采集（{job.start} 至 {job.end}）
                    </button>
                  )}
                {["unknown", "outcome_unknown"].includes(job.state) && (
                  <p role="status">
                    结果未知，保留请求并自动跟踪，请勿重复提交。
                  </p>
                )}
                {intent &&
                  sameIntent(job, intent) &&
                  !unresolved &&
                  matched && (
                    <button
                      disabled={busy || storageFailed}
                      onClick={newIntent}
                    >
                      开始另一项采集
                    </button>
                  )}
              </li>
            ))}
          </ol>
          {!briefJobs.length && !intent && (
            <p>
              {jobs.busy
                ? "正在读取执行简报…"
                : jobs.failure
                  ? "执行简报读取失败，当前记录尚未核实。"
                  : "暂无手动采集记录。"}
            </p>
          )}
          {jobs.failure && (
            <p role="alert">任务状态已陈旧，保留上次结果并自动重读。</p>
          )}
        </aside>
      </div>
    </section>
  );
}
export function CpiTimeSeriesStrategy(props: {
  profile?: MacroProfile;
  transport: Transport;
  availability: CpiAvailability;
  generation?: number;
}) {
  return <CpiStrategyWorkbench {...props} />;
}
