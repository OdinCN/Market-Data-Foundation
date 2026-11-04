import { ClaimsCollectionDetails } from "./ClaimsCollectionDetails";
import { CodeBlock } from "./CpiQueryDetails";
import { quarterValid } from "./QuarterField";
import {
  GdpCollectionDetails,
  gdpExecutionState,
} from "./GdpCollectionDetails";
import { macroAssetLabel, gdpEstimateText } from "./macroTimeSeriesProfile";
import { MacroSourceCoverage, coverageEvidence } from "./MacroSourceCoverage";
import {
  CPI_PROFILE,
  profileScopeText,
  type MacroProfile,
} from "./macroTimeSeriesProfile";
import { useEffect, useRef, useState } from "react";
import type { Transport } from "./api";
import { formatBeijingTime } from "./beijingTime";
import { releaseLink, releaseReasonTexts } from "./macroReleaseLinks";
import { Fact, WorkflowTable } from "./WorkflowTable";
import { useAutoCollection, autoCollectionState } from "./useAutoCollection";
import { StrategySwitch } from "./StrategySwitch";
import { keyOf } from "./workflowModel";
import { useVisiblePolling } from "./useVisiblePolling";
import {
  monthValid,
  cpiRequest,
  readCpiStrategy,
  readCpiExecutions,
  type CpiAvailability,
  type CpiExecution,
} from "./cpiTimeSeriesApi";

const states: Record<string, string> = {
  running: "执行中",
  complete: "已完成",
  succeeded: "已完成",
  failed: "失败",
  unknown: "结果未知",
  outcome_unknown: "结果未知",
  pending: "等待执行",
  awaiting_provider: "等待来源",
  calendar_removed: "日历已移除",
  dismissed: "已解除",
  blocked: "受阻",
  paused: "已暂停",
  enabled: "已启动",
  active: "已启动",
  not_created: "未启动",
  not_started: "尚未启动",
  idle: "空闲",
  healthy: "正常",
  fresh: "正常",
  stale: "已陈旧",
  missing: "未记录",
  stopped: "已停止",
  error: "异常",
  source_unavailable: "来源缺值",
  history_pending: "等待历史核实",
  unavailable: "不可用",
  disabled: "已暂停",
  waiting: "等待执行",
  cancelled: "已取消",
  pending_exhausted: "重试次数已耗尽",
};
export const cpiStateText = (value?: string | null) =>
  value ? (states[value] ?? "尚未核实") : "未记录";
export const workerStateText = (value?: string | null) =>
  value && ["idle", "running", "paused"].includes(value)
    ? `在线（${cpiStateText(value)}）`
    : value === "stale"
      ? "心跳已过期"
      : cpiStateText(value);
export function cpiExecutionStateText(
  state: string | undefined,
  result: Record<string, unknown> | null | undefined,
  _profile: MacroProfile = CPI_PROFILE,
  scope: { start?: string | null; end?: string | null } = {},
) {
  if (_profile.id === "m3" && state === "waiting") return "等待来源准入";
  if (_profile.id === "claims") {
    const data = (result?.result ?? result) as Record<string, unknown> | null;
    const c = data?.coverage as Record<string, number> | undefined;
    return ["complete", "succeeded"].includes(state ?? "") &&
      c &&
      c.available < c.expected
      ? "已完成（各系列存在缺周或提供方缺值）"
      : cpiStateText(state);
  }
  if (_profile.id === "gdp")
    return gdpExecutionState(state, result) ?? cpiStateText(state);
  const { data, missing, absent, nulls } = coverageEvidence(result);
  if (!["complete", "succeeded"].includes(state ?? ""))
    return cpiStateText(state);
  const rangeKnown =
    monthValid(scope.start) &&
    monthValid(scope.end) &&
    scope.start <= scope.end;
  const inScope = (p: { month: string }) =>
    !rangeKnown || (p.month >= scope.start! && p.month <= scope.end!);
  const nullKeys = new Set(nulls.map((p) => `${p.series_id}:${p.month}`));
  const sourceKeys = new Set(
    absent
      .filter(inScope)
      .filter((p) => !nullKeys.has(`${p.series_id}:${p.month}`))
      .map((p) => `${p.series_id}:${p.month}`),
  );
  if (
    missing
      .filter(inScope)
      .some((p) => !sourceKeys.has(`${p.series_id}:${p.month}`))
  )
    return "已完成（存在缺口）";
  // Explicit nulls count as covered; retain actual readback gaps as evidence.
  if (data.coverage_complete === true && !missing.some(inScope))
    return cpiStateText(state);
  if (sourceKeys.size > 0 || data.completion === "source_coverage_partial")
    return "已完成（来源有缺口）";
  if (data.coverage_complete === false) return "已完成（存在缺口）";
  return cpiStateText(state);
}

export const cpiActionText = (value: string) =>
  ({
    initialize: "初始化采集",
    init: "历史初始化",
    update: "更新指定范围",
    target: "当期采集",
    history: "历史核实",
    recovery: "恢复采集",
    withdraw: "撤回",
    signal: "修订信号核实",
  })[value.startsWith("signal:") ? "signal" : value] ?? "尚未核实";
type Stage = {
  state?: string;
  update_start?: string;
  update_end?: string;
  coverage_complete?: boolean;
  source_missing?: { series_id: string; month: string; reason?: string }[];
  next_attempt_at?: string | null;
  last_attempt_at?: string | null;
  completed_at?: string | null;
  classification?: string | null;
  result?: Record<string, unknown> | null;
};
type Schedule = {
  calendar_status: string;
  checked_at?: string;
  calendar_coverage?: string;
  calendar?: {
    error?: string | null;
    last_refresh_error?: string | null;
    last_refresh_attempt?: string | null;
    acquired_at?: string | null;
    fetched_at?: string | null;
    source?: string;
    primary_source?: string;
    refresh_mode?: "primary" | "fallback" | "cache" | "unavailable";
    next_refresh_at?: string | null;
    cache_valid_until?: string | null;
    primary_error?: string | null;
    refresh_attempts?: {
      source: string;
      outcome: string;
      classification?: string;
      http_status?: number | null;
    }[];
  };
  calendar_error?: string | null;
  calendar_refresh_error?: string | null;
  targets: (Stage & {
    reference_week?: string | null;
    reference_month?: string;
    dataset?: "m3" | "advm3";
    reference_quarter?: string;
    estimate_kind?: string;
    publication_status?: string;
    calendar_validation?: string | null;
    key?: string;
    calendar_execution_blocked?: boolean;
    release_at: string | null;
    target?: Stage;
    history?: Stage;
    auto?: Stage;
  })[];
};
function ResultFacts({
  result,
  profile,
}: {
  result: Record<string, unknown> | null | undefined;
  profile?: MacroProfile;
}) {
  if (!result) return <p>执行结果未记录</p>;
  // Some automatic receipts wrap the job result. Preserve statistics recorded
  // on either level; a missing statistic must never become a synthetic zero.
  const facts: Record<string, unknown> = { ...result };
  let nested = result.result;
  while (nested && typeof nested === "object" && !Array.isArray(nested)) {
    Object.assign(facts, nested);
    nested = (nested as Record<string, unknown>).result;
  }
  const labels: Record<string, string> = {
    saved_points: profile?.id === "m3" ? "本次执行新增观测" : "新增观测",
    saved_revisions: profile?.id === "m3" ? "本次执行新增修订" : "新增修订",
    provider_requests: profile?.id === "m3" ? "本次执行来源请求" : "来源请求",
    provider_attempts: profile?.id === "m3" ? "本次执行来源尝试" : "来源尝试",
    scheduler_attempts_total: "累计调度尝试",
    actual_http_total: "累计实际请求",
  };
  const entries = Object.entries(labels).filter(
    ([key]) => facts[key] !== undefined,
  );
  return entries.length ? (
    <dl>
      {entries.map(([key, label]) => (
        <Fact key={key} name={label}>
          {typeof facts[key] === "boolean"
            ? facts[key]
              ? "完整"
              : "存在缺口"
            : typeof facts[key] === "number"
              ? String(facts[key])
              : "未记录"}
        </Fact>
      ))}
    </dl>
  ) : null;
}
export function latestRealExecution(executions: readonly CpiExecution[]) {
  const time = (e: CpiExecution) =>
    e.started_at ?? (e.state !== "running" ? e.finished_at : null);
  return executions
    .filter(
      (e) =>
        [
          "running",
          "complete",
          "succeeded",
          "failed",
          "cancelled",
          "outcome_unknown",
          "unknown",
        ].includes(e.state) &&
        !!time(e) &&
        Number.isFinite(Date.parse(time(e)!)),
    )
    .slice()
    .sort(
      (a, b) =>
        Date.parse(time(b)!) - Date.parse(time(a)!) || a.id.localeCompare(b.id),
    )[0];
}

function RecentExecution({
  transport,
  profile,
  generation,
  visible,
}: {
  transport: Transport;
  profile: MacroProfile;
  generation: number;
  visible: boolean;
}) {
  const records = useVisiblePolling(
    (signal) => readCpiExecutions(transport, signal, profile),
    [transport, profile.id, generation],
    visible,
  );
  const execution = latestRealExecution(records.data?.executions ?? []);
  const automatic = latestRealExecution(
    (records.data?.executions ?? []).filter((e) => e.origin !== "manual"),
  );
  const href = execution
    ? `#${new URLSearchParams({
        ui: "r5",
        view: "collection",
        tab: "executions",
        asset: profile.asset.identity,
        asset_revision: profile.asset.revision,
        execution_asset: keyOf(profile.asset),
        execution_schedule: "all",
        job: execution.id,
      })}`
    : "";
  return (
    <section aria-label="最近真实执行摘要">
      <h4>最近真实执行摘要</h4>
      {records.stale || records.failure ? (
        <p role="alert">
          执行记录读取失败，最近结果暂未核实；保留的记录可能已陈旧。
        </p>
      ) : null}
      {!records.data ? (
        <p>
          {records.failure
            ? "执行记录暂不可读，无法确认最近真实执行。"
            : "正在读取真实执行记录…"}
        </p>
      ) : !execution ? (
        <p>无真实执行记录；心跳、计划与任务受理不代表已执行。</p>
      ) : (
        <>
          <p>
            本资产最近执行来源：
            {execution.origin === "manual" ? "手动采集" : "自动策略"}。
          </p>
          {execution.state === "running" && (
            <p>任务执行中，尚未确认完成；受理回执不代表采集成功。</p>
          )}
          {execution.origin === "manual" && !automatic && (
            <p>自动策略尚无真实执行记录；以下为手动采集结果。</p>
          )}
          <dl>
            <Fact name="采集方式">
              {cpiActionText(
                execution.origin === "manual"
                  ? execution.action
                  : (execution.stage ?? execution.action),
              )}
            </Fact>
            <Fact name="范围">
              {execution.start ?? "未记录"} 至 {execution.end ?? "未记录"}
            </Fact>
            <Fact name="开始时间（北京时间）">
              {formatBeijingTime(execution.started_at, "未记录")}
            </Fact>
            <Fact name="结束时间（北京时间）">
              {formatBeijingTime(execution.finished_at, "未记录")}
            </Fact>
            <Fact name="执行结果">
              {cpiExecutionStateText(
                execution.state,
                execution.result,
                profile,
                execution,
              )}
            </Fact>
            {execution.classification && (
              <Fact name="结果分类">{execution.classification}</Fact>
            )}
          </dl>
          <ResultFacts result={execution.result} />
          <a href={href}>打开对应执行详情</a>
        </>
      )}
    </section>
  );
}
export function CpiExecutionDetail({
  execution: e,
  profile = CPI_PROFILE,
}: {
  execution: CpiExecution;
  profile?: MacroProfile;
}) {
  const result =
    e.result?.result && typeof e.result.result === "object"
      ? (e.result.result as Record<string, unknown>)
      : e.result;
  const material = result?.release_material as
    | {
        state?: string;
        reasons?: string[];
        event_ids?: string[];
        evidence_ids?: string[];
      }
    | undefined;
  return (
    <>
      <h3>执行详情</h3>
      {profile.id === "m3" &&
        result?.reports &&
        typeof result.reports === "object" && (
          <section aria-label="分报告采集结果">
            <h4>两阶段采集结果</h4>
            {Object.entries(
              result.reports as Record<
                string,
                {
                  coverage?: { available: number; expected: number };
                  remaining_requests?: number;
                  source_failure?: unknown;
                  storage_failure?: unknown;
                }
              >,
            ).map(([d, r]) => (
              <p key={d}>
                {d === "m3" ? "制造业完整报告" : "耐用品先行报告（Advance）"}：
                {r.coverage
                  ? `${r.coverage.available} / ${r.coverage.expected} 有值`
                  : "执行中"}
                ，待请求 {r.remaining_requests ?? "未记录"}；
                {r.storage_failure
                  ? "存储结果未知"
                  : r.source_failure
                    ? "来源请求失败/等待"
                    : "已返回结果"}
              </p>
            ))}
          </section>
        )}

      {profile.id === "m3" && result && (
        <dl>
          <Fact name="已确认来源分片">
            {String(result.completed_requests ?? "未记录")}
          </Fact>
          <Fact name="待请求分片">
            {String(result.remaining_requests ?? "未记录")}
          </Fact>
          {!!result.source_failure &&
            typeof result.source_failure === "object" &&
            !!(result.source_failure as Record<string, unknown>).retry_at && (
              <Fact name="来源准入重试时间（北京时间）">
                {formatBeijingTime(
                  String(
                    (result.source_failure as Record<string, unknown>).retry_at,
                  ),
                )}
              </Fact>
            )}
          <Fact name="已确认请求点数">
            {String(result.requested_points ?? "未记录")}
          </Fact>
          {Array.isArray(result.pending) && (
            <Fact name="尚未确认请求的指标月">
              {String(result.pending.length)}
            </Fact>
          )}
          {!!result.source_failure && (
            <Fact name="来源状态">
              {typeof result.source_failure === "object"
                ? String(
                    (result.source_failure as Record<string, unknown>)
                      .classification ?? "来源请求失败",
                  )
                : "来源请求失败"}
            </Fact>
          )}
        </dl>
      )}
      {material && (
        <section aria-label="发布材料执行结果">
          <h4>发布材料</h4>
          <p>
            {material.state === "retained"
              ? "材料已留存；支持范围见事件详情"
              : "材料尚未完整确认；连续数值结果另列"}
          </p>
          {releaseReasonTexts(material.reasons).map((reason) => (
            <p key={reason}>{reason}</p>
          ))}
          {material.event_ids?.map((id, i) => (
            <p key={id}>
              <a href={releaseLink(profile, id, material.evidence_ids?.[i])}>
                打开发布事件与证据
              </a>
            </p>
          ))}
        </section>
      )}
      <dl>
        <Fact name="记录编号">{e.id}</Fact>
        <Fact name="关联资产">{macroAssetLabel(profile)}</Fact>
        <Fact name="来源">
          {e.origin !== "manual" ? "自动策略" : "手动采集"}
        </Fact>
        <Fact name="采集方式">
          {cpiActionText(
            e.origin !== "manual"
              ? e.action === "withdraw"
                ? "withdraw"
                : (e.stage ?? e.action)
              : e.action,
          )}
        </Fact>
        <Fact name="范围">
          {e.start ?? "未记录"} 至 {e.end ?? "未记录"}
        </Fact>
        <Fact name="创建时间（北京时间）">
          {formatBeijingTime(e.created_at, "未记录")}
        </Fact>
        <Fact name="开始时间（北京时间）">
          {formatBeijingTime(e.started_at, "未记录")}
        </Fact>
        <Fact name="结束时间（北京时间）">
          {formatBeijingTime(e.finished_at, "未记录")}
        </Fact>
        <Fact name="执行结果">
          {cpiExecutionStateText(e.state, e.result, profile, e)}
        </Fact>
        {e.classification && <Fact name="结果分类">{e.classification}</Fact>}
      </dl>
      {profile.id === "m3" && (
        <p className="small">
          以下新增和请求数仅属于本次执行；恢复时已确认的分片不会重复请求，范围覆盖包括之前确认的分片。
        </p>
      )}
      <ResultFacts result={e.result} profile={profile} />
      {profile.id === "claims" ? (
        <ClaimsCollectionDetails result={e.result} />
      ) : profile.id === "gdp" ? (
        <GdpCollectionDetails result={e.result} />
      ) : (
        <MacroSourceCoverage
          key={e.id}
          result={e.result}
          start={e.start}
          end={e.end}
        />
      )}
    </>
  );
}
export function CpiStrategyWorkbench(
  props: Parameters<typeof CpiStrategyWorkbenchContent>[0],
) {
  return (
    <CpiStrategyWorkbenchContent
      {...props}
      key={(props.profile ?? CPI_PROFILE).id}
    />
  );
}
function CpiStrategyWorkbenchContent({
  transport,
  profile: primaryProfile = CPI_PROFILE,
  availability: primaryAvailability,
  generation = 0,
  visible = true,
  additionalProfile,
  additionalAvailability,
  additionalVisible = true,
  thirdProfile,
  thirdAvailability,
  thirdVisible = true,
  fourthProfile,
  fourthAvailability,
  fourthVisible = true,
  fifthProfile,
  fifthAvailability,
  fifthVisible = true,
  sixthProfile,
  sixthAvailability,
  sixthVisible = true,
  tenthProfile,
  tenthAvailability,
  tenthVisible = true,
  ninthProfile,
  ninthAvailability,
  ninthVisible = true,
  eighthProfile,
  eighthAvailability,
  eighthVisible = true,
  seventhProfile,
  seventhAvailability,
  seventhVisible = true,
  selectedId,
  selected: selectedProp,
  onSelect,
}: {
  transport: Transport;
  profile?: MacroProfile;
  availability: CpiAvailability;
  generation?: number;
  visible?: boolean;
  selected?: boolean;
  onSelect?: (id: string) => void;
  additionalProfile?: MacroProfile;
  additionalAvailability?: CpiAvailability;
  additionalVisible?: boolean;
  tenthProfile?: MacroProfile;
  tenthAvailability?: CpiAvailability;
  tenthVisible?: boolean;
  ninthProfile?: MacroProfile;
  ninthAvailability?: CpiAvailability;
  ninthVisible?: boolean;
  eighthProfile?: MacroProfile;
  eighthAvailability?: CpiAvailability;
  eighthVisible?: boolean;
  seventhProfile?: MacroProfile;
  seventhAvailability?: CpiAvailability;
  seventhVisible?: boolean;
  sixthProfile?: MacroProfile;
  sixthAvailability?: CpiAvailability;
  sixthVisible?: boolean;
  fifthProfile?: MacroProfile;
  fifthAvailability?: CpiAvailability;
  fifthVisible?: boolean;
  fourthProfile?: MacroProfile;
  fourthAvailability?: CpiAvailability;
  fourthVisible?: boolean;
  thirdProfile?: MacroProfile;
  thirdAvailability?: CpiAvailability;
  thirdVisible?: boolean;
  selectedId?: string | null;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const emptyAvailability: CpiAvailability = {
    data: null,
    legacy: false,
    busy: false,
    failure: null,
  };
  const primary = useAutoCollection(
    transport,
    generation,
    primaryProfile,
    primaryAvailability,
    visible,
  );
  const secondary = useAutoCollection(
    transport,
    generation,
    additionalProfile ?? primaryProfile,
    additionalAvailability ?? emptyAvailability,
    !!additionalProfile && additionalVisible,
  );
  const third = useAutoCollection(
    transport,
    generation,
    thirdProfile ?? primaryProfile,
    thirdAvailability ?? emptyAvailability,
    !!thirdProfile && thirdVisible,
  );
  const fourth = useAutoCollection(
    transport,
    generation,
    fourthProfile ?? primaryProfile,
    fourthAvailability ?? emptyAvailability,
    !!fourthProfile && fourthVisible,
  );
  const fifth = useAutoCollection(
    transport,
    generation,
    fifthProfile ?? primaryProfile,
    fifthAvailability ?? emptyAvailability,
    !!fifthProfile && fifthVisible,
  );
  const sixth = useAutoCollection(
    transport,
    generation,
    sixthProfile ?? primaryProfile,
    sixthAvailability ?? emptyAvailability,
    !!sixthProfile && sixthVisible,
  );
  const seventh = useAutoCollection(
    transport,
    generation,
    seventhProfile ?? primaryProfile,
    seventhAvailability ?? emptyAvailability,
    !!seventhProfile && seventhVisible,
  );
  const tenth = useAutoCollection(
    transport,
    generation,
    tenthProfile ?? primaryProfile,
    tenthAvailability ?? emptyAvailability,
    !!tenthProfile && tenthVisible,
  );
  const ninth = useAutoCollection(
    transport,
    generation,
    ninthProfile ?? primaryProfile,
    ninthAvailability ?? emptyAvailability,
    !!ninthProfile && ninthVisible,
  );
  const eighth = useAutoCollection(
    transport,
    generation,
    eighthProfile ?? primaryProfile,
    eighthAvailability ?? emptyAvailability,
    !!eighthProfile && eighthVisible,
  );
  const entries = [
    ...(tenthProfile
      ? [
          {
            profile: tenthProfile,
            availability: tenthAvailability ?? emptyAvailability,
            visible: tenthVisible,
            ...tenth,
          },
        ]
      : []),
    ...(ninthProfile
      ? [
          {
            profile: ninthProfile,
            availability: ninthAvailability ?? emptyAvailability,
            visible: ninthVisible,
            ...ninth,
          },
        ]
      : []),
    ...(eighthProfile
      ? [
          {
            profile: eighthProfile,
            availability: eighthAvailability ?? emptyAvailability,
            visible: eighthVisible,
            ...eighth,
          },
        ]
      : []),
    ...(seventhProfile
      ? [
          {
            profile: seventhProfile,
            availability: seventhAvailability ?? emptyAvailability,
            visible: seventhVisible,
            ...seventh,
          },
        ]
      : []),
    ...(sixthProfile
      ? [
          {
            profile: sixthProfile,
            availability: sixthAvailability ?? emptyAvailability,
            visible: sixthVisible,
            ...sixth,
          },
        ]
      : []),
    ...(fifthProfile
      ? [
          {
            profile: fifthProfile,
            availability: fifthAvailability ?? emptyAvailability,
            visible: fifthVisible,
            ...fifth,
          },
        ]
      : []),
    ...(fourthProfile
      ? [
          {
            profile: fourthProfile,
            availability: fourthAvailability ?? emptyAvailability,
            visible: fourthVisible,
            ...fourth,
          },
        ]
      : []),
    {
      profile: primaryProfile,
      availability: primaryAvailability,
      visible,
      ...primary,
    },
    ...(additionalProfile
      ? [
          {
            profile: additionalProfile,
            availability: additionalAvailability ?? emptyAvailability,
            visible: additionalVisible,
            ...secondary,
          },
        ]
      : []),
    ...(thirdProfile
      ? [
          {
            profile: thirdProfile,
            availability: thirdAvailability ?? emptyAvailability,
            visible: thirdVisible,
            ...third,
          },
        ]
      : []),
  ];
  const selection =
    selectedId ?? (selectedProp ? `${primaryProfile.id}-auto` : selected);
  const entry =
    entries.find((e) => `${e.profile.id}-auto` === selection) ?? entries[0];
  const { profile, data, resource, uncertain } = entry;
  const detailVisible = entry.visible;
  const calendar = useVisiblePolling(
    async (signal) => {
      const value = await cpiRequest<Schedule>(
        transport,
        "/schedule",
        {
          signal,
        },
        20000,
        profile,
      );
      if (
        !value ||
        typeof value.calendar_status !== "string" ||
        !Array.isArray(value.targets)
      )
        throw new Error("schedule_unknown");
      if (
        profile.id === "gdp" &&
        !value.targets.every(
          (t) =>
            quarterValid(t.reference_quarter) &&
            typeof t.estimate_kind === "string" &&
            typeof t.key === "string",
        )
      )
        throw new Error("gdp_schedule_unknown");
      return value;
    },
    [transport, generation, profile.id],
    !!entry.availability.data && detailVisible,
  );
  const weekly = profile.id === "claims";
  const quarterly = profile.id === "gdp";
  const periodLabel = weekly ? "周频" : quarterly ? "季度" : "月份";
  const planPeriod = (target: Schedule["targets"][number]) =>
    weekly
      ? `${target.release_at?.slice(0, 10) ?? "计划日期未知"} · ${target.reference_week ?? "参考周待发布材料确认"}`
      : quarterly
        ? target.reference_quarter
        : profile.id === "m3"
          ? `${target.reference_month} · ${target.dataset === "advm3" ? "耐用品先行报告（Advance）" : "制造业完整报告"}`
          : target.reference_month;
  const shown = !!selection;
  const currentCalendarError =
    calendar.data?.calendar?.error ?? calendar.data?.calendar_error;
  const refresh = calendar.data?.calendar;
  const calendarUsable =
    calendar.data?.calendar_status === "fresh" ||
    (calendar.data?.calendar_status === "degraded_cached_calendar" &&
      refresh?.refresh_mode === "cache" &&
      !!refresh.cache_valid_until);
  const calendarTime = (value?: string | null) =>
    formatBeijingTime(value, "未记录").replace(" 北京时间", "");
  return (
    <div className="periodic-layout">
      {(resource.stale || calendar.stale) && (
        <p role="alert">状态已陈旧，保留上次数据并自动重读。</p>
      )}
      {uncertain && <p role="alert">自动采集操作结果未知，正在独立核实。</p>}
      <WorkflowTable
        label="周期任务"
        className="periodic-workbench"
        rightDetails
        columns={["采集目标", "自动采集与运行情况", "数据更新"]}
        tools={
          <button
            onClick={() => {
              entries.forEach((e) => e.resource.refresh());
              calendar.refresh();
            }}
          >
            刷新页面
          </button>
        }
        selected={selection}
        onSelect={(id) => {
          setSelected(id);
          onSelect?.(id);
        }}
        empty={
          visible
            ? `正在读取 ${profile.label} 策略…`
            : "此资产暂无可执行的预置策略。"
        }
        rows={entries
          .filter((e) => e.visible)
          .map((e) => ({
            id: `${e.profile.id}-auto`,
            search: `${macroAssetLabel(e.profile)} 官方发布自动采集`,
            cells: [
              <span className="strategy-target">
                {macroAssetLabel(e.profile)}
              </span>,
              <span key="toggle" className="strategy-control">
                <span className="auto-control-line">
                  <span className="small">自动采集</span>
                  <StrategySwitch
                    asset={macroAssetLabel(e.profile)}
                    enabled={e.data?.enabled ?? null}
                    saving={e.saving}
                    uncertain={e.uncertain}
                    disabled={
                      !e.data ||
                      e.saving ||
                      e.uncertain ||
                      e.resource.stale ||
                      (!e.data.enabled &&
                        (!e.availability.data?.web_capabilities.collect ||
                          (e.profile.id === "gdp" && !!e.availability.failure)))
                    }
                    onToggle={() => void e.toggle()}
                  />
                </span>
                <span className="auto-state">
                  运行情况：{autoCollectionState(e)}
                </span>
                {e.data?.enabled &&
                  e.data.control &&
                  !e.data.control.online &&
                  !e.uncertain && (
                    <button
                      className="auto-recover"
                      disabled={e.saving || e.resource.stale}
                      onClick={(event) => {
                        event.stopPropagation();
                        void e.recover();
                      }}
                    >
                      恢复自动采集
                    </button>
                  )}
                {e.notice && (
                  <span className="strategy-control-note" role="status">
                    {e.notice}
                  </span>
                )}
                {e.data?.enabled &&
                  (!e.data.control?.online ||
                    autoCollectionState(e) === "需处理") && (
                    <span className="strategy-control-note">{e.reason}</span>
                  )}
                {!e.availability.data?.web_capabilities.collect && (
                  <span className="strategy-control-note">
                    来源未就绪：
                    {e.availability.data?.web_capabilities.reason ??
                      "请检查来源配置与凭据"}
                  </span>
                )}
              </span>,
              <span className="strategy-data">
                <span>
                  最近成功采集（北京时间）：
                  <br />
                  {formatBeijingTime(
                    e.data?.last_successful_collection_at,
                    "未记录",
                  ).replace(" 北京时间", "")}
                </span>
                <span>最新数据期：{e.data?.latest_data_period ?? "未知"}</span>
              </span>,
            ],
          }))}
      >
        {shown && detailVisible && !data && (
          <p role="status">
            {profile.label} 策略
            {resource.failure || entry.availability.failure
              ? "读取失败，等待重读。"
              : "正在读取…"}
          </p>
        )}
        {shown && detailVisible && data && (
          <>
            <h3>{profile.label} 策略详情</h3>
            <p className="auto-summary">
              自动采集{data.enabled ? "已开启" : "已暂停"} ·{" "}
              {autoCollectionState(entry)}
            </p>
            {data.enabled && !data.control?.online && (
              <p role="status">{entry.reason}；请使用任务行的恢复自动采集。</p>
            )}
            <p className="small">
              暂停只停止后续自动触发；已在采集的任务正常收尾，手动采集不受影响。最新数据期不代表所有序列齐全。
            </p>
            <details>
              <summary>口径与采集说明</summary>
              <p>{profileScopeText(profile)}</p>
              {profile.historyNotice && <p>{profile.historyNotice}</p>}
            </details>
            <dl>
              <Fact name="最近成功采集（北京时间）">
                {formatBeijingTime(
                  data.last_successful_collection_at,
                  "未记录",
                )}
              </Fact>
              <Fact name="最新数据期">{data.latest_data_period ?? "未知"}</Fact>
              <Fact name="下次官方日历时间（北京时间）">
                {formatBeijingTime(data.next_release_at, "未记录")}
              </Fact>
            </dl>
            <details className="technical">
              <summary>技术详情与日历状态</summary>
              <dl>
                <Fact name="策略启用意图">
                  {data.enabled ? "已启用" : "已暂停"}
                </Fact>
                <Fact name="服务">
                  {data.control?.service_state ?? "待核实"}
                </Fact>
                <Fact name="PID">{data.control?.process_id ?? "未运行"}</Fact>
                <Fact name="实际进程状态">
                  {workerStateText(data.worker.state)}
                </Fact>
                <Fact name="心跳（北京时间）">
                  {formatBeijingTime(data.worker.last_heartbeat_at, "未记录")}
                </Fact>
                <Fact name="DEV绝对截止">
                  {formatBeijingTime(data.control?.expires_at, "未核实")}
                </Fact>
                <Fact name="后台维护时间（非官方日历时间）">
                  {formatBeijingTime(data.next_due, "未记录")}
                </Fact>
                <Fact name="日历状态">
                  {calendarUsable
                    ? refresh?.refresh_mode === "fallback"
                      ? "官方备用日历可用"
                      : refresh?.refresh_mode === "cache"
                        ? "使用有效缓存"
                        : "日历可用"
                    : ((
                        {
                          fresh: "日历可用",
                          stale: "日历已陈旧，尚未确认可用",
                          expired: "日历已过期，不可用",
                          missing: "尚无日历",
                          unavailable: "日历不可用",
                          degraded_cached_calendar:
                            "日历来源异常，缓存有效性尚未确认",
                          clock_reversal: "时间异常",
                        } as Record<string, string>
                      )[calendar.data?.calendar_status ?? ""] ?? "尚未核实")}
                </Fact>
                <Fact name="日历最后成功获取时间（北京时间）">
                  {formatBeijingTime(
                    calendar.data?.calendar?.acquired_at ??
                      calendar.data?.calendar?.fetched_at,
                    "未记录",
                  )}
                </Fact>
              </dl>
              <dl>
                <Fact name="当前日历入口">
                  {refresh?.source || refresh?.primary_source ? (
                    <a
                      href={refresh.source ?? refresh.primary_source}
                      target="_blank"
                      rel="noreferrer"
                      className="calendar-source-link"
                    >
                      {refresh.source ?? refresh.primary_source}
                    </a>
                  ) : (
                    "未记录"
                  )}
                </Fact>
                <Fact name="日历缓存有效期至（北京时间）">
                  {formatBeijingTime(refresh?.cache_valid_until, "未记录")}
                </Fact>
                <Fact name="下次日历刷新（北京时间）">
                  {formatBeijingTime(refresh?.next_refresh_at, "未记录")}
                </Fact>
              </dl>
            </details>
            <details>
              <summary>各序列覆盖与缺口</summary>
              <p className="small">
                仅列已取得的各序列最新期；缺少的序列或期间不能据此推定完整，分期覆盖见下方结果。
              </p>
              <dl>
                {" "}
                <Fact name="各序列最新参考期">
                  {Object.keys(data.latest_data_periods ?? {}).length ? (
                    <ul className="strategy-periods">
                      {Object.entries(data.latest_data_periods ?? {}).map(
                        ([id, period]) => (
                          <li key={id}>
                            <span>{id}</span>
                            <span>{period ?? "未知"}</span>
                          </li>
                        ),
                      )}
                    </ul>
                  ) : (
                    "未知"
                  )}
                </Fact>
                {data.collection_facts_classification && (
                  <Fact name="采集状态读取">
                    {data.collection_facts_classification}
                  </Fact>
                )}
              </dl>
            </details>
            {!data.enabled && <p>策略已暂停。</p>}
            {refresh?.refresh_mode === "fallback" ? (
              <p role="status">
                主日历入口暂不可用，已切换到同一机构的官方备用入口。存在冲突或未覆盖的
                {periodLabel}暂停自动采集，等待主入口确认。
              </p>
            ) : calendar.data?.calendar_status === "degraded_cached_calendar" &&
              !calendarUsable ? (
              <p role="status">
                本次日历刷新失败，保留已缓存计划，等待刷新确认。
              </p>
            ) : currentCalendarError ? (
              <p role="status">
                最近刷新失败，
                {calendarUsable
                  ? "继续使用有效缓存。"
                  : "无可用的有效日历，暂停新的日历触发采集，等待刷新确认。"}
              </p>
            ) : null}
            {refresh?.refresh_attempts?.some((a) => a.outcome === "failed") && (
              <details>
                <summary>日历刷新详情</summary>
                <ul>
                  {refresh.refresh_attempts.map((a, i) => (
                    <li key={i} style={{ overflowWrap: "anywhere" }}>
                      {a.source}：
                      {a.outcome === "accepted"
                        ? "已验证"
                        : `失败${a.http_status ? `（HTTP ${a.http_status}）` : ""}`}{" "}
                      {a.classification ?? ""}
                    </li>
                  ))}
                </ul>
              </details>
            )}
            {currentCalendarError && !refresh?.refresh_attempts?.length && (
              <details>
                <summary>日历刷新详情</summary>
                <p>{currentCalendarError}</p>
              </details>
            )}
            <RecentExecution
              transport={transport}
              profile={profile}
              generation={generation}
              visible={detailVisible && !!entry.availability.data}
            />
            <details>
              <summary>策略上次执行回执与审计</summary>
              {data.last_result && (
                <dl>
                  <Fact name="最近执行状态">
                    {quarterly
                      ? cpiExecutionStateText(
                          typeof data.last_result.state === "string"
                            ? data.last_result.state
                            : undefined,
                          data.last_result,
                          profile,
                        )
                      : cpiStateText(
                          typeof data.last_result.state === "string"
                            ? data.last_result.state
                            : null,
                        )}
                  </Fact>
                  <Fact name="最近执行阶段">
                    {cpiActionText(
                      typeof data.last_result.stage === "string"
                        ? data.last_result.stage
                        : typeof data.last_result.action === "string"
                          ? data.last_result.action
                          : "",
                    )}
                  </Fact>
                  <Fact name="最近开始时间（北京时间）">
                    {formatBeijingTime(
                      typeof data.last_result.started_at === "string"
                        ? data.last_result.started_at
                        : null,
                      "未记录",
                    )}
                  </Fact>
                  {typeof data.last_result.classification === "string" && (
                    <Fact name="最近结果分类">
                      {data.last_result.classification}
                    </Fact>
                  )}
                </dl>
              )}
              <ResultFacts result={data.last_result} />
              {weekly ? (
                <ClaimsCollectionDetails result={data.last_result} />
              ) : quarterly ? (
                <GdpCollectionDetails result={data.last_result} />
              ) : (
                <MacroSourceCoverage result={data.last_result} />
              )}
            </details>
            <h4>{periodLabel}计划与执行结果</h4>
            <p className="small">
              时间基准：北京时间（UTC+08:00）。计划时间不代表已经执行。
            </p>
            <div
              className="month-plan-list"
              role="list"
              aria-label={`${profile.label} ${periodLabel}计划`}
            >
              {[...(calendar.data?.targets ?? [])]
                .sort((a, b) =>
                  quarterly
                    ? (b.reference_quarter ?? "").localeCompare(
                        a.reference_quarter ?? "",
                      ) ||
                      (a.release_at ?? "").localeCompare(b.release_at ?? "")
                    : 0,
                )
                .map((target) => (
                  <article
                    className="month-plan-card"
                    role="listitem"
                    key={
                      quarterly || weekly || profile.id === "m3"
                        ? target.key
                        : target.reference_month
                    }
                  >
                    <header>
                      <strong>
                        {planPeriod(target)}
                        {quarterly
                          ? ` · ${gdpEstimateText(target.estimate_kind ?? "")}`
                          : ""}
                      </strong>
                      <span>
                        官方日历时间{" "}
                        <time>{calendarTime(target.release_at)}</time>
                      </span>
                    </header>
                    {target.calendar_execution_blocked && (
                      <p role="status">
                        此{periodLabel}的日历信息
                        {quarterly
                          ? "已取消、有冲突或尚未重新确认"
                          : "有冲突或尚未重新确认"}
                        ，暂停新的自动采集。以下保留原计划与执行记录。
                      </p>
                    )}
                    {(quarterly || weekly) && (
                      <p>
                        {target.publication_status === "cancelled"
                          ? "计划已取消"
                          : "官方发布计划"}{" "}
                        {target.calendar_validation ?? ""}
                      </p>
                    )}
                    <div className="month-stage-grid">
                      <div className="month-stage-state">
                        <strong>当期采集</strong>
                        <span>
                          {quarterly
                            ? cpiExecutionStateText(
                                target.target?.state ?? target.state,
                                target.target?.result,
                                profile,
                              )
                            : cpiStateText(
                                target.target?.state ?? target.state,
                              )}
                        </span>
                      </div>
                      <div className="month-stage-detail">
                        <p>
                          最近尝试：
                          <time>
                            {calendarTime(
                              target.auto?.last_attempt_at ??
                                target.target?.last_attempt_at ??
                                target.last_attempt_at,
                            )}
                          </time>
                        </p>
                        <p>
                          下次尝试：
                          <time>
                            {calendarTime(
                              target.auto?.next_attempt_at ??
                                target.target?.next_attempt_at ??
                                target.next_attempt_at,
                            )}
                          </time>
                        </p>
                        <ResultFacts
                          result={target.target?.result ?? target.result}
                        />
                        {weekly ? (
                          <ClaimsCollectionDetails
                            result={target.target?.result ?? target.result}
                          />
                        ) : quarterly ? (
                          <GdpCollectionDetails
                            result={target.target?.result ?? target.result}
                          />
                        ) : (
                          <MacroSourceCoverage
                            result={target.target?.result ?? target.result}
                            start={target.reference_month}
                            end={target.reference_month}
                          />
                        )}
                        {(target.auto?.classification ??
                          target.target?.classification) && (
                          <p>
                            {target.auto?.classification ??
                              target.target?.classification}
                          </p>
                        )}
                      </div>
                      <details className="stage-history">
                        <summary>历史核实与结果</summary>
                        <div className="month-stage-state">
                          <strong>历史核实</strong>
                          <span>
                            {target.history?.state === "blocked" &&
                            [
                              "pending",
                              "running",
                              "awaiting_provider",
                              "unknown",
                              "failed",
                            ].includes(target.target?.state ?? "")
                              ? "等待当期采集"
                              : cpiExecutionStateText(
                                  target.history?.state,
                                  target.history,
                                  profile,
                                  {
                                    start: target.history?.update_start,
                                    end: target.history?.update_end,
                                  },
                                )}
                          </span>
                        </div>
                        <div className="month-stage-detail">
                          <p>
                            最近尝试：
                            <time>
                              {calendarTime(target.history?.last_attempt_at)}
                            </time>
                          </p>
                          <p>
                            下次核实：
                            <time>
                              {calendarTime(target.history?.next_attempt_at)}
                            </time>
                          </p>
                          <ResultFacts
                            result={
                              quarterly
                                ? target.history?.result
                                : (target.history?.result ?? target.history)
                            }
                          />
                          {weekly ? (
                            <ClaimsCollectionDetails
                              result={target.history?.result}
                            />
                          ) : quarterly ? (
                            <GdpCollectionDetails
                              result={target.history?.result}
                            />
                          ) : (
                            <MacroSourceCoverage
                              result={target.history}
                              start={target.history?.update_start}
                              end={target.history?.update_end}
                            />
                          )}
                          {target.history?.classification && (
                            <p>{target.history.classification}</p>
                          )}
                        </div>
                      </details>
                    </div>
                  </article>
                ))}
              {!calendar.data?.targets.length && (
                <p>
                  {calendar.failure
                    ? `${periodLabel}计划读取失败，当前计划尚未核实。`
                    : `暂无${periodLabel}计划。`}
                </p>
              )}
            </div>
          </>
        )}
      </WorkflowTable>
    </div>
  );
}
