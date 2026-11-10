import { claimsPlanPeriod } from "./claimsPeriod";
import { formatBeijingTime } from "./beijingTime";
import type { CpiStrategy, CpiAvailability } from "./cpiTimeSeriesApi";
import type { WorkerObservation } from "./useAutoCollection";
type RecordValue = Record<string, unknown>;
export type PlanSnapshot = {
  calendar_status: string;
  targets: RecordValue[];
  calendar?: RecordValue;
};
export type DiagnosisEntry = {
  data:
    | (CpiStrategy & {
        control: WorkerObservation | null;
        schedule?: PlanSnapshot | null;
      })
    | null;
  saving: boolean;
  uncertain: boolean;
  resource: { stale: boolean };
  availability?: CpiAvailability;
};
export type PlanIssue = {
  kind:
    | "blocked"
    | "failed"
    | "unknown"
    | "retry"
    | "future"
    | "service"
    | "source";
  period: string;
  stage: string;
  summary: string;
  reason: string;
  impact: string;
  next: string;
  targetKey?: string;
  historical?: boolean;
  code?: string;
};
export function planTargetKey(target: RecordValue): string {
  return String(
    target.key ??
      [
        target.reference_month ??
          target.reference_quarter ??
          target.reference_week ??
          "期间未知",
        target.dataset ?? "",
        target.estimate_kind ?? "",
      ].join("/"),
  );
}
export function planPeriod(target: RecordValue): string {
  if (
    String(target.event_id ?? "").startsWith("dol-claims-") ||
    String(target.key ?? "").startsWith("dol-claims-week-")
  )
    return claimsPlanPeriod(target);
  return (
    String(
      target.reference_month ??
        target.reference_quarter ??
        target.reference_week ??
        "期间未知",
    ) +
    (target.dataset
      ? ` · ${target.dataset === "advm3" ? "耐用品先行报告" : "制造业完整报告"}`
      : "") +
    (target.estimate_kind ? ` · ${target.estimate_kind}` : "")
  );
}
const text = (v: unknown) => (typeof v === "string" && v ? v : undefined);
const obj = (v: unknown): RecordValue =>
  v && typeof v === "object" && !Array.isArray(v) ? (v as RecordValue) : {};
const reasons: Record<string, string> = {
  calendar_fallback_not_covered: "官方备用日历未覆盖该期，主日历尚待确认",
  calendar_fallback_conflict: "官方备用日历与已保留的该期状态或发布时间不一致",
  calendar_cancelled_after_completion:
    "该期执行完成后，官方日历又撤销了该期发布",
  calendar_changed_after_completion: "该期执行完成后，官方日历信息发生变化",
  calendar_moved_after_execution: "该期已有执行记录，官方发布时间随后发生变更",
  gdp_calendar_moved_after_execution:
    "该季度估计已有执行记录，官方发布时间随后发生变更",
  calendar_conflict: "官方日历信息存在冲突",
  calendar_reference_month_conflict: "官方日历参考期不一致",
  calendar_removed: "该期已从官方日历移除",
  source_unavailable: "来源尚未提供该期数据",
  claims_current_actuals_missing:
    "该次回读尚未核实本次报告的初请/续请参考周数值",
  claims_material_unconfirmed: "本次官方报告材料尚未核实",
  claims_rolling_http_budget:
    "滚动24小时HTTP额度已占用，等待已核实消耗结算或额度到期",
  storage_commit_unknown: "写入提交结果未确认，需要先核对持久回读",
  historical_source_gaps: "历史来源仍有缺值",
  provider_http_403: "来源拒绝访问（HTTP403）",
  worker_service_not_started: "此资产的自动采集服务尚未启动",
  worker_service_stopped: "此资产的自动采集服务已停止",
  worker_heartbeat_stale: "服务心跳已过期，无法确认持续运行",
  worker_process_not_confirmed: "实际服务进程与心跳未对齐",
  worker_loop_error: "自动检查循环出现异常",
  worker_unit_not_registered: "此资产的受控服务尚未装配",
};
export function diagnosisReason(code: string | undefined, fallback: string) {
  if (!code) return fallback;
  if (reasons[code]) return reasons[code];
  if (/403/.test(code)) return "来源拒绝访问（HTTP403）";
  if (/429|rate_limit|cooldown/.test(code)) return "来源限流，等待准入窗口";
  if (/timeout/.test(code)) return "来源请求超时，结果尚未确认";
  if (/unbound|binding|not_configured/.test(code))
    return "该资产来源绑定或凭据尚未配置完整";
  return `${fallback}（分类：${code}）`;
}
function attempted(stage: RecordValue): boolean {
  return !!(
    stage.last_attempt_at ||
    stage.started_at ||
    stage.finished_at ||
    stage.completed_at ||
    Number(stage.attempts) > 0 ||
    Number(stage.actual_http) > 0
  );
}
export function autoCollectionDiagnosis(
  entry: DiagnosisEntry,
  now = Date.now(),
) {
  const issues: PlanIssue[] = [],
    advisories: PlanIssue[] = [],
    { data } = entry;
  const finish = (state: string, summary: string) => ({
    state,
    summary,
    issues,
    advisories,
    listReason: issues[0]?.summary ?? summary,
  });
  const global = (
    kind: PlanIssue["kind"],
    reason: string,
    impact: string,
    next: string,
    state: string,
  ) => {
    issues.push({
      kind,
      period: "当前资产",
      stage: "自动采集",
      summary: reason,
      reason,
      impact,
      next,
    });
    return finish(state, reason);
  };
  if (entry.saving)
    return finish(
      data?.enabled ? "正在处理" : "正在开启",
      "正在协调当前资产策略与服务，等待实际回读；请勿重复操作。",
    );
  if (!data || entry.resource.stale)
    return global(
      "unknown",
      "当前资产策略读取未确认",
      "自动采集意图与分期状态暂不能核实；保留已有数据。",
      "使用页面刷新读取当前状态；读取成功前不重复操作。",
      "待核实",
    );
  if (entry.uncertain)
    return global(
      "unknown",
      "本次开关或恢复操作的结果未知",
      "策略保存或服务动作尚未确认，不能报告开启成功。",
      "等待独立回读；使用页面刷新核实，不自动重复启动。",
      "待核实",
    );

  if (
    data.enabled &&
    entry.availability?.data?.web_capabilities.collect === false
  ) {
    const code = entry.availability.data.web_capabilities.reason ?? undefined;
    return global(
      "source",
      diagnosisReason(code, "来源未获得采集授权"),
      "此资产后续自动采集受阻；已有本地数据不因此失效。",
      "到数据源管理核对该资产绑定与凭据，然后刷新页面核实。",
      "需处理",
    );
  }
  if (data.enabled && !data.control)
    return global(
      "unknown",
      "实际服务状态读取失败",
      "尚不能确认该资产执行器在线；不据策略绿色推定可运行。",
      "使用页面刷新读取实际服务；确认前不重复启动。",
      "待核实",
    );
  if (data.enabled && data.control && !data.control.online)
    return global(
      "service",
      diagnosisReason(data.control.reason, "自动采集服务未确认在线"),
      "此资产后续自动触发无法确认；不代表已存数据不可用。",
      data.control.registered
        ? "使用本任务行的恢复自动采集；操作后等待实际进程和心跳回读。"
        : "固定服务尚未装配，需要运行维护处理；当前没有可用的网页修复动作。",
      "需处理",
    );
  if (!data.schedule)
    return global(
      "unknown",
      "分期计划读取失败",
      "无法核实当前自动计划；已有成功采集不能证明本轮正常。",
      "使用页面刷新重新读取计划，读取成功后按具体期次核实。",
      "待核实（计划读取未确认）",
    );
  const targets = data.schedule.targets;
  const upcoming = targets
    .filter((t) => text(t.release_at) && Date.parse(String(t.release_at)) > now)
    .sort(
      (a, b) =>
        Date.parse(String(a.release_at)) - Date.parse(String(b.release_at)),
    )[0];
  const nextAt =
    text(upcoming?.release_at) ??
    (data.next_release_at && Date.parse(data.next_release_at) > now
      ? data.next_release_at
      : null);
  const current = upcoming ? planPeriod(upcoming) : "最近一期";
  const normal = nextAt
    ? `${current}${String(upcoming?.event_id ?? "").startsWith("dol-claims-") ? "；" : ""}待发布：${formatBeijingTime(nextAt, "时间未确认")}`
    : "下次官方发布时间尚未确认";
  let running = false;
  for (const target of targets) {
    const period = planPeriod(target),
      targetKey = planTargetKey(target);
    const stages = [
      ["当期采集", obj(target.target ?? target)],
      ["自动调度", obj(target.auto)],
      ["历史核实", obj(target.history)],
    ] as const;
    const anyAttempt = stages.some(([, s]) => attempted(s));
    const reference = String(
      target.reference_month ??
        target.reference_quarter ??
        target.reference_week ??
        "",
    );
    const futureReference = /^\d{4}-Q[1-4]$/.test(reference)
      ? reference >
        `${new Date(now).getUTCFullYear()}-Q${Math.floor(new Date(now).getUTCMonth() / 3) + 1}`
      : reference >
        new Date(now).toISOString().slice(0, reference.length === 10 ? 10 : 7);
    // Only affirmative unpublished/not-covered evidence can be advisory.
    // Cancellation and post-execution changes outrank a retained TBA placeholder.
    const validation = text(target.calendar_validation);
    const adverseCode = target.calendar_cancelled_after_completion
      ? "calendar_cancelled_after_completion"
      : target.calendar_changed_after_completion
        ? "calendar_changed_after_completion"
        : target.state === "calendar_removed" ||
            [target.publication_status, target.status].some((status) =>
              ["CANCELLED", "CANCELED", "WITHDRAWN"].includes(
                String(status).toUpperCase(),
              ),
            ) ||
            (target.cancellation_evidence &&
              (typeof target.cancellation_evidence !== "object" ||
                Object.keys(target.cancellation_evidence).length > 0))
          ? "calendar_removed"
          : undefined;
    const code = adverseCode ?? validation;
    const explicitCalendarProblem =
      !!adverseCode ||
      (!!code &&
        /conflict|cancel|removed|moved_after_execution|changed_after_completion/.test(
          code,
        ));
    const ordinaryUnpublished =
      !code || code === "calendar_fallback_not_covered";
    const farTba =
      !target.release_at &&
      futureReference &&
      !anyAttempt &&
      ordinaryUnpublished &&
      !explicitCalendarProblem &&
      (String(target.publication_status).toUpperCase() === "TBA" ||
        target.calendar_validation === "calendar_fallback_not_covered");
    const historical =
      !!target.release_at &&
      Date.parse(String(target.release_at)) < now &&
      !!upcoming &&
      reference <
        String(
          upcoming.reference_month ??
            upcoming.reference_quarter ??
            upcoming.reference_week ??
            "",
        );
    if (
      target.calendar_execution_blocked ||
      explicitCalendarProblem ||
      farTba
    ) {
      const reason = diagnosisReason(
        code,
        farTba
          ? "官方尚未公布该期发布时间"
          : "官方日历已取消、有冲突或尚未确认",
      );
      const item: PlanIssue = {
        kind: farTba ? "future" : "blocked",
        period,
        stage: "官方日历",
        targetKey,
        historical: !farTba && historical,
        code,
        summary: farTba
          ? `${period}发布时间待定，不影响${current}计划`
          : `${historical ? "历史事项：" : ""}${period}官方日历阻塞：${reason}`,
        reason,
        impact: farTba
          ? `仅${period}等待官方确认，不影响${current}的发布计划。`
          : `仅该期自动触发暂停${historical ? "；历史覆盖尚未确认，其他已确认期次按各自计划执行" : "，该期不能按原日历确认采集"}。${upcoming && upcoming !== target && !upcoming.calendar_execution_blocked ? `不影响${current}已确认期次的发布计划。` : ""}`,
        next: farTba
          ? "等待官方公布；由现有日历刷新取得证据后再核实该期。"
          : "等待主官方日历重新确认该期状态和时间，并核对保留的阶段证据；现有执行结果独立保留，不自动重放该期采集。当前没有绕过日历准入的操作。",
      };
      (farTba ? advisories : issues).push(item);
    }
    for (const [stage, value] of stages) {
      const state = text(value.state);
      if (state === "running") running = true;
      const result = obj(value.result);
      const claimsGap =
        String(target.event_id ?? "").startsWith("dol-claims-") &&
        obj(result.readback).actuals_verified === false &&
        Object.keys(obj(obj(result.material).actual_weeks)).length > 0;
      const code =
        (value.budget_blocked_until
          ? "claims_rolling_http_budget"
          : undefined) ??
        text(value.classification) ??
        (claimsGap ? "claims_current_actuals_missing" : undefined) ??
        text(obj(obj(value.result).source_failure).classification);
      const retryAt = text(value.next_attempt_at);
      let kind: PlanIssue["kind"] | null = [
        "unknown",
        "outcome_unknown",
      ].includes(state ?? "")
        ? "unknown"
        : ["awaiting_provider", "retrying"].includes(state ?? "") ||
            (state === "failed" && !!retryAt)
          ? "retry"
          : ["failed", "error", "pending_exhausted"].includes(state ?? "")
            ? "failed"
            : state === "blocked" &&
                attempted(value) &&
                !target.calendar_execution_blocked
              ? "blocked"
              : null;
      if (!kind) continue;
      const reason = diagnosisReason(
        code,
        kind === "unknown"
          ? "该次执行未取得可确认结果"
          : kind === "retry"
            ? "来源暂未就绪或上次尝试失败，正在等待重试"
            : "该阶段执行失败，尚无成功回执",
      );
      const issue: PlanIssue = {
        kind,
        period,
        stage,
        targetKey,
        historical: historical || stage === "历史核实",
        code,
        summary: `${historical || stage === "历史核实" ? "历史事项：" : ""}${period} · ${stage}：${reason}`,
        reason,
        impact: `${period}的${stage}${kind === "unknown" ? "结果未确认" : kind === "retry" ? "尚未完成" : "受阻"}；不以其他期次或手动成功记录抵消该事项。`,
        next:
          kind === "unknown"
            ? "先核对该期阶段的回执和覆盖；等待正常持久回读，勿直接重复采集。"
            : kind === "retry"
              ? retryAt
                ? `等待现有调度于${formatBeijingTime(retryAt, "时间未确认")}重试；到时刷新核实结果。`
                : "等待来源恢复或准入，使用页面刷新核实是否已取得重试时间。"
              : "核对该期失败分类及来源/覆盖证据；来源恢复后沿用现有采集入口处理对应范围，当前不提供一键修复。",
      };
      issues.push(issue);
    }
  }
  const calendar = data.schedule.calendar_status;
  if (!["fresh", "degraded_cached_calendar"].includes(calendar)) {
    issues.unshift({
      kind: "blocked",
      period: "当前资产",
      stage: "官方日历",
      summary: `当前官方日历${calendar === "expired" ? "已过期" : "尚未确认可用"}`,
      reason: `日历状态：${calendar}`,
      impact: "未确认的日历触发受阻；已存数据与执行回执分别保留。",
      next: "等待现有日历刷新取得有效证据；使用页面刷新核实。",
    });
  }
  const active = issues.filter((i) => !i.historical);
  const severity = (items: PlanIssue[]) =>
    items.some((i) =>
      ["failed", "blocked", "source", "service"].includes(i.kind),
    )
      ? "需处理"
      : items.some((i) => i.kind === "unknown")
        ? "待核实"
        : items.some((i) => i.kind === "retry")
          ? "重试中"
          : null;
  const currentState = severity(active);
  const historicalState = severity(issues.filter((i) => i.historical));
  const state =
    currentState ??
    (running ? "采集中" : nextAt ? "待发布" : "待核实（下次发布未确认）");
  if (!nextAt && !issues.length)
    issues.push({
      kind: "unknown",
      period: "最近一期",
      stage: "官方日历",
      summary: "最近一期官方发布时间尚未确认",
      reason: "未取得下一次官方发布的有效时间证据",
      impact: "后续自动触发时间无法核实。",
      next: "等待官方日历公布或刷新；使用页面刷新核实。",
    });
  // Current issues are actionable first; old unresolved work stays explicitly visible.
  issues.sort((a, b) => Number(!!a.historical) - Number(!!b.historical));
  return finish(
    !data.enabled
      ? "已暂停"
      : !currentState && historicalState
        ? `${state}；历史${historicalState}`
        : state,
    !data.enabled
      ? `后续自动触发已暂停，当前任务正常收尾；官方计划：${normal}`
      : currentState
        ? `最近官方发布计划：${current} · ${formatBeijingTime(nextAt, "时间未确认")}；自动采集当前${currentState}，影响期次如下。`
        : normal,
  );
}
