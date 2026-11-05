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
    "本组部分来源未完成；已确认段复用，按来源冷却自动续跑。",
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
  const latest = summary.data?.series
    .filter((s) => s.last)
    .map((s) => s.last!)
    .sort();
  const [saving, setSaving] = useState(false),
    [pending, setPending] = useState<{ enabled: boolean } | null>(null),
    [notice, setNotice] = useState("");
  const busy = useRef(false);
  const data = resource.data;
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
  const failures = data?.tasks.filter((t) => t.state === "failed") ?? [];
  const current = data?.tasks.find((t) => t.state === "running");
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
            ? "正在读取策略与实际进程。"
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
          {formatBeijingTime(data?.last_successful_collection_at, "未记录")}
        </span>
        <span>
          最新数据期：
          {latest?.length
            ? latest[0] === latest.at(-1)
              ? latest[0]
              : `${latest[0]} 至 ${latest.at(-1)}（指标不同）`
            : "尚未核实"}
        </span>
      </span>,
    ],
  };
  const detail = (
    <>
      <h3>金融条件日频 策略详情</h3>
      <p>
        自动采集{data?.enabled ? "已开启" : "已暂停"} · {state}
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
        <Fact name="下次维护检查（北京时间）">
          {formatBeijingTime(data?.next_check_at, "未安排")}
        </Fact>
        <Fact name="最近成功自动作业（北京时间）">
          {formatBeijingTime(data?.last_successful_collection_at, "未记录")}
        </Fact>
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
        <ul>
          {summary.data?.series.map((s) => (
            <li key={s.series_id}>
              {s.series_id}：{s.last ?? "未取得"}
            </li>
          ))}
        </ul>
        <p>族级最新日期不能证明全部指标齐全；范围缺口以本地查询为准。</p>
      </details>
      <h4>分组检查与执行</h4>
      <p>
        {data?.schedule_semantics}{" "}
        以下时点为美国东部时间，执行详情统一显示北京时间；隔夜/国债/信用只在周一至周五检查，实际休市与未公布保留来源缺项。
      </p>
      <ul>
        {Object.entries(data?.maintenance_times ?? {}).map(([g, t]) => (
          <li key={g}>
            {groups[g]}：{t.join("、")}
          </li>
        ))}
      </ul>
      {data?.tasks.slice(0, 12).map((t) => (
        <article className="auto-issue" key={t.request_id}>
          <strong>
            {groups[t.group]} · {formatBeijingTime(t.slot)}
          </strong>
          <p>
            {t.state === "complete"
              ? "该组采集作业已完成"
              : t.state === "running"
                ? "正在采集"
                : `已尝试 ${t.attempts}/${data.max_attempts} 次；${reason(t.classification)}`}
          </p>
          {t.failures?.map((f) => (
            <p key={f.series_id}>
              {f.series_id}：{reason(f.classification)}
            </p>
          ))}
          {t.state === "failed" && (
            <p>
              {t.attempts < data.max_attempts
                ? `下次自动续跑：${formatBeijingTime(t.retry_at)}`
                : "本次时点自动重试已达上限；可进入该作业查看原因并续跑。后续时点仍按计划检查近14日，不覆盖旧失败事实。"}
            </p>
          )}
          <button
            onClick={() =>
              navigate("collection", {
                tab: "executions",
                asset: FINANCIAL.identity,
                asset_revision: "r1",
                execution_asset: FINANCIAL.identity + "@r1",
                job: t.request_id,
              })
            }
          >
            查看日频执行详情 →
          </button>
        </article>
      ))}
      {!data?.tasks.length && <p>尚无日频自动执行记录。</p>}
      <details>
        <summary>技术详情</summary>
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
    },
  };
}
