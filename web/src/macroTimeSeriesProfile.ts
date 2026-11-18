import {
  M3,
  JOLTS,
  CLAIMS,
  GDP,
  assetTitle,
  CPI,
  PPI,
  PCE,
  EMPLOYMENT,
  RETAIL,
  CONSUMPTION,
  type Key,
} from "./workflowModel";

export type MacroProfile = Readonly<{
  id:
    | "m3"
    | "cpi"
    | "ppi"
    | "pce"
    | "employment"
    | "retail"
    | "consumption"
    | "gdp"
    | "claims"
    | "jolts";
  label: string;
  provider: "BLS" | "BEA" | "Census" | "FRED";
  asset: Key;
  basePath: string;
  resumePath?: string;
  series: readonly (readonly [string, string, string, string])[];
  historyNotice?: string;
  defaultQuerySeries?: readonly string[];
}>;
export const CPI_PROFILE: MacroProfile = Object.freeze({
  id: "cpi",
  label: "CPI",
  provider: "BLS",
  asset: CPI,
  basePath: "/api/r5/cpi/timeseries",
  defaultQuerySeries: [
    "CUSR0000SA0",
    "CUUR0000SA0",
    "CUSR0000SA0L1E",
    "CUUR0000SA0L1E",
  ],
  series: [
    ["CUSR0000SA0", "总体 · 季调", "headline", "sa"],
    ["CUUR0000SA0", "总体 · 未季调", "headline", "nsa"],
    ["CUSR0000SA0L1E", "核心 · 季调", "core", "sa"],
    ["CUUR0000SA0L1E", "核心 · 未季调", "core", "nsa"],
    ["CUSR0000SAH1", "居住 · 季调", "shelter", "sa"],
    ["CUUR0000SAH1", "居住 · 未季调", "shelter", "nsa"],
    ["CUSR0000SAF1", "食品 · 季调", "food", "sa"],
    ["CUUR0000SAF1", "食品 · 未季调", "food", "nsa"],
    ["CUSR0000SA0E", "能源 · 季调", "energy", "sa"],
    ["CUUR0000SA0E", "能源 · 未季调", "energy", "nsa"],
    ["CUSR0000SACL1E", "核心商品 · 季调", "core_goods", "sa"],
    ["CUUR0000SACL1E", "核心商品 · 未季调", "core_goods", "nsa"],
    [
      "CUSR0000SASLE",
      "服务（不含能源服务） · 季调",
      "services_less_energy",
      "sa",
    ],
    [
      "CUUR0000SASLE",
      "服务（不含能源服务） · 未季调",
      "services_less_energy",
      "nsa",
    ],
  ] as const,
  historyNotice:
    "短期研究使用季调（SA）变化，年度研究使用未季调（NSA）同比；各行保留独立来源序列，不拼接。采集覆盖总体、核心、居住、食品、能源、核心商品和服务（不含能源服务）。实际覆盖以来源返回为准，原生历史起点未知时不推算。",
});
export const PPI_PROFILE: MacroProfile = Object.freeze({
  id: "ppi",
  label: "PPI",
  provider: "BLS",
  asset: PPI,
  basePath: "/api/r5/ppi/timeseries",
  defaultQuerySeries: ["WPSFD4", "WPUFD4", "WPSFD49104", "WPUFD49104"],
  series: [
    ["WPSFD4", "总量 · 季调", "headline", "sa"],
    ["WPUFD4", "总量 · 未季调", "headline", "nsa"],
    ["WPSFD49104", "核心 · 季调", "core", "sa"],
    ["WPUFD49104", "核心 · 未季调", "core", "nsa"],
    ["WPSFD49116", "剔除食品、能源及贸易服务 · 季调", "core_ex_trade", "sa"],
    ["WPUFD49116", "剔除食品、能源及贸易服务 · 未季调", "core_ex_trade", "nsa"],
  ] as const,
  historyNotice:
    "核心指剔除食品和能源的最终需求，仍含贸易服务；第三篮子另剔除贸易服务。初步/修订状态只按官方脚注，不由MDF修订号推断。初始化范围可修改，实际历史覆盖以来源返回为准。",
});
export const PCE_PROFILE: MacroProfile = Object.freeze({
  id: "pce",
  label: "PCE价格指数",
  provider: "BEA",
  asset: PCE,
  basePath: "/api/r5/pce/timeseries",
  series: [
    ["DPCERG", "总体 · 季调", "headline", "sa"],
    ["DPCCRG", "核心 · 季调", "core", "sa"],
  ] as const,
  historyNotice:
    "BEA NIPA T20804 月度指数，仅总体与核心季调两系列。初始化默认240个月，可修改；实际覆盖以来源返回为准。1/3/6/12月累计变化及另列3/6月年化变化（指数比值的4/2次方减1）由同一截止时点的未舍入指数端点计算，需前12个月缓冲。入库时间不代表官方发布时间；日历仅提供发布计划，事件数值以发布材料及版本证据为准，未知不推算。",
});
export const EMPLOYMENT_PROFILE: MacroProfile = Object.freeze({
  id: "employment",
  label: "就业",
  provider: "BLS",
  asset: EMPLOYMENT,
  basePath: "/api/r5/employment/timeseries",
  defaultQuerySeries: [
    "CES0000000001",
    "LNS14000000",
    "CES0500000003",
    "LNS11300000",
    "CES0500000002",
  ],
  series: [
    ["CES0000000001", "非农 · CES · 千岗位", "total_nonfarm", "sa"],
    ["LNS14000000", "失业率 · CPS · %", "unemployment_rate", "sa"],
    [
      "CES0500000003",
      "平均时薪 · CES · 美元/小时",
      "average_hourly_earnings",
      "sa",
    ],
    [
      "LNS11300000",
      "劳动参与率 · CPS · %",
      "labor_force_participation_rate",
      "sa",
    ],
    [
      "CES0500000002",
      "平均每周计薪工时 · CES · 小时",
      "average_weekly_hours",
      "sa",
    ],
    ["CES0500000001", "私营就业 · CES · 千岗位", "total_private", "sa"],
    [
      "LNS12300000",
      "就业人口比 · CPS · %",
      "employment_population_ratio",
      "sa",
    ],
    ["CES1000000001", "采矿与伐木 · CES · 千岗位", "mining_logging", "sa"],
    ["CES2000000001", "建筑 · CES · 千岗位", "construction", "sa"],
    ["CES3000000001", "制造 · CES · 千岗位", "manufacturing", "sa"],
    [
      "CES4000000001",
      "贸易、运输及公用事业 · CES · 千岗位",
      "trade_transportation_utilities",
      "sa",
    ],
    ["CES5000000001", "信息 · CES · 千岗位", "information", "sa"],
    ["CES5500000001", "金融活动 · CES · 千岗位", "financial_activities", "sa"],
    [
      "CES6000000001",
      "专业与商业服务 · CES · 千岗位",
      "professional_business_services",
      "sa",
    ],
    [
      "CES6500000001",
      "私营教育与医疗 · CES · 千岗位",
      "private_education_health",
      "sa",
    ],
    ["CES7000000001", "休闲与酒店 · CES · 千岗位", "leisure_hospitality", "sa"],
    ["CES8000000001", "其他服务 · CES · 千岗位", "other_services", "sa"],
    ["CES9000000001", "政府 · CES · 千岗位", "government", "sa"],
  ] as const,
  historyNotice:
    "非农、私营及十一项互不重叠主要行业为千岗位及月净增；总量和私营是聚合项，不能与行业重复相加，也不加入嵌套子行业。就业人口比为百分数及百分点变化。非农为千岗位及月净增，岗位不是人数；失业率、劳动参与率为百分数及百分点变化；平均每周计薪工时为小时及小时变化；平均时薪为美元/小时及环比、同比。初始化240个月可编辑，无发布证据时截至上一完整自然月，仅为预置范围。官方变化与本地计算分别保留，revision 1 是首次本地观察。",
});
export const profileScopeText = (profile: MacroProfile) =>
  `${profile.series.map(([, title]) => title).join("、")}，共${profile.series.length}种口径。`;

export const RETAIL_PROFILE: MacroProfile = Object.freeze({
  id: "retail",
  label: "零售销售",
  provider: "Census",
  asset: RETAIL,
  basePath: "/api/r5/retail/timeseries",
  resumePath: "/api/r5/retail/timeseries/jobs/resume",
  series: [
    ["headline", "总体 · 季调", "headline", "sa"],
    ["control", "控制组（MDF推导）· 季调", "core", "sa"],
    ["ex_auto", "除汽车（MDF推导）· 季调", "ex_auto", "sa"],
  ] as const,
  historyNotice:
    "仅季调（SA）。金额单位为百万当期美元；控制组为 MDF 推导：44X72−441−444−447−722，包含445。除汽车=44X72−441；两者保留公式、派生身份和同截止输入版本，复用现有原始输入。1/3/6/12月变化为未年化比率，官方 MPCSM 另以 percent 保留。六原始输入一起采集。初始化默认240个月可编辑；首次本地修订不是官方首发。周期策略按 Census 官方 Data Month 发布日历运行，计划时间不代表实际发布或入库时间。",
});

export const CONSUMPTION_PROFILE: MacroProfile = Object.freeze({
  id: "consumption",
  label: "美国消费与收入",
  provider: "BEA",
  asset: CONSUMPTION,
  basePath: "/api/r5/consumption/timeseries",
  series: [
    ["DPCERC", "名义个人消费支出 · 百万当期美元 · SAAR", "nominal_pce", "sa"],
    ["DPCERX", "实际个人消费支出 · 百万2017链价美元 · SAAR", "real_pce", "sa"],
    ["DGDSRX", "实际商品消费 · 百万2017链价美元 · SAAR", "real_goods", "sa"],
    ["DSERRX", "实际服务消费 · 百万2017链价美元 · SAAR", "real_services", "sa"],
    ["A067RC", "名义可支配个人收入 · 百万当期美元 · SAAR", "nominal_dpi", "sa"],
    [
      "A067RX",
      "实际可支配个人收入 · 百万2017链价美元 · SAAR",
      "real_dpi",
      "sa",
    ],
    ["A072RC", "个人储蓄率 · % · 季调非年化", "saving_rate", "sa"],
  ] as const,
  historyNotice:
    "金额按 BEA UNIT_MULT=6 保留百万美元原值，SAAR 是季调年率水平，不是年化增长。实际值为2017链价美元，商品与服务不可相加替代实际PCE；不使用CPI平减。储蓄率原值为百分数，可为零或负数，仅计算百分点变化。其余变化按同一截止未舍入端点计算。初始化默认240月可编辑，实际覆盖按来源返回，2026样本不证明原生历史起点。来源历史与事件材料按已留存证据读取，不回退最新值。",
});

export function macroAssetLabel(profile: MacroProfile): string {
  return assetTitle(profile.asset);
}

export function initializationAnchorText(basis?: string) {
  const labels: Record<string, string> = {
    "official_elapsed_release/reference_month":
      "官方日历中已过计划时间的参考月；不代表该期数据已确认发布",
    confirmed_local_available: "本地已确认可消费的最新观测月份",
    previous_utc_calendar_month_not_release_readiness:
      "当前 UTC 自然月的上个月；不代表已发布或来源已就绪",
    request_calendar_month:
      "请求时所在自然月的上个月；不代表已发布或来源已就绪",
    request_calendar_month_no_release_claim:
      "请求时北京时间自然月的上个月；不代表已发布或来源已就绪",
  };
  return labels[basis ?? ""] ?? "服务未提供可解释的月份依据，请核实采集范围";
}

export const GDP_PROFILE: MacroProfile = Object.freeze({
  id: "gdp",
  label: "GDP与增长",
  provider: "BEA",
  asset: GDP,
  basePath: "/api/r5/gdp/timeseries",
  series: [
    ["A191RX", "实际GDP · 百万2017链价美元 · SAAR", "real_gdp", "sa"],
    [
      "LB000003",
      "实际私人国内最终需求 · 百万2017链价美元 · SAAR",
      "real_private_final_demand",
      "sa",
    ],
    ["A191RL", "官方年化季环比 · %", "gdp_qoq_annualized", "sa"],
    ["DPCERY", "消费贡献 · 百分点", "consumption_contribution", "sa"],
    ["A007RY", "固定投资贡献 · 百分点", "fixed_investment_contribution", "sa"],
    ["A014RY", "库存贡献 · 百分点", "inventories_contribution", "sa"],
    ["A822RY", "政府贡献 · 百分点", "government_contribution", "sa"],
    ["A019RY", "净出口贡献 · 百分点", "net_exports_contribution", "sa"],
  ] as const,
  historyNotice:
    "原生季度 YYYY-Qn。金额是百万2017链价美元的季调年率水平；官方年化季环比为原生 percent，贡献为原生百分点，均不再乘100。仅两个水平系列计算非年化季变与年变。连续序列估计为 unknown、事件未绑定，不推定官方首发；发布材料与日历计划分别展示。",
});

export const gdpEstimateText = (kind: string) =>
  (
    ({
      advance: "初值",
      second: "第二次估计",
      third: "第三次估计",
      initial: "首次估计",
      updated: "更新估计",
    }) as Record<string, string>
  )[kind] ?? kind;

export const CLAIMS_PROFILE: MacroProfile = Object.freeze({
  id: "claims",
  label: "美国初请与续请失业金",
  provider: "FRED",
  asset: CLAIMS,
  basePath: "/api/r5/claims/timeseries",
  series: [
    ["ICSA", "初请 · 季调 · Number", "initial", "sa"],
    ["ICNSA", "初请 · 未季调 · Number", "initial", "nsa"],
    ["CCSA", "续请 · 季调 · Number", "continued", "sa"],
    ["CCNSA", "续请 · 未季调 · Number", "continued", "nsa"],
    ["IC4WSA", "初请官方四周均值 · 季调 · Number", "initial_average", "sa"],
    ["CC4WSA", "续请官方四周均值 · 季调 · Number", "continued_average", "sa"],
  ] as const,
  historyNotice:
    "原机构 DOL-ETA，提供者 FRED。原生截止周为周六，初请与续请各有独立参考周。默认最近约52周可编辑；初始化20日历年可编辑，来源额外读取28天计算缓冲。Number计数不代表新增失业人数或跨周去重个人。官方四周均值独立于MDF本地四周均值；缺周不补零，首次捕获不代表官方首发。",
});

export const JOLTS_PROFILE: MacroProfile = Object.freeze({
  id: "jolts",
  label: "JOLTS",
  provider: "BLS",
  asset: JOLTS,
  basePath: "/api/r5/jolts/timeseries",
  series: [
    [
      "JTS000000000000000JOL",
      "职位空缺 · 千岗位 · 月末存量",
      "job_openings",
      "sa",
    ],
    ["JTS000000000000000HIL", "招聘 · 千人次 · 月内流量", "hires", "sa"],
    ["JTS000000000000000QUL", "主动离职 · 千人次 · 月内流量", "quits", "sa"],
    [
      "JTS000000000000000LDL",
      "裁员与解雇 · 千人次 · 月内流量",
      "layoffs_discharges",
      "sa",
    ],
    ["JTS000000000000000JOR", "职位空缺率 · %", "job_openings_rate", "sa"],
    ["JTS000000000000000HIR", "招聘率 · %", "hires_rate", "sa"],
    ["JTS000000000000000QUR", "主动离职率 · %", "quits_rate", "sa"],
    [
      "JTS000000000000000LDR",
      "裁员与解雇率 · %",
      "layoffs_discharges_rate",
      "sa",
    ],
  ] as const,
  historyNotice:
    "全国非农合计、季调（SA）八项。数量为千岗位/千人次流量，流量不代表去重人数；职位空缺月末存量不可跨月求和。官方Rate已是percent（4.3即4.3%）；数量月差保留原单位，数量累计ratio按百分比展示，比率变化为百分点。零值有效，null与缺行不补零。初始化240月可编辑，实际覆盖以本地和来源返回为准。首次捕获不是官方首发；日历计划不当实际发布，连续版本未绑定官方事件。source_qualified=false：官方定义/方法与具名发布正文资格仍缺，网页403缺口保留。",
});

export const M3_PROFILE: MacroProfile = Object.freeze({
  id: "m3",
  label: "美国耐用品与核心资本品",
  provider: "Census",
  asset: M3,
  basePath: "/api/r5/m3/timeseries",
  resumePath: "/api/r5/m3/timeseries/jobs/resume",
  series: [
    ["durable_goods_new_orders", "耐用品新订单 · 季调", "durable", "sa"],
    [
      "nondefense_ex_aircraft_capital_goods_new_orders",
      "核心资本品新订单 · 季调",
      "orders",
      "sa",
    ],
    [
      "nondefense_ex_aircraft_capital_goods_shipments",
      "核心资本品出货 · 季调",
      "shipments",
      "sa",
    ],
  ] as const,
  historyNotice:
    "月频季调，百万当期美元，非年化；新订单是扣除取消订单的净额。耐用品先行报告（Advance）与制造业完整报告独立留存并排比较。当前旧月快照及首次捕获不代表官方首发；尚未绑定事件 actual。",
});

// Product collection registration; query/strategy adapters keep their own gates.
export const MACRO_PROFILES: readonly MacroProfile[] = [
  CPI_PROFILE,
  PPI_PROFILE,
  PCE_PROFILE,
  EMPLOYMENT_PROFILE,
  RETAIL_PROFILE,
  CONSUMPTION_PROFILE,
  GDP_PROFILE,
  CLAIMS_PROFILE,
  JOLTS_PROFILE,
  M3_PROFILE,
];
