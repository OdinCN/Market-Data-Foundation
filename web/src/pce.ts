import { ApiFailure, pceRequest, type Transport } from "./api";
import {
  PCE,
  PCE_OPERATION,
  sameKey,
  type Interface,
  type Execution,
} from "./workflowModel";

export const PCE_REFERENCE = "2026-07";
export type PceBinding = {
  operator: string;
  definition: string;
  definition_revision: string;
  source: string;
  source_revision: string;
  operation: string;
  binding: string;
  binding_revision: string;
  reference: string;
  source_qualified: false;
  target: { host: string; database: string; schema: string };
};
export type PceCatalog = {
  binding: PceBinding;
  parameters: Record<string, string>[];
  measurements: string[];
  source_qualified: false;
  write_eligible: boolean;
  submission: {
    confirmation: string;
    job_identity: string;
    binding: PceBinding;
  };
};
export type PceBackfill = {
  eligible: boolean;
  reason: string | null;
  submission: {
    confirmation: string;
    job_identity: string;
    parent_job_id: string;
    reference: string;
    binding: PceBinding;
  };
};
export type PceJobs = { jobs: PceJob[]; backfill?: PceBackfill };
export type PceJob = {
  parent_job_id?: string | null;
  job_id: string;
  binding: PceBinding;
  attempt_id: string | null;
  attempts: number;
  state: string;
  stage: string;
  classification: string | null;
  updated_at: string;
  events?: PceJob[];
  reconciliation?: {
    outcome?: string;
    evidence?: {
      version_identity: string;
      publication_identity: string;
      usable_at: string;
    };
  };
};
export type PceAssets = {
  binding: PceBinding;
  as_of: string;
  versions: {
    version_identity: string;
    frozen_at: string;
    selection_state: string;
    publications: {
      publication_identity: string;
      decision: string;
      known_at: string;
      usable_at: string;
    }[];
  }[];
};
export type PceRead = {
  binding: PceBinding;
  reference: string;
  as_of: string;
  version_identity: string;
  publication_identity: string;
  acquisition_identity: string;
  known_at: string;
  usable_at: string;
  source_qualified: false;
  measurements: Record<string, { value: string; classification: string }>;
};
export type PceEvidence = {
  catalog: PceCatalog;
  assets: PceAssets;
  reading: PceRead | null;
};
export const pceLabels: Record<string, string> = {
  headline_mom_sa: "PCE 总体环比（季调）",
  core_mom_sa: "PCE 核心环比（季调）",
  headline_yoy_sa: "PCE 总体同比（季调）",
  core_yoy_sa: "PCE 核心同比（季调）",
};
export function validPceBinding(b?: PceBinding): boolean {
  return (
    !!b &&
    b.definition === PCE.identity &&
    b.definition_revision === PCE.revision &&
    `${b.source}@${b.source_revision}` === PCE_OPERATION &&
    b.operation === "GetData:NIPA:T20807:T20804:M" &&
    b.binding === "mdf.binding-candidate.bea.nipa.pce" &&
    b.binding_revision === "t04-retained-candidate-r1" &&
    b.reference === PCE_REFERENCE &&
    b.source_qualified === false &&
    b.operator === "ao-dev-bea-pce" &&
    b.target?.host === "192.168.10.4" &&
    b.target.database === "market_data_foundation_dev" &&
    b.target.schema === "mdf_macro_bea_pce_vertical_r1"
  );
}
export function pceBound(
  source?: Interface | null,
  catalog?: PceCatalog | null,
): boolean {
  const b = catalog?.binding;
  return (
    validPceBinding(b) &&
    validPceBinding(catalog?.submission?.binding) &&
    source?.id === PCE_OPERATION &&
    source.dto.operation === b!.operation &&
    sameKey(source.asset, PCE) &&
    source.dto.source_contract?.identity === b!.source &&
    source.dto.source_contract?.revision === b!.source_revision &&
    source.dto.binding_contract?.identity === b!.binding &&
    source.dto.binding_contract?.revision === b!.binding_revision
  );
}
export { pceRequest } from "./api";
export async function readPce(
  transport: Transport,
  reference: string,
  asOf: string,
  version?: string,
  signal?: AbortSignal,
) {
  if (reference !== PCE_REFERENCE)
    throw new ApiFailure("pce_reference_mismatch");
  const query = new URLSearchParams({ reference, as_of: asOf });
  if (version) query.set("version", version);
  const result = await pceRequest<PceRead>(
    transport,
    `/api/pce/read?${query}`,
    { signal },
  );
  if (
    !validPceBinding(result.binding) ||
    result.reference !== reference ||
    Date.parse(result.as_of) !== Date.parse(asOf) ||
    result.source_qualified !== false ||
    !result.version_identity ||
    !result.publication_identity ||
    (version && result.version_identity !== version) ||
    Object.keys(pceLabels).some(
      (k) => typeof result.measurements?.[k]?.value !== "string",
    )
  )
    throw new ApiFailure("pce_read_identity_mismatch");
  return result;
}
export function pceExecution(j: PceJob): Execution {
  const evidence =
    j.state === "succeeded" &&
    (!j.parent_job_id ||
      (j.reconciliation?.outcome === "confirmed_published" &&
        j.reconciliation.evidence?.version_identity === `${j.job_id}:version` &&
        j.reconciliation.evidence?.publication_identity ===
          `${j.job_id}:publication`))
      ? j.reconciliation?.evidence
      : undefined;
  return {
    id: j.job_id,
    asset: PCE,
    operation: PCE_OPERATION,
    schedule: null,
    reference: PCE_REFERENCE,
    state: j.state,
    attempt: j.attempt_id,
    version: evidence?.version_identity ?? null,
    publication: evidence?.publication_identity ?? null,
    readAsOf: evidence?.usable_at ?? null,
    dto: null,
    pce: j,
  };
}
