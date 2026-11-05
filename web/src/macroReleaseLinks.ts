import type { MacroProfile } from "./macroTimeSeriesProfile";
export function releaseLink(
  profile: MacroProfile,
  eventId: string,
  evidenceId?: string,
) {
  const fields = new URLSearchParams({
    ui: "r5",
    view: "assets",
    asset: profile.asset.identity,
    asset_revision: profile.asset.revision,
    release_event: eventId,
  });
  if (evidenceId) fields.set("release_evidence", evidenceId);
  return `#${fields}`;
}
export const releaseReasonText = (reason: string): string =>
  ({
    official_release_evidence_not_available_at_cutoff:
      "截止时没有可消费的官方发布材料",
    release_storage_failed: "发布材料暂时无法读取",
    release_schema_not_prepared: "发布记录存储尚未由操作者准备",
    actual_publication_instant_not_proven:
      "材料只证明发布日期，实际传输时刻未知",
    actual_publication_order_not_proven: "现有时间证据不足以证明发布先后关系",
    consensus_source_unavailable: "没有合格的具名事前共识",
    continuous_version_not_bound_to_material:
      "连续版本与此次发布尚无明确关联证据",
    pre_event_last_value_order_unknown:
      "发布日期内存在可用版本，但其与发布的先后关系未知；不能确认最后前值",
    no_consumable_prior_value_before_event_bound:
      "发布前的保守时间界限内没有可消费前值",
    mdf_statistic_not_captured_at_cutoff: "截止时尚未捕获此统计量",
    consensus_scope_mismatch: "共识与实际值的事件或统计口径不一致",
    consensus_not_frozen_before_release: "共识未能证明在发布前冻结",
    release_date_timezone_missing: "日期证据未提供时区，无法确认先后关系",
    some_official_scopes_not_confirmed: "合并材料仍未覆盖部分指标口径",
    official_material_claim_conflict: "官方材料的数值存在尚未解决的冲突",
    event_actual_unconfirmed: "实际值尚未确认属于此次发布",
    some_official_statistics_unavailable_in_material:
      "材料中部分统计量为空值、抑制值或不可用，未补零",
    cpi_sa_index_not_in_selected_table1: "所选材料未提供季调指数水平",
    ppi_core_ex_food_energy_not_in_selected_material:
      "所选材料未提供剔除食品和能源的最终需求核心篮子",
    ppi_index_not_in_selected_material: "所选材料未提供这些篮子的指数水平",
    pce_index_not_in_release_html: "发布网页未提供价格指数水平",
    native_money_levels_not_in_release_html:
      "发布网页未提供金额水平，连续金额序列另列",
    real_goods_services_not_in_release_html:
      "发布网页未提供实际商品及服务消费的完整口径",
    historical_comparisons_xlsx_is_not_vintage_revision_table:
      "历史比较表不构成历史版本修订证据",
    official_previous_requires_separate_prior_event_material:
      "官方原前值需查阅独立的上次发布材料",
    employment_sector_levels_not_in_summary_B_changes_table:
      "行业摘要表提供月净变动，未提供岗位水平",
    employment_trade_transport_utilities_aggregate_not_in_material:
      "所选材料未列贸易、运输及公用事业聚合项",
    retail_control_ex_auto_inputs_not_in_release_html:
      "发布网页未提供计算控制组或除汽车的完整输入",
    nfp_current_net_change_not_found_in_material:
      "材料中没有已确认的当期非农月净变动",
    nfp_previous_month_revision_not_found_in_material:
      "材料中没有已确认的上月非农修订值",
    no_due_official_material_selected:
      "查询范围内未选出已到期且参考月明确的官方材料",
    http_403: "官方档案拒绝访问，连续数值采集结果另列",
    release_http_403: "官方档案拒绝访问，连续数值采集结果另列",
    release_material_calendar_identity_mismatch:
      "取得的网页与所选发布日期或参考月不一致",
    m3_report_stage_unproven:
      "取得的M3报告阶段与本次先行/完整报告任务不一致，未绑定事件",
    numeric_result_not_confirmed:
      "该报告数值回读尚未确认，本次未取得材料；数值任务结果另列",
    m3_material_request_control_unavailable:
      "M3材料请求未绑定既有限流器，未请求来源",
    release_retained_scope_unconfirmed:
      "已有材料的阶段或参考月不能对应当前任务，需核对具名材料",
    m3_material_acquisition_failed:
      "M3材料日历或留存状态读取失败，数值结果已独立保留",
    release_material_transport_or_storage_failed:
      "发布材料取得或留存失败，数值结果另列",
  })[reason] ?? "该口径的材料、来源或时间证据尚不充分";

export const releaseReasonTexts = (
  reasons: readonly string[] = [],
): string[] => [...new Set(reasons.map(releaseReasonText))];
