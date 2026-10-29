import { formatBeijingTime } from "./beijingTime";
import { useEffect, useState } from "react";
import { ApiFailure, request, type Management, type Transport } from "./api";

const ownership = {
  MDF_MANAGED: "MDF 管理",
  OWNER_MANAGED: "Owner 独立管理",
  OBSERVE_ONLY: "只观察",
};

export function ManagementConsole({
  transport,
  generation,
  selectedIdentity,
  selectedAsset = null,
  selectedRevision = null,
  onSelect,
  onInstances,
}: {
  transport: Transport;
  generation: number;
  selectedIdentity: string | null;
  selectedAsset?: string | null;
  selectedRevision?: string | null;
  onSelect: (entry: Management["entries"][number]) => void;
  onInstances?: () => void;
}) {
  const [data, setData] = useState<Management | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [family, setFamily] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    const timers: ReturnType<typeof setTimeout>[] = [];
    const startedAt = performance.now();
    setData(null);
    setFailure(null);
    request(transport, "/api/r3/management", undefined, controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        setData(value);
        // Only degrade a trusted observation. Expiry never probes or acquires.
        for (const instance of value.instances) {
          if (instance.freshness !== "FRESH") continue;
          timers.push(
            setTimeout(
              () => {
                if (controller.signal.aborted) return;
                setData(
                  (current) =>
                    current && {
                      ...current,
                      instances: current.instances.map((item) =>
                        item.record.identity === instance.record.identity
                          ? {
                              ...item,
                              effective_state: "UNKNOWN",
                              freshness: "EXPIRED",
                              classification: "expired",
                              valid_for_ms: 0,
                            }
                          : item,
                      ),
                    },
                );
              },
              Math.max(
                0,
                instance.valid_for_ms - (performance.now() - startedAt),
              ),
            ),
          );
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted)
          setFailure(
            error instanceof ApiFailure
              ? error.classification
              : "request_failed",
          );
      });
    return () => {
      controller.abort();
      timers.forEach(clearTimeout);
    };
  }, [transport, generation]);

  const identities = data?.families.find(
    (item) => item.identity === family,
  )?.catalog_identities;
  const entries =
    data?.entries.filter(
      (entry) => !identities || identities.includes(entry.identity),
    ) ?? [];
  const selected = data?.entries.find(
    (entry) =>
      entry.identity === selectedIdentity &&
      (selectedAsset === null || entry.asset?.identity === selectedAsset) &&
      (selectedRevision === null || entry.asset?.revision === selectedRevision),
  );
  const instances =
    data?.instances.filter(
      (item) => item.record.catalog_identity === selected?.identity,
    ) ?? [];

  return (
    <section className="panel management" aria-label="A1 共用接入链">
      <div className="panel-title">
        <h2>来源 → 能力 → 实现 → 实例 → 观察</h2>
        <span className="chip">READONLY · A1</span>
      </div>
      <p className="muted">
        九族为具名代码目录；代码交付不代表当前在线或可用。PG 管理持久化待独立
        DEV 验收。
      </p>
      <p role="status">
        {failure
          ? `UNKNOWN · ${failure}`
          : data
            ? `${data.evidence_mode} · ${data.instance_scope}`
            : "正在读取共用目录…"}
      </p>
      {data && (
        <>
          <div className="family-grid" role="group" aria-label="Macro 九族">
            {data.families.map((item) => (
              <button
                type="button"
                key={item.identity}
                aria-pressed={family === item.identity}
                onClick={() =>
                  setFamily(family === item.identity ? "" : item.identity)
                }
              >
                <strong>{item.title}</strong>
                <span>
                  {item.catalog_identities.length} 合同 · {item.qualification}
                </span>
              </button>
            ))}
          </div>
          <label className="management-picker">
            选择来源与能力{family ? ` · ${family}` : " · 全部领域"}
            <select
              aria-label="选择来源与能力"
              value={
                selected &&
                entries.some((entry) => entry.identity === selected.identity)
                  ? selected.identity
                  : ""
              }
              onChange={(event) => {
                const entry = entries.find(
                  (item) => item.identity === event.target.value,
                );
                if (entry) onSelect(entry);
              }}
            >
              <option value="">请选择具名合同</option>
              {entries.map((entry) => (
                <option key={entry.identity} value={entry.identity}>
                  {entry.source_name} · {entry.operation} · {entry.identity}
                </option>
              ))}
            </select>
          </label>
          <p>
            QMT · DEFERRED · 暂缓；实例归属模式：MDF 管理 / Owner 独立管理 /
            只观察。目录不授予控制权。
          </p>
          {selectedIdentity && !selected && (
            <p role="alert">UNKNOWN · 目录身份不匹配，无法关联实例。</p>
          )}
          {selected && (
            <section className="version" aria-label="接入链详情">
              <h3>
                {selected.source_name} · {selected.operation}
              </h3>
              <dl>
                <dt>共用目录身份</dt>
                <dd>
                  <code>{selected.identity}</code>
                </dd>
                <dt>来源修订</dt>
                <dd>
                  {selected.source_contract
                    ? `${selected.source_contract.identity}@${selected.source_contract.revision}`
                    : "UNKNOWN · 候选缺少 Source 合同"}
                </dd>
                <dt>能力</dt>
                <dd>
                  <code>{selected.capability_identity}</code>
                </dd>
                <dt>Binding 修订</dt>
                <dd>
                  {selected.binding_contract
                    ? `${selected.binding_contract.identity}@${selected.binding_contract.revision}`
                    : "UNKNOWN · 无绑定合同"}
                </dd>
                <dt>实现</dt>
                <dd>
                  <code>{selected.component_identity}</code> ·{" "}
                  {selected.code_delivery}
                </dd>
                <dt>代码证据</dt>
                <dd>{selected.evidence_refs.join(" · ")}</dd>
                <dt>来源资格 / 当前绑定</dt>
                <dd>
                  {selected.source_qualification} / {selected.current_binding}
                </dd>
                <dt>运行实例</dt>
                <dd>
                  {instances.length === 0
                    ? "未登记 · UNKNOWN（当前管理读取范围无实例记录）"
                    : `${instances.length} 个已登记实例；登记不证明在线`}
                </dd>
              </dl>
              {instances.length === 0 && (
                <p>
                  归属 UNKNOWN · 端点 UNKNOWN · 观察 UNKNOWN · 观察时间
                  UNKNOWN。未执行探活。
                </p>
              )}
              {instances.map((instance) => (
                <section
                  key={instance.record.identity}
                  aria-label={`实例 ${instance.record.identity}`}
                >
                  <h4>
                    {instance.record.identity} ·{" "}
                    {ownership[instance.record.ownership]}
                  </h4>
                  <dl>
                    <dt>端点引用</dt>
                    <dd>{instance.record.endpoint_ref ?? "UNKNOWN"}</dd>
                    <dt>观察状态</dt>
                    <dd>
                      {instance.effective_state} · {instance.freshness} ·{" "}
                      {instance.classification}
                    </dd>
                    <dt>观察时间 / 过期时间</dt>
                    <dd>
                      {formatBeijingTime(
                        instance.record.observed_at,
                        "UNKNOWN",
                      )}{" "}
                      /{" "}
                      {formatBeijingTime(instance.record.expires_at, "UNKNOWN")}
                    </dd>
                  </dl>
                  <button
                    type="button"
                    disabled
                    aria-label={`控制实例 ${instance.record.identity}`}
                  >
                    实例操作 · COMING LATER
                  </button>
                </section>
              ))}
              {onInstances && (
                <button type="button" onClick={onInstances}>
                  查看同源实例记录
                </button>
              )}
            </section>
          )}
        </>
      )}
    </section>
  );
}
