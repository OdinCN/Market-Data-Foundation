import type { Directory, DirectoryEntry } from "./api";

export function SourceDirectory({
  directory,
  failure,
  busy,
  page,
  selected,
  missing,
  onSelect,
  onNavigate,
}: {
  directory: Directory | null;
  failure: string | null;
  busy: boolean;
  page: number;
  selected: DirectoryEntry | undefined;
  missing: boolean;
  onSelect: (entry: DirectoryEntry) => void;
  onNavigate: (page: number, entry: DirectoryEntry) => void;
}) {
  return (
    <section className="panel" aria-label="来源合同目录">
      <div className="panel-title">
        <h2>
          {page === 1 ? "来源 / 接入组件" : "Source / Capability / Operation"}
        </h2>
        <span className="chip">只读代码目录</span>
      </div>
      <p className="muted">
        代码交付、来源资格、当前绑定、运行观测分别核实。合同与历史资格不证明在线、已绑定或已持有。
        实时 / Socket / WS 通用接口未实现。QMT 未物化。
      </p>
      <p role="status">
        {busy
          ? "正在读取代码合同目录…"
          : failure
            ? `UNKNOWN · 目录不可核实 · ${failure}`
            : directory?.evidence_mode}
      </p>
      {missing && (
        <p role="alert">关联对象缺失：未找到请求的具名来源操作或资产。</p>
      )}
      {directory && page !== 5 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>来源 / 组件</th>
                <th>具名能力 / 操作</th>
                <th>代码交付 / 合同范围</th>
                <th>来源资格 / 当前绑定 / 运行观测</th>
              </tr>
            </thead>
            <tbody>
              {directory.entries.map((entry) => (
                <tr
                  key={entry.identity}
                  className={
                    selected?.identity === entry.identity ? "selected" : ""
                  }
                >
                  <td>
                    {entry.source_name} / {entry.domain}
                    <br />
                    <code>{entry.component_identity}</code>
                  </td>
                  <td>
                    <button
                      type="button"
                      onClick={() => onSelect(entry)}
                      aria-label={`选择 ${entry.operation} ${entry.identity}`}
                    >
                      {entry.operation}
                    </button>
                    <br />
                    <code>{entry.capability_identity}</code>
                  </td>
                  <td>
                    {entry.code_delivery}
                    <br />
                    {entry.contract_disposition}
                  </td>
                  <td>
                    {entry.source_qualification} / {entry.current_binding} /{" "}
                    {entry.runtime_observation}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {selected && (
        <section aria-label="具名操作详情" className="version">
          <h3>{selected.operation}</h3>
          <dl>
            <dt>代码交付 / 合同范围</dt>
            <dd>
              {selected.code_delivery} / {selected.contract_disposition}
            </dd>
            <dt>目录身份</dt>
            <dd data-testid="operation-identity">
              <code>{selected.identity}</code>
            </dd>
            <dt>来源 / 组件</dt>
            <dd>
              {selected.source_name} /{" "}
              <code>{selected.component_identity}</code>
            </dd>
            <dt>Capability</dt>
            <dd>
              <code>{selected.capability_identity}</code>
            </dd>
            <dt>关联 Definition</dt>
            <dd data-testid="directory-asset-identity">
              {selected.asset
                ? `${selected.asset.identity}@${selected.asset.revision}`
                : "无已 admitted Definition"}
            </dd>
            <dt>来源语义限定（非资格）</dt>
            <dd>{String(selected.source_qualified_contract ?? "UNKNOWN")}</dd>
            <dt>Source 合同身份</dt>
            <dd>
              {selected.source_contract
                ? `${selected.source_contract.identity}@${selected.source_contract.revision}`
                : "缺少 SourceOperationRef；候选不能替代 fresh descriptor"}
            </dd>
            <dt>构建 / Registry / Projection</dt>
            <dd>
              {selected.source_contract?.build_revision ?? "UNKNOWN"} /{" "}
              {selected.source_contract?.registry_revision ?? "UNKNOWN"} /{" "}
              {selected.source_contract?.projection_revision ?? "UNKNOWN"}
            </dd>
            <dt>来源变体</dt>
            <dd>{selected.source_contract?.variant ?? "UNKNOWN"}</dd>
            <dt>代码 Binding 候选</dt>
            <dd>
              {selected.binding_contract
                ? `${selected.binding_contract.identity}@${selected.binding_contract.revision}`
                : "未提供候选 Binding revision"}
            </dd>
            <dt>来源资格 / 当前绑定 / 运行观测</dt>
            <dd>UNKNOWN / UNKNOWN / UNKNOWN</dd>
            <dt>本地持有</dt>
            <dd>UNKNOWN（代码目录无持有证据）</dd>
            <dt>Web 读取支持</dt>
            <dd>{selected.web_read}</dd>
          </dl>
          {selected.source_contract && (
            <dl>
              <dt>采集模式</dt>
              <dd>{selected.source_contract.acquisition_mode}</dd>
              <dt>时间语义</dt>
              <dd>{selected.source_contract.temporal_contract}</dd>
              <dt>完整性边界</dt>
              <dd>{selected.source_contract.completeness_contract}</dd>
              <dt>分页 / 新鲜度合同</dt>
              <dd>
                {selected.source_contract.pagination_contract} /{" "}
                {selected.source_contract.freshness_contract}
              </dd>
              <dt>证据能力声明（非观测）</dt>
              <dd>{selected.source_contract.evidence_capability.join(", ")}</dd>
            </dl>
          )}
          {selected.binding_contract && (
            <>
              <p>
                兼容策略：{selected.binding_contract.compatibility_policy}
                ；禁止静默回退。
              </p>
              {selected.binding_contract.mappings.map((mapping) => (
                <p key={mapping.identity}>
                  Mapping {mapping.identity} → {mapping.definition.identity}@
                  {mapping.definition.revision}；语义{" "}
                  {mapping.semantic_revision}；时间 {mapping.temporal_revision}
                  ；证据要求 {mapping.evidence_grades.join(", ")}；durable=
                  {String(mapping.durable_required)} / replayable=
                  {String(mapping.replayable_required)}（要求不代表已满足）
                </p>
              ))}
              <ul>
                {selected.binding_contract.limitations.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            </>
          )}
          <ul>
            {selected.reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
          <p className="muted">
            代码证据：{selected.evidence_refs.join(" · ")}
          </p>
          {(selected.web_read !== "CPI_REQUEST_QUALIFIED" || page === 1) && (
            <div className="actions">
              {page !== 3 && (
                <button
                  type="button"
                  disabled={!selected.asset}
                  onClick={() => onNavigate(3, selected)}
                >
                  查看关联资产
                </button>
              )}
              {page === 3 && (
                <button
                  type="button"
                  disabled={selected.web_read !== "CPI_REQUEST_QUALIFIED"}
                  onClick={() => onNavigate(5, selected)}
                >
                  查看数据服务
                </button>
              )}
            </div>
          )}
          {!selected.asset && (
            <p>没有已 admitted 关联资产；不创建虚构 Definition。</p>
          )}
          {selected.web_read !== "CPI_REQUEST_QUALIFIED" && (
            <p>
              没有满足当前条件的数据服务。该具名合同未接入通用 Web reader；独立
              Macro / TDX 合同与 KD 候选不能冒充已开放服务。
            </p>
          )}
        </section>
      )}
    </section>
  );
}
