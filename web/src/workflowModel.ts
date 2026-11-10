import type { PceCatalog, PceEvidence, PceJob } from "./pce";
import type {
  Catalog,
  Directory,
  DirectoryEntry,
  Job,
  Management,
  Observability,
} from "./api";
import type { components as DTO } from "./api.generated";
export type Plans = DTO["schemas"]["PlansDTO"];
export type Key = { identity: string; revision: string };
export const keyOf = (key: Key) => `${key.identity}@${key.revision}`;
export const sameKey = (a?: Key | null, b?: Key | null) =>
  !!a && !!b && a.identity === b.identity && a.revision === b.revision;
export const M3: Key = {
  identity: "mdf.macro.census.durable-capital-goods",
  revision: "r1",
};
export const JOLTS: Key = {
  identity: "mdf.macro.bls.jolts",
  revision: "r1-segment1",
};
export const JOLTS_OPERATION = "mdf.source.bls.jolts@r1";
export function joltsOperationMatches(entry?: DirectoryEntry): boolean {
  const source = { identity: "mdf.source.bls.jolts", revision: "r1" };
  const binding = entry?.binding_contract;
  return (
    !!entry &&
    entry.identity === JOLTS_OPERATION &&
    entry.source_name === "BLS" &&
    sameKey(entry.asset, JOLTS) &&
    sameKey(entry.source_contract, source) &&
    sameKey(binding, { identity: "mdf.binding.bls.jolts", revision: "r1" }) &&
    sameKey(binding?.source, source) &&
    binding?.silent_fallback === false &&
    binding.mappings.some((m) => sameKey(m.definition, JOLTS))
  );
}
export const CLAIMS: Key = { identity: "mdf.macro.dol.claims", revision: "r1" };
export const CLAIMS_OPERATION = "mdf.source.fred.dol.claims@r1";
export function claimsOperationMatches(entry?: DirectoryEntry): boolean {
  const source = { identity: "mdf.source.fred.dol.claims", revision: "r1" };
  const binding = entry?.binding_contract;
  return (
    !!entry &&
    entry.identity === CLAIMS_OPERATION &&
    entry.source_name === "FRED" &&
    sameKey(entry.asset, CLAIMS) &&
    sameKey(entry.source_contract, source) &&
    sameKey(binding, {
      identity: "mdf.binding.fred.dol.claims",
      revision: "r1",
    }) &&
    sameKey(binding?.source, source) &&
    binding?.silent_fallback === false &&
    binding.mappings.some((m) => sameKey(m.definition, CLAIMS))
  );
}
export const GDP: Key = {
  identity: "mdf.macro.bea.gdp-growth",
  revision: "r1",
};
export const CONSUMPTION: Key = {
  identity: "mdf.macro.bea.consumption-income",
  revision: "r1",
};
export const CONSUMPTION_OPERATION =
  "mdf.source.bea.nipa.consumption-income@r1";
export function consumptionOperationMatches(entry?: DirectoryEntry): boolean {
  const binding = entry?.binding_contract;
  const source = {
    identity: "mdf.source.bea.nipa.consumption-income",
    revision: "r1",
  };
  return (
    !!entry &&
    entry.identity === CONSUMPTION_OPERATION &&
    sameKey(entry.asset, CONSUMPTION) &&
    sameKey(entry.source_contract, source) &&
    sameKey(binding, {
      identity: "mdf.binding.bea.nipa.consumption-income",
      revision: "r1",
    }) &&
    sameKey(binding?.source, source) &&
    binding?.silent_fallback === false &&
    binding.mappings.some((mapping) => sameKey(mapping.definition, CONSUMPTION))
  );
}
export const PCE: Key = {
  identity: "mdf.macro.bea.pce.headline-core",
  revision: "t04-r1",
};
export const PCE_OPERATION =
  "mdf.source-candidate.bea.nipa.pce@t04-retained-candidate-r1";
export const CPI: Key = {
  identity: "mdf.macro.bls.cpi.headlines",
  revision: "p3-r1",
};
export const EMPLOYMENT: Key = {
  identity: "mdf.macro.bls.employment.ces-cps",
  revision: "t03-r1",
};
export const EMPLOYMENT_OPERATION =
  "mdf.source-candidate.bls-v2.employment-2026-09@t03-snapshot-2026-09-r1";
// This is an explicit navigation mapping. Runtime permissions still come from
// the continuous employment capability, never from the retained snapshot.
export function employmentOperationMatches(entry?: DirectoryEntry): boolean {
  const binding = entry?.binding_contract;
  return (
    !!entry &&
    entry.identity === EMPLOYMENT_OPERATION &&
    sameKey(entry.asset, EMPLOYMENT) &&
    sameKey(entry.source_contract, {
      identity: "mdf.source-candidate.bls-v2.employment-2026-09",
      revision: "t03-snapshot-2026-09-r1",
    }) &&
    sameKey(binding, {
      identity: "mdf.binding-candidate.bls-v2.employment-2026-09",
      revision: "t03-snapshot-2026-09-r1",
    }) &&
    sameKey(binding?.source, entry.source_contract) &&
    binding?.silent_fallback === false &&
    binding.mappings.some((mapping) => sameKey(mapping.definition, EMPLOYMENT))
  );
}
export const RETAIL: Key = {
  identity: "mdf.macro.census.retail",
  revision: "t05-r1",
};
export const RETAIL_OPERATION =
  "mdf.source-candidate.census-marts.retail-2026-09@t05-snapshot-2026-09-r1";
export function retailOperationMatches(entry?: DirectoryEntry): boolean {
  const binding = entry?.binding_contract;
  const source = {
    identity: "mdf.source-candidate.census-marts.retail-2026-09",
    revision: "t05-snapshot-2026-09-r1",
  };
  return (
    !!entry &&
    entry.identity === RETAIL_OPERATION &&
    sameKey(entry.asset, RETAIL) &&
    sameKey(entry.source_contract, source) &&
    sameKey(binding, {
      identity: "mdf.binding-candidate.census-marts.retail-2026-09",
      revision: source.revision,
    }) &&
    sameKey(binding?.source, source) &&
    binding?.silent_fallback === false &&
    binding.mappings.some((mapping) => sameKey(mapping.definition, RETAIL))
  );
}
export const PPI: Key = {
  identity: "mdf.macro.bls.ppi.final-demand",
  revision: "t01-r1",
};
// This is the existing manual handler's bounded SourceOperationRef, not the first CPI label.
export const HISTORY_OPERATION =
  "mdf.source-candidate.bls-v2.cpi.history-index@r5-cpi-history-index-v1";
export const MANUAL_OPERATION =
  "mdf.source-candidate.bls-v2.cpi.release-2026-09@p4-release-reference-2026-09-r1";
export type Component = {
  id: DirectoryEntry["domain"];
  label: string;
  upstreams: string[];
  implementations: string[];
  interfaces: Interface[];
};
export type Interface = {
  id: string;
  label: string;
  component: Component["id"];
  source: string;
  variant: string;
  asset: Key | null;
  pce?: PceCatalog;
  dto: DirectoryEntry;
};
export type Asset = {
  pce?: PceEvidence;
  key: Key;
  label: string;
  interfaces: Interface[];
  catalog: Catalog | null;
  holding: string;
  versions: Catalog["asset"];
  publication: string;
  readability: string;
  storage: string;
};
export type CollectionContext = {
  asset: Asset;
  source: Interface | null;
  storage: string;
};
export type Execution = {
  id: string;
  asset: Key;
  operation: string | null;
  schedule: string | null;
  reference: string;
  state: string;
  attempt: string | null;
  version: string | null;
  publication: string | null;
  readAsOf: string | null;
  dto: Job | null;
  pce?: PceJob;
};
export type Service = {
  id: string;
  asset: Asset;
  version: string;
  publication: string;
};
export type RuntimeInstance = {
  id: string;
  label: string;
  dto: Management["instances"][number];
};
export type StorageObservation = {
  id: string;
  label: string;
  dto: Observability["storage"][number] | null;
};
const titles: Record<string, string> = {
  "mdf.macro.financial-conditions.daily": "金融条件日频（十项）",
  "mdf.macro.bls.jolts": "美国 JOLTS 职位空缺与劳动力流动",
  "mdf.macro.dol.claims": "美国初请与续请失业金",
  "mdf.macro.bls.cpi.headlines": "美国 CPI",
  "mdf.macro.bls.ppi.final-demand": "美国 PPI",
  "mdf.macro.bls.employment.ces-cps": "美国就业",
  "mdf.macro.bea.pce.headline-core": "美国 PCE价格指数",
  "mdf.macro.bea.consumption-income": "美国消费与收入",
  "mdf.macro.bea.gdp-growth": "美国 GDP 与增长",
  "mdf.macro.census.retail": "美国零售销售",
  "mdf.macro.fred-alfred.vintage-bundle": "FRED / ALFRED · 历史修订",
  "mdf.macro.fomc-statement-document": "美国 FOMC · 政策声明",
  "mdf.macro.ecb.sdmx.exr": "ECB SDMX · 汇率",
  "mdf.macro.census.durable-capital-goods": "美国耐用品与核心资本品",
  "mdf.macro.estat.sdmx.prc_hicp_minr": "Eurostat SDMX · HICP",
  "mdf.market.bars.daily.raw": "TDX · 原始日线",
  "mdf.kd.sector-analysis.current-rank": "KD · 板块当前排名",
};
export const assetTitle = (key: Key, fallback = key.identity) =>
  titles[key.identity] ?? fallback;
export const componentTitle = {
  Macro: "Macro 接入组件",
  TDX: "TDX Go Driver",
  KD: "KD Adapter",
};
// The API qualifies a Definition. A selected Operation also needs an exact
// binding match; sharing the CPI Definition does not make variants substitutes.
export function catalogForOperation(
  catalog: Catalog | null,
  operation: DirectoryEntry | undefined,
): Catalog | null {
  if (!catalog || !catalog.asset) return catalog;
  const binding = operation?.binding_contract;
  if (
    !binding ||
    !sameKey(operation?.asset, catalog.definition) ||
    !sameKey(binding.source, operation?.source_contract)
  )
    return null;
  const versions = catalog.asset.versions.filter(
    (v) =>
      v.binding_identity === binding.identity &&
      v.binding_revision === binding.revision,
  );
  const service = versions.some(
    (v) => v.identity === catalog.service?.version_identity,
  )
    ? catalog.service
    : null;
  return {
    ...catalog,
    status: service ? catalog.status : "NOT_AVAILABLE",
    service,
    asset: {
      ...catalog.asset,
      versions,
      local_holding: service ? catalog.asset.local_holding : "UNKNOWN",
    },
  };
}
function variantLabel(dto: DirectoryEntry): string {
  if (dto.component_identity.endsWith("financial_daily_catalog"))
    return dto.source_contract?.product ?? "日频具名人工通路";
  if (dto.component_identity.endsWith("daily_bars")) return "具名日线合同";
  if (dto.component_identity.endsWith("tdx_recent_daily"))
    return "最近 30 日有界窗口";
  if (dto.component_identity.endsWith("fred_alfred_vintage"))
    return "双时点历史修订";
  if (dto.component_identity.endsWith("fomc_statement"))
    return "官方声明发现与读取";
  if (dto.source_name === "ECB") return "汇率单序列";
  if (dto.source_name === "ESTAT") return "HICP 单序列";
  return "具名参考期快照";
}
export function sourceVariantLabel(dto: DirectoryEntry): string {
  if (joltsOperationMatches(dto)) return "BLS 全国非农SA四数量与四官方比率";
  if (claimsOperationMatches(dto)) return "FRED 周频原生六系列（DOL-ETA）";
  return dto.source_contract
    ? dto.identity === MANUAL_OPERATION
      ? "2026-09 公布窗口"
      : dto.component_identity.endsWith("cpi_history")
        ? "历史指数推导"
        : dto.component_identity.endsWith("bls_cpi_direct")
          ? "直接计算值"
          : variantLabel(dto)
    : "候选合同";
}
export function project(
  directory: Directory | null,
  catalog: Catalog | null,
  management: Management | null,
  pceCatalog: PceCatalog | null = null,
  pce: PceEvidence | null = null,
) {
  const interfaces: Interface[] = (directory?.entries ?? []).map((dto) => ({
    pce: dto.identity === PCE_OPERATION ? (pceCatalog ?? undefined) : undefined,
    id: dto.identity,
    label: dto.asset ? assetTitle(dto.asset) : dto.operation,
    component: dto.domain,
    source: dto.source_name,
    variant: sourceVariantLabel(dto),
    asset: dto.asset,
    dto,
  }));
  const components: Component[] = (["Macro", "TDX", "KD"] as const)
    .filter((id) => interfaces.some((i) => i.component === id))
    .map((id) => ({
      id,
      label: componentTitle[id],
      interfaces: interfaces.filter((i) => i.component === id),
      upstreams: [
        ...new Set(
          interfaces
            .filter((i) => i.component === id)
            .filter((i) => i.source !== "MDD")
            .map((i) =>
              i.source === "Federal Reserve"
                ? "FOMC"
                : i.source === "ESTAT"
                  ? "Eurostat"
                  : i.source,
            ),
        ),
      ],
      implementations: [
        ...new Set(
          interfaces
            .filter((i) => i.component === id)
            .map((i) => i.dto.component_identity),
        ),
      ],
    }));
  const keys = new Map<string, Key>();
  for (const i of interfaces) if (i.asset) keys.set(keyOf(i.asset), i.asset);
  if (catalog) keys.set(keyOf(catalog.definition), catalog.definition);
  const assets: Asset[] = [...keys.values()].map((key) => {
    const evidence =
      catalog && sameKey(catalog.definition, key) ? catalog : null;
    const versions = evidence?.asset ?? null;
    return {
      pce: sameKey(key, PCE) ? (pce ?? undefined) : undefined,
      key,
      label: assetTitle(key),
      interfaces: interfaces.filter((i) => sameKey(i.asset, key)),
      catalog: evidence,
      versions,
      holding:
        evidence?.asset?.local_holding === "VERIFIED_FOR_REQUEST"
          ? "已核实持有（当前查询）"
          : "持有状态待确认",
      publication: evidence?.service
        ? "已发布（当前查询）"
        : versions?.versions.some((v) => v.publications.length)
          ? "有发布记录，当前读取资格待确认"
          : evidence?.asset
            ? "未发布（当前范围）"
            : "发布状态待确认",
      readability: evidence?.service ? "可本地读取" : "读取暂不可用",
      storage: "继承资产 / 接口的 Host 存储绑定（目标未向浏览器公开）",
    };
  });
  for (const asset of assets) {
    if (!asset.pce) continue;
    asset.holding = asset.pce.reading
      ? "已核实持有（当前查询）"
      : "持有状态待确认";
    asset.publication = asset.pce.assets.versions.some(
      (v) => v.publications.length,
    )
      ? "有发布记录，当前读取资格单独核实"
      : "当前无发布记录";
    asset.readability = asset.pce.reading
      ? "可本地读取（开发测试）"
      : "读取暂不可用";
  }
  const services: Service[] = assets.flatMap((asset) =>
    asset.pce?.reading
      ? [
          {
            id: "pce-local-reader",
            asset,
            version: asset.pce.reading.version_identity,
            publication: asset.pce.reading.publication_identity,
          },
        ]
      : asset.catalog?.status === "OK" &&
          asset.catalog.service &&
          asset.versions?.local_holding === "VERIFIED_FOR_REQUEST"
        ? [
            {
              id: asset.catalog.service.identity,
              asset,
              version: asset.catalog.service.version_identity,
              publication: asset.catalog.service.publication_identity,
            },
          ]
        : [],
  );
  const instances: RuntimeInstance[] = (management?.instances ?? []).map(
    (dto) => {
      const registered = management?.entries.find(
        (e) => e.identity === dto.record.catalog_identity,
      );
      return {
        id: dto.record.identity,
        label:
          registered?.domain === "KD"
            ? "KD 外部实例"
            : registered?.domain === "TDX"
              ? "TDX Driver 运行实例"
              : registered?.domain === "Macro"
                ? "Macro 接入运行实例"
                : "运行实例（类型待核实）",
        dto,
      };
    },
  );
  return { components, interfaces, assets, services, instances };
}
export function executions(jobs: Job[], plans: Plans | null): Execution[] {
  const readablePlans =
    plans?.status === "OK" && plans.asset_identity && plans.asset_revision
      ? plans.plans
      : [];
  const children = readablePlans.flatMap((p) =>
    p.batches.flatMap((b) =>
      b.jobs.map((j) => ({
        id: j.job_identity,
        asset: {
          identity: plans!.asset_identity!,
          revision: plans!.asset_revision!,
        },
        operation: HISTORY_OPERATION,
        schedule: p.identity,
        reference: b.reference,
        state: j.state,
        attempt: j.attempt_identity,
        version: j.version_identity,
        publication: j.publication_occurrence,
        readAsOf:
          j.version_identity && j.publication_occurrence ? j.read_as_of : null,
        dto: null,
      })),
    ),
  );
  const byId = new Map<string, Execution>(children.map((c) => [c.id, c]));
  for (const j of jobs)
    byId.set(j.job_identity, {
      id: j.job_identity,
      asset: { identity: j.asset_identity, revision: j.asset_revision },
      operation:
        byId.get(j.job_identity)?.operation ??
        (j.reference === "2026-09" &&
        j.release_slot === "CPI/2026-09/2026-10-14T12:30:00Z"
          ? MANUAL_OPERATION
          : null),
      schedule: byId.get(j.job_identity)?.schedule ?? null,
      reference: j.reference,
      state: j.state,
      attempt: j.attempt_identity,
      version: j.version_identity,
      publication: j.publication_occurrence,
      readAsOf:
        byId.get(j.job_identity)?.readAsOf ??
        (j.state === "succeeded" &&
        j.stage === "local_read_verified" &&
        j.version_identity &&
        j.publication_occurrence
          ? j.finished_at
          : null),
      dto: j,
    });
  return [...byId.values()];
}
export const stateLabel = (state: string) =>
  ({
    pending: "待执行",
    running: "执行中",
    succeeded: "成功",
    failed: "失败",
    cancelled: "已取消",
    outcome_unknown: "结果未知",
    active: "启用",
    paused: "暂停",
    completed: "完成",
  })[state] ?? state;
