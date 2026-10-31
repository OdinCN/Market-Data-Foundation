import { CPI_PROFILE, type MacroProfile } from "./macroTimeSeriesProfile";
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { CpiAvailability, CpiHttpExchange } from "./cpiTimeSeriesApi";
import { formatBeijingTime } from "./beijingTime";

type RequestDescription = { method: "GET"; path: string };

function CodeBlock({
  text,
  copyLabel,
  label,
  prefix,
}: {
  text: string;
  copyLabel: string;
  label?: string;
  prefix?: string;
}) {
  const code = useRef<HTMLSpanElement>(null);
  const attempt = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const [feedback, setFeedback] = useState<{
    text: string;
    status: "copied" | "failed";
  } | null>(null);
  const status = feedback?.text === text ? feedback.status : null;
  useEffect(() => {
    setFeedback(null);
    return () => {
      attempt.current++;
      clearTimeout(timer.current);
    };
  }, [text]);

  async function copy() {
    const current = ++attempt.current;
    clearTimeout(timer.current);
    setFeedback(null);
    let success = false;
    try {
      await navigator.clipboard.writeText(text);
      success = true;
    } catch {
      if (current !== attempt.current) return;
      const focus = document.activeElement;
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.readOnly = true;
      textarea.style.cssText = "position:fixed;left:-9999px;top:0";
      document.body.appendChild(textarea);
      try {
        textarea.focus();
        textarea.select();
        success = document.execCommand?.("copy") === true;
      } catch {
        // Keep the original code available for manual copying.
      } finally {
        textarea.remove();
        if (focus instanceof HTMLElement && focus.isConnected) focus.focus();
      }
    }
    if (current !== attempt.current) return;
    if (success) {
      setFeedback({ text, status: "copied" });
      timer.current = setTimeout(() => {
        if (current === attempt.current) setFeedback(null);
      }, 2000);
      return;
    }
    if (code.current) {
      code.current.focus();
      const range = document.createRange();
      range.selectNodeContents(code.current);
      const selection = window.getSelection();
      selection?.removeAllRanges();
      selection?.addRange(range);
    }
    setFeedback({ text, status: "failed" });
  }

  const buttonLabel =
    status === "copied" ? `${copyLabel}（已复制）` : copyLabel;
  return (
    <div className="cpi-code-block">
      <pre aria-label={label}>
        <code>
          {prefix}
          <span ref={code} tabIndex={-1}>
            {text}
          </span>
        </code>
      </pre>
      <button
        type="button"
        className="cpi-copy-button"
        title={buttonLabel}
        aria-label={buttonLabel}
        onClick={(event) => {
          event.stopPropagation();
          void copy();
        }}
      >
        <svg
          aria-hidden="true"
          focusable="false"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          {status === "copied" ? (
            <path d="m5 12 4 4L19 6" />
          ) : (
            <>
              <rect x="8" y="8" width="12" height="12" rx="2" />
              <path d="M16 8V4H4v12h4" />
            </>
          )}
        </svg>
      </button>
      {status === "failed" && (
        <p role="status" className="cpi-copy-failure">
          复制未完成，已选中{copyLabel.replace(/^复制/, "")}，请按
          Ctrl+C（Mac：⌘C）复制。
        </p>
      )}
    </div>
  );
}

export function RequestPreview({ request }: { request: RequestDescription }) {
  const url = new URL(request.path, window.location.origin).href;
  const curl = `curl --request GET '${url.replaceAll("'", "'\\''")}'`;
  return (
    <section aria-label="请求预览" className="cpi-request-preview">
      <h4>请求预览</h4>
      <CodeBlock
        text={url}
        prefix={`${request.method} `}
        label="请求 URL"
        copyLabel="复制请求预览URL"
      />
      <details>
        <summary>cURL 示例（当前服务地址）</summary>
        <CodeBlock text={curl} copyLabel="复制cURL" />
      </details>
    </section>
  );
}
export function HttpExchangePanel({
  exchange,
  showBody,
}: {
  exchange: CpiHttpExchange;
  showBody: boolean;
}) {
  return (
    <section aria-label="本次原始响应" className="cpi-http-exchange">
      <dl>
        <dt>本次请求</dt>
        <dd>
          <CodeBlock
            text={exchange.url}
            prefix={`${exchange.method} `}
            copyLabel="复制本次请求URL"
            label="本次请求 URL"
          />
        </dd>
        <dt>HTTP 状态</dt>
        <dd>{exchange.status}</dd>
        <dt>耗时</dt>
        <dd>{exchange.elapsedMs.toFixed(0)} ms</dd>
        <dt>完成时间</dt>
        <dd>{formatBeijingTime(exchange.completedAt)}</dd>
      </dl>
      <div hidden={!showBody}>
        <p className="small">
          以下为本次实际 HTTP 响应正文，保留原始文本、数值精度和未知字段。
        </p>
        <CodeBlock
          text={exchange.bodyText}
          label="原始 HTTP 正文"
          copyLabel="复制原始响应"
        />
      </div>
    </section>
  );
}
function CompactTable({
  headers,
  rows,
  label,
}: {
  headers: string[];
  rows: ReactNode[][];
  label: string;
}) {
  return (
    <div className="table-wrap">
      <table aria-label={label}>
        <thead>
          <tr>
            {headers.map((h) => (
              <th key={h}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {row.map((cell, j) => (
                <td key={j}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
export function CpiQueryApiHelp({
  profile = CPI_PROFILE,
  availability,
  request,
}: {
  availability: CpiAvailability;
  request: RequestDescription;
  profile?: MacroProfile;
}) {
  if (profile.id === "employment")
    return (
      <section aria-label="就业 API说明" className="cpi-api-help">
        <h4>GET {profile.basePath}/query</h4>
        <p>{profile.historyNotice}</p>
        <CompactTable
          label="查询参数"
          headers={["参数", "默认", "说明"]}
          rows={[
            ["start / end", "必填", "YYYY-MM，最多2400个月"],
            [
              "series",
              "全部三系列",
              "逗号分隔 CES0000000001、LNS14000000、CES0500000003",
            ],
            [
              "query_mode",
              "current",
              "current 本地优先缺口透传不保存；history 仅本地",
            ],
            ["local_only", "false", "true 禁止上游；history 始终仅本地"],
            [
              "as_of / as_of_basis",
              "available",
              "带时区截止时间，历史按入库时间",
            ],
            ["measure", "index", "就业仅此值，不使用 basket / adjustment"],
          ]}
        />
        <CompactTable
          label="就业字段"
          headers={["字段", "单位及语义"]}
          rows={[
            [
              "value / value_unit / survey",
              "非农 CES 千岗位；CPS 失业率 %；CES 时薪 USD/hour",
            ],
            ["net_change_1m_thousand_jobs", "当月减上月，千岗位"],
            ["change_1m_percentage_points", "失业率差值，百分点，非通胀同比"],
            ["mom_pct / yoy_pct", "仅时薪本地端点计算，百分数"],
            [
              "provider_calculations.net_changes / pct_changes / footnotes",
              "官方计算与脚注原样保留，可能采用不同精度，不替代本地推导",
            ],
            [
              "revision / available_at / observed_at",
              "本地修订号、入库可用时间、来源观察时间；不是官方首发或修订发布时间",
            ],
            [
              "origin / availability",
              "local 或 upstream；available 或未保存临时来源值",
            ],
            [
              "calculation_inputs / raw_sha256 / batch_id",
              "同截止基期值及来源、原始材料摘要、本地批次",
            ],
            ["change_1m_ratio / change_12m_ratio", "仅时薪比例；0.01 表示1%"],
            ["null / missing", "缺值或比较基期缺失，不补零"],
          ]}
        />
        <p>
          以下为合成响应示例，展示三种单位、官方计算和本地差值；不作为真实来源证据。
        </p>
        <CodeBlock
          label="就业响应示例"
          copyLabel="复制就业响应示例"
          text={JSON.stringify(
            {
              start_month: "2026-09",
              end_month: "2026-09",
              series_ids: profile.series.map((s) => s[0]),
              query_mode: "current",
              as_of_basis: "observed",
              vintage_mode: "observed",
              as_of: null,
              persisted: false,
              provider_requests: 1,
              provider_attempts: 1,
              complete: true,
              coverage_complete: true,
              points: [
                {
                  series_id: "CES0000000001",
                  month: "2026-09",
                  survey: "CES",
                  value: "150010",
                  value_unit: "thousand_jobs",
                  net_change_1m_thousand_jobs: "10",
                  provider_calculations: {
                    net_changes: { "1": "10" },
                    pct_changes: {},
                  },
                  footnotes: [{}],
                  origin: "upstream",
                  availability: "unpersisted",
                  available_at: null,
                  revision: null,
                },
                {
                  series_id: "LNS14000000",
                  month: "2026-09",
                  survey: "CPS",
                  value: "4.2",
                  value_unit: "percent",
                  change_1m_percentage_points: "0.1",
                  provider_calculations: {
                    net_changes: { "1": "0.1" },
                    pct_changes: {},
                  },
                  footnotes: [{}],
                  origin: "upstream",
                  availability: "unpersisted",
                  available_at: null,
                  revision: null,
                },
                {
                  series_id: "CES0500000003",
                  month: "2026-09",
                  survey: "CES",
                  value: "30.10",
                  value_unit: "USD/hour",
                  mom_pct: "0.33",
                  yoy_pct: "3.08",
                  change_1m_ratio: "0.0033",
                  change_12m_ratio: "0.0308",
                  provider_calculations: {
                    net_changes: { "1": "0.10" },
                    pct_changes: { "1": "0.3", "12": "3.1" },
                  },
                  footnotes: [{ code: "P", text: "Preliminary" }],
                  origin: "upstream",
                  availability: "unpersisted",
                  available_at: null,
                  revision: null,
                },
              ],
              missing: [],
              calculation_missing: [],
            },
            null,
            2,
          )}
        />
        <p>
          官方日历时间只绑定参考月事件，不证明本次修订当时已知。来源 BLS
          Employment Situation，CES 与 CPS 调查口径分别保留。
        </p>
        <RequestPreview
          request={{
            method: "GET",
            path: `${profile.basePath}/query?start=2025-01&end=2026-09&series=CES0000000001,LNS14000000,CES0500000003&query_mode=current&local_only=false&measure=index`,
          }}
        />
        <RequestPreview
          request={{
            method: "GET",
            path: `${profile.basePath}/query?start=2025-01&end=2026-09&series=LNS14000000&query_mode=history&local_only=true&as_of_basis=available&as_of=2026-10-31T08%3A00%3A00%2B08%3A00`,
          }}
        />
      </section>
    );
  const permission = availability.data?.web_capabilities.query;
  return (
    <section aria-label={`${profile.label} API说明`} className="cpi-api-help">
      <h4>GET {profile.basePath}/query</h4>
      {profile.historyNotice && <p>{profile.historyNotice}</p>}
      <p>
        当前 DEV 接口：
        {availability.failure
          ? "服务状态暂未核实"
          : permission === true
            ? "已开放查询"
            : permission === false
              ? "未开放查询"
              : "资格待核实"}
        。这是当前开发接口，不代表生产稳定承诺；访问仍受当前服务授权约束。
      </p>
      <CompactTable
        label="查询参数"
        headers={["参数", "必填 / 默认", "类型与取值"]}
        rows={[
          [
            "start / end",
            "是",
            "YYYY-MM；start≥1901-01，最多2400个月（含首尾）",
          ],
          [
            "basket",
            "否 / headline",
            "headline（overall 为别名）、core、all；本表单默认 all",
          ],
          [
            "adjustment",
            "否 / sa",
            `${[...new Set(profile.series.map((s) => s[3]))].join("、")}、all；仅包含本资产支持的口径`,
          ],
          [
            "measure",
            "否 / index",
            "index、mom、yoy、change_1m_ratio、change_3m_ratio、change_6m_ratio、change_12m_ratio；本界面固定 index",
          ],
          [
            "local_only",
            "否 / true（Web）",
            "true / false；当前表单发 false，历史发 true",
          ],
          [
            "query_mode",
            "否 / 按截止参数推断",
            "current / history；无截止参数时 current",
          ],
          [
            "as_of",
            "否 / 无",
            "带时区的 ISO 时间；历史表单必填，使用北京时间 +08:00",
          ],
          [
            "as_of_basis / vintage_mode",
            "否 / current: observed；history: available",
            "observed / available；两者同时提供须一致；official 不支持。历史表单固定 available",
          ],
        ]}
      />
      <details>
        <summary>{profile.series.length}个统计序列</summary>
        <CompactTable
          label="统计序列"
          headers={["series_id", "篮子 / 季调"]}
          rows={profile.series.map(([id, , basket, adjustment]) => [
            id,
            `${basket} / ${adjustment}`,
          ])}
        />
      </details>
      <p>
        年月 month 是统计归属月份，不是发布时间或入库时间。current
        优先读取本地，缺口可从上游补取，但查询不落库；history 截止只读当时
        available 的本地记录，不补取上游。available_at 是 MDF
        提交后确认的可用时间，observed_at 是来源响应观察时间，二者不可互换。
      </p>
      <details>
        <summary>返回字段、类型与单位</summary>
        <CompactTable
          label="返回字段"
          headers={["字段", "类型 / nullable", "单位与含义"]}
          rows={[
            [
              "start_month / end_month / series_ids",
              "string / string / string[]",
              "月份边界与序列范围",
            ],
            [
              "points[]: series_id / month / indicator / seasonal_adjustment",
              "string；非空",
              "序列 / 归属月 / headline|core / SA|NSA",
            ],
            [
              "points[].index_value / value",
              "decimal string 或 number；value 可 null",
              "指数；value 由 measure 选择，当前等于 index_value",
            ],
            [
              "points[].change_{1,3,6,12}m_ratio",
              "decimal string 或 number 或 null",
              "ratio：0.0024=0.24%；表格乘100显示。累计变化，不年化",
            ],
            [
              "points[].mom_pct / yoy_pct",
              "decimal string 或 number 或 null",
              "percent：0.24=0.24%，不能再乘100",
            ],
            [
              "points[].origin / availability / revision",
              "string / string / integer|null",
              `来源、可用状态与本地修订编号（不是 ${profile.provider} 官方版本）；临时上游点无本地修订序号。仅 availability=unpersisted 表示临时补源`,
            ],
            [
              "points[].observed_at / available_at",
              "ISO string / ISO string|null",
              "来源观察时间 / 本地可用时间",
            ],
            [
              "points[].calculation_inputs",
              "object",
              "各指标分母月份、指数、来源、修订及时间证据",
            ],
            [
              "missing[]",
              "object[]；非空元素",
              "目标月指数缺失：series_id、month、可选 reason",
            ],
            [
              "calculation_missing[]",
              "object[]；非空元素",
              "计算基期缺失：series_id、month、metric、required_month、reason；不等于目标月指数缺失",
            ],
            [
              "source_unavailable / withdrawn",
              "object[]",
              "来源缺值或撤回的记录证据",
            ],
            ["complete", "boolean", "只表示 missing 为空，不保证计算基期齐全"],
            [
              "coverage_complete",
              "boolean",
              "范围记录覆盖，包括临时补源记录；记录可能缺值或撤回",
            ],
            [
              "measure_complete / persisted",
              "boolean",
              "所选 value 完整 / 本次是否写库；persisted=false 仅表示本次 query 未写库，仍可能包含既有本地数据",
            ],
            [
              "provider_requests / provider_attempts / source_request_count",
              "integer",
              "来源批次 / 尝试 / 来源请求次数",
            ],
            [
              "measure / unit / units / calculation_method",
              "string / string / object / string",
              "选定指标、单位映射与计算说明",
            ],
            [
              "as_of / as_of_semantics / query_mode / as_of_basis / vintage_mode",
              "ISO string|null；其余 string",
              "截止时点与查询口径",
            ],
            [
              "publication_events / points[].publication_event",
              "object[] / object|null（可选）",
              "官方日历时间及 evidence；不代表当前修订值当时已知，历史截止仍按入库时间",
            ],
          ]}
        />
        <p className="small">
          missing 的 reason 可为
          source_unavailable、withdrawn、availability_evidence_insufficient；缺少
          reason 时表示未观察到记录。calculation_missing 另含 not_observed
          等计算基期原因。
        </p>
      </details>
      <details>
        <summary>成功示例（字段节选，非本次响应）</summary>
        <pre>
          <code>{`{"start_month":"2026-05","end_month":"2026-05","measure":"index","unit":"index","points":[{"series_id":"${profile.series.find((s) => s[2] === "core")![0]}","month":"2026-05","index_value":"320.125","value":"320.125","change_1m_ratio":"0.0024","mom_pct":"0.24","available_at":null,"availability":"unpersisted"}],"missing":[],"calculation_missing":[],"persisted":false,"query_mode":"current"}`}</code>
        </pre>
      </details>
      <details>
        <summary>失败示例与诊断</summary>
        <p>
          400 参数无效；403 权限拒绝；503 服务或来源不可用。失败响应可含
          status、classification；网络失败可能没有 HTTP
          状态或正文。实际失败请查看查询后的原始响应。
        </p>
        <pre>
          <code>{`HTTP 403\n{"status":"ERROR","classification":"local_read_not_authorized"}\nHTTP 400\n{"status":"ERROR","classification":"invalid_measure","provider_attempts":0}`}</code>
        </pre>
      </details>
      <RequestPreview request={request} />
    </section>
  );
}
