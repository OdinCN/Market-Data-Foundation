import type { MacroProfile } from "./macroTimeSeriesProfile";

type Help = {
  parameters: string[][];
  fields: string[][];
  source: string;
  coverage: string;
  missing: string;
  example: Record<string, unknown>;
  failureExample: string;
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
export function nativeQueryHelp(profile: MacroProfile): Help | undefined {
  return profile.id === "retail"
    ? retail
    : profile.id === "consumption"
      ? consumption
      : undefined;
}
