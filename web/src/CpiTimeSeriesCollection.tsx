import {
  useCollectionRecords,
  collectionRecent,
  CollectionBriefs,
  CollectionFormLock,
  collectionCanReopen,
} from "./CollectionLifecycle";
import { ExecutionBriefCard } from "./DailyFormControls";
import { useCollectionSubmit } from "./useCollectionSubmit";
import { claimsExecutionPeriod } from "./claimsPeriod";
import {
  dayRangeValid,
  claimsLatest,
  weekCount,
} from "./claimsTimeSeriesAdapter";
import { macroAssetLabel } from "./macroTimeSeriesProfile";
import {
  CPI_PROFILE,
  profileScopeText,
  initializationAnchorText,
  type MacroProfile,
} from "./macroTimeSeriesProfile";
import {
  QuarterField,
  previousCompleteQuarter,
  quarterRangeValid,
  quarterCount,
} from "./QuarterField";
import { Fact } from "./WorkflowTable";
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

function WeekField(props: Parameters<typeof QuarterField>[0]) {
  return <DateField {...props} type="date" />;
}
function MonthField(props: Parameters<typeof QuarterField>[0]) {
  return <DateField {...props} type="month" />;
}
const profileIntentKey = (profile: MacroProfile) =>
  `mdf.${profile.id}.timeseries.pending.v1`;
export function requestId() {
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
function storedIntent(
  intentKey: string,
  quarterly = false,
  weekly = false,
): CpiJobIntent | null {
  const raw = localStorage.getItem(intentKey);
  if (raw === null) return null;
  const value: CpiJobIntent = JSON.parse(raw);
  if (
    !value ||
    !uuidValid(value.request_id) ||
    !(
      quarterly ? ["init", "update"] : ["init", "initialize", "update"]
    ).includes(value.action) ||
    !(weekly ? dayRangeValid : quarterly ? quarterRangeValid : rangeValid)(
      value.start,
      value.end,
    )
  )
    throw new Error("intent_unreadable");
  return value;
}
const sameIntent = (a: CpiJobIntent, b: CpiJobIntent) =>
  a.request_id === b.request_id &&
  a.action === b.action &&
  a.start === b.start &&
  a.end === b.end &&
  a.series === b.series;
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
  const submission = useCollectionSubmit();
  const weekly = profile.id === "claims";
  const PeriodField = weekly
    ? WeekField
    : profile.id === "gdp"
      ? QuarterField
      : MonthField;
  const intentKey = profileIntentKey(profile);
  const quarterly = profile.id === "gdp";
  const validPeriodRange = weekly
    ? dayRangeValid
    : quarterly
      ? quarterRangeValid
      : rangeValid;
  const periodLabel = weekly
    ? "日期（截止周范围）"
    : quarterly
      ? "季度"
      : "月份";
  const latest = weekly
    ? claimsLatest(availability.data)
    : quarterly
      ? (availability.data?.quarter_initialization?.end ??
        previousCompleteQuarter())
      : latestMonth(availability.data);
  const [start, setStart] = useState(latest);
  const [end, setEnd] = useState(latest);
  const [action, setAction] = useState<"initialize" | "update">("update");
  const [confirmed, setConfirmed] = useState(false);
  const [initial] = useState(() => {
    try {
      return {
        intent: storedIntent(intentKey, quarterly, weekly),
        failed: false,
      };
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
        setIntent(storedIntent(intentKey, quarterly, weekly));
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
  const currentJobs = useCollectionRecords([
    ...(receipt ? [receipt] : []),
    ...(jobs.data?.jobs ?? []),
  ]);
  const matched = intent
    ? (currentJobs.find((job) => sameIntent(job, intent)) ??
      (receipt && sameIntent(receipt, intent) ? receipt : null))
    : null;
  const unresolved =
    !!intent && (!matched || !collectionCanReopen(matched.state) || jobs.stale);
  const enabled =
    availability.data?.web_capabilities.collect === true &&
    !availability.failure;
  const maxPeriods = quarterly
    ? availability.data?.collection_scope?.max_quarters
    : availability.data?.collection_scope?.max_months;
  const preset = weekly
    ? availability.data?.week_initialization
    : quarterly
      ? availability.data?.quarter_initialization
      : availability.data?.initialization;
  const initializing = action === "initialize";
  const requestedStart = start;
  const requestedEnd = end;
  const validRange =
    validPeriodRange(start, end) &&
    (!weekly || weekCount(start, end) > 0) &&
    (!quarterly || end <= previousCompleteQuarter()) &&
    (maxPeriods == null ||
      (quarterly ? quarterCount : monthCount)(start, end) <= maxPeriods);
  useEffect(() => {
    if (
      initializing &&
      !edited.current.initialize &&
      !intent &&
      preset?.available &&
      validPeriodRange(preset.start ?? "", preset.end ?? "")
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
  async function readIntent(intent: CpiJobIntent) {
    const result = await readCpiJobs(transport, undefined, profile);
    const job = result.jobs.find((value) => sameIntent(value, intent));
    if (!job) throw Error("receipt_unknown");
    return job;
  }
  function recallIntent() {
    if (!intent || busy) return;
    void submission
      .recall(
        intent.request_id,
        () => readIntent(intent),
        (value) => validJob(value, profile) && sameIntent(value, intent),
      )
      .then((job) => {
        setReceipt(job);
        setConfirmed(false);
        jobs.refresh();
        setNotice("已回读原请求，未重复提交。");
      })
      .catch(() => setNotice("原请求回执仍待核实，保留编号，不重复提交。"));
  }
  function refusal(error: unknown, intent: CpiJobIntent | null) {
    // The host refuses the execution lock before saving this request's journal.
    const jobsBusy =
      error instanceof CpiServiceFailure &&
      error.status === 409 &&
      error.classification === `${profile.id}_jobs_busy`;
    const initializationRefused =
      intent?.action === "initialize" &&
      error instanceof CpiServiceFailure &&
      ((error.status === 409 &&
        error.classification === "initialization_anchor_unavailable") ||
        (error.status === 400 &&
          error.classification === "initialization_preset_range_required"));
    const refused =
      jobsBusy ||
      ((quarterly || weekly) &&
        error instanceof CpiServiceFailure &&
        [400, 403, 422].includes(error.status ?? 0)) ||
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
    return { jobsBusy, initializationRefused, refused };
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
      const existing = storedIntent(intentKey, quarterly, weekly);
      if (existing) {
        setIntent(existing);
        setNotice("已有请求待回查，请先核实原任务。");
        writing.current = false;
        setBusy(false);
        return;
      }
      reserved = {
        request_id: requestId(),
        action:
          (quarterly || weekly) && action === "initialize" ? "init" : action,
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
      const intent = reserved;
      const job = await submission.submit(
        intent.request_id,
        () =>
          cpiRequest<CpiJob>(
            transport,
            "/jobs",
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(intent),
            },
            20000,
            profile,
          ),
        () => readIntent(intent),
        (value) => validJob(value, profile) && sameIntent(value, intent),
        (error) => refusal(error, intent).refused,
      );
      if (!validJob(job, profile) || !sameIntent(job, reserved))
        throw new Error("receipt_unknown");
      if (mounted.current) {
        setReceipt(job);
        setConfirmed(false);
        jobs.refresh();
        setNotice("已收到任务状态，正在自动跟踪同一请求。");
      }
    } catch (error) {
      if (mounted.current) {
        const { jobsBusy, initializationRefused, refused } = refusal(
          error,
          reserved,
        );
        if (refused && reserved) {
          try {
            const stored = storedIntent(intentKey, quarterly, weekly);
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
              ? `服务明确拒绝本次采集，未创建任务。请核实写入资格或范围后重新确认。${error instanceof CpiServiceFailure ? `（${error.classification}）` : ""}`
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
      const stored = storedIntent(intentKey, quarterly, weekly);
      if (!stored || !sameIntent(stored, intent))
        throw new Error("intent_changed");
      localStorage.removeItem(intentKey);
      setIntent(null);
      submission.reset();
      setNotice("");
      setConfirmed(false);
    } catch {
      setStorageFailed(true);
    }
  }
  const briefJobs = collectionRecent(currentJobs, intent?.request_id);
  async function resumeJob(job: CpiJob) {
    if (writing.current || !enabled || !profile.resumePath) return;
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
      if (!validJob(next, profile) || !sameIntent(next, job))
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
      <p>
        共 {profile.series.length} 种口径一起采集。
        {profile.id === "m3" &&
          "每项同时采集耐用品先行报告（Advance）与制造业完整报告，分别保留。"}
        {profile.id === "m3"
          ? "历史按已验证的最多240个月窗口逐指标获取，后台推进并持久记录进度；实际错误可按原请求恢复。"
          : "长范围由后端自动分批，无需手动拆分。"}
      </p>
      <details>
        <summary>口径与采集说明</summary>
        <p>{profileScopeText(profile)}</p>
        {profile.historyNotice && (
          <p className="small">{profile.historyNotice}</p>
        )}
      </details>
      {profile.id === "jolts" && availability.data && (
        <details>
          <summary>本地实际覆盖</summary>
          <p>
            本地已持有 {availability.data.observation_count} 项观测、
            {availability.data.batch_count}{" "}
            个批次。各项最新月份不证明中间月份完整，完整窗口以查询或采集回读为准。
          </p>
          <dl>
            {profile.series.map(([id, label]) => (
              <Fact key={id} name={label}>
                {availability.data?.latest_months[id] ?? "尚无本地记录"}
              </Fact>
            ))}
          </dl>
        </details>
      )}
      {maxPeriods != null && (
        <p className="small">
          当前主机每次允许最多 {maxPeriods} {quarterly ? "个季度" : "个月"}。
        </p>
      )}
      {!enabled && availability.data && (
        <p role="status">
          当前未开放 {profile.label} 采集：
          {availability.data.web_capabilities.reason ?? "资格未确认"}。
        </p>
      )}
      <div className="workbench cpi-collection-workbench">
        <div className="collection-input-pane">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (e.currentTarget.checkValidity()) void submit();
            }}
          >
            <CollectionFormLock locked={busy || !!intent}>
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
                <PeriodField
                  key={`${action}-start`}
                  label={`采集开始${periodLabel}`}
                  value={start}
                  onChange={(value) => {
                    edited.current[action] = true;
                    setStart(value);
                  }}
                />
                <PeriodField
                  key={`${action}-end`}
                  label={`采集结束${periodLabel}`}
                  value={end}
                  onChange={(value) => {
                    edited.current[action] = true;
                    setEnd(value);
                  }}
                />
              </div>
              {initializing && (
                <p>
                  初始化服务预置范围：
                  {(preset &&
                    ("years" in preset
                      ? preset.years
                      : "quarters" in preset
                        ? preset.quarters
                        : preset.months)) ||
                    "待核实"}
                  {weekly
                    ? "日历年（来源含28天缓冲）"
                    : quarterly
                      ? "个季度"
                      : "个月"}
                  ，可自行修改
                </p>
              )}
              {initializing && preset?.available && (
                <>
                  <p>
                    服务预置{periodLabel}：{preset.start} 至 {preset.end}
                  </p>
                  <p className="small">
                    范围依据：
                    {weekly
                      ? "20日历年，按后端自然日预置；来源额外读取28天计算缓冲"
                      : quarterly
                        ? "上一完整 UTC 季度；不代表已发布或来源已就绪"
                        : initializationAnchorText(preset.anchor_basis)}
                    。实际覆盖以采集回读为准。
                  </p>
                </>
              )}
              {initializing && !preset?.available && (
                <p role="status">
                  服务暂无默认范围（尚未确认可用锚点 ），请自行填写合法
                  {periodLabel}范围。
                </p>
              )}
              {start && end && !validRange && (
                <p role="alert">
                  起止{periodLabel}
                  {quarterly
                    ? "无效、尚未完整结束或超过服务允许的采集范围。"
                    : weekly
                      ? "无效、不含周六或超过服务允许的100年范围。"
                      : "无效或超过服务允许的采集范围。"}
                </p>
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
            </CollectionFormLock>
          </form>
          {storageFailed && (
            <p role="alert">本机请求记录无法核实，已阻止提交。</p>
          )}
        </div>
        <CollectionBriefs>
          {notice && <p role="status">{notice}</p>}
          {intent && !matched && !busy && (
            <button onClick={recallIntent}>核实原请求</button>
          )}
          <ol className="execution-brief-list">
            {intent && !briefJobs.some((job) => sameIntent(job, intent)) && (
              <li>
                <ExecutionBriefCard
                  id={intent.request_id}
                  title={`${macroAssetLabel(profile)} · ${cpiActionText(intent.action)}`}
                  createdAt=""
                  start={intent.start}
                  end={intent.end}
                  current
                  disabled
                  result={
                    busy
                      ? "正在提交，尚未确认任务创建。"
                      : "结果未知，保留请求并自动跟踪，请勿重复提交；尚未确认任务创建。"
                  }
                  onSelect={() => {}}
                />
              </li>
            )}
            {briefJobs.map((job) => (
              <li key={job.request_id}>
                <ExecutionBriefCard
                  id={job.request_id}
                  title={`${macroAssetLabel(profile)} · ${cpiActionText(job.action)}`}
                  current={!!intent && sameIntent(job, intent)}
                  createdAt={job.created_at}
                  start={job.start}
                  end={job.end}
                  onResume={
                    profile.resumePath &&
                    [
                      "failed",
                      "unknown",
                      ...(profile.id === "m3" ? ["waiting"] : []),
                    ].includes(job.state)
                      ? () => void resumeJob(job)
                      : undefined
                  }
                  resumeDisabled={busy || !enabled || jobs.stale}
                  onRefresh={recallIntent}
                  state={job.state}
                  canReopen={!unresolved && !jobs.stale}
                  reopenDisabled={busy || storageFailed}
                  onReopen={newIntent}
                  result={
                    <>
                      {cpiExecutionStateText(
                        job.state,
                        job.result,
                        profile,
                        job,
                      )}
                      {job.classification ? ` · ${job.classification}` : ""}
                      {weekly && <> · {claimsExecutionPeriod(job.result)}</>}
                    </>
                  }
                  onSelect={() => onExecution?.(job.job_id)}
                />
                {["unknown", "outcome_unknown"].includes(job.state) && (
                  <p role="status">
                    结果未知，保留请求并自动跟踪，请勿重复提交。
                  </p>
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
        </CollectionBriefs>
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
