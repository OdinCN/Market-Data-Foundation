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
  if (profile.id === "retail")
    return (
      <section aria-label="零售销售 API说明" className="cpi-api-help">
        <h4>GET {profile.basePath}/query</h4>
        <p>{profile.historyNotice}</p>
        <CompactTable
          label="查询参数"
          headers={["参数", "语义"]}
          rows={[
            ["start/end", "必填 YYYY-MM，含首尾；自动加入12个月比较缓冲"],
            ["local_only", "true 禁止上游；GET 不入库、不创建表"],
            ["query_mode", "current / history；历史只读本地"],
            [
              "as_of",
              "带时区截止时间；北京时间 +08:00 在 URL 中编码为 %2B08%3A00",
            ],
            [
              "as_of_basis / vintage_mode",
              "observed / available；历史默认 available，同一截止约束全部输入及基期",
            ],
          ]}
        />
        <CompactTable
          label="响应字段"
          headers={["字段", "含义 / 单位"]}
          rows={[
            [
              "status/source_failure",
              "PARTIAL 表示来源未返回记录或补取临时失败；source_failure 的 classification/http_status/retry_at 为失败诊断。OK 不保证无缺口。",
            ],
            [
              "source_no_content",
              "HTTP 204 空正文及请求起止月；表示本次未返回记录，不是服务故障、官方null或发布时间证据；不生成批次、不落库。",
            ],
            [
              "missing_input_count/last_completed_month/source_skipped_months",
              "含12月缓冲的缺失输入数 / UTC上个完整月 / 未请求的未结束或未来月份；响应保留所选全部月份，不推定官方发布时间。",
            ],
            [
              "headline_level/control_level",
              "Decimal字符串或null；million_current_usd；控制组为MDF推导",
            ],
            [
              "headline_rates/control_rates",
              "1/3/6/12月 Decimal ratio，0.01=1%；未经年化",
            ],
            [
              "official_headline_mpcsm",
              "Census官方 percent；与推导 ratio 独立；0.8 显示为 0.8%，不再乘100",
            ],
            [
              "publication_events/points.publication_event",
              "留存的官方月份日历；总体及控制组含空值行均显示北京时间计划。无记录、TBA/未知不推算；as_of 早于日历获取时间时隐藏该日历；GET 不刷新或写入。",
            ],
            [
              "source_cells/input_versions",
              "六输入、原始值、状态/原因、修订、原始哈希与指针、观察/入库时间；缺值不补0",
            ],
            [
              "observed_at/available_at",
              "采集/本地提交时间，不是官方发布时间；revision 1 不代表官方首发",
            ],
          ]}
        />
        <RequestPreview request={request} />
        <p>
          GET {profile.basePath}/api-help 提供完整接口合同。POST{" "}
          {profile.basePath}/jobs 提交后台串行采集；request_id 与 collection_id
          相同。恢复保留原请求及范围。周期策略未启用。
        </p>
      </section>
    );
  const employment = profile.id === "employment";
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
            "string（YYYY-MM）；start≥1901-01，最多2400个月（含首尾）",
          ],
          ...(employment
            ? [
                [
                  "series",
                  "否 / 全部五系列",
                  "string；逗号分隔 CES0000000001、LNS14000000、CES0500000003、LNS11300000、CES0500000002；拒绝 basket / adjustment",
                ],
              ]
            : [
                [
                  "basket",
                  "否 / headline",
                  "string；headline（overall 为别名）、core、all；本表单默认 all",
                ],
                [
                  "adjustment",
                  "否 / sa",
                  `string；${[...new Set(profile.series.map((s) => s[3]))].join("、")}、all；仅包含本资产支持的口径`,
                ],
              ]),
          [
            "measure",
            "否 / index",
            employment
              ? "string；仅接受 index，返回各序列原生单位的 value，不是通胀指数"
              : "string；index、mom、yoy、change_1m_ratio、change_3m_ratio、change_6m_ratio、change_12m_ratio；本界面固定 index",
          ],
          [
            "local_only",
            `否 / ${profile.id === "cpi" ? "true" : "false"}（Web）`,
            "boolean 查询文本 true / false；当前表单发 false，历史发 true；history 始终禁止上游",
          ],
          [
            "query_mode",
            "否 / 按截止参数推断",
            "string；current / history；as_of 或 available 口径推断 history，否则 current",
          ],
          [
            "as_of",
            "否 / 无",
            "string，带时区的 ISO 时间；历史表单必填，使用北京时间 +08:00",
          ],
          [
            "as_of_basis / vintage_mode",
            "否 / 无截止 current: observed；history 或有截止: available",
            "string；observed / available；两者同时提供须一致；official 不支持。observed 按来源响应观察时间，available 按 MDF 提交确认时间；current 无截止时实际固定 observed；历史表单固定 available",
          ],
        ]}
      />
      <details>
        <summary>{profile.series.length}个统计序列</summary>
        <CompactTable
          label="统计序列"
          headers={[
            "series_id",
            employment ? "指标 / 调查 / 单位 / 季调" : "篮子 / 季调",
          ]}
          rows={profile.series.map(([id, title, basket, adjustment]) => [
            id,
            employment
              ? `${title} / ${adjustment}`
              : `${basket} / ${adjustment}`,
          ])}
        />
      </details>
      <p>
        年月 month 是统计归属月份，不是发布时间或入库时间。current
        优先读取本地，缺口可从上游补取，但查询不落库；history
        只读本地记录，不补取上游；默认按 available 截止，也可显式选择 observed。
        observed 不证明当时已入库，也不重建官方历史版本。 无 as_of 的 history
        读取现有本地记录，不承诺某个历史时点。available_at 是 MDF
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
              employment
                ? "points[]: series_id / month / indicator / seasonal_adjustment / survey / value_unit"
                : "points[]: series_id / month / indicator / seasonal_adjustment",
              "string；非空",
              employment
                ? "序列 / 归属月 / 指标 / SA / CES|CPS / 原生单位"
                : "序列 / 归属月 / headline|core / SA|NSA",
            ],
            [
              employment
                ? "points[].value / value_unit"
                : "points[].index_value / value",
              employment
                ? "decimal string / string；points 中 value 非空，来源缺值在 source_unavailable 中为 null"
                : "decimal string；value 可 null",
              employment
                ? "非农 thousand_jobs（千岗位，不是人数）；失业率 / 劳动参与率 percent；时薪 USD/hour；周工时 hours。不返回 index_value"
                : "指数；value 由 measure 选择，当前等于 index_value",
            ],
            ...(employment
              ? [
                  [
                    "points[].net_change_1m_thousand_jobs",
                    "decimal string|null；仅非农",
                    "value[t]−value[t−1]，千岗位",
                  ],
                  [
                    "points[].change_1m_percentage_points",
                    "decimal string|null；失业率 / 劳动参与率",
                    "value[t]−value[t−1]，百分点",
                  ],
                  [
                    "points[].change_1m_hours",
                    "decimal string|null；仅周工时",
                    "value[t]−value[t−1]，小时",
                  ],
                ]
              : []),
            [
              employment
                ? "points[].change_{1,12}m_ratio"
                : "points[].change_{1,3,6,12}m_ratio",
              employment
                ? "decimal string|null；仅时薪"
                : "decimal string|null",
              "ratio：0.0024=0.24%；同截止时点端点比值减1。累计变化，不年化",
            ],
            [
              "points[].mom_pct / yoy_pct",
              employment
                ? "decimal string|null；仅时薪"
                : "decimal string|null",
              "percent：0.24=0.24%，不能再乘100；本地端点计算，不替代官方变化",
            ],
            [
              "points[].origin / availability / revision",
              "string / string / integer|null",
              `origin=local|upstream；availability=available|unpersisted|source_unavailable|withdrawn|unknown（后三者在缺值证据数组中）；本地修订编号不是 ${profile.provider} 官方版本，revision 1 仅首次本地观察；临时上游点无本地修订序号`,
            ],
            [
              "points[].observed_at / available_at",
              "ISO string / ISO string|null",
              "来源观察时间 / 本地可用时间",
            ],
            [
              "points[].calculation_inputs",
              "object",
              employment
                ? "同截止比较月、comparison_value、来源、修订及时间证据；不含 denominator_index_value"
                : "各指标分母月份、denominator_index_value、来源、修订及时间证据",
            ],
            [
              "points[].provider_calculations / footnotes",
              "object / object[]",
              employment
                ? "BLS 官方 net_changes / pct_changes 与脚注（包括空对象）分别保留；官方精度可能不同于本地计算"
                : profile.id === "pce"
                  ? "BEA 官方 T20807 月变化另见 bea_pct_1m（decimal string|null，percent）；本地变化从 T20804 指数推导"
                  : "BLS 官方 pct_changes 与脚注保留；与本地端点计算分开",
            ],
            [
              "points[].raw_sha256 / raw_locator",
              "string / string|null",
              "原始材料摘要 / 已留存定位；未保存临时来源值无持久定位。HTTP 点不返回 batch_id",
            ],
            [
              "missing[]",
              "object[]；非空元素",
              "目标月数值缺失：series_id、month、可选 reason；不补零",
            ],
            [
              "calculation_missing[]",
              "object[]；非空元素",
              "计算基期缺失：series_id、month、metric、required_month、reason；不等于目标月数值缺失",
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
            ...(!employment
              ? [
                  [
                    "measure_complete",
                    "boolean",
                    "所选 value 完整，不保证其他派生指标完整",
                  ],
                ]
              : []),
            [
              "persisted",
              "boolean",
              "本次是否写库；query 为 false，仍可能包含既有本地数据",
            ],
            [
              "provider_requests / provider_attempts / source_request_count",
              "integer",
              "来源请求数 / 尝试次数 / 尝试次数的兼容字段",
            ],
            [
              employment
                ? "units / calculation_method"
                : "measure / unit / units / calculation_method",
              employment
                ? "object / string"
                : "string / string / object / string",
              employment
                ? "按字段映射单位；value 单位见各点 value_unit。不返回顶层 measure / unit / measure_complete"
                : "选定指标、单位映射与计算说明",
            ],
            [
              "as_of / as_of_semantics / query_mode / as_of_basis / vintage_mode",
              "ISO string|null；其余 string",
              "截止时点与查询口径",
            ],
            [
              "publication_events / points[].publication_event",
              "object[] / object|null（可选）",
              "官方日历时间及 evidence；不代表当前修订值当时已知，历史默认按入库可用时间；显式 observed 按来源响应观察时间",
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
        <summary>来源与官方计算</summary>
        <p>
          {employment
            ? "BLS Employment Situation：CES 非农、平均时薪与每周计薪工时，CPS 失业率与劳动参与率分别保留调查口径。非农、失业率、劳动参与率与周工时本地计算为相邻月差值，仅时薪计算环同比；官方 net_changes / pct_changes 不替代本地端点计算。"
            : profile.id === "pce"
              ? "BEA NIPA T20804 月度指数；官方月环比来自 T20807，见 bea_pct_1m（百分数，可 null），不等同于从指数计算的 mom_pct。留存材料为去凭据的 Results 投影，不声称保留完整原始响应。"
              : `BLS ${profile.label} 月度指数；官方 pct_changes 与本地同截止端点推导分别保留，可能因官方精度和方法不同而不同。`}
        </p>
        <p>
          缺值不补零；complete 仅表示目标月数值完整，coverage_complete
          表示记录覆盖，均不保证比较基期或官方变化齐全。官方日历事件只绑定参考月，
          revision 1 是首次本地观察，不能据此认定官方首发。
        </p>
      </details>
      <details>
        <summary>成功示例（字段节选，非本次响应）</summary>
        <p>
          合成示例，不作为来源证据；官方日历不证明当前修订是官方首发或当时已知。
        </p>
        <CodeBlock
          label={`${profile.label} 响应示例`}
          copyLabel="复制响应示例"
          text={JSON.stringify(
            employment
              ? {
                  start_month: "2026-09",
                  end_month: "2026-09",
                  series_ids: profile.series.map((s) => s[0]),
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
                      revision: null,
                      available_at: null,
                    },
                    {
                      series_id: "LNS14000000",
                      month: "2026-09",
                      survey: "CPS",
                      value: "4.2",
                      value_unit: "percent",
                      change_1m_percentage_points: "0.1",
                      origin: "upstream",
                      availability: "unpersisted",
                      revision: null,
                      available_at: null,
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
                      revision: null,
                      available_at: null,
                    },
                    {
                      series_id: "LNS11300000",
                      month: "2026-09",
                      survey: "CPS",
                      value: "62.5",
                      value_unit: "percent",
                      change_1m_percentage_points: "0.1",
                    },
                    {
                      series_id: "CES0500000002",
                      month: "2026-09",
                      survey: "CES",
                      value: "34.3",
                      value_unit: "hours",
                      change_1m_hours: "-0.1",
                    },
                  ],
                  missing: [],
                  calculation_missing: [],
                  persisted: false,
                  query_mode: "current",
                  as_of_basis: "observed",
                  as_of: null,
                }
              : {
                  start_month: "2026-05",
                  end_month: "2026-05",
                  measure: "index",
                  unit: "index",
                  points: [
                    {
                      series_id: profile.series.find(
                        (s) => s[2] === "core",
                      )![0],
                      month: "2026-05",
                      index_value: "320.125",
                      value: "320.125",
                      change_1m_ratio: "0.0024",
                      mom_pct: "0.24",
                      available_at: null,
                      availability: "unpersisted",
                    },
                  ],
                  missing: [],
                  calculation_missing: [],
                  persisted: false,
                  query_mode: "current",
                },
            null,
            2,
          )}
        />
      </details>
      <details>
        <summary>失败示例与诊断</summary>
        <p>
          400 参数无效；403 权限拒绝；503 服务或来源不可用。失败响应可含
          status、classification；网络失败可能没有 HTTP
          状态或正文。实际失败请查看查询后的原始响应。
        </p>
        <pre>
          <code>{`HTTP 403\n{"status":"ERROR","classification":"local_read_not_authorized"}\nHTTP 400\n{"status":"ERROR","classification":"${employment ? "employment_series_filter_required" : "invalid_measure"}","provider_attempts":0}`}</code>
        </pre>
      </details>
      <RequestPreview request={request} />
    </section>
  );
}
