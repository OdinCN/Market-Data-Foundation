import { nativeQueryHelp } from "./macroQueryHelp";
import { CPI_PROFILE, type MacroProfile } from "./macroTimeSeriesProfile";
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { CpiAvailability, CpiHttpExchange } from "./cpiTimeSeriesApi";
import { formatBeijingTime } from "./beijingTime";

type RequestDescription = { method: "GET"; path: string };

export function CodeBlock({
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
function RelatedReleaseHelp() {
  return (
    <p>
      related_release_events 列出相关发布事件；source_version
      保留来源日期或时刻及精度，roles
      区分当期公布（actual）、后续修订（prior_revised）和此前公布值（official_previous）。
      association=explicit_version_proof
      才表示当前连续版本已有明确证明；related_scope_only
      仅表示口径或月份相关，不能据此认定表格数值是该事件公布值。日期级信息不补时刻。
    </p>
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
  if (profile.id === "gdp")
    return (
      <GdpQueryApiHelp
        profile={profile}
        availability={availability}
        request={request}
      />
    );
  const nativeHelp = nativeQueryHelp(profile);
  const employment = profile.id === "employment";
  const permission = availability.data?.web_capabilities.query;
  return (
    <section aria-label={`${profile.label} API说明`} className="cpi-api-help">
      <RelatedReleaseHelp />
      <h4>GET {profile.basePath}/releases</h4>
      <p>
        发布记录只读本地证据，不联网、不写入、不自动建表。参数：event_id（精确事件）、as_of（含时区的MDF可用截止）、read_mode=event/source_history、reference_month（事件覆盖月，可多月）、source_version（YYYY-MM-DD档案日期）。日期精度保留；历史证据晚取得不会倒填。缺少正式材料、修订或共识时返回具体
        missing，不回退最新值。
      </p>
      <p>
        claims.role：actual=本次官方公布值；official_previous=官方此前公布的前值；prior_revised=上期值（本次修订后）。value_identity是内容身份；连续版本仅在明确版本证据支持时关联，数值相等不构成证明。MDF首次捕获、发布前最后可消费值、具名冻结共识与预期差逐口径返回，缺前值不阻断合格预期差。完整合同见{" "}
        {profile.basePath}/api-help 的 release_records。
      </p>
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
        rows={
          nativeHelp?.parameters ?? [
            [
              "start / end",
              "是",
              "string（YYYY-MM）；start≥1901-01，最多2400个月（含首尾）",
            ],
            ...(employment
              ? [
                  [
                    "series",
                    "否 / 原有五系列",
                    `string；逗号分隔下表允许的${profile.series.length}个系列；省略保留原有五项；拒绝 basket / adjustment`,
                  ],
                ]
              : [
                  [
                    "basket",
                    "否 / headline",
                    `string；${[...new Set(profile.series.map((s) => s[2]))].join("、")}、all；支持逗号多选；本表单保留总体与核心默认`,
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
                : "string；index、mom、yoy、change_1m_ratio、change_3m_ratio、change_6m_ratio、change_12m_ratio；本界面固定 index；PCE价格另支持 annualized_3m_ratio、annualized_6m_ratio",
            ],
            [
              "columns",
              "否 / 原有完整字段",
              "string；逗号分隔数值字段；与显示指标对应，元数据与measure选择的value别名保留",
            ],
            [
              "local_only",
              `否 / ${profile.id === "cpi" ? "true" : "false"}（API默认）`,
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
          ]
        }
      />
      <details>
        <summary>{profile.series.length}个统计序列</summary>
        <CompactTable
          label="统计序列"
          headers={[
            profile.id === "retail" ? "口径标识（basket）" : "series_id",
            employment || nativeHelp ? "指标 / 单位 / 季调" : "篮子 / 季调",
          ]}
          rows={profile.series.map(([id, title, basket, adjustment]) => [
            id,
            employment || nativeHelp
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
          rows={
            nativeHelp?.fields ?? [
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
                  : "序列 / 归属月 / 明确篮子身份 / SA|NSA",
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
                      "decimal string|null；非农、私营和主要行业",
                      "value[t]−value[t−1]，千岗位",
                    ],
                    [
                      "points[].change_1m_percentage_points",
                      "decimal string|null；失业率 / 劳动参与率 / 就业人口比",
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
              ...(profile.id === "pce"
                ? [
                    [
                      "points[].annualized_3m_ratio / annualized_6m_ratio",
                      "decimal string|null；ratio",
                      "同截止未舍入指数比值的4/2次方减1；最后舍入4位，独立于累计变化",
                    ],
                  ]
                : []),
              ...(profile.id === "ppi"
                ? [
                    [
                      "points[].provider_status",
                      "object；preliminary/revised可null",
                      "仅官方脚注确认初步/修订；无脚注不推断最终值，不使用MDF revision",
                    ],
                  ]
                : []),
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
                "points[].raw_sha256 / raw_locator / batch_id",
                "string / string|null",
                "原始材料摘要 / 已留存定位 / 已保存版本的批次身份；临时来源值不保证有持久定位或批次",
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
              [
                "complete",
                "boolean",
                "只表示 missing 为空，不保证计算基期齐全",
              ],
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
            ]
          }
        />
        <p className="small">
          {nativeHelp ? (
            nativeHelp.missing
          ) : (
            <>
              missing 的 reason 可为
              source_unavailable、withdrawn、availability_evidence_insufficient；缺少
              reason 时表示未观察到记录。calculation_missing 另含 not_observed
              等计算基期原因。
            </>
          )}
        </p>
      </details>
      <details>
        <summary>来源与官方计算</summary>
        <p>
          {nativeHelp?.source ??
            (employment
              ? "BLS Employment Situation：CES 非农、平均时薪与每周计薪工时，CPS 失业率与劳动参与率分别保留调查口径。非农、失业率、劳动参与率与周工时本地计算为相邻月差值，仅时薪计算环同比；官方 net_changes / pct_changes 不替代本地端点计算。"
              : profile.id === "pce"
                ? "BEA NIPA T20804 月度指数；官方月环比来自 T20807，见 bea_pct_1m（百分数，可 null），不等同于从指数计算的 mom_pct。留存材料为去凭据的 Results 投影，不声称保留完整原始响应。"
                : `BLS ${profile.label} 月度指数；官方 pct_changes 与本地同截止端点推导分别保留，可能因官方精度和方法不同而不同。`)}
        </p>
        <p>
          {nativeHelp ? (
            nativeHelp.coverage
          ) : (
            <>
              缺值不补零；complete 仅表示目标月数值完整，coverage_complete
              表示记录覆盖，均不保证比较基期或官方变化齐全。官方日历事件只绑定参考月，
              revision 1 是首次本地观察，不能据此认定官方首发。
            </>
          )}
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
            nativeHelp?.example ??
              (employment
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
                  }),
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
          <code>
            {nativeHelp?.failureExample ??
              `HTTP 403\n{"status":"ERROR","classification":"local_read_not_authorized"}\nHTTP 400\n{"status":"ERROR","classification":"${employment ? "employment_series_filter_required" : "invalid_measure"}","provider_attempts":0}`}
          </code>
        </pre>
      </details>
      <details>
        <summary>采集与策略接口</summary>
        <p>
          GET {profile.basePath}/api-help 提供完整接口合同。POST{" "}
          {profile.basePath}/jobs 提交采集，PUT {profile.basePath}/strategy
          开始或暂停预置官方日历策略。策略启用、后台心跳与实际执行结果分别查看。
        </p>
        {profile.id === "retail" && (
          <p>request_id 与 collection_id 相同；恢复采集保留原请求和范围。</p>
        )}
        {profile.id === "consumption" && (
          <p>
            采集参数为 request_id、action、start、end 及可选 series；消费收入与
            PCE 价格分别采集和验收。
          </p>
        )}
      </details>
      <RequestPreview request={request} />
    </section>
  );
}

function GdpQueryApiHelp({
  profile,
  availability,
  request,
}: {
  profile: MacroProfile;
  availability: CpiAvailability;
  request: RequestDescription;
}) {
  return (
    <section aria-label={`${profile.label} API说明`} className="cpi-api-help">
      <h4>GET {profile.basePath}/query</h4>
      <p>
        当前 DEV 接口：
        {availability.failure
          ? "服务状态暂未核实"
          : availability.data?.web_capabilities.query
            ? "已开放查询"
            : "未开放或资格待核实"}
        。注册资产不自动启用读取；访问受服务授权约束。
      </p>
      <CompactTable
        label="查询参数"
        headers={["参数", "必填 / 默认", "类型与取值"]}
        rows={[
          [
            "start / end",
            "是",
            "YYYY-Qn，包含首尾；季度范围1900–9999；最多800个目标季度；水平比较另需前4季度缓冲，表单起点≥1901-Q1",
          ],
          [
            "series",
            "否 / 八项全部",
            "原生 SeriesCode CSV；下表八项可多选，不接受月度 basket / adjustment / measure / columns / vintage_mode",
          ],
          [
            "local_only",
            "否 / true",
            "true / false；默认显式 true。仅当前查询且 source_allowed=true 时可勾选临时补源并发送 false，不写库，部分失败保留本地值；历史强制 true",
          ],
          [
            "query_mode",
            "否 / current（有as_of时history）",
            "current / history；history 必须有带时区截止，current 不得带截止",
          ],
          [
            "as_of",
            "历史必填",
            "带时区 ISO8601；北京时间表单转为+08:00，禁止无时区截止",
          ],
          [
            "as_of_basis",
            "current: observed；history: available",
            "observed / available；表单历史固定available，MDF提交确认时间，不重建官方vintage",
          ],
        ]}
      />
      <details>
        <summary>8个统计序列</summary>
        <CompactTable
          label="统计序列"
          headers={["series_id", "指标 / 原生单位"]}
          rows={profile.series.map(([id, title]) => [id, title])}
        />
      </details>
      <p>
        {profile.historyNotice} BEA T10106 / T10406 的金额按
        UNIT_MULT=6，不按表头 billions 再缩放；官方增长读 T10101，贡献读
        T10102。四表有独立观察时间，不是原子发布快照；链价水平不可任意相加。原生历史起点未核验。
      </p>
      <details>
        <summary>返回字段、类型与单位</summary>
        <CompactTable
          label="返回字段"
          headers={["字段", "类型 / nullable", "单位与含义"]}
          rows={[
            [
              "start_quarter / end_quarter / series_ids",
              "string / string / string[]",
              "原生季度边界与系列代码",
            ],
            [
              "points[].quarter / series_id / value",
              "string / string / decimal string|null",
              "季度倒序，同季按注册指标顺序；缺行和官方null均保留逐点状态，零不等于缺值",
            ],
            [
              "value_unit / statistic / chained_base",
              "string / string / string|null",
              "million_chained_2017_USD / SAAR_level；percent / official_annualized_qoq；percentage_points / contribution_to_annualized_gdp_growth",
            ],
            [
              "change_1q_ratio / change_4q_ratio",
              "decimal string|null",
              "仅两个水平系列；未年化，同截止端点，0.01=1%；其余六项为null，不派生官方增长或贡献",
            ],
            [
              "availability / missing_reason / calculation_missing",
              "string / string|null / object",
              "available或missing；区分local_record_missing、official_null、local_availability_unconfirmed；计算缺值按指标记录，不补零",
            ],
            [
              "coverage / status",
              "object / string",
              "expected / available / official_null / missing；complete / partial / unavailable，不保证比较基期齐全",
            ],
            [
              "origin / observed_at / available_at / revision / batch_id",
              "string / ISO / ISO|null / integer|null / string|null",
              "local或upstream；取得与提交确认时间分开。本地revision不是官方估计；临时值无持久批次",
            ],
            [
              "estimate_kind / event_binding_status",
              "string / string",
              "连续NIPA固定unknown / unconfirmed；数值相同或日历触发均不证明官方首发",
            ],
            [
              "related_releases",
              "object[]",
              "季度与指标相关材料；estimate_kind区分advance/second/third；scope_only且continuous_version_bound=false",
            ],
            [
              "publication_events / points[].publication_events",
              "object[]",
              "季度+估计的日历计划，release_at显示北京时间；取消/未知不推定已发布，也不证明事件值",
            ],
            [
              "query_mode / as_of / as_of_basis / local_only",
              "string / ISO|null / string / boolean",
              "模式、含时区截止、MDF时间口径与本地读取限制；历史不回退最新或补源",
            ],
            [
              "provider_requests / provider_attempts / source_failure",
              "integer / integer / string|null",
              "本次来源请求/尝试及失败分类；补源失败保留已有本地数据，不把缺口视为官方null。GDP无persisted字段；query不写库",
            ],
          ]}
        />
      </details>
      <h4>GET {profile.basePath}/releases</h4>
      <p>
        只读本地真实发布材料。reference_quarter=YYYY-Qn、event_id、source_version=YYYY-MM-DD、read_mode=event/source_history、as_of=带时区MDF可用截止均可选。返回
        events、as_of、read_mode、missing，与月度合同不同。
      </p>
      <p>
        事件按 reference_quarter + estimate_kind
        标识。claims.role：actual=本次估计，previous_estimate=同季度前一次估计，prior_original=上季度修订前，prior_revised=上季度修订后。previous_estimate_revision_pp
        是百分点差；来源版本保持日期精度，actual_at为空不补造时刻。晚取得材料不倒填MDF可用时间。当前摘要仅覆盖A191RL，其余七项返回not_covered_by_summary_adapter；无具名合格共识不计算surprise。日历计划独立于材料证据。
      </p>
      <h4>请求示例</h4>
      <RequestPreview request={request} />
      <CodeBlock
        copyLabel="复制GDP历史示例"
        text={`${profile.basePath}/query?start=2026-Q2&end=2026-Q3&series=A191RX,A191RL&local_only=true&query_mode=history&as_of_basis=available&as_of=2026-11-02T20%3A00%3A00%2B08%3A00`}
      />
      <CodeBlock
        copyLabel="复制GDP发布示例"
        text={`${profile.basePath}/releases?reference_quarter=2026-Q3&read_mode=event`}
      />
      <h4>响应示例（合成字段节选）</h4>
      <CodeBlock
        copyLabel="复制GDP响应示例"
        text={JSON.stringify(
          {
            start_quarter: "2026-Q3",
            end_quarter: "2026-Q3",
            points: [
              {
                series_id: "A191RL",
                quarter: "2026-Q3",
                value: "3.4",
                value_unit: "percent",
                change_1q_ratio: null,
                change_4q_ratio: null,
                estimate_kind: "unknown",
                event_binding_status: "unconfirmed",
              },
            ],
            local_only: true,
            source_failure: null,
          },
          null,
          2,
        )}
      />
      <h4>错误与缺值</h4>
      <CompactTable
        label="错误响应"
        headers={["HTTP", "classification / 语义"]}
        rows={[
          [
            "400",
            "unsupported_parameters、duplicate_parameter、invalid_local_only、gdp_timeseries_history_cutoff_required；非法季度/系列/时区截止被拒绝",
          ],
          [
            "403",
            "local_read_not_authorized、gdp_source_query_not_enabled或能力权限分类；不静默切换供应商",
          ],
          [
            "503",
            "gdp_timeseries_not_enabled、gdp_service_not_bound或存储失败分类；未部署时不代表空数据",
          ],
          [
            "200 partial / unavailable",
            "已有结果保留，逐点missing_reason及source_failure说明缺口；不将unknown推断为官方首发",
          ],
        ]}
      />
      <CodeBlock
        copyLabel="复制GDP错误示例"
        text={
          'HTTP 503\n{"status":"ERROR","classification":"gdp_timeseries_not_enabled"}'
        }
      />
      <p>
        完整服务说明：{profile.basePath}
        /api-help。采集、周期策略和执行记录暂未开放。
      </p>
    </section>
  );
}
