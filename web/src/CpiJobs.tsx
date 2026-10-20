import { CpiSchedules } from "./CpiSchedules";
import { useEffect, useRef, useState } from "react";
import {
  ApiFailure,
  request,
  submitJob,
  type Jobs,
  type Transport,
} from "./api";

type Tab = "new" | "periodic" | "executions";
const tabs: [Tab, string][] = [
  ["new", "新建采集"],
  ["periodic", "周期任务"],
  ["executions", "执行记录"],
];
function selectedTab(): Tab {
  const value = new URLSearchParams(location.hash.slice(1)).get("tab");
  return tabs.some(([key]) => key === value) ? (value as Tab) : "new";
}

export function CpiJobsView({
  transport,
  generation,
  asset,
  revision,
}: {
  transport: Transport;
  generation: number;
  asset: string | null;
  revision: string | null;
}) {
  const [tab, setTab] = useState<Tab>(selectedTab);
  const [data, setData] = useState<Jobs | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const active = useRef(true);
  const writing = useRef(false);
  const job = new URLSearchParams(location.hash.slice(1)).get("job");
  useEffect(() => {
    active.current = true;
    const onHash = () => setTab(selectedTab());
    window.addEventListener("hashchange", onHash);
    return () => {
      active.current = false;
      window.removeEventListener("hashchange", onHash);
    };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    setFailure(null);
    request(
      transport,
      "/api/r5/cpi/jobs",
      job ? { job_identity: job } : {},
      controller.signal,
    )
      .then((value) => {
        if (!controller.signal.aborted) setData(value);
      })
      .catch((error) => {
        if (!controller.signal.aborted)
          setFailure(
            error instanceof ApiFailure
              ? error.classification
              : "request_failed",
          );
      });
    return () => controller.abort();
  }, [transport, generation, refresh, job, asset, revision]);
  const matches = Boolean(
    data?.asset_identity &&
      asset === data.asset_identity &&
      revision === data.asset_revision,
  );
  const canWrite = matches && data?.status === "OK" && data.write_eligible;
  function choose(next: Tab) {
    const hash = new URLSearchParams(location.hash.slice(1));
    hash.set("tab", next);
    location.hash = hash.toString();
    setTab(next);
  }
  async function write(cancel?: string) {
    if (!canWrite || !data?.intent_identity || writing.current) return;
    writing.current = true;
    setBusy(true);
    setNotice(null);
    try {
      if (cancel)
        await submitJob(transport, `/api/r5/cpi/jobs/${cancel}/cancel`, {});
      else
        await submitJob(transport, "/api/r5/cpi/jobs", {
          job_identity: data.intent_identity,
          asset_identity: data.asset_identity!,
          asset_revision: data.asset_revision!,
          reference: data.reference!,
          release_slot: data.release_slot!,
          mode: "manual_once",
        });
      if (active.current) {
        setNotice(
          cancel
            ? "取消请求已持久接收；以记录中的执行结果为准。"
            : "意图已持久接收；等待独立 Worker，不表示已执行成功。",
        );
        choose("executions");
        setRefresh((n) => n + 1);
      }
    } catch (error) {
      if (active.current)
        setNotice(
          `UNKNOWN · ${error instanceof ApiFailure ? error.classification : "request_failed"}。先刷新回查；重复提交使用同一 Job 身份。`,
        );
    } finally {
      writing.current = false;
      if (active.current) setBusy(false);
    }
  }
  return (
    <>
      <div className="tabs" role="tablist" aria-label="采集任务页签">
        {tabs.map(([key, label]) => (
          <button
            type="button"
            role="tab"
            aria-selected={tab === key}
            key={key}
            onClick={() => choose(key)}
          >
            {label}
          </button>
        ))}
      </div>
      <p className="notice" role="status">
        {failure
          ? `UNKNOWN · ${failure}`
          : !data
            ? "正在回查持久记录…"
            : `${data.evidence_mode} · ${data.status} · ${data.classification ?? "FileJobJournal"}`}
        {data?.evidence_mode === "OFFLINE_FIXTURE" &&
          " · 离线合成验证，不证明 Provider / PG / Garage 在线或已授权。"}
      </p>
      <p>
        服务端 Operator：{data?.operator_identity ?? "未绑定"} · 写入资格：
        {data?.write_eligible ? "已绑定" : "不可用"}
      </p>
      {notice && <p role="alert">{notice}</p>}
      <CpiSchedules
        transport={transport}
        generation={generation}
        asset={asset}
        revision={revision}
        mode={tab}
      />
      {tab === "new" && (
        <section className="panel">
          <h2>CPI 人工一次性初始化</h2>
          {!matches && (
            <p role="alert">
              请从 04 具名 CPI 资产携带身份及修订进入；当前关联缺失或不受支持。
            </p>
          )}
          <dl>
            <dt>资产 / 修订</dt>
            <dd>
              {asset ?? "UNKNOWN"} / {revision ?? "UNKNOWN"}
            </dd>
            <dt>已准入目标</dt>
            <dd>
              {data?.reference ?? "UNKNOWN"} · {data?.release_slot ?? "UNKNOWN"}
            </dd>
            <dt>存储目标</dt>
            <dd>继承 03 / 04 的 Host 绑定</dd>
            <dt>幂等 Job 身份</dt>
            <dd>
              <code>{data?.intent_identity ?? "UNKNOWN"}</code>
            </dd>
          </dl>
          <div className="actions">
            <button type="button" aria-pressed="true">
              人工一次性
            </button>
            <button type="button" disabled>
              多来源 · UNSUPPORTED
            </button>
          </div>
          <p>
            本 I4 兼容入口支持 2026-09 CPI release
            slot。提交只持久接受意图；由单独授权的 Host Worker
            执行。页面关闭不会取消或重新执行任务。
          </p>
          <label>
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            我明确提交此目标的一次性采集意图
          </label>
          <div className="actions">
            <button
              type="button"
              className="primary"
              disabled={!canWrite || !confirmed || busy}
              onClick={() => write()}
            >
              持久提交一次性意图
            </button>
          </div>
          {!data?.write_eligible && (
            <p>
              写入已关闭：需可信 Host 为当前请求绑定 Operator
              身份、写入资格及请求保护。
            </p>
          )}
        </section>
      )}
      {tab === "periodic" && (
        <section className="panel">
          <h2>周期任务</h2>
          <p>常驻 Cron / Worker 部署激活 · UNSUPPORTED</p>
          <button type="button" disabled>
            创建 Cron / 激活计划
          </button>
        </section>
      )}
      {tab === "executions" && (
        <section className="panel">
          <h2>持久 Job / Attempt / History</h2>
          <button type="button" onClick={() => setRefresh((n) => n + 1)}>
            回查执行记录
          </button>
          <p>
            状态来自 SQLite 持久记录。running 表示已记录的尝试，不证明 Worker
            当前在线；取消是请求，不能撤回已发生的效果。outcome_unknown
            只核实，不自动重试。
          </p>
          {data?.status === "OK" && !data.jobs.length && (
            <p>当前范围无持久 Job。</p>
          )}
          {data?.jobs.map((item) => (
            <article className="version" key={item.job_identity}>
              <h3>
                <code>{item.job_identity}</code>
              </h3>
              <p>
                {item.state} · {item.stage} ·{" "}
                {item.classification ?? "无错误分类"}
              </p>
              <p>
                Attempt：{item.attempt_identity ?? "尚无"} · Worker：
                {item.worker_identity ?? "UNKNOWN"} · 次数 {item.attempts} ·
                取消请求 {String(item.cancel_requested)}
              </p>
              <p>
                创建 {item.created_at} · 开始 {item.started_at ?? "尚无"} · 完成{" "}
                {item.finished_at ?? "尚无"} · 更新 {item.updated_at}
              </p>
              <p>
                失败阶段 {item.failure_stage ?? "无"} · 核实{" "}
                {item.reconciliation ?? "尚无"}
              </p>
              <p>
                关联候选 Version：{item.version_identity ?? "尚无"} · 已核实
                Publication occurrence：{item.publication_occurrence ?? "尚无"}
              </p>
              <div className="actions">
                <button
                  type="button"
                  disabled={
                    !canWrite ||
                    busy ||
                    !["pending", "running"].includes(item.state) ||
                    item.cancel_requested
                  }
                  onClick={() => write(item.job_identity)}
                >
                  请求取消
                </button>
                <a
                  href={`#page=3&asset=${encodeURIComponent(item.asset_identity)}&asset_revision=${encodeURIComponent(item.asset_revision)}`}
                >
                  04 版本与读取资格
                </a>
                <a
                  href={`#page=0&tab=diagnostics&job=${encodeURIComponent(item.job_identity)}`}
                >
                  01 关联异常
                </a>
              </div>
              <ol>
                {item.history.map((event, i) => (
                  <li key={i}>
                    {event.updated_at} · {event.state} / {event.stage} ·{" "}
                    {event.attempt_identity ?? "无 Attempt"} ·{" "}
                    {event.classification ?? "无错误分类"} · 取消请求{" "}
                    {String(event.cancel_requested)}
                  </li>
                ))}
              </ol>
            </article>
          ))}
        </section>
      )}
    </>
  );
}

export function CpiJobAlerts({
  transport,
  generation,
  job,
}: {
  transport: Transport;
  generation: number;
  job: string;
}) {
  const [data, setData] = useState<Jobs | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    setFailure(null);
    request(
      transport,
      "/api/r5/cpi/jobs",
      job ? { job_identity: job } : {},
      controller.signal,
    )
      .then((value) => {
        if (!controller.signal.aborted) setData(value);
      })
      .catch((error) => {
        if (!controller.signal.aborted)
          setFailure(
            error instanceof ApiFailure
              ? error.classification
              : "request_failed",
          );
      });
    return () => controller.abort();
  }, [transport, generation, job]);
  const alerts =
    data?.jobs.filter(
      (item) =>
        (!job || job === item.job_identity) &&
        ["failed", "outcome_unknown"].includes(item.state),
    ) ?? [];
  return (
    <section className="panel">
      <h2>CPI 持久 Job 异常</h2>
      <p>
        {failure
          ? `UNKNOWN · ${failure}`
          : data
            ? `${data.evidence_mode} · ${data.status} · ${data.classification ?? "持久记录快照"}`
            : "正在回查持久记录…"}{" "}
        · 记录不证明 Worker 当前在线。
      </p>
      {alerts.map((item) => (
        <div className="version" key={item.job_identity}>
          <p>
            {item.job_identity} · {item.state} ·{" "}
            {item.classification ?? "UNKNOWN"} · {item.updated_at}
          </p>
          <p>
            Attempt {item.attempt_identity ?? "无"} · Worker{" "}
            {item.worker_identity ?? "UNKNOWN"} · 核实{" "}
            {item.reconciliation ?? "尚无"}
          </p>
          <a
            href={`#page=4&tab=executions&asset=${encodeURIComponent(item.asset_identity)}&asset_revision=${encodeURIComponent(item.asset_revision)}&job=${encodeURIComponent(item.job_identity)}`}
          >
            05 回查同一 Job / Attempt
          </a>
        </div>
      ))}
      {data?.status === "OK" && !alerts.length && (
        <p>此快照无 failed / outcome_unknown 记录；不表示全平台无故障。</p>
      )}
    </section>
  );
}
