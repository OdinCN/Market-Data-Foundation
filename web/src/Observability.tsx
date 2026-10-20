import { useEffect, useState } from "react";
import {
  ApiFailure,
  request,
  type ObjectRef,
  type Observation,
  type Observability,
  type Transport,
} from "./api";

type Tab = "instances" | "storage" | "diagnostics";
const tabs: [Tab, string][] = [
  ["instances", "运行实例"],
  ["storage", "数据库与存储"],
  ["diagnostics", "日志与诊断"],
];
function initialTab(): Tab {
  const value = new URLSearchParams(location.hash.slice(1)).get("tab");
  return tabs.some(([key]) => key === value) ? (value as Tab) : "instances";
}
type Snapshot = {
  data: Observability;
  receivedAt: number;
  checkedAt: number | null;
  lifetimes: Map<Observation, number>;
};
export function monotonicNow() {
  try {
    return performance.now();
  } catch {
    return NaN;
  }
}
function receive(data: Observability): Snapshot {
  const receivedAt = monotonicNow();
  const wall = Date.now();
  const completion = Date.parse(data.generated_at);
  const maxAge = data.max_age_seconds * 1000;
  const trusted =
    Number.isFinite(receivedAt) &&
    receivedAt >= 0 &&
    Number.isFinite(wall) &&
    Number.isFinite(completion) &&
    wall >= completion &&
    Number.isFinite(maxAge) &&
    maxAge > 0 &&
    maxAge <= 60000 &&
    wall - completion <= maxAge;
  const lifetimes = new Map<Observation, number>();
  // Validate wall time once, at successful receipt. Later checks only consume
  // these bounded lifetimes; wall-clock corrections cannot restore evidence.
  if (trusted) {
    for (const stamp of [
      data.evidence_observation,
      data.resources.cpu_load.observation,
      data.resources.memory.observation,
      ...data.resources.disks.map((d) => d.observation),
      ...data.resources.networks.map((n) => n.observation),
      ...data.instances.map((i) => i.observation),
      ...data.storage.map((s) => s.observation),
      ...data.diagnostics.map((d) => d.observation),
    ]) {
      const sampled = Date.parse(stamp.sampled_at ?? "");
      if (
        stamp.status === "OK" &&
        Number.isFinite(sampled) &&
        sampled <= completion &&
        wall - sampled <= maxAge
      )
        lifetimes.set(stamp, maxAge - (wall - sampled));
    }
  }
  return {
    data,
    receivedAt,
    checkedAt: trusted ? receivedAt : null,
    lifetimes,
  };
}
function current(stamp: Observation, snapshot: Snapshot) {
  return (
    snapshot.checkedAt !== null &&
    snapshot.checkedAt - snapshot.receivedAt <=
      (snapshot.lifetimes.get(stamp) ?? -1)
  );
}
function Stamp({ stamp, valid }: { stamp: Observation; valid: boolean }) {
  return (
    <p className="muted">
      {valid ? "OBSERVED" : "UNKNOWN"} · {stamp.classification}
      {stamp.status === "OK" && !valid && " · stale_or_clock_invalid"}
      <br />
      来源 {stamp.source ?? "UNKNOWN"} · {stamp.clock} ·{" "}
      {stamp.sampled_at ?? "UNKNOWN"}
    </p>
  );
}
export function ObservabilityView({
  transport,
  generation,
  page,
  onNavigate,
}: {
  transport: Transport;
  generation: number;
  page: number;
  onNavigate: (page: number, ref: ObjectRef) => void;
}) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const data = snapshot?.data ?? null;
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [tab, setTab] = useState<Tab>(initialTab);
  const [filter, setFilter] = useState("");
  const [state, setState] = useState("ALL");
  const [module, setModule] = useState("ALL");
  const [diagnostic, setDiagnostic] = useState(
    () => new URLSearchParams(location.hash.slice(1)).get("diagnostic") ?? "",
  );
  const [job, setJob] = useState(
    () => new URLSearchParams(location.hash.slice(1)).get("job") ?? "",
  );
  useEffect(() => {
    const update = () => {
      setTab(initialTab());
      setJob(new URLSearchParams(location.hash.slice(1)).get("job") ?? "");
      setDiagnostic(
        new URLSearchParams(location.hash.slice(1)).get("diagnostic") ?? "",
      );
      setFilter("");
      setState("ALL");
      setModule("ALL");
    };
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setSnapshot(null);
    setFailure(null);
    setBusy(true);
    request(transport, "/api/r5/observability", undefined, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setSnapshot(receive(value));
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted)
          setFailure(
            error instanceof ApiFailure
              ? error.classification
              : "request_failed",
          );
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [transport, generation, page]);
  useEffect(() => {
    // Local expiry only. No background requests or runtime workers.
    const timer = window.setInterval(() => {
      const now = monotonicNow();
      setSnapshot((previous) => {
        if (!previous || previous.checkedAt === null) return previous;
        // A nonfinite or regressing monotonic clock invalidates this snapshot
        // permanently, including regressions that stay above receipt time.
        return {
          ...previous,
          checkedAt:
            Number.isFinite(now) && now >= previous.checkedAt ? now : null,
        };
      });
    }, 1000);
    return () => window.clearInterval(timer);
  }, []);
  function selectTab(next: Tab, jobIdentity = "") {
    setTab(next);
    setJob(jobIdentity);
    setDiagnostic("");
    setFilter("");
    setState("ALL");
    setModule("ALL");
    const hash = new URLSearchParams(location.hash.slice(1));
    hash.set("tab", next);
    hash.delete("diagnostic");
    if (jobIdentity) hash.set("job", jobIdentity);
    else hash.delete("job");
    history.replaceState(null, "", `#${hash}`);
  }
  const valid = (stamp: Observation) =>
    Boolean(snapshot && current(stamp, snapshot));
  const recordValid = (stamp: Observation) =>
    Boolean(data && valid(data.evidence_observation) && valid(stamp));
  const refs = (ref: ObjectRef) => (
    <>
      {ref.operation_identity && <code>{ref.operation_identity}</code>}
      {ref.asset_identity && (
        <button type="button" onClick={() => onNavigate(3, ref)}>
          查看关联资产 {ref.asset_identity}@{ref.asset_revision}
        </button>
      )}
      {ref.version_identity && <p>Version {ref.version_identity}</p>}
      {ref.publication_identity && (
        <p>Publication {ref.publication_identity}</p>
      )}
      {ref.job_identity && (
        <p>
          Job <code>{ref.job_identity}</code> / Attempt{" "}
          <code>{ref.attempt_identity ?? "UNKNOWN"}</code>
          <br />
          05 具名执行查询尚未实现
        </p>
      )}
    </>
  );
  const matches = (value: unknown) =>
    JSON.stringify(value).toLowerCase().includes(filter.toLowerCase());
  const diagnostics = (data?.diagnostics ?? []).filter(
    (d) =>
      matches(d) &&
      (state === "ALL" || d.severity === state) &&
      (module === "ALL" || d.module === module) &&
      (!diagnostic || d.identity === diagnostic) &&
      (!job || d.related.job_identity === job),
  );
  const alerts = (data?.diagnostics ?? []).filter(
    (d) => recordValid(d.observation) && d.severity !== "INFO",
  );
  const storage = (
    <div className="storage-grid">
      {(
        data?.storage ??
        ["postgresql", "garage", "questdb"].map((identity) => ({
          identity,
          state: identity === "questdb" ? "DEFER" : "UNKNOWN",
          qualification: "UNKNOWN",
          instance_identity: null,
          qualification_identity: null,
          observation: null,
        }))
      )
        .filter((s) => page === 0 || matches(s))
        .map((s) => (
          <section className="panel" key={s.identity}>
            <h2>
              {s.identity === "postgresql"
                ? "PostgreSQL"
                : s.identity === "garage"
                  ? "Garage"
                  : "QuestDB"}
            </h2>
            <p className="chip">
              {s.identity === "questdb"
                ? "DEFER"
                : s.observation &&
                    recordValid(s.observation) &&
                    s.qualification === "QUALIFIED_READ_ONLY"
                  ? s.state
                  : "UNKNOWN"}
            </p>
            <p>资格 {s.qualification}</p>
            <p className="muted">
              实例 {s.instance_identity ?? "UNKNOWN"}
              <br />
              资格依据 {s.qualification_identity ?? "UNKNOWN"}
            </p>
            {s.observation && (
              <Stamp stamp={s.observation} valid={recordValid(s.observation)} />
            )}
            <p className="muted">只读状态注入；无连接测试、配置或初始化操作</p>
          </section>
        ))}
    </div>
  );
  return (
    <div aria-label="运行观测">
      <p role="status">
        {busy
          ? "正在核实运行观测…"
          : failure
            ? `UNKNOWN · ${failure}`
            : `${data?.evidence_mode ?? "UNKNOWN"} · ${data && valid(data.evidence_observation) ? data.status : "UNKNOWN"}`}
      </p>
      <p className="notice">
        仅受信 Host 显式注入的当前证据可用。CPU
        为负载；网络为累计字节，不表示使用率或吞吐。实例均
        observe-only。未配置与过期状态为 UNKNOWN。
      </p>
      {data && (
        <p className="muted">
          投影时钟 {data.generated_at} · 有效期 {data.max_age_seconds} 秒 · Host{" "}
          {data.resources.host_identity ?? "UNKNOWN"}
        </p>
      )}
      {page === 6 && (
        <div role="tablist" aria-label="平台管理页签" className="tabs actions">
          {tabs.map(([key, label]) => (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={tab === key}
              aria-controls={`panel-${key}`}
              id={`tab-${key}`}
              onClick={() => selectTab(key)}
            >
              {label}
            </button>
          ))}
        </div>
      )}
      {page === 0 && (
        <>
          <div className="resource-grid">
            <section className="panel">
              <h2>CPU 负载</h2>
              <p>
                {data && valid(data.resources.cpu_load.observation)
                  ? (data.resources.cpu_load.values?.join(" / ") ?? "UNKNOWN")
                  : "UNKNOWN"}
              </p>
              <p>runnable tasks · 1 / 5 / 15 分钟</p>
              {data && (
                <Stamp
                  stamp={data.resources.cpu_load.observation}
                  valid={valid(data.resources.cpu_load.observation)}
                />
              )}
            </section>
            <section className="panel">
              <h2>内存</h2>
              <p>
                {data && valid(data.resources.memory.observation)
                  ? `${data.resources.memory.available ?? "UNKNOWN"} / ${data.resources.memory.total ?? "UNKNOWN"}`
                  : "UNKNOWN"}
              </p>
              <p>可用 / 总量 · bytes</p>
              {data && (
                <Stamp
                  stamp={data.resources.memory.observation}
                  valid={valid(data.resources.memory.observation)}
                />
              )}
            </section>
            <section className="panel">
              <h2>磁盘</h2>
              {data?.resources.disks.length ? (
                data.resources.disks.map((d) => (
                  <div key={d.mount}>
                    <p>
                      {d.mount} ·{" "}
                      {valid(d.observation)
                        ? `${d.available ?? "UNKNOWN"} / ${d.total ?? "UNKNOWN"} bytes`
                        : "UNKNOWN"}
                    </p>
                    <p>可用 / 总量</p>
                    <Stamp stamp={d.observation} valid={valid(d.observation)} />
                  </div>
                ))
              ) : (
                <p>UNKNOWN · root</p>
              )}
            </section>
            <section className="panel">
              <h2>网络 I/O</h2>
              {data?.resources.networks.length ? (
                data.resources.networks.map((n) => (
                  <div key={n.interface}>
                    <p>
                      {n.interface} ·{" "}
                      {valid(n.observation)
                        ? `接收 ${n.received ?? "UNKNOWN"} / 发送 ${n.transmitted ?? "UNKNOWN"} cumulative bytes`
                        : "UNKNOWN"}
                    </p>
                    <Stamp stamp={n.observation} valid={valid(n.observation)} />
                  </div>
                ))
              ) : (
                <p>UNKNOWN · 未注入具名网卡</p>
              )}
            </section>
          </div>
          {storage}
          <div className="detail-grid">
            <section className="panel">
              <h2>待处理异常</h2>
              {alerts.length ? (
                alerts.map((d) => (
                  <div className="version" key={d.identity}>
                    <p>
                      {d.severity} · {d.classification} · {d.module}
                    </p>
                    {refs(d.related)}
                    <Stamp
                      stamp={d.observation}
                      valid={recordValid(d.observation)}
                    />
                    <button
                      type="button"
                      onClick={() => {
                        const hash = new URLSearchParams({
                          page: "6",
                          tab: "diagnostics",
                          diagnostic: d.identity,
                        });
                        if (d.related.job_identity)
                          hash.set("job", d.related.job_identity);
                        location.hash = hash.toString();
                      }}
                    >
                      查看同一诊断 {d.identity}
                    </button>
                  </div>
                ))
              ) : (
                <p>UNKNOWN · 无当前受信异常记录，不代表无故障</p>
              )}
            </section>
            <section className="panel">
              <h2>模块健康度</h2>
              <p className="muted">
                模块聚合资格尚未建立；具名实例不代表整个模块健康。
              </p>
              {[
                "接入管理",
                "接口注册",
                "数据资产",
                "采集任务",
                "数据服务",
                "平台管理",
              ].map((name) => (
                <p key={name}>{name} · UNKNOWN</p>
              ))}
            </section>
          </div>
        </>
      )}
      {page === 6 && (
        <section
          role="tabpanel"
          id={`panel-${tab}`}
          aria-labelledby={`tab-${tab}`}
        >
          <div className="filters panel">
            <label>
              筛选具名记录
              <input
                aria-label="筛选具名记录"
                maxLength={256}
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              />
            </label>
            {tab === "instances" && (
              <label>
                实例状态
                <select
                  aria-label="实例状态"
                  value={state}
                  onChange={(e) => setState(e.target.value)}
                >
                  {["ALL", "RUNNING", "STOPPED", "DEGRADED", "UNKNOWN"].map(
                    (s) => (
                      <option key={s}>{s}</option>
                    ),
                  )}
                </select>
              </label>
            )}
            {tab === "diagnostics" && (
              <>
                <label>
                  严重程度
                  <select
                    aria-label="严重程度"
                    value={state}
                    onChange={(e) => setState(e.target.value)}
                  >
                    {["ALL", "INFO", "WARNING", "ERROR"].map((s) => (
                      <option key={s}>{s}</option>
                    ))}
                  </select>
                </label>
                <label>
                  诊断模块
                  <select
                    aria-label="诊断模块"
                    value={module}
                    onChange={(e) => setModule(e.target.value)}
                  >
                    {[
                      "ALL",
                      "runtime",
                      "source",
                      "asset",
                      "service",
                      "storage",
                      "job",
                    ].map((s) => (
                      <option key={s}>{s}</option>
                    ))}
                  </select>
                </label>
                <label>
                  Job 身份
                  <input
                    aria-label="Job 身份"
                    maxLength={256}
                    value={job}
                    onChange={(e) => setJob(e.target.value)}
                  />
                </label>
              </>
            )}
          </div>
          {tab === "storage" && storage}
          {tab === "instances" && (
            <section className="panel">
              <h2>具名运行实例</h2>
              <p className="muted">
                仅显示受信实例身份；不从进程名或组件交付推断在线。
              </p>
              {(data?.instances ?? [])
                .filter(
                  (i) =>
                    matches(i) &&
                    (state === "ALL" ||
                      (recordValid(i.observation) ? i.state : "UNKNOWN") ===
                        state),
                )
                .map((i) => (
                  <div className="version" key={i.instance_identity}>
                    <h3>{i.instance_identity}</h3>
                    <code>{i.component_identity}</code>
                    <p>
                      {recordValid(i.observation) ? i.state : "UNKNOWN"} ·
                      observe-only
                    </p>
                    <p>
                      启动{" "}
                      {recordValid(i.observation)
                        ? (i.started_at ?? "UNKNOWN")
                        : "UNKNOWN"}{" "}
                      / 心跳{" "}
                      {recordValid(i.observation)
                        ? (i.heartbeat_at ?? "UNKNOWN")
                        : "UNKNOWN"}
                    </p>
                    <Stamp
                      stamp={i.observation}
                      valid={recordValid(i.observation)}
                    />
                    {refs(i.related)}
                  </div>
                ))}
              {!(data?.instances ?? []).some(
                (i) =>
                  matches(i) &&
                  (state === "ALL" ||
                    (recordValid(i.observation) ? i.state : "UNKNOWN") ===
                      state),
              ) && <p>UNKNOWN · 没有匹配的受信实例</p>}
            </section>
          )}
          {tab === "diagnostics" && (
            <section className="panel">
              <h2>受信结构化诊断</h2>
              <p className="muted">
                最多 100 条，仅分类与公开关联键。没有任意日志查询或原始消息。
              </p>
              {data && (
                <Stamp
                  stamp={data.evidence_observation}
                  valid={valid(data.evidence_observation)}
                />
              )}
              {diagnostic && (
                <button type="button" onClick={() => selectTab("diagnostics")}>
                  清除诊断定位 {diagnostic}
                </button>
              )}
              {diagnostics.map((d) => (
                <div className="version" key={d.identity}>
                  <code>{d.identity}</code>
                  <p>
                    {d.severity} · {d.module} · {d.classification}
                  </p>
                  <Stamp
                    stamp={d.observation}
                    valid={recordValid(d.observation)}
                  />
                  {refs(d.related)}
                </div>
              ))}
              {!diagnostics.length && (
                <p>UNKNOWN · 没有匹配的受信诊断；空态不证明无故障</p>
              )}
            </section>
          )}
        </section>
      )}
    </div>
  );
}
