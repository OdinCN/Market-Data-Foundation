import { MacroSourceCoverage, coverageEvidence } from "./MacroSourceCoverage";
import {
  CPI_PROFILE,
  profileScopeText,
  type MacroProfile,
} from "./macroTimeSeriesProfile";
import { useEffect, useRef, useState } from "react";
import type { Transport } from "./api";
import { formatBeijingTime } from "./beijingTime";
import { Fact, WorkflowTable } from "./WorkflowTable";
import { useVisiblePolling } from "./useVisiblePolling";
import {
  monthValid,
  cpiRequest,
  readCpiStrategy,
  saveCpiStrategy,
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
};
export const cpiStateText = (value?: string | null) =>
  value ? (states[value] ?? "尚未核实") : "未记录";
export function cpiExecutionStateText(
  state: string | undefined,
  result: Record<string, unknown> | null | undefined,
  _profile: MacroProfile = CPI_PROFILE,
  scope: { start?: string | null; end?: string | null } = {},
) {
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
  };
  calendar_error?: string | null;
  calendar_refresh_error?: string | null;
  targets: (Stage & {
    reference_month: string;
    release_at: string | null;
    target?: Stage;
    history?: Stage;
    auto?: Stage;
  })[];
};
function ResultFacts({
  result,
}: {
  result: Record<string, unknown> | null | undefined;
}) {
  if (!result) return <p>执行结果未记录</p>;
  if (
    result.result &&
    typeof result.result === "object" &&
    !Array.isArray(result.result)
  )
    return <ResultFacts result={result.result as Record<string, unknown>} />;
  const labels: Record<string, string> = {
    saved_points: "新增观测",
    saved_revisions: "新增修订",
    provider_requests: "来源请求",
    provider_attempts: "来源尝试",
    scheduler_attempts_total: "累计调度尝试",
    actual_http_total: "累计实际请求",
  };
  const entries = Object.entries(labels).filter(
    ([key]) => result[key] !== undefined,
  );
  return entries.length ? (
    <dl>
      {entries.map(([key, label]) => (
        <Fact key={key} name={label}>
          {typeof result[key] === "boolean"
            ? result[key]
              ? "完整"
              : "存在缺口"
            : typeof result[key] === "number"
              ? String(result[key])
              : "未记录"}
        </Fact>
      ))}
    </dl>
  ) : null;
}
export function CpiExecutionDetail({
  execution: e,
  profile = CPI_PROFILE,
}: {
  execution: CpiExecution;
  profile?: MacroProfile;
}) {
  return (
    <>
      <h3>执行详情</h3>
      <dl>
        <Fact name="记录编号">{e.id}</Fact>
        <Fact name="关联资产">美国 {profile.label}</Fact>
        <Fact name="来源">
          {e.origin === "automatic"
            ? "自动策略 · 最近真实执行摘要"
            : "手动采集"}
        </Fact>
        <Fact name="采集方式">
          {cpiActionText(
            e.origin === "automatic"
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
      <ResultFacts result={e.result} />
      <MacroSourceCoverage
        key={e.id}
        result={e.result}
        start={e.start}
        end={e.end}
      />
    </>
  );
}
function useStrategyControl(
  transport: Transport,
  generation: number,
  profile: MacroProfile,
  availability: CpiAvailability,
  visible: boolean,
) {
  const [saving, setSaving] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const writing = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  // No mutation AbortController: navigation or a read refresh cannot cancel a PUT.
  const resource = useVisiblePolling(
    async (signal) => {
      const strategy = await readCpiStrategy(transport, signal, profile);
      return strategy;
    },
    [transport, generation, profile.id],
    !!availability.data && visible,
    () => !writing.current,
  );
  const data = resource.data;
  async function toggle() {
    if (
      !data ||
      writing.current ||
      uncertain ||
      resource.busy ||
      resource.stale ||
      (!data.enabled && !availability.data?.web_capabilities.collect)
    )
      return;
    writing.current = true;
    resource.invalidate();
    setSaving(true);
    try {
      const value = await saveCpiStrategy(
        transport,
        !data.enabled,
        undefined,
        profile,
      );
      if (mounted.current) resource.commit(value);
    } catch {
      if (mounted.current) setUncertain(true);
    } finally {
      writing.current = false;
      if (mounted.current) setSaving(false);
      resource.refresh();
    }
  }
  // A successful independent read resolves an uncertain PUT, without repeating it.
  useEffect(() => {
    if (data) setUncertain(false);
  }, [data]);
  return { resource, data, saving, uncertain, toggle };
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
  const primary = useStrategyControl(
    transport,
    generation,
    primaryProfile,
    primaryAvailability,
    visible,
  );
  const secondary = useStrategyControl(
    transport,
    generation,
    additionalProfile ?? primaryProfile,
    additionalAvailability ?? emptyAvailability,
    !!additionalProfile && additionalVisible,
  );
  const third = useStrategyControl(
    transport,
    generation,
    thirdProfile ?? primaryProfile,
    thirdAvailability ?? emptyAvailability,
    !!thirdProfile && thirdVisible,
  );
  const entries = [
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
      return value;
    },
    [transport, generation, profile.id],
    !!entry.availability.data && detailVisible,
  );
  const shown = !!selection;
  const currentCalendarError =
    calendar.data?.calendar?.error ?? calendar.data?.calendar_error;
  const calendarUsable = calendar.data?.calendar_status === "fresh";
  const calendarTime = (value?: string | null) =>
    formatBeijingTime(value, "未记录").replace(" 北京时间", "");
  return (
    <>
      {(resource.stale || calendar.stale) && (
        <p role="alert">状态已陈旧，保留上次数据并自动重读。</p>
      )}
      {uncertain && <p role="alert">策略保存结果未知，正在自动核实。</p>}
      <WorkflowTable
        label="周期任务"
        columns={["策略", "采集目标", "策略状态", "操作"]}
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
            search: `美国 ${e.profile.label} 官方发布自动采集`,
            cells: [
              "官方发布自动采集",
              `美国 ${e.profile.label}`,
              e.data
                ? e.data.exists
                  ? cpiStateText(
                      e.data.state ?? (e.data.enabled ? "enabled" : "paused"),
                    )
                  : "未启动"
                : "尚未核实",
              <button
                key="toggle"
                disabled={
                  !e.data ||
                  e.saving ||
                  e.uncertain ||
                  e.resource.busy ||
                  e.resource.stale ||
                  (!e.data.enabled &&
                    !e.availability.data?.web_capabilities.collect)
                }
                onClick={(event) => {
                  event.stopPropagation();
                  void e.toggle();
                }}
              >
                {e.saving ? "保存中…" : e.data?.enabled ? "暂停" : "启动"}
              </button>,
            ],
          }))}
      >
        {shown && detailVisible && data && (
          <>
            <h3>{profile.label} 策略详情</h3>
            <p>{profileScopeText(profile)}</p>
            {profile.historyNotice && <p>{profile.historyNotice}</p>}
            <dl>
              <Fact name="策略状态">
                {data.exists
                  ? cpiStateText(
                      data.state ?? (data.enabled ? "enabled" : "paused"),
                    )
                  : "未启动"}
              </Fact>
              <Fact name="运行状态">{cpiStateText(data.execution_state)}</Fact>
              <Fact name="后台进程">{cpiStateText(data.worker.state)}</Fact>
              <Fact name="最近心跳（北京时间）">
                {formatBeijingTime(data.worker.last_heartbeat_at, "未记录")}
              </Fact>
              <Fact name="下次官方日历时间（北京时间）">
                {formatBeijingTime(data.next_release_at, "未记录")}
              </Fact>
              <Fact name="后台维护时间（非官方日历时间）">
                {formatBeijingTime(data.next_due, "未记录")}
              </Fact>
              <Fact name="日历状态">
                {calendarUsable
                  ? "日历可用"
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
            {!data.enabled && <p>策略已暂停。</p>}
            {calendar.data?.calendar_status === "degraded_cached_calendar" ? (
              <p role="status">
                本次日历刷新失败，保留已缓存计划，等待刷新确认。
              </p>
            ) : currentCalendarError ? (
              <p role="status">
                最近刷新失败
                {currentCalendarError === "calendar_http_403"
                  ? "（HTTP 403）"
                  : `（${currentCalendarError}）`}
                ，
                {calendarUsable
                  ? "继续使用有效缓存。"
                  : ["unavailable", "missing", "expired", "stale"].includes(
                        calendar.data?.calendar_status ?? "",
                      )
                    ? "无可用的有效日历，等待刷新确认。"
                    : "保留已缓存计划，等待刷新确认。"}
              </p>
            ) : null}
            <h4>最近真实执行摘要</h4>
            {data.last_result && (
              <dl>
                <Fact name="最近执行状态">
                  {cpiStateText(
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
            <MacroSourceCoverage result={data.last_result} />
            <h4>月份计划与执行结果</h4>
            <p className="small">
              时间基准：北京时间（UTC+08:00）。计划时间不代表已经执行。
            </p>
            <div
              className="month-plan-list"
              role="list"
              aria-label={`${profile.label} 月份计划`}
            >
              {(calendar.data?.targets ?? []).map((target) => (
                <article
                  className="month-plan-card"
                  role="listitem"
                  key={target.reference_month}
                >
                  <header>
                    <strong>{target.reference_month}</strong>
                    <span>
                      官方日历时间{" "}
                      <time>{calendarTime(target.release_at)}</time>
                    </span>
                  </header>
                  <div className="month-stage-grid">
                    <div className="month-stage-state">
                      <strong>当期采集</strong>
                      <span>
                        {cpiStateText(target.target?.state ?? target.state)}
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
                      <MacroSourceCoverage
                        result={target.target?.result ?? target.result}
                        start={target.reference_month}
                        end={target.reference_month}
                      />
                      {(target.auto?.classification ??
                        target.target?.classification) && (
                        <p>
                          {target.auto?.classification ??
                            target.target?.classification}
                        </p>
                      )}
                    </div>
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
                        result={target.history?.result ?? target.history}
                      />
                      <MacroSourceCoverage
                        result={target.history}
                        start={target.history?.update_start}
                        end={target.history?.update_end}
                      />
                      {target.history?.classification && (
                        <p>{target.history.classification}</p>
                      )}
                    </div>
                  </div>
                </article>
              ))}
              {!calendar.data?.targets.length && (
                <p>
                  {calendar.failure
                    ? "月份计划读取失败，当前计划尚未核实。"
                    : "暂无月份计划。"}
                </p>
              )}
            </div>
          </>
        )}
      </WorkflowTable>
    </>
  );
}
