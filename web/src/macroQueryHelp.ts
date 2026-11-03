import type { MacroProfile } from "./macroTimeSeriesProfile";

type Help = {
  parameters: string[][];
  fields: string[][];
  source: string;
  coverage: string;
  missing: string;
  example: Record<string, unknown>;
  failureExample: string;
  periodNotice?: string;
  releaseHelp?: string;
  collectionHelp?: string;
  historyRequest?: string;
  releaseRequest?: string;
};
const historyParameters = [
  [
    "local_only",
    "否 / false（API默认）",
    "boolean 查询文本 true / false；当前表单发 false，历史发 true；history 始终禁止上游；query 不写库",
  ],
  [
    "query_mode",
    "否 / 按截止参数推断",
    "string；current / history；截止参数可推断 history；历史不回退最新值",
  ],
  [
    "as_of",
    "否 / 无",
    "带时区 ISO string，inclusive 截止；历史表单必填；北京时间 +08:00 编码为 %2B08%3A00",
  ],
  [
    "as_of_basis / vintage_mode",
    "否 / 无截止 current: observed；history 或有截止: available",
    "string；observed / available；同时提供须一致；available 是 MDF 提交后确认可用时间，observed 不证明盘中已可消费",
  ],
];
const commonFields = [
  [
    "start_month / end_month",
    "string / string；非空",
    "YYYY-MM，包含首尾；month 为统计归属月",
  ],
  [
    "selected_columns",
    "string[]；按列选择请求返回",
    "数值字段投影；省略 columns 保留默认完整字段，元数据不因列选择丢失",
  ],
  [
    "persisted / provider_requests / provider_attempts",
    "boolean / integer / integer",
    "query 的 persisted=false；本次逻辑来源请求和实际 HTTP 尝试次数，不表示历史采集次数",
  ],
  [
    "query_mode / as_of / as_of_basis",
    "string / ISO string|null / string",
    "查询模式、inclusive 截止及时间口径；所有操作数和比较期使用同一截止",
  ],
  [
    "publication_events / points[].publication_event",
    "object[] / object|null",
    "有证据的官方日历计划；未知不推算，不自动变为实际发布，不证明连续版本属于该事件",
  ],
];
const consumption: Help = {
  parameters: [
    [
      "start / end",
      "是",
      "string（YYYY-MM），包含首尾；start≥1901-01，最多2400目标月；比较需要前12月缓冲",
    ],
    [
      "series",
      "否 / 全部七系列",
      "string；DPCERC、DPCERX、DGDSRX、DSERRX、A067RC、A067RX、A072RC，逗号多选；不接受 basket / adjustment",
    ],
    [
      "measure",
      "否 / native",
      "string；仅 native，返回原生金额或储蓄率 value，不是价格指数",
    ],
    [
      "columns",
      "否 / 原有完整字段",
      "string CSV；value、mom_pct、yoy_pct、change_{1,3,6,12}m_ratio、change_{1,12}m_percentage_points；修改显示指标后发送，元数据保留",
    ],
    ...historyParameters,
  ],
  fields: [
    ...commonFields,
    [
      "series_ids / points[].series_id / month",
      "string[] / string / string",
      "七项 BEA 原生系列身份与统计月份",
    ],
    [
      "points[].value / value_unit",
      "decimal string / string；缺值证据可 null",
      "金额为 million_USD 或 million_chained_2017_USD；储蓄率 percent，3.8即3.8%，可为零或负数",
    ],
    [
      "points[].annual_rate / chained_base",
      "boolean / string|null",
      "金额 SAAR 是季调年率水平，不是年化增长；实际金额2017链价，商品与服务不可相加替代总量",
    ],
    [
      "points[].change_{1,3,6,12}m_ratio / mom_pct / yoy_pct",
      "decimal string|null；仅金额适用",
      "累计 ratio（0.01=1%）/ 环比 percent / 同比 percent；未年化，不替代事件公布率",
    ],
    [
      "points[].change_{1,12}m_percentage_points",
      "decimal string|null；仅储蓄率适用",
      "当期减比较期，单位百分点；不对储蓄率计算百分比增长",
    ],
    [
      "points[].origin / availability / revision / batch_id",
      "string / string / integer|null / string|null",
      "本地或临时上游身份；revision 1 仅首次本地观察，不是官方首发；临时值无已提交本地批次",
    ],
    [
      "points[].observed_at / available_at",
      "ISO string / ISO string|null",
      "取得时间 / 提交确认可消费时间；临时补源 available_at=null，不倒填历史可用性",
    ],
    [
      "points[].calculation_inputs / provider_calculations",
      "object / object",
      "同截止比较值及版本证据；provider_calculations.bea 保留原生行，retained_notes 保留来源 Notes",
    ],
    [
      "points[].raw_sha256 / raw_locator",
      "string / string|null",
      "去凭据 Results 投影摘要与留存定位，不称完整原始认证响应",
    ],
    [
      "missing / source_unavailable / withdrawn / calculation_missing",
      "object[]",
      "目标月缺行、官方空值、撤回与比较期缺失分别保留；空值不补零",
    ],
    [
      "complete / coverage_complete / status / source_failure",
      "boolean / boolean / string / object|null",
      "目标值完整与范围记录覆盖分别判断；部分来源失败保留本地结果及原因，不伪装完整",
    ],
  ],
  source:
    "BEA NIPA T20600 与 T20806；金额按 UNIT_MULT=6 保留百万美元原值，实际值为2017链价美元，不以CPI平减。两表分别请求，不是原子官方快照。储蓄率按原生百分数及百分点变化。事件材料与PCE价格共享具名PIO事件，解析证据按资产独立；HTML缺金额水平或商品/服务时逐项说明。",
  coverage:
    "complete 仅表示目标月数值完整，coverage_complete 表示记录覆盖；二者均不保证比较基期齐全。revision 1 是首次本地观察，不证明官方首发。",
  missing:
    "missing、source_unavailable 和 calculation_missing 分别记录目标缺行、来源空值与比较基期缺失；不以零或最新版本代替。",
  example: {
    start_month: "2026-09",
    end_month: "2026-09",
    series_ids: ["DPCERC", "A072RC"],
    measure: "native",
    points: [
      {
        series_id: "DPCERC",
        month: "2026-09",
        value: "22452780",
        value_unit: "million_USD",
        annual_rate: true,
        chained_base: null,
        change_1m_ratio: "0.0051",
        mom_pct: "0.51",
      },
      {
        series_id: "A072RC",
        month: "2026-09",
        value: "3.8",
        value_unit: "percent",
        annual_rate: false,
        chained_base: null,
        change_1m_percentage_points: "-0.2",
      },
    ],
    missing: [],
    calculation_missing: [],
    persisted: false,
    provider_requests: 0,
    provider_attempts: 0,
    query_mode: "current",
  },
  failureExample:
    'HTTP 403\n{"status":"ERROR","classification":"local_read_not_authorized"}',
};
const retail: Help = {
  parameters: [
    [
      "start / end",
      "是",
      "string（YYYY-MM），包含首尾；API最多2520输入月（含12月比较缓冲），当前表单最多2400目标月",
    ],
    [
      "basket",
      "否 / headline,control",
      "string CSV；headline、control（core别名）、ex_auto、all；本表单默认总体/控制组；仅季调，不接受 measure / adjustment / series",
    ],
    [
      "columns",
      "否 / 原有完整字段",
      "string CSV；index_value、change_{1,3,6,12}m_ratio、official_mpcsm_percent；index_value投影 *_level，变化列投影 *_rates，元数据保留",
    ],
    ...historyParameters,
  ],
  fields: [
    ...commonFields,
    [
      "points[].month / snapshot_identity",
      "string / string",
      "每参考月一条，包含空值月份；查询投影身份不是官方原子版本",
    ],
    [
      "points[].headline_level / control_level / ex_auto_level",
      "object；value为decimal string|null",
      "value/reason/inputs/formula/unit；单位 million_current_usd，季调非年化；控制组与除汽车为MDF推导",
    ],
    [
      "points[].headline_rates / control_rates / ex_auto_rates",
      "object；各期限项value为decimal string|null",
      "1/3/6/12月累计 ratio（0.01=1%），未经年化；缺输入或零分母保留 reason",
    ],
    [
      "points[].official_headline_mpcsm",
      "object；value为decimal string|null",
      "Census 官方 percent；0.8即0.8%，不再乘100，与派生变化率分别保留",
    ],
    [
      "points[].derivations / input_versions / source_cells",
      "object / object / object[]",
      "派生身份、公式、同截止当前/比较输入版本及六项原始输入；保留状态、原始值、摘要与定位",
    ],
    [
      "input_versions 中 local_revision / observed_at / available_at",
      "integer|null / ISO string / ISO string|null",
      "本地修订、取得和提交确认时间；revision 1 仅首次本地观察，不是官方首发；上游临时值不冒充已入库",
    ],
    [
      "status / source_failure",
      "string（OK|PARTIAL）/ object|null",
      "补源暂时失败或无记录可部分返回；失败对象含classification/http_status/retry_at；OK不保证无缺口",
    ],
    [
      "source_no_content",
      "object[]",
      "HTTP204空正文及请求月份；不等于官方null、服务故障或已发布，不生成批次",
    ],
    [
      "missing_input_count / last_completed_month / source_skipped_months",
      "integer / string / string[]",
      "含12月缓冲的缺失输入数 / UTC上一完整月 / 未请求的未结束或未来月份；响应仍保留目标月份",
    ],
  ],
  source:
    "Census MARTS，US、seasonally_adj=yes、error_data=no。总体=44X72；控制组=44X72−441−444−447−722（含445）；除汽车=44X72−441。复用五个SM与一个MPCSM输入。派生保留公式及同截止输入版本，不能称为官方控制组。官方MPCSM已是百分数，派生变化为Decimal ratio。",
  coverage:
    "OK 不保证无缺口；PARTIAL 表示本次补源未完整返回。原始输入的 missing、not_provided、suppressed、unavailable 分别保留，不把无本地证据当作官方空值。日历依据 Data Month，不按月份推算发布时间。",
  missing:
    "数值对象的 reason 与 source_cells 的 status 分别解释缺输入、抑制值及官方缺行；未来月份、HTTP204与本地未知不合并成官方null。",
  example: {
    status: "OK",
    start_month: "2026-09",
    end_month: "2026-09",
    points: [
      {
        month: "2026-09",
        headline_level: {
          value: "745021",
          reason: null,
          unit: "million_current_usd",
        },
        control_level: {
          value: "400184",
          reason: null,
          unit: "million_current_usd",
        },
        official_headline_mpcsm: {
          value: "0.8",
          reason: null,
          unit: "percent",
        },
      },
    ],
    source_failure: null,
    source_no_content: [],
    persisted: false,
    provider_requests: 0,
    provider_attempts: 0,
    query_mode: "current",
  },
  failureExample:
    'HTTP 403\n{"status":"ERROR","classification":"local_read_not_authorized"}\nHTTP 200（部分返回，字段节选）\n{"status":"PARTIAL","source_failure":null,"source_no_content":[{"classification":"census_retail_no_content","http_status":204,"response_bytes":0,"start_month":"2026-10","end_month":"2026-10"}],"persisted":false}',
};

const gdp: Help = {
  parameters: [
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
      "否 / true（API默认）",
      "boolean 查询文本 true / false；当前表单显式 false，历史 true；服务授权约束保留，临时补源不写库，部分失败保留本地值",
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
  ],
  fields: [
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
  ],
  periodNotice:
    "季度 quarter 是统计归属季度。当前查询显式 local_only=false，优先本地，缺口自动请求上游；history 显式 true，只读本地，全部比较端点使用同一 available 截止。available_at 是 MDF 提交确认可用时间；observed_at 不是官方发布时间，不重建官方 vintage。",
  releaseHelp:
    "reference_quarter=YYYY-Qn、event_id、source_version=YYYY-MM-DD、read_mode=event/source_history、as_of=带时区MDF可用截止。返回 events、as_of、read_mode、missing。事件按 reference_quarter + estimate_kind 标识。claims.role：actual=本次估计，previous_estimate=同季度前次估计，prior_original=上季度修订前，prior_revised=上季度修订后；previous_estimate_revision_pp 为百分点差。当前摘要仅覆盖 A191RL，其余七项为 not_covered_by_summary_adapter；scope_only 且 continuous_version_bound=false，不以数值相等认定版本绑定，无具名共识不计算 surprise。",
  source:
    "BEA T10106 / T10406 金额按 UNIT_MULT=6，不按表头 billions 再缩放；官方增长读 T10101，贡献读 T10102。四表有独立观察时间，不是原子发布快照；链价水平不可任意相加。原生历史起点未核验。",
  coverage:
    "coverage 区分 expected / available / official_null / missing；complete / partial / unavailable 只描述目标值，不保证比较基期完整；本地 revision 不是官方估计版本，连续NIPA estimate_kind=unknown、event_binding_status=unconfirmed。",
  missing:
    "local_record_missing、official_null、local_availability_unconfirmed 与派生 calculation_missing 分别保留，缺值不补零；补源失败保留本地值及 source_failure 安全分类。",
  example: {
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
    local_only: false,
    provider_requests: 0,
    provider_attempts: 0,
    source_failure: null,
  },
  failureExample:
    'HTTP 400\n{"status":"ERROR","classification":"gdp_timeseries_history_cutoff_required"}\nHTTP 403\n{"status":"ERROR","classification":"gdp_source_query_not_enabled"}\nHTTP 503\n{"status":"ERROR","classification":"gdp_timeseries_not_enabled"}',
  collectionHelp:
    "POST jobs 精确四键 request_id/action/start/end；UUID，action=init/update，季度 YYYY-Qn，八系列一起采集。202 只代表任务受理，GET jobs/executions 核验真实运行、完成及回读状态。PUT strategy 控制策略；GET strategy/schedule 读取独立 worker 心跳与计划，启用不代表进程运行，日历不代表已执行。",
  historyRequest:
    "/query?start=2026-Q2&end=2026-Q3&series=A191RX,A191RL&local_only=true&query_mode=history&as_of_basis=available&as_of=2026-11-02T20%3A00%3A00%2B08%3A00",
  releaseRequest: "/releases?reference_quarter=2026-Q3&read_mode=event",
};

const claims: Help = {
  parameters: [
    [
      "start / end",
      "是",
      "YYYY-MM-DD，含首尾自然日，投影其中周六；最大36600天（100年），表单起点≥1901-01-01；默认最近约52周可编辑",
    ],
    [
      "series",
      "否 / 全部六系列",
      "原生 FRED ID CSV：ICSA,ICNSA,CCSA,CCNSA,IC4WSA,CC4WSA；不接受月份 reference_month、basket、adjustment、measure、columns 或 vintage_mode",
    ],
    [
      "local_only",
      "否 / true（API默认）",
      "boolean 查询文本 true / false；当前表单显式 false，历史 true；来源受服务授权约束，补源不写库，不覆盖本地值或提供方缺值",
    ],
    [
      "query_mode",
      "否 / current（有as_of时history）",
      "current / history；显式 history 必须有截止，current 不得带截止",
    ],
    [
      "as_of",
      "历史必填",
      "带时区 ISO8601，inclusive 截止；北京时间表单转 +08:00，历史不回退最新值",
    ],
    [
      "as_of_basis",
      "否 / available",
      "available / observed；表单固定 available，全部操作数同 cutoff",
    ],
  ],
  fields: [
    [
      "asset_id / start_date / end_date / buffer_start_date",
      "string",
      "资产与自然日范围；计算含28天缓冲",
    ],
    [
      "rows[].series_id / week_ending / value / unit",
      "string / string / decimal string|null / string",
      "每 series 独立周六，整体倒序，同周按请求 series 顺序稳定；单位 Number",
    ],
    [
      "rows[].weekly_change / weekly_change_ratio / local_four_week_average",
      "object（local_four_week_average可null）；value为decimal string|null",
      "周数值差 Number；周比率 ratio（0.01=1%）；本地四周均值 Number、local_derived。IC4WSA/CC4WSA 为官方四周均值，不以本地均值替代，也不重复派生",
    ],
    [
      "rows[].provenance.origin / observed_at / available_at / revision / batch_id",
      "string / ISO / ISO|null / integer|null / string|null",
      "以上均位于 rows[].provenance；取得与提交确认时间分开，临时值无持久批次。rows[].value_origin 标识官方系列经 FRED 传输，first_capture_provenance 保留首次本地捕获，不证明官方首发",
    ],
    [
      "coverage / status / source_failure",
      "object / string / string|null",
      "coverage.expected / available / missing_rows / provider_missing_values 为目标值计数；不保证计算基期完整。status=complete/partial/unavailable；补源失败保留本地结果及安全分类",
    ],
    [
      "query_mode / as_of / as_of_basis / local_only",
      "string / ISO|null / string / boolean",
      "查询模式、同截止和本地限制；历史无最新回退或上游请求",
    ],
    [
      "provider_requests / provider_attempts",
      "integer / integer",
      "本次逻辑来源请求及实际 HTTP 尝试数；query 不落库",
    ],
    [
      "rows[].related_releases / 派生inputs",
      "object[] / object[]",
      "材料关联与每项计算操作数溯源；材料 actual 不自动绑定当前连续版本",
    ],
  ],
  periodNotice:
    "week_ending 为各系列独立所属周，初请与续请不可从发布日期减固定天数推导。当前表单 local_only=false 自动临时补缺；history true，只读本地，所有操作数同 available 截止；取得时间不等于提交确认时间。",
  releaseHelp:
    "as_of、read_mode=event/source_history、event_id=dol-claims-YYYY-MM-DD、reference_week=周六YYYY-MM-DD、source_version=YYYY-MM-DD 严格过滤；reference_week 为材料覆盖过滤，可含 actual 与 prior，不使用 reference_month。返回 events/as_of/read_mode。actual=本次值；official_previously_reported_prior=材料明确引述官方原前值；prior_as_reported_in_this_event=本次报告前值。MDF事前值缺足够时间及版本证明时 unknown；连续版本未绑定时 event actual 不代表当前值。",
  source:
    "原机构 DOL-ETA，传输提供者 FRED，不静默替换提供者。FRED realtime_start/end 保持日期精度及 fred_not_disambiguated 依据，last_updated 不是官方发布时间。只显示已保存材料支持的历史发布；embargo 仅是计划，未知实际时间不补造；无具名共识不计算 surprise。",
  coverage:
    "各系列分别保留所属周与修订；coverage 只描述目标水平，不保证四周计算基期完整；零不等于缺值。",
  missing:
    "local_not_provided 为未采集或来源未返回；provider_missing 是 FRED 提供方缺值，不称官方 null；zero_denominator 不可计算比率；缺周不补零。",
  example: {
    asset_id: "mdf.macro.dol.claims",
    rows: [
      {
        series_id: "ICSA",
        week_ending: "2026-10-24",
        value: "202000",
        unit: "Number",
        weekly_change: { value: "2000", unit: "Number" },
        weekly_change_ratio: {
          value: "0.01",
          unit: "ratio",
          missing_reason: null,
        },
      },
    ],
    as_of_basis: "available",
    local_only: false,
    provider_requests: 0,
    provider_attempts: 0,
    source_failure: null,
  },
  failureExample:
    'HTTP 400\n{"status":"ERROR","classification":"claims_history_cutoff_required"}\nHTTP 403\n{"status":"ERROR","classification":"claims_source_query_not_enabled"}\nHTTP 503\n{"status":"ERROR","classification":"claims_timeseries_not_enabled"}',
  collectionHelp:
    "POST jobs 精确四键 request_id/action/start/end；UUID，action=init/update，六系列一起采集。初始化默认20日历年可修改，含28天计算缓冲。202 仅证明 durable running 回执；GET jobs/executions 追踪，complete 需本地回读，材料状态另列。紧凑回执 series_coverage 保留各自 latest_week、missing_weeks_total/trailing_missing_weeks_total；缺周样本最多16且显式 truncated，完整序列由 query 获取。GET strategy/schedule 读取独立 worker 心跳及计划，reference_week=null 表示参考周待发布材料确认；GET 不刷新日历、不启动 worker。",
  historyRequest:
    "/query?start=2026-10-01&end=2026-10-31&series=ICSA,CCSA&query_mode=history&local_only=true&as_of_basis=available&as_of=2026-11-02T18%3A00%3A00Z",
  releaseRequest:
    "/releases?event_id=dol-claims-2026-10-29&reference_week=2026-10-24&source_version=2026-10-29&read_mode=event",
};

const m3: Help = {
  parameters: [
    ["start / end", "是", "YYYY-MM，含首尾，最多2400个月"],
    ["series", "否 / 全部三项", "三项 series_id，逗号分隔"],
    ["dataset", "否 / m3", "m3 完整报告；advm3 预报；彼此独立"],
    [
      "local_only",
      "否 / true",
      "当前页面发 false 允许临时补源；历史页面发 true；查询永不写库",
    ],
    ["query_mode", "否 / current", "current 或 history，历史必须指定 as_of"],
    ["as_of", "否", "含时区的历史截止时刻，inclusive"],
    [
      "as_of_basis",
      "否 / available",
      "available 为 MDF 提交后确认可用；observed 为来源观察时间",
    ],
  ],
  fields: [
    ["dataset / report_stage", "string", "独立的 m3/full 或 advm3/advance"],
    ["points[].series_id / month", "string", "指标及参考月"],
    [
      "points[].value",
      "decimal string|null",
      "百万当期美元；零有效，缺值为 null",
    ],
    ["points[].source", "object", "季调、月频、单位、报告阶段和原始来源指针"],
    [
      "points[].observed_at / available_at",
      "ISO string|null",
      "来源观察时刻 / MDF 提交确认时刻；临时补源 available_at=null",
    ],
    [
      "missing / source_failure / budget_exhausted",
      "array / object|null / boolean",
      "完整缺口、安全来源失败和有界查询状态",
    ],
    [
      "coverage / persisted / provider_requests / provider_attempts",
      "object / boolean / integer / integer|null",
      "有效值覆盖、是否写库及本次请求次数；未知次数为 null",
    ],
    [
      "query_mode / as_of / as_of_basis",
      "string / ISO string|null / string",
      "历史截止及观察或可用时间口径",
    ],
  ],
  source:
    "Census MDM/NO、NXA/NO、NXA/VS 分别请求；新订单扣除取消订单。m3 与 advm3 不拼接，快照不证明首发。",
  coverage:
    "本地已有值优先，补源失败保留已有值和全部缺口；每次至多六个分片并有总耗时预算。",
  missing:
    "null、未返回和真实零分开；同一截止只读本地，不用今天值重建官方历史。",
  periodNotice:
    "参考月不是发布时间；临时补源未落库，available_at 为 null。历史截止读取 MDF observed 或 available，原始响应保留报告阶段。",
  example: {
    dataset: "m3",
    report_stage: "full",
    query_mode: "current",
    as_of: null,
    as_of_basis: "available",
    local_only: false,
    points: [
      {
        series_id: "durable_goods_new_orders",
        month: "2025-01",
        dataset: "m3",
        value: "100",
        status: "available",
        origin: "upstream",
        observed_at: "2026-11-03T00:00:00Z",
        available_at: null,
        revision: null,
      },
    ],
    coverage: { expected: 1, available: 1 },
    missing: [],
    source_failure: null,
    budget_exhausted: false,
    status: "complete",
    persisted: false,
    provider_requests: 1,
    provider_attempts: 1,
  },
  failureExample:
    'HTTP 400\n{"status":"ERROR","classification":"m3_request_invalid"}\nHTTP 403\n{"status":"ERROR","classification":"m3_source_not_bound"}',
  collectionHelp:
    "POST jobs 四键 request_id/action/start/end，采集 m3 全部三项；后台按已验证的最多240个月窗口逐指标推进，并保存已确认分片与待请求进度。240个月三指标通常三次来源请求，这不是官方范围上限。来源缺值可完成并明确缺口，未请求不冒充来源缺值；来源失败或存储未知时 POST jobs/resume 使用同一编号和范围继续；限流等待保留 retry_at。新更新重新核验。GET releases 只读官方材料；未留存时返回证据空态。GET/PUT strategy 读取或保存暂停默认的周期策略，GET schedule 读取官方计划；只有显式运行 worker 才有进程和心跳。",
  historyRequest:
    "/query?start=2025-01&end=2025-02&dataset=m3&query_mode=history&local_only=true&as_of_basis=available&as_of=2026-11-03T12%3A00%3A00Z",
};

export function nativeQueryHelp(profile: MacroProfile): Help | undefined {
  if (profile.id === "m3") return m3;
  if (profile.id === "jolts") return jolts;
  if (profile.id === "gdp") return gdp;
  if (profile.id === "claims") return claims;
  return profile.id === "retail"
    ? retail
    : profile.id === "consumption"
      ? consumption
      : undefined;
}

const jolts: Help = {
  parameters: [
    ["start / end", "是", "YYYY-MM，包含首尾，最多2400月；比较需要前12月缓冲"],
    [
      "series",
      "否 / 八项全国非农SA",
      "BLS原生ID逗号多选；数量JOL/HIL/QUL/LDL与官方Rate JOR/HIR/QUR/LDR独立；不接受basket/adjustment",
    ],
    [
      "measure",
      "否 / index",
      "仅index；兼容接口名称，实际为数量或官方比率，非价格指数",
    ],
    [
      "columns",
      "否 / 全部数值字段",
      "CSV：value、change_1m_level、change_1m_ratio、change_12m_ratio、change_1m_percentage_points、change_12m_percentage_points；修改列后发送",
    ],
    ...historyParameters.map((p) =>
      p[0] === "local_only" ? [p[0], "否 / true（API默认）", p[2]] : p,
    ),
  ],
  fields: [
    ...commonFields,
    [
      "points[].series_id / month",
      "string / YYYY-MM",
      "全国非农、SA原生身份；月份倒序展示",
    ],
    [
      "points[].value / value_unit",
      "decimal string|null / string",
      "thousand_jobs为千岗位；thousand_persons为千人次流量，非去重人数；官方Rate为percent，4.3即4.3%，不再乘100",
    ],
    [
      "points[].change_1m_level",
      "decimal string|null",
      "数量月差，原生单位；不适用于Rate",
    ],
    [
      "points[].change_1m_ratio / change_12m_ratio",
      "decimal string|null",
      "数量未年化累计ratio；0.01=1%，表格按百分比显示",
    ],
    [
      "points[].change_1m_percentage_points / change_12m_percentage_points",
      "decimal string|null",
      "仅官方Rate的月/年端点差，单位百分点",
    ],
    [
      "points[].observed_at / available_at / revision",
      "ISO string / ISO string|null / integer|null",
      "取得/提交确认时间；首次本地捕获不是官方首发；临时值无持久批次或available时间",
    ],
    [
      "points[].provider_calculations / provider_status",
      "object / object",
      "BLS原计算和P/R脚注独立于官方Rate及本地派生",
    ],
    [
      "points[].source_version / surprise",
      "object|null / object|null",
      "没有独立证据不绑定事件或版本；没有具名事前共识不算预期差",
    ],
  ],
  source:
    "BLS JOLTS八项原生序列，无其他provider补源。source_qualified=false；定义/方法与具名官方发布正文仍缺，403缺口保留，不阻断连续序列。",
  coverage:
    "初始化默认240个月可编辑；锚点仅为预置月份依据，实际覆盖单独展示。月末职位空缺存量不可跨月求和。",
  missing:
    "0是有效数值；官方null、本地缺行和零分母分别保留。零分母ratio返回null/zero_denominator；不补零。",
  periodNotice:
    "数量和官方Rate独立；所有比较操作数使用同一历史截止，history始终local_only=true。",
  releaseHelp:
    "发布计划不代表实际发布，首次捕获不代表首发。发布记录仅本地留存；未绑定版本与未取得材料如实提示。",
  collectionHelp:
    "POST jobs 使用request_id/action/start/end及可选series，UUID重放不重复执行。initialize/update显式刷新及12月缓冲；GET jobs/executions读取真实结果。策略启用、进程、心跳、完成和目标可用分别报告。五年维护为MDF保守策略，不冒称官方基准修订日。GET不刷新日历或启动worker。",
  example: {
    start_month: "2026-08",
    end_month: "2026-08",
    points: [
      {
        series_id: "JTS000000000000000JOR",
        month: "2026-08",
        value: "4.3",
        value_unit: "percent",
        change_1m_percentage_points: "0.1",
      },
    ],
    local_only: true,
    provider_requests: 0,
    persisted: false,
  },
  failureExample:
    'HTTP 503\n{"status":"ERROR","classification":"jolts_timeseries_not_enabled"}',
  historyRequest:
    "/query?start=2026-07&end=2026-08&query_mode=history&local_only=true&as_of_basis=available&as_of=2026-11-03T00%3A00%3A00Z",
  releaseRequest: "/releases?read_mode=event",
};
