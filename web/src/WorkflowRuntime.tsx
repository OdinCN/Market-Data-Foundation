import { formatBeijingTime } from "./beijingTime";
import { useEffect, useState } from "react";
import type { Jobs, Management, Observation, Observability } from "./api";
import {
  type RuntimeInstance,
  type StorageObservation,
  assetTitle,
  stateLabel,
} from "./workflowModel";
import { monotonicNow } from "./Observability";
import { useObservation } from "./workflowHooks";
import { menus, type Navigate, type Route } from "./workflowRoute";
import { Fact, Tabs, Technical, WorkflowTable } from "./WorkflowTable";
const storageRows = (data: Observability | null): StorageObservation[] => [
  {
    id: "postgresql",
    label: "MDF PostgreSQL",
    dto: data?.storage.find((s) => s.identity === "postgresql") ?? null,
  },
  {
    id: "garage",
    label: "MDF Garage",
    dto: data?.storage.find((s) => s.identity === "garage") ?? null,
  },
  { id: "filesystem", label: "文件存储", dto: null },
  {
    id: "questdb",
    label: "QuestDB",
    dto: data?.storage.find((s) => s.identity === "questdb") ?? null,
  },
];
function storageState(
  s: StorageObservation,
  valid: (o?: Observation) => boolean,
) {
  return s.id === "questdb"
    ? "暂缓 / 未启用"
    : s.dto &&
        valid(s.dto.observation) &&
        s.dto.qualification === "QUALIFIED_READ_ONLY"
      ? s.dto.state === "AVAILABLE"
        ? "只读观察可用"
        : "观察异常"
      : s.id === "filesystem"
        ? "未配置"
        : "未接入合格观测";
}
const percentage = (a?: { total: number | null; available: number | null }) =>
  a?.total && a.available !== null
    ? `${Math.round(((a.total - a.available) / a.total) * 100)}%`
    : "—";
export function OverviewPage({
  data,
  jobs,
  failure,
  navigate,
}: {
  data: Observability | null;
  jobs: Jobs | null;
  failure: string | null;
  navigate: Navigate;
}) {
  const valid = useObservation(data);
  const r = data?.resources;
  const alerts =
    data?.diagnostics.filter(
      (d) => valid(d.observation) && d.severity !== "INFO",
    ) ?? [];
  const failedJobs =
    jobs?.jobs.filter((j) => ["failed", "outcome_unknown"].includes(j.state)) ??
    [];
  const network = r?.networks.filter((n) => valid(n.observation)) ?? [];
  return (
    <div className="overview">
      <section className="panel">
        <div className="toolbar">
          <h2>系统资源监控</h2>
          <small>本机与数据库资源 · 受信只读观测</small>
        </div>
        <p className="small">本机资源</p>
        <div className="resource-grid">
          {[
            [
              "CPU",
              r && valid(r.cpu_load.observation)
                ? r.cpu_load.values?.join(" / ")
                : "—",
              "1 / 5 / 15 分钟负载",
            ],
            [
              "内存",
              r && valid(r.memory.observation) ? percentage(r.memory) : "—",
              "已使用比例",
            ],
            [
              "磁盘",
              r?.disks[0] && valid(r.disks[0].observation)
                ? percentage(r.disks[0])
                : "—",
              "root 挂载点",
            ],
            [
              "网络 I/O",
              network.length
                ? network
                    .map((n) => `接收 ${n.received} / 发送 ${n.transmitted}`)
                    .join(" · ")
                : "—",
              "累计字节；不是瞬时速率",
            ],
          ].map(([label, value, unit]) => (
            <div className="metric" key={label}>
              <p>{label}</p>
              <strong>{value}</strong>
              <small>{value === "—" ? "未接入观测 / 已过期" : unit}</small>
              <div className="metric-track" />
            </div>
          ))}
        </div>
        <p className="small">数据库与存储资源</p>
        <div className="storage-grid">
          {storageRows(data)
            .filter((s) => s.id !== "filesystem")
            .map((s) => (
              <button
                className="storage-card"
                key={s.id}
                onClick={() =>
                  navigate("platform", { tab: "storage", instance: s.id })
                }
              >
                <b>{s.label}</b>
                <span className="chip">{storageState(s, valid)}</span>
                <small>
                  {s.id === "garage"
                    ? "对象存储 · 不可变原始材料"
                    : s.id === "questdb"
                      ? "时序数据库 · 非首版前置"
                      : "数据库 · 管理与数据记录"}
                </small>
              </button>
            ))}
        </div>
        <Technical>
          <p>{failure ?? data?.evidence_observation.classification}</p>
          <p>
            采样主机：{r?.host_identity ?? "未绑定"} ·{" "}
            {formatBeijingTime(data?.generated_at)}
          </p>
          <p>
            {data?.evidence_observation.source} ·{" "}
            {data?.evidence_observation.classification}
          </p>
          {network.map((n) => (
            <p key={n.interface}>
              {n.interface} · {n.unit} ·{" "}
              {formatBeijingTime(n.observation.sampled_at)}
            </p>
          ))}
        </Technical>
      </section>
      <div className="bottom-panels">
        <section className="panel">
          <h2>待处理异常</h2>
          {!alerts.length && !failedJobs.length && (
            <p className="empty">
              {!data || !valid(data.evidence_observation)
                ? "未接入当前受信异常观测；状态待确认。"
                : "当前受信范围暂无异常记录。"}
            </p>
          )}
          {failedJobs.map((j) => (
            <div className="alert-row" key={j.job_identity}>
              <b>采集{stateLabel(j.state)}</b>
              <p>
                {assetTitle({
                  identity: j.asset_identity,
                  revision: j.asset_revision,
                })}{" "}
                · {j.reference}
              </p>
              <button
                onClick={() =>
                  navigate("collection", {
                    tab: "executions",
                    job: j.job_identity,
                    asset: j.asset_identity,
                    asset_revision: j.asset_revision,
                  })
                }
              >
                查看执行 {j.job_identity} →
              </button>
            </div>
          ))}
          {alerts.map((d) => (
            <div className="alert-row" key={d.identity}>
              <b>{d.module === "job" ? "采集执行异常" : "模块观察异常"}</b>
              <p>{d.related.job_identity ?? d.identity}</p>
              <button
                onClick={() =>
                  navigate("platform", {
                    tab: "diagnostics",
                    diagnostic: d.identity,
                    job: d.related.job_identity,
                    asset: d.related.asset_identity,
                    asset_revision: d.related.asset_revision,
                    operation: d.related.operation_identity,
                  })
                }
              >
                查看同一诊断 {d.identity}
              </button>
            </div>
          ))}
        </section>
        <section className="panel">
          <h2>模块健康度</h2>
          {menus
            .filter(([view]) => view !== "overview")
            .map(([, name]) => (
              <div className="health-row" key={name}>
                <span>{name}</span>
                <span className="chip">未接入聚合观测</span>
              </div>
            ))}
        </section>
      </div>
    </div>
  );
}
export function PlatformPage({
  data,
  management,
  instances,
  route,
  navigate,
  failure,
  requestedAt,
}: {
  data: Observability | null;
  requestedAt: number;
  management: Management | null;
  instances: RuntimeInstance[];
  route: Route;
  navigate: Navigate;
  failure: string | null;
}) {
  const tab = ["instances", "storage", "diagnostics"].includes(
    route.fields.get("tab") ?? "",
  )
    ? route.fields.get("tab")!
    : "instances";
  const [live, setLive] = useState(new Set<string>());
  useEffect(() => {
    const start = requestedAt;
    let last = start;
    const lifetimes = instances.map(
      (i) =>
        [i.id, i.dto.freshness === "FRESH" ? i.dto.valid_for_ms : 0] as const,
    );
    const update = () => {
      const now = monotonicNow();
      const elapsed = now - start;
      const okay =
        Number.isFinite(now) && Number.isFinite(start) && now >= last;
      last = okay ? now : Infinity;
      setLive(
        new Set(
          lifetimes
            .filter(([, ms]) => okay && ms > 0 && ms <= 60000 && elapsed < ms)
            .map(([id]) => id),
        ),
      );
    };
    update();
    const timer = setInterval(update, 1000);
    return () => clearInterval(timer);
  }, [management, requestedAt]);
  const valid = useObservation(data);
  const store = storageRows(data);
  const selected = route.fields.get("instance");
  const instance = instances.find((i) => i.id === selected);
  const storage = store.find((s) => s.id === selected);
  const diagnostics = (data?.diagnostics ?? []).filter(
    (d) =>
      (!route.fields.get("job") ||
        d.related.job_identity === route.fields.get("job")) &&
      (!route.fields.get("diagnostic") ||
        d.identity === route.fields.get("diagnostic")),
  );
  const diagnostic = diagnostics.find(
    (d) => d.identity === route.fields.get("diagnostic"),
  );
  return (
    <>
      <Tabs
        label="平台管理页签"
        values={[
          ["instances", "运行实例"],
          ["storage", "数据库与存储"],
          ["diagnostics", "日志与诊断"],
        ]}
        value={tab}
        onChange={(tab) =>
          navigate("platform", { tab, instance: null, diagnostic: null }, true)
        }
      />
      {failure && (
        <div role="status">
          平台读取暂不可用。<Technical>{failure}</Technical>
        </div>
      )}
      {tab === "instances" ? (
        <>
          <div className="toolbar">
            <h2>运行实例</h2>
            <small>
              MDF API · TDX Driver · KD 外部实例（预期类型，不代表已登记）
            </small>
          </div>
          <WorkflowTable
            label="运行实例"
            columns={["实例名称", "实例类型", "管理归属", "状态"]}
            selected={instance?.id}
            rows={instances.map((i) => ({
              id: i.id,
              search: `${i.id} ${i.label} ${i.dto.record.ownership}`,
              cells: [
                i.id,
                i.label,
                i.dto.record.ownership === "OWNER_MANAGED"
                  ? "Owner 独立管理"
                  : "MDF / 只观察",
                live.has(i.id)
                  ? i.dto.effective_state === "AVAILABLE"
                    ? "观察可用"
                    : i.dto.effective_state === "UNAVAILABLE"
                      ? "观察不可用"
                      : "状态待确认"
                  : "观测待确认 / 已过期",
              ],
            }))}
            empty={
              management?.instance_scope === "UNCONFIGURED"
                ? "实例读取范围未配置，当前没有已登记记录。"
                : "当前没有匹配的受信实例记录。"
            }
            onSelect={(instance) => navigate("platform", { instance }, true)}
          >
            {instance && (
              <>
                <h3>{instance.label}</h3>
                <dl>
                  <Fact name="实例身份">{instance.id}</Fact>
                  <Fact name="管理归属">
                    {instance.dto.record.ownership === "OWNER_MANAGED"
                      ? "Owner 独立管理；仅观察"
                      : "按已登记归属观察"}
                  </Fact>
                  <Fact name="端点引用">
                    {instance.dto.record.endpoint_ref ?? "未配置"}
                  </Fact>
                  <Fact name="最近观察">
                    {formatBeijingTime(instance.dto.record.observed_at)}
                  </Fact>
                  <Fact name="状态">
                    {live.has(instance.id)
                      ? instance.dto.effective_state
                      : "观测待确认 / 已过期"}
                  </Fact>
                </dl>
                <button
                  onClick={() =>
                    navigate("access", {
                      operation: instance.dto.record.catalog_identity,
                    })
                  }
                >
                  查看来源合同 →
                </button>
                <Technical>
                  <p>{instance.dto.record.catalog_identity}</p>
                  <p>
                    {instance.dto.classification} · {management?.evidence_mode}
                  </p>
                </Technical>
              </>
            )}
          </WorkflowTable>
        </>
      ) : tab === "storage" ? (
        <WorkflowTable
          label="数据库与存储"
          columns={["对象名称", "存储职责", "状态"]}
          rows={store.map((s) => ({
            id: s.id,
            search: s.label,
            cells: [
              s.label,
              s.id === "postgresql"
                ? "持久数据库"
                : s.id === "garage"
                  ? "原始材料 / S3"
                  : s.id === "filesystem"
                    ? "受控目录"
                    : "时序数据库",
              storageState(s, valid),
            ],
          }))}
          selected={storage?.id}
          onSelect={(instance) => navigate("platform", { instance }, true)}
        >
          {storage && (
            <>
              <h3>{storage.label}</h3>
              <dl>
                <Fact name="当前状态">{storageState(storage, valid)}</Fact>
                <Fact name="实例引用">
                  {storage.dto?.instance_identity ?? "未提供"}
                </Fact>
                <Fact name="观察时间">
                  {formatBeijingTime(
                    storage.dto?.observation.sampled_at,
                    "未接入观测",
                  )}
                </Fact>
                <Fact name="可用操作">
                  只读观察；没有连接测试或数据库初始化处理器
                </Fact>
              </dl>
              <Technical>
                <p>
                  {storage.dto?.qualification} ·{" "}
                  {storage.dto?.qualification_identity}
                </p>
                <p>{storage.dto?.observation.classification}</p>
              </Technical>
            </>
          )}
        </WorkflowTable>
      ) : (
        <>
          <div className="toolbar">
            <h2>日志与诊断</h2>
            <small>结构化分类与公开关联身份</small>
            {route.fields.has("job") && (
              <button
                onClick={() =>
                  navigate("platform", { job: null, diagnostic: null }, true)
                }
              >
                清除任务筛选
              </button>
            )}
          </div>
          <WorkflowTable
            label="结构化诊断"
            columns={["记录", "模块", "级别", "观测"]}
            selected={diagnostic?.identity}
            rows={diagnostics.map((d) => ({
              id: d.identity,
              search: `${d.identity} ${d.module} ${d.classification} ${d.related.job_identity ?? ""}`,
              cells: [
                d.identity,
                d.module,
                d.severity,
                valid(d.observation) ? "已观察" : "已过期 / 待确认",
              ],
            }))}
            empty="当前范围没有受信诊断记录；统一原始日志检索尚未接入。"
            onSelect={(diagnostic) =>
              navigate("platform", { diagnostic }, true)
            }
          >
            {diagnostic && (
              <>
                <h3>诊断详情</h3>
                <dl>
                  <Fact name="记录身份">{diagnostic.identity}</Fact>
                  <Fact name="观察时间">
                    {formatBeijingTime(diagnostic.observation.sampled_at)}
                  </Fact>
                  <Fact name="Job / Attempt">
                    {diagnostic.related.job_identity ?? "无"} /{" "}
                    {diagnostic.related.attempt_identity ?? "无"}
                  </Fact>
                </dl>
                {diagnostic.related.asset_identity && (
                  <button
                    onClick={() =>
                      navigate("assets", {
                        asset: diagnostic.related.asset_identity,
                        asset_revision: diagnostic.related.asset_revision,
                        operation: diagnostic.related.operation_identity,
                      })
                    }
                  >
                    查看关联资产 →
                  </button>
                )}
                {diagnostic.related.job_identity && (
                  <button
                    onClick={() =>
                      navigate("collection", {
                        tab: "executions",
                        job: diagnostic.related.job_identity,
                        asset: diagnostic.related.asset_identity,
                        asset_revision: diagnostic.related.asset_revision,
                      })
                    }
                  >
                    查看同一执行 →
                  </button>
                )}
                <Technical>
                  <p>
                    {diagnostic.module} · {diagnostic.classification} ·{" "}
                    {diagnostic.severity}
                  </p>
                </Technical>
              </>
            )}
          </WorkflowTable>
        </>
      )}
    </>
  );
}
