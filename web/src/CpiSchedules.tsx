import {
  formatBeijingTime,
  nowBeijingIso,
  toBeijingInput,
  beijingInputToIso,
} from "./beijingTime";
import { DateField } from "./DateField";
import { useEffect, useRef, useState } from "react";
import { ApiFailure, request, type Transport } from "./api";
import type { components } from "./api.generated";

type Plans = components["schemas"]["PlansDTO"];
type Intent = components["schemas"]["PlanIntent"];

export function CpiSchedules({
  transport,
  generation,
  asset,
  revision,
  mode,
}: {
  transport: Transport;
  generation: number;
  asset: string | null;
  revision: string | null;
  mode: "new" | "periodic" | "executions";
}) {
  const [data, setData] = useState<Plans | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const writing = useRef(false);
  const active = useRef(true);
  const [form, setForm] = useState({
    plan_identity: "cpi-history-example",
    name: "cpi-history-example",
    start: "2024-12",
    end: "2025-01",
    timezone: "Asia/Shanghai",
    window_start: "00:00",
    window_end: "23:59",
    first_due: nowBeijingIso(),
    interval_seconds: 86400,
    cycles: 1,
  });
  const [repair, setRepair] = useState({ start: "2024-12", end: "2024-12" });
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
    };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    setFailure(null);
    request(transport, "/api/r5/cpi/plans", undefined, controller.signal)
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
  }, [transport, generation, refresh, asset, revision]);
  const eligible =
    data?.status === "OK" &&
    data.write_eligible &&
    data.asset_identity === asset &&
    data.asset_revision === revision;
  async function write(path: string, body: object) {
    if (!eligible || writing.current) return;
    writing.current = true;
    setBusy(true);
    setNotice(null);
    try {
      const response = await transport(path, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const value = await response.json();
      if (
        !response.ok ||
        value.status !== "OK" ||
        value.acceptance !== "DURABLE"
      ) {
        throw new ApiFailure(
          typeof value.classification === "string" &&
          /^[a-z_]{1,80}$/.test(value.classification)
            ? value.classification
            : "request_failed",
        );
      }
      if (active.current)
        setNotice("意图已持久接受。执行与完成须回查批次证据。");
    } catch (error) {
      if (active.current)
        setNotice(
          `UNKNOWN · ${error instanceof ApiFailure ? error.classification : "request_failed"} · 回查后使用同一计划身份重提。`,
        );
    } finally {
      writing.current = false;
      if (active.current) {
        setBusy(false);
        setRefresh((n) => n + 1);
      }
    }
  }
  return (
    <section className="panel">
      <h2>
        {mode === "new"
          ? "CPI 多期历史采集"
          : mode === "periodic"
            ? "CPI 有界周期计划"
            : "CPI 历史批次执行记录"}
      </h2>
      <p role="status">
        {failure
          ? `UNKNOWN · ${failure}`
          : data?.plans
            ? `${data.evidence_mode} · ${data.status} · ${data.classification ?? "持久计划快照"} · ${data.tier ?? "UNKNOWN"}`
            : "UNKNOWN · 历史计划资格未取得"}
      </p>
      <p>
        固定 BLS 四条 CPI
        指数。环比使用季调序列，同比使用未季调序列。今天取得的历史值不代表首次公布
        vintage 或历史 as-of 已核实状态。每次受信 Host tick
        最多执行一个月批次；没有常驻调度器。
      </p>
      {notice && <p role="alert">{notice}</p>}
      {mode !== "executions" && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!e.currentTarget.checkValidity()) return;
            const intent: Intent = {
              ...form,
              cycles: mode === "new" ? 1 : form.cycles,
              asset_identity: asset!,
              asset_revision: revision!,
            };
            void write("/api/r5/cpi/plans", intent);
          }}
        >
          <label>
            计划身份（重提保持一致）
            <input
              required
              value={form.plan_identity}
              onChange={(e) =>
                setForm({ ...form, plan_identity: e.target.value })
              }
            />
          </label>
          <label>
            计划名称
            <input
              required
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </label>
          <DateField
            label="历史起始月"
            type="month"
            required
            value={form.start}
            onChange={(value) => setForm({ ...form, start: value })}
          />
          <DateField
            label="历史结束月"
            type="month"
            required
            value={form.end}
            onChange={(value) => setForm({ ...form, end: value })}
          />
          <label>
            执行时区（北京时间）
            <input required value={form.timezone} readOnly />
          </label>
          <label>
            窗口起始（含，北京时间）
            <input
              type="text"
              inputMode="numeric"
              placeholder="HH:mm"
              pattern="(?:[01][0-9]|2[0-3]):[0-5][0-9]"
              required
              value={form.window_start}
              onChange={(e) =>
                setForm({ ...form, window_start: e.target.value })
              }
            />
          </label>
          <label>
            窗口结束（不含，北京时间）
            <input
              type="text"
              inputMode="numeric"
              placeholder="HH:mm"
              pattern="(?:[01][0-9]|2[0-3]):[0-5][0-9]"
              required
              value={form.window_end}
              onChange={(e) => setForm({ ...form, window_end: e.target.value })}
            />
          </label>
          <DateField
            label="首次到期（北京时间）"
            type="datetime-local"
            step="1"
            required
            value={toBeijingInput(form.first_due)}
            onChange={(value) =>
              setForm({ ...form, first_due: beijingInputToIso(value) })
            }
          />
          {mode === "periodic" && (
            <>
              <label>
                周期间隔秒数
                <input
                  type="number"
                  min="60"
                  max="31622400"
                  value={form.interval_seconds}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      interval_seconds: Number(e.target.value),
                    })
                  }
                />
              </label>
              <label>
                有限周期数
                <input
                  type="number"
                  min="1"
                  max="12"
                  value={form.cycles}
                  onChange={(e) =>
                    setForm({ ...form, cycles: Number(e.target.value) })
                  }
                />
              </label>
            </>
          )}
          <p>
            每计划最多 360 个月、12
            个周期。周期重复观测指定历史区间；自动发现新发布月份、跨午夜窗口、保留材料修复及多来源为
            UNSUPPORTED。存储继承 03/04 Host 绑定。
          </p>
          <label>
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            确认提交此有界历史计划
          </label>
          <button type="submit" disabled={!eligible || !confirmed || busy}>
            持久提交历史计划
          </button>
        </form>
      )}
      <button type="button" onClick={() => setRefresh((n) => n + 1)}>
        回查历史计划
      </button>
      <DateField
        label="失败回补起始月"
        type="month"
        value={repair.start}
        onChange={(value) => setRepair({ ...repair, start: value })}
      />
      <DateField
        label="失败回补结束月"
        type="month"
        value={repair.end}
        onChange={(value) => setRepair({ ...repair, end: value })}
      />
      {data?.plans?.map((plan) => (
        <article className="version" key={plan.identity}>
          <h3>
            {plan.spec.name} · <code>{plan.identity}</code>
          </h3>
          <p>
            {plan.state} · {plan.spec.start} 至 {plan.spec.end} ·{" "}
            {plan.spec.timezone} {plan.spec.window_start}–{plan.spec.window_end}{" "}
            · 下次到期 {formatBeijingTime(plan.next_due)}
          </p>
          <p>
            已证实完成 {plan.completed_batches} / {plan.total_batches}；扫描断点{" "}
            {plan.cycle}:{plan.cursor}。暂停/取消在有限批次间生效；UNKNOWN
            仅核实。
          </p>
          <div className="actions">
            {(["pause", "resume", "cancel"] as const).map((action, i) => (
              <button
                type="button"
                key={action}
                disabled={
                  !eligible ||
                  busy ||
                  ["cancelled", "completed"].includes(plan.state)
                }
                onClick={() =>
                  write(`/api/r5/cpi/plans/${plan.identity}/action`, { action })
                }
              >
                {["暂停计划", "恢复计划", "取消计划"][i]}
              </button>
            ))}
            <button
              type="button"
              disabled={
                !eligible || busy || !["active", "paused"].includes(plan.state)
              }
              onClick={() =>
                write(`/api/r5/cpi/plans/${plan.identity}/backfill`, repair)
              }
            >
              提交指定失败窗口回补
            </button>
            <a
              href={`#page=3&asset=${encodeURIComponent(asset ?? "")}&asset_revision=${encodeURIComponent(revision ?? "")}`}
            >
              04 版本与读取资格
            </a>
            <a href="#page=1">02 BLS 来源资格</a>
            <a
              href={`#page=5&asset=${encodeURIComponent(asset ?? "")}&asset_revision=${encodeURIComponent(revision ?? "")}`}
            >
              06 读取服务
            </a>
            <a
              href={`#page=6&tab=diagnostics&asset=${encodeURIComponent(asset ?? "")}&asset_revision=${encodeURIComponent(revision ?? "")}`}
            >
              07 平台诊断
            </a>
          </div>
          {plan.batches.map((batch) => (
            <div key={batch.key}>
              <p>
                {batch.reference} ·{" "}
                {batch.complete ? "已证实完成" : "未证实完成"}
              </p>
              {batch.jobs.map((job) => (
                <p key={job.job_identity}>
                  Job <code>{job.job_identity}</code> · Attempt{" "}
                  {job.attempt_identity ?? "无"} · {job.state} ·{" "}
                  {job.classification ?? "无分类"} · reconcile{" "}
                  {job.reconciliation ?? "尚无"}
                  <br />
                  Raw {job.raw_sha256 ?? "尚无"} · Version{" "}
                  {job.version_identity ?? "尚无"} · Publication{" "}
                  {job.publication_occurrence ?? "尚无"}
                  <br />
                  {data.status === "OK" &&
                    data.asset_identity === asset &&
                    data.asset_revision === revision &&
                    job.publication_occurrence &&
                    job.version_identity &&
                    job.read_as_of && (
                      <a
                        href={`#page=5&asset=${encodeURIComponent(asset ?? "")}&asset_revision=${encodeURIComponent(revision ?? "")}&reference=${batch.reference}&version=${encodeURIComponent(job.version_identity)}&as_of=${encodeURIComponent(job.read_as_of)}`}
                      >
                        06 精确读取此版本
                      </a>
                    )}
                  {" · "}
                  <a href={`#page=0&tab=diagnostics&job=${job.job_identity}`}>
                    01 关联异常
                  </a>
                </p>
              ))}
            </div>
          ))}
        </article>
      ))}
    </section>
  );
}
