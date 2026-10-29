import { formatBeijingTime } from "./beijingTime";
import { DateField } from "./DateField";
import { useEffect, useRef, useState } from "react";
import type { Transport } from "./api";
import {
  queryCpi,
  CpiQueryFailure,
  type CpiOrdinaryResult,
  type CpiOrdinaryFailure,
} from "./cpiQueryApi";
import type { RuntimeRevision } from "./packageApi";
import { sourceVariantLabel } from "./workflowModel";
import { Fact } from "./WorkflowTable";

const failureText = (failure: CpiOrdinaryFailure) => {
  const messages: Record<string, string> = {
    source_selection_required: "本地数据未能满足请求，请先选择具名来源。",
    source_binding_unconfigured: "来源查询尚未接通，本次仅能读取已保存的数据。",
    storage_unavailable: "本地存储暂不可用，本次没有转向来源。",
    content_missing: "本地内容缺失，无法确认可用结果，本次没有转向来源。",
    content_digest_mismatch: "本地内容校验未通过，本次没有转向来源。",
    query_host_not_configured: "普通查询尚未接入当前环境。",
    cpi_query_host_not_configured: "普通查询尚未接入当前环境。",
    credential_missing: "所选来源缺少凭据配置。",
    capability_disabled: "当前能力已停用，不能发起新的来源请求。",
    source_disabled: "当前能力已停用，不能发起新的来源请求。",
    read_permission_denied: "当前读取权限被拒绝，本次没有访问来源。",
    read_permission_unknown: "读取权限尚未确认，本次没有访问来源。",
    metadata_permission_denied: "当前没有查看该资产的权限。",
    metadata_permission_unknown: "资产查看权限尚未确认。",
    source_qualification_unknown: "所选来源的执行资格尚未确认。",
    source_not_qualified: "所选来源尚未取得当前执行资格。",
    source_scope_not_admitted: "所选来源不允许查询这个参考月。",
    source_reference_not_admitted: "这个参考月不在所选具名来源的合同范围内。",
    credential_unknown: "所选来源的凭据状态尚未确认。",
    source_failed: "所选来源请求失败，本次没有切换其他来源。",
    source_payload_invalid: "来源结果不完整或未通过校验，本次查询未完成。",
    source_query_failed: "所选来源请求失败，请核实后重试。",
    source_unavailable: "所选来源暂不可用，请核实后重试。",
    query_response_unavailable: "本次查询响应无法核实，请重新查询。",
  };
  return (
    messages[failure.classification] ??
    "本次查询未完成。请核对当前范围、读取权限和所选来源的执行资格。"
  );
};

export function CpiOrdinaryQuery({
  transport,
  initialReference,
  operationId,
  entries,
  readExact,
}: {
  transport: Transport;
  initialReference: string;
  operationId: string | null;
  entries: RuntimeRevision["source_entries"];
  readExact: (result: CpiOrdinaryResult) => void;
}) {
  const [reference, setReference] = useState(initialReference);
  const [selectedOperation, setSelectedOperation] = useState(operationId);
  const [result, setResult] = useState<CpiOrdinaryResult | null>(null);
  const [failure, setFailure] = useState<CpiOrdinaryFailure | null>(null);
  const [pending, setPending] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const sourceMethods = entries.filter(
    (entry) =>
      entry.source_contract &&
      entry.binding_contract &&
      entry.code_delivery === "DELIVERED",
  );
  const selected = sourceMethods.find(
    (entry) => entry.identity === selectedOperation,
  );
  const returnedSource = entries.find(
    (entry) => entry.identity === result?.operation_id,
  );
  async function submit() {
    if (request.current && !request.current.signal.aborted) return;
    const controller = new AbortController();
    request.current = controller;
    setPending(true);
    setResult(null);
    setFailure(null);
    try {
      const response = await queryCpi(
        transport,
        {
          reference,
          ...(selectedOperation ? { operation_id: selectedOperation } : {}),
        },
        controller.signal,
      );
      if (!controller.signal.aborted && request.current === controller)
        setResult(response);
    } catch (error) {
      if (!controller.signal.aborted && request.current === controller)
        setFailure(
          error instanceof CpiQueryFailure
            ? error.detail
            : {
                status: "UNKNOWN",
                classification: "query_response_unavailable",
                local_reason: null,
              },
        );
    } finally {
      if (request.current === controller) {
        request.current = null;
        setPending(false);
      }
    }
  }
  return (
    <section aria-label="普通查询" className="ordinary-query">
      <h4>查询数据</h4>
      <p className="muted">
        优先查询已保存的数据；本地无法满足时，再检查来源是否可用。
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (!event.currentTarget.checkValidity()) return;
          void submit();
        }}
      >
        <DateField
          label="查询月份"
          type="month"
          value={reference}
          onChange={(value) => {
            request.current?.abort();
            request.current = null;
            setPending(false);
            setResult(null);
            setFailure(null);
            setReference(value);
          }}
        />
        {failure?.classification === "source_selection_required" && (
          <label>
            来源与取数方式
            <select
              aria-label="来源与取数方式"
              value={selectedOperation ?? ""}
              onChange={(event) => {
                setSelectedOperation(event.target.value || null);
                setResult(null);
              }}
            >
              <option value="">请选择取数方式</option>
              {sourceMethods.map((entry) => (
                <option key={entry.identity} value={entry.identity}>
                  {entry.source_name} · {sourceVariantLabel(entry)}
                </option>
              ))}
            </select>
            <span className="small">
              同一提供方的不同取数实现，不是不同统计口径；不会自动选择或切换。
            </span>
          </label>
        )}
        {selected && (
          <p className="small">
            已指定：{selected.source_name} · {sourceVariantLabel(selected)}
          </p>
        )}
        <button
          className="primary"
          type="submit"
          disabled={pending || !/^\d{4}-(0[1-9]|1[0-2])$/.test(reference)}
        >
          查询数据
        </button>
      </form>
      {pending && <p role="status">正在查询数据…</p>}
      {failure && (
        <div role="alert">
          <p>{failureText(failure)}</p>
          <p className="small">错误代码：{failure.classification}</p>
        </div>
      )}
      {result && (
        <section aria-label="普通查询结果">
          <h4>查询结果 · {result.reference}</h4>
          <p role="status">
            {result.channel === "local"
              ? "返回渠道：本地已发布数据。"
              : "返回渠道：来源透传。本次结果未持久化。"}
          </p>
          {result.local_reason === "local_coverage_partial" && (
            <p>本地数据覆盖不全，本次返回所选来源的完整结果。</p>
          )}
          {result.local_reason === "local_freshness_unsatisfied" && (
            <p>本地数据未满足本次更新要求，本次返回所选来源的完整结果。</p>
          )}
          {result.channel === "upstream" && (
            <p>
              实际来源：
              {returnedSource
                ? `${returnedSource.source_name} · ${sourceVariantLabel(returnedSource)}`
                : result.operation_id}
            </p>
          )}
          <div className="table-wrap">
            <table aria-label="普通查询测量结果">
              <thead>
                <tr>
                  <th>指标</th>
                  <th>参考期</th>
                  <th>数值</th>
                  <th>单位</th>
                </tr>
              </thead>
              <tbody>
                {result.measurements.map((measurement) => (
                  <tr key={measurement.measure_identity}>
                    <td>{measurement.name}</td>
                    <td>{measurement.reference_period}</td>
                    <td>
                      {measurement.value ??
                        measurement.unavailable_reason ??
                        "未提供"}
                    </td>
                    <td>{measurement.value === null ? "—" : "%"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {result.channel === "local" && (
            <button type="button" onClick={() => readExact(result)}>
              精确读取本次本地版本 →
            </button>
          )}
          <details className="provenance">
            <summary>数据来源与版本</summary>
            <dl>
              <Fact name="查询时间">
                {formatBeijingTime(result.decision_at)}
              </Fact>
              <Fact name="来源获取时间">
                {formatBeijingTime(result.obtained_at)}
              </Fact>
              <Fact name="来源版本">{result.provider_vintage}</Fact>
              {result.channel === "local" && (
                <>
                  <Fact name="本地版本">{result.version_identity}</Fact>
                  <Fact name="发布">{result.publication_identity}</Fact>
                </>
              )}
            </dl>
          </details>
        </section>
      )}
    </section>
  );
}
