import { FinancialResultFacts } from "./FinancialResultFacts";
import { CompactTable } from "./CpiQueryDetails";
import {
  financialJobOrigin,
  financialJobResult,
} from "./financialJobPresentation";
import type { FinancialJob } from "./FinancialDailyWorkbench";
import { executionCreatedOrder } from "./executionOrder";
import { financialSeriesSource } from "./financialJobPresentation";
import { useRef, useState } from "react";
import type { Transport } from "./api";
import type { Navigate } from "./workflowRoute";
import { useVisiblePolling } from "./useVisiblePolling";
import { financialCall, FINANCIAL } from "./FinancialDailyWorkbench";
import { StrategySwitch } from "./StrategySwitch";
import { Fact } from "./WorkflowTable";
import { formatBeijingTime } from "./beijingTime";

type Task = {
  group: string;
  slot: string;
  state: string;
  request_id: string;
  attempts: number;
  classification?: string;
  retry_at: string;
  failures?: { series_id: string; classification?: string; state: string }[];
};
type Strategy = {
  enabled: boolean;
  revision?: number;
  updated_at?: string | null;
  next_group_checks?: { group: string; at: string }[];
  next_check_at: string | null;
  last_successful_collection_at: string | null;
  max_attempts: number;
  tasks: Task[];
  worker: {
    state: string;
    last_heartbeat_at?: string;
    process?: { pid: number };
    classification?: string;
  };
  control: {
    online: boolean;
    registered: boolean;
    reason: string;
    process_id?: number;
    expires_at?: string;
    code_commit?: string;
  };
  schedule_semantics: string;
  maintenance_times: Record<string, string[]>;
};
const groups: Record<string, string> = {
  policy: "政策上下限",
  overnight: "EFFR / SOFR",
  treasury: "国债与派生通胀补偿",
  credit: "IG / HY 信用利差",
};
const reasons: Record<string, string> = {
  worker_service_not_started: "日频执行器尚未启动，开启时将启动并核对心跳。",
  worker_service_action_failed:
    "服务启动或恢复未能确认；保留策略意图，请先核实当前进程后重试。",
  worker_active_collection_in_progress:
    "存在采集中作业，待其收尾后再恢复服务，避免打断采集。",
  worker_service_stopped: "日频服务已停止，需恢复自动采集。",
  worker_heartbeat_stale: "日频心跳已过期，需核对并恢复服务。",
  worker_unit_not_registered: "固定日频服务尚未装配，请联系本 DEV 的维护人员。",
  dev_window_expired: "DEV 授权窗口已到期，不能继续启动。",
  financial_periodic_partial_failure:
    "本组部分来源未完成；已确认段保留，是否继续自动尝试取决于本维护时点的剩余预算。",
  financial_timeout:
    "该来源请求超时；保留实际尝试与失败时间，按本时点剩余预算决定后续处理。",
  financial_bound_credential_missing:
    "现有 FRED 凭据绑定缺失，请在接入管理核实该绑定后重试。",
  financial_worker_awaiting_heartbeat:
    "启动请求已发出，尚未确认在线；先刷新核实，勿重复启动。",
};
const reason = (s?: string) =>
  s
    ? (reasons[s] ?? `分类：${s}；请核实该来源或服务后重试，已确认数据保留。`)
    : "尚未取得失败原因，等待回读。";
export function useFinancialPeriodic(
  transport: Transport,
  active: boolean,
  navigate: Navigate,
) {
  const resource = useVisiblePolling(
    async (signal) => {
      const value = await financialCall<Strategy>(
        transport,
        "/strategy",
        signal,
      );
      if (typeof value.enabled !== "boolean" || !Array.isArray(value.tasks))
        throw Error("financial_strategy_unknown");
      return value;
    },
    [transport],
    active,
  );
  const summary = useVisiblePolling(
    async (signal) => {
      const value = await financialCall<{
        series: { series_id: string; last: string | null }[];
      }>(transport, "/summary", signal);
      if (!Array.isArray(value.series)) throw Error("summary_unknown");
      return value;
    },
    [transport],
    active,
  );
  const jobs = useVisiblePolling(
    (signal) =>
      financialCall<{ jobs: FinancialJob[] }>(transport, "/jobs", signal),
    [transport],
    active,
  );
  const orderedJobs = [...(jobs.data?.jobs ?? [])].sort((a, b) =>
    executionCreatedOrder(
      { id: a.request_id, createdAt: a.created_at },
      { id: b.request_id, createdAt: b.created_at },
    ),
  );
  const recentJob =
    orderedJobs.find((j) => j.origin === "periodic") ??
    orderedJobs.find((j) => j.origin == null || j.origin === "manual");
  const openJob = (id: string) =>
    navigate("collection", {
      tab: "executions",
      asset: FINANCIAL.identity,
      asset_revision: "r1",
      execution_asset: FINANCIAL.identity + "@r1",
      job: id,
    });
  const latest = summary.data?.series
    .filter((s) => s.last)
    .map((s) => s.last!)
    .sort();
  const latestText = !summary.data
    ? summary.failure
      ? "读取失败，待核实"
      : summary.busy
        ? "正在读取本地覆盖…"
        : "尚未读取本地覆盖"
    : latest?.length
      ? latest[0] === latest.at(-1)
        ? latest[0]
        : `${latest[0]} 至 ${latest.at(-1)}（指标不同）`
      : "本地尚无已确认的有效观测";
  const [saving, setSaving] = useState(false),
    [pending, setPending] = useState<{ enabled: boolean } | null>(null),
    [notice, setNotice] = useState("");
  const busy = useRef(false);
  const data = resource.data;
  const successText = data
    ? formatBeijingTime(data.last_successful_collection_at, "尚无成功自动作业")
    : resource.failure
      ? "读取失败，待核实"
      : "正在读取执行记录…";
  const nextText = data
    ? formatBeijingTime(data.next_check_at, "未安排")
    : resource.failure
      ? "读取失败，待核实"
      : "正在读取计划…";
  async function act(enabled: boolean, recover = false) {
    if (busy.current || pending) return;
    busy.current = true;
    setSaving(true);
    setNotice("");
    try {
      const response = await transport(
        "/api/r5/financial/timeseries/strategy",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            enabled,
            ...(recover ? { recover: true } : {}),
          }),
        },
      );
      const v = (await response.json()) as Strategy & {
        classification?: string;
      };
      if (!response.ok) {
        if (response.status >= 500) {
          setPending({ enabled });
          setNotice(
            `${reason(v.classification)} 操作结果需核实，不自动重试启动。`,
          );
        } else setNotice(reason(v.classification));
        resource.refresh();
        return;
      }
      if (typeof v.enabled !== "boolean" || (enabled && !v.control?.online))
        throw Error("unconfirmed");
      setNotice(
        enabled
          ? "已核对日频服务在线；采集成功以实际作业回读为准。"
          : "已暂停后续自动触发；在途作业正常收尾，手动采集可用。",
      );
      resource.refresh();
    } catch {
      setPending({ enabled });
      setNotice("操作结果未知，请先核实当前策略与进程；不会自动重复启动。");
      resource.refresh();
    } finally {
      busy.current = false;
      setSaving(false);
    }
  }
  async function verify() {
    if (busy.current) return;
    busy.current = true;
    try {
      const value = await financialCall<Strategy>(transport, "/strategy");
      if (typeof value.enabled !== "boolean" || !value.control)
        throw Error("unconfirmed");
      setPending(null);
      setNotice(
        `已核实：策略${value.enabled ? "开启" : "暂停"}，服务${value.control.online ? "在线" : "未在线"}。`,
      );
      resource.refresh();
    } catch {
      setNotice("独立回读仍失败，操作结果未知；保留原意图且不重复启动。");
    } finally {
      busy.current = false;
    }
  }
  const latestGroups = new Map<string, Task>();
  for (const task of [...(data?.tasks ?? [])].sort((a, b) =>
    b.slot.localeCompare(a.slot),
  )) {
    if (!latestGroups.has(task.group)) latestGroups.set(task.group, task);
  }
  const failures = [...latestGroups.values()].filter(
    (t) => t.state === "failed",
  );
  const current = [...latestGroups.values()].find((t) => t.state === "running");
  const state = !data
    ? "待核实"
    : saving
      ? "正在协调"
      : pending
        ? "待核实"
        : !data.enabled
          ? "已暂停"
          : !data.control.online
            ? "需处理"
            : current
              ? "采集中"
              : failures.some((t) => t.attempts >= data.max_attempts)
                ? "需处理"
                : failures.length
                  ? "重试中"
                  : "等待下次检查";
  const problem = failures[0];
  const row = {
    id: "financial-auto",
    search: "金融条件日频 十项 自动采集",
    cells: [
      <span className="strategy-target">金融条件日频（十项）</span>,
      <span className="strategy-control">
        <span className="auto-control-line">
          <span className="small">自动采集</span>
          <StrategySwitch
            asset="金融条件日频"
            enabled={data?.enabled ?? null}
            saving={saving}
            uncertain={!!pending}
            disabled={!data || saving || !!pending || resource.stale}
            onToggle={() => void act(!data?.enabled)}
          />
        </span>
        <span className="auto-state">运行情况：{state}</span>
        {data?.enabled &&
          !data.control.online &&
          data.control.registered &&
          !pending && (
            <button
              className="auto-recover"
              disabled={saving}
              onClick={(e) => {
                e.stopPropagation();
                void act(true, true);
              }}
            >
              恢复自动采集
            </button>
          )}
        <span className="strategy-control-note">
          {!data
            ? resource.failure
              ? "策略与进程读取失败，状态待核实。"
              : "正在读取策略与实际进程。"
            : !data.enabled
              ? "暂停后不再自动触发；手动采集不受影响。"
              : !data.control.online
                ? reason(data.control.reason)
                : problem
                  ? `${groups[problem.group]} · ${formatBeijingTime(problem.slot)}：${reason(problem.classification)}`
                  : `下次检查：${formatBeijingTime(data.next_check_at, "尚未确定")}`}
        </span>
        {notice && <span role="status">{notice}</span>}
        {pending && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              verify();
            }}
          >
            核实操作结果
          </button>
        )}
      </span>,
      <span className="strategy-data">
        <span>
          最近成功采集（北京时间）：
          <br />
          {successText}
        </span>
        <span>
          最新数据期：{latestText}
          {summary.data && summary.failure
            ? "（保留上次回读，当前读取失败）"
            : ""}
        </span>
      </span>,
    ],
  };
  const detail = (
    <>
      <h3>金融条件日频 策略详情</h3>
      <p>
        {data
          ? `自动采集${data.enabled ? "已开启" : "已暂停"} · ${state}`
          : resource.failure
            ? "自动采集状态：读取失败，待核实"
            : "自动采集状态：正在读取…"}
      </p>
      {notice && <p role="status">{notice}</p>}
      {resource.failure && (
        <p role="alert">策略读取失败，保留上次事实并自动重读。</p>
      )}
      <p>
        每次按来源分组回看近 14
        个日历日，保留真实修订；停用时间更长的历史区间可从新建采集手动更新。已有数据、维护检查完成和自然发布捕获分别记录。
      </p>
      <dl>
        <Fact name="最近成功采集（北京时间）">{successText}</Fact>
        <Fact name="最新数据期">{latestText}</Fact>
        <Fact name="下次维护检查（北京时间）">{nextText}</Fact>
      </dl>
      <button
        onClick={() =>
          navigate("assets", {
            asset: FINANCIAL.identity,
            asset_revision: "r1",
          })
        }
      >
        查询各指标本地覆盖 →
      </button>
      <details>
        <summary>各指标最新本地数据期</summary>
        {!summary.data && <p role="status">{latestText}</p>}
        <ul>
          {summary.data?.series.map((s) => (
            <li key={s.series_id}>
              {s.series_id} · {financialSeriesSource(s.series_id)}：
              {s.last ?? "本地尚无有效观测"}
            </li>
          ))}
        </ul>
        <p>
          来源按已登记口径列示；信用可来自归档或FRED，逐点版本与实际分发者见本地查询，不由汇总日期推断。
        </p>
        <p>族级最新日期不能证明全部指标齐全；范围缺口以本地查询为准。</p>
      </details>
      <section aria-label="最近真实执行摘要">
        <h4>最近真实执行摘要</h4>
        {!jobs.data && (
          <p role={jobs.failure ? "alert" : "status"}>
            {jobs.failure
              ? "执行记录读取失败，最近执行待核实。"
              : "正在读取真实执行记录…"}
          </p>
        )}
        {jobs.data && jobs.failure && (
          <p role="alert">保留上次执行回读，当前刷新失败。</p>
        )}
        {jobs.data && !recentJob && <p>尚无真实执行记录。</p>}
        {recentJob && (
          <>
            <p>
              本资产最近执行来源：{financialJobOrigin(recentJob)}。
              {recentJob.origin !== "periodic" &&
                "自动策略尚无真实执行记录；以下为手动采集结果。"}
            </p>
            <dl>
              <Fact name="记录编号">{recentJob.request_id}</Fact>
              <Fact name="采集方式">
                {recentJob.command.action === "init"
                  ? "初始化采集"
                  : "更新指定范围"}
              </Fact>
              <Fact name="范围">
                {recentJob.command.start} 至 {recentJob.command.end}
              </Fact>
              <Fact name="开始时间（北京时间）">
                {formatBeijingTime(recentJob.started_at, "未记录")}
              </Fact>
              <Fact name="结束时间（北京时间）">
                {formatBeijingTime(
                  recentJob.state === "running" ? null : recentJob.finished_at,
                  "未记录",
                )}
              </Fact>
              <Fact name="执行结果">{financialJobResult(recentJob)}</Fact>
            </dl>
            <FinancialResultFacts job={recentJob} />
            <div className="actions">
              <button onClick={() => openJob(recentJob.request_id)}>
                打开对应执行详情
              </button>
            </div>
          </>
        )}
      </section>
      <details>
        <summary>策略上次执行回执与审计</summary>
        {!data ? (
          <p role="status">
            {resource.failure
              ? "策略回执读取失败，待核实。"
              : "正在读取策略回执…"}
          </p>
        ) : (
          <dl>
            <Fact name="策略意图">{data.enabled ? "开启" : "暂停"}</Fact>
            <Fact name="策略修订">{data.revision ?? "未记录"}</Fact>
            <Fact name="意图保存时间（北京时间）">
              {formatBeijingTime(data.updated_at, "未记录")}
            </Fact>
            <Fact name="最近成功自动作业（北京时间）">{successText}</Fact>
          </dl>
        )}
        <p>
          策略意图保存、服务在线与采集成功分别记录，不以其中任一项推定其他项。
        </p>
      </details>
      <section aria-label="分组维护计划与结果">
        <h4>分组维护计划与执行结果</h4>
        <p>
          时间基准：北京时间（UTC+08:00）。MDF
          维护计划不代表官方发布或已经执行；每组回看近14个日历日。
        </p>
        {!data ? (
          <p role={resource.failure ? "alert" : "status"}>
            {resource.failure
              ? "维护计划读取失败，待核实。"
              : "正在读取维护计划…"}
          </p>
        ) : data.next_group_checks ? (
          <CompactTable
            label="下一次分组维护计划"
            headers={["分组", "下次计划（北京时间）", "触发意图"]}
            rows={data.next_group_checks.map((g) => [
              groups[g.group] ?? g.group,
              formatBeijingTime(g.at, "待核实"),
              data.enabled ? "按计划检查" : "已暂停，不触发",
            ])}
          />
        ) : (
          <p>尚未取得带日期的分组计划；下次整体检查：{nextText}。</p>
        )}
        <details>
          <summary>近期分组执行与结果</summary>
          {!data && (
            <p role="status">
              {resource.failure
                ? "执行回执读取失败，待核实。"
                : "正在读取执行回执…"}
            </p>
          )}
          {data && !data.tasks.length && <p>尚无日频自动执行记录。</p>}
          {data?.tasks.slice(0, 12).map((t) => (
            <details className="month-stage-detail" key={t.request_id}>
              <summary>
                {groups[t.group]} · {formatBeijingTime(t.slot)} ·{" "}
                {t.state === "complete"
                  ? "已完成"
                  : t.state === "running"
                    ? "采集中"
                    : t.state === "pending"
                      ? "等待执行"
                      : "需核对失败与重试"}
              </summary>
              <dl>
                <Fact name="记录编号">{t.request_id}</Fact>
                <Fact name="维护计划（北京时间）">
                  {formatBeijingTime(t.slot)}
                </Fact>
                <Fact name="累计调度尝试">
                  {t.attempts}/{data.max_attempts}
                </Fact>
                {latestGroups.get(t.group)?.request_id !== t.request_id && (
                  <Fact name="记录范围">
                    历史维护时点；该组当前状态见最新时点，原失败及时间保留。
                  </Fact>
                )}
              </dl>
              {t.state === "failed" && (
                <p role="alert">{reason(t.classification)}</p>
              )}
              {t.failures?.map((f) => (
                <p key={f.series_id}>
                  {f.series_id}：{reason(f.classification)}
                </p>
              ))}
              {t.state === "failed" && (
                <p>
                  {latestGroups.get(t.group)?.request_id !== t.request_id
                    ? "旧维护时点；不将旧 retry_at 当作当前自动重试承诺。原作业仍可核对。"
                    : t.attempts < data.max_attempts
                      ? `下次自动续跑：${formatBeijingTime(t.retry_at)}`
                      : "本次时点自动重试已达上限；进入原作业核对原因后恢复。后续时点仍按计划检查近14日，旧失败事实保留。"}
                </p>
              )}
              <div className="actions">
                <button onClick={() => openJob(t.request_id)}>
                  查看日频执行详情 →
                </button>
              </div>
            </details>
          ))}
        </details>
      </section>
      <details>
        <summary>技术详情</summary>
        <p>
          {data?.schedule_semantics}{" "}
          美国东部规则（America/New_York，自动适用DST）；隔夜/国债/信用限周一至周五，实际休市或未公布独立留缺。
        </p>
        <ul>
          {Object.entries(data?.maintenance_times ?? {}).map(([g, t]) => (
            <li key={g}>
              {groups[g]}：{t.join("、")}（美东规则）
            </li>
          ))}
        </ul>
        <dl>
          <Fact name="服务与心跳">
            {data?.control.online ? "已核对在线" : reason(data?.control.reason)}
          </Fact>
          <Fact name="进程编号">{data?.control.process_id ?? "未核实"}</Fact>
          <Fact name="最近心跳（北京时间）">
            {formatBeijingTime(data?.worker.last_heartbeat_at, "未记录")}
          </Fact>
          <Fact name="固定版本">{data?.control.code_commit ?? "未核实"}</Fact>
          <Fact name="DEV 绝对截止（北京时间）">
            {formatBeijingTime(data?.control.expires_at, "未核实")}
          </Fact>
        </dl>
      </details>
    </>
  );
  return {
    row,
    detail,
    refresh: () => {
      resource.refresh();
      summary.refresh();
      jobs.refresh();
    },
  };
}
