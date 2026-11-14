import { CodeBlock, CompactTable } from "./CpiQueryDetails";
import type { FamilyProps } from "./RuntimeFamily";
import { useVisiblePolling } from "./useVisiblePolling";

export function TdxApiDocumentation({
  transport,
  roster = false,
}: Pick<FamilyProps, "transport"> & { roster?: boolean }) {
  const prefix = `/api/r5/tdx/${roster ? "roster" : "daily"}`;
  const help = useVisiblePolling(
    async (signal) => {
      const response = await transport(prefix + "/help", {
        signal,
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) throw Error("help_unavailable");
      return (await response.json()) as { notes?: string[] };
    },
    [transport, prefix],
    true,
    () => true,
    false,
  );
  const query = roster
    ? `${prefix}/query?day=2026-11-12&market=2&search=920002&limit=100`
    : `${prefix}/query?market=1&instrument=600519&start=2026-11-09&end=2026-11-12&order=desc&local_only=true&basis=native_projection&limit=100`;
  return (
    <section
      aria-label={roster ? "TDX 名册 API说明" : "TDX API说明"}
      className="cpi-api-help"
    >
      <h4>GET {prefix}/query</h4>
      <p>
        {roster
          ? "独立基础名册：只读已发布快照，日线使用同一 snapshot_id。来源目录与交易所生命周期证据分别保留，不能用当前存续名册推造完整历史。"
          : "证券历史与单日横截面共用本地查询；价基 native_projection，收益口径 NOT_QUALIFIED。所有 GET 均不触发来源请求或创建采集作业。"}
      </p>
      <CompactTable
        label="接口与方法"
        headers={["方法 / 路径", "作用与返回", "边界"]}
        rows={[
          [
            "GET " + prefix + "/status",
            "能力与默认范围 / 名册有效性",
            "零来源请求",
          ],
          [
            "GET " + prefix + "/query",
            "分页 rows、total、next_offset",
            "只读已发布本地版本",
          ],
          ...(!roster
            ? [
                [
                  "GET " + prefix + "/summary",
                  "本地日线持有摘要",
                  "等待中不等于空持有",
                ],
                [
                  "GET " + prefix + "/coverage",
                  "逐市场义务及独立缺项分页",
                  "day必填；可指定request_id、as_of、offset≥0、limit=1..10000",
                ],
                [
                  "GET /api/r5/tdx/roster/query",
                  "独立基础名册",
                  "旧daily/universe保留兼容；不能用刷新目录延长官方证据有效期",
                ],
              ]
            : []),
          [
            "GET " + prefix + "/jobs 或 /jobs/{request_id}",
            "真实执行列表 / 分片详情",
            roster
              ? "仅名册刷新作业；与日线共用执行器和请求身份"
              : "旧universe作业继续可查；基础名册新入口独立可见",
          ],
          [
            "POST " + prefix + "/jobs",
            "202接收回执，不代表执行完成",
            roster
              ? "只接受request_id；三市刷新复用旧action=universe"
              : "action=init|update；旧universe继续兼容",
          ],
          [
            "POST " + prefix + "/jobs/resume",
            "续跑原request_id的未完成步骤",
            "不新建请求身份；完成步骤不重复执行",
          ],
          [
            "GET /api/r5/tdx/daily/strategy；POST 同路径",
            "交易日北京时间09:00刷新目录，15:35复用目录采集一次日线，范围从当日往前14个自然日至当日，同批比对历史修订；POST体为enabled布尔值",
            "名册刷新与日线共用此策略，不启动另一worker",
          ],
        ]}
      />
      <CompactTable
        label="查询参数"
        headers={["参数", "必填 / 默认", "类型、含义与边界"]}
        rows={
          roster
            ? [
                [
                  "day",
                  "否 / 最近盘后且有交易所名册证据的日期（无交易所日期时用盘后日期）",
                  "YYYY-MM-DD；判定该日生命周期与证据有效性，不是回造历史存续名册",
                ],
                ["market", "否 / 全部", "整数0深市、1沪市、2北交所"],
                [
                  "search",
                  "否 / 空",
                  "代码、简称、拼音片段或简拼；忽略空格、全半角和大小写。涵盖深交所简称变更、通达信沪北股票更名和三市名册曾记录的名称；准确输入六位代码可查看该证券的名称记录，先筛选后分页",
                ],
                [
                  "snapshot_id",
                  "否 / 最新完整发布",
                  "精确已发布快照；与as_of合取，截止前不可见不会借用后来版本",
                ],
                [
                  "as_of",
                  "否 / 当前",
                  "含时区ISO8601；接受微秒，北京时间如2026-11-13T00:00:00.123456+08:00",
                ],
                [
                  "offset / limit",
                  "否 / 0、100",
                  "offset≥0；limit=1..1000，末页next_offset=null",
                ],
              ]
            : [
                [
                  "day",
                  "横截面必填",
                  "YYYY-MM-DD；与start/end互斥；不传market时返回三市",
                ],
                [
                  "start / end",
                  "历史必填",
                  "YYYY-MM-DD含两端；1990-01-01≤start≤end≤2100-12-31",
                ],
                [
                  "market / instrument",
                  "否 / 全部",
                  "market=0深市、1沪市、2北交所；instrument为六位代码，提供时必须同时指定market",
                ],
                [
                  "as_of",
                  "否 / 当前",
                  "含时区ISO8601，接受微秒；过滤MDF实际版本可用时间。不是来源首发，source_event_time未知",
                ],
                [
                  "local_only / basis",
                  "否 / true、native_projection",
                  "仅接受这两个值；拒绝联网查询、前后复权或其他价基",
                ],
                [
                  "period / security_type",
                  "否 / 1d、stock",
                  "仅支持A股原生日线，不映射宏观指标",
                ],
                [
                  "order",
                  "否 / asc",
                  "仅asc/desc；表单历史显式desc，先全范围排序后分页",
                ],
                [
                  "offset / limit",
                  "否 / 0、100",
                  "offset≥0，limit=1..10000；按next_offset翻页，不再套一层客户端分页",
                ],
              ]
        }
      />
      <p>
        未知参数、重复参数和无效日期均拒绝。URL中的正时区加号应编码为%2B；页面请求预览与cURL会正确编码。
      </p>
      <details open>
        <summary>返回字段、类型与单位</summary>
        <CompactTable
          label="返回字段"
          headers={["字段", "类型", "语义"]}
          rows={[
            [
              "rows / total / offset / next_offset",
              "object[] / integer / integer / integer|null",
              "筛选后结果、总行数、当前偏移、下一页；空结果不代表采集完整",
            ],
            [
              "snapshot_id",
              "string|null",
              "本次实际使用的已发布名册；采集作业记录同一身份以供追踪",
            ],
            [
              "provider_requests",
              "integer",
              "本地读取固定0；采集作业另计真实来源请求",
            ],
            ...(roster
              ? [
                  [
                    "state / classification / valid_through",
                    "string / string|null / date|null",
                    "MISSING无快照、STALE证据过期、DATED有据日期、CURRENT_DIRECTORY_ONLY历史范围未知；DATED也不代表完整历史",
                  ],
                  [
                    "rows[].market / instrument / name / membership_state",
                    "integer / string / string / string",
                    "三市、六位代码、名称；ACTIVE/NOT_YET_LISTED/DELISTED_OR_TERMINATED/UNKNOWN，退市日为排除边界",
                  ],
                  [
                    "observed_at / available_at / sources[] / rows[].evidence",
                    "ISO time / ISO time / array / object",
                    "实际观察、发布可用时间、原件URL/摘要/接收时间；北交所旧代码与映射依据保留，代码沿革未核实不推造",
                  ],
                ]
              : [
                  [
                    "rows[].open / high / low / close",
                    "integer",
                    "每个整数单位为 0.001 CNY/股（人民币元/股）。显示价 = 原值 ÷ 1000。",
                  ],
                  [
                    "rows[].amount / volume",
                    "integer / integer",
                    "成交额每单位 0.001 元，显示金额 = amount ÷ 1000；volume 原值为手。手数 × 100 为百股粒度的换算，无法还原零股尾数。",
                  ],
                  [
                    "SDK 投射中的 Last / Order / UpCount / DownCount",
                    "保留在来源证据",
                    "Last 是分页解码的前收上下文，页首允许为 0。以上字段均未进入日线事实表；涨跌幅、复权价及复权因子也不在这六个行情字段中。",
                  ],
                  [
                    "rows[].trading_day / market / instrument",
                    "date / integer / string",
                    "交易日期、市场、六位代码；源事件绝对时点未知",
                  ],
                  [
                    "rows[].received_at / available_at / batch_id / source",
                    "ISO time / ISO time / string / string",
                    "真实接收与PG发布可用时间、留存批次及具名来源；不是历史官方发布时间",
                  ],
                  [
                    "price_basis / return_qualification / source_event_time",
                    "string / string / null",
                    "native_projection / NOT_QUALIFIED / null；不能据此声称未复权收益正确",
                  ],
                  [
                    "/coverage: markets / missing / missing_total",
                    "array / array / integer",
                    "名册、日历、有效行、已确认停牌和未知缺项分别核定；零成交/空响应不自动等于停牌",
                  ],
                ]),
          ]}
        />
      </details>
      <details>
        <summary>查询与采集示例</summary>
        <CodeBlock
          label="查询示例"
          copyLabel="复制查询示例"
          text={`curl --request GET '${window.location.origin}${query}'`}
        />
        {!roster && (
          <CodeBlock
            label="截面与覆盖示例"
            copyLabel="复制截面与覆盖示例"
            text={`${prefix}/query?day=2026-11-12&local_only=true&limit=100\n${prefix}/coverage?day=2026-11-12&offset=0&limit=100`}
          />
        )}
        <CodeBlock
          label="采集命令示例"
          copyLabel="复制采集命令示例"
          text={JSON.stringify(
            roster
              ? { request_id: "8c9604c4-3281-4f14-9f08-939a7a362d11" }
              : {
                  request_id: "8c9604c4-3281-4f14-9f08-939a7a362d11",
                  action: "update",
                  start: "2026-10-30",
                  end: "2026-11-12",
                  markets: [0, 1, 2],
                  instruments: ["0:000001", "1:600519", "2:920002"],
                },
            null,
            2,
          )}
        />
        <p>
          以上是命令示例，不会自动执行。POST要求已授权DEV、真实同源Origin及application/json。日线初始化默认1990-01-01至最近北京时间15:05后交易日期候选；更新回看14个日历日，禁止早盘采集未收盘日。交易所名册日期滞后时，当天完整刷新的三市行情目录仍可用于采集；当日全市场覆盖须等交易所名册核定。
        </p>
        <CodeBlock
          label="响应结构示例"
          copyLabel="复制响应结构示例"
          text={JSON.stringify(
            {
              rows: [],
              total: 0,
              offset: 0,
              next_offset: null,
              provider_requests: 0,
              ...(roster
                ? {
                    state: "MISSING",
                    classification: "tdx_universe_required",
                    snapshot_id: null,
                  }
                : {
                    price_basis: "native_projection",
                    return_qualification: "NOT_QUALIFIED",
                    source_event_time: null,
                  }),
            },
            null,
            2,
          )}
        />
        <p>
          响应结构示例为无本地匹配数据情形；实际数值以查询面板表格和原始响应为准。
        </p>
      </details>
      <details>
        <summary>错误与恢复</summary>
        <CompactTable
          label="错误与恢复"
          headers={["HTTP / 分类", "含义", "处理"]}
          rows={[
            [
              "400 / tdx_query_parameters、tdx_roster_parameters",
              "未知或重复参数",
              "按参数表修正，不能靠自动重试",
            ],
            [
              "400 / tdx_query_direction、tdx_query_contract、tdx_query_page",
              "查询方向、价基、页码不合法",
              "选择历史或截面的一种；保留本地native价基",
            ],
            [
              "400 / tdx_invalid_parameter、tdx_missing_parameter、naive_time",
              "日期/整数/必填字段或时区无效",
              "使用含时区截止，检查必填值",
            ],
            [
              "403 / operator_not_authorized、tdx_capability_not_enabled",
              "DEV来源/授权/能力未开",
              "核对当前入口与已有授权；不伪造Origin、不切换提供者",
            ],
            [
              "404 / job_not_found；409 / job_running",
              "请求不存在 / 原执行仍持锁",
              "核对原request_id或等待实际执行，勿重复提交",
            ],
            [
              "503 / 来源或存储分类",
              "读取能力不可用，或提交前故障",
              "保留分类；不将故障显示为空结果",
            ],
            [
              "作业PARTIAL / tdx_universe_required、tdx_roster_stale",
              "无已发布名册 / 证据不足以覆盖结束日期",
              "从基础名册核对发布和官方证据，再续跑原作业",
            ],
            [
              "作业PARTIAL / commit_outcome_unknown、tdx_page_offset_exhausted",
              "提交回执未知 / 来源偏移范围耗尽",
              "保留原始批次与请求身份，核实后续跑；不得重建大采集或掩盖失败",
            ],
          ]}
        />
      </details>
      {help.busy && !help.data && <p role="status">正在读取来源说明…</p>}
      {help.failure && <p role="alert">API说明读取失败，请刷新核实。</p>}
      {help.data?.notes && (
        <details>
          <summary>来源、证据与兼容说明</summary>
          <ul>
            {help.data.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
