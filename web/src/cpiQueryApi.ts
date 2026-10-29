import type { paths } from "./api.generated";
import type { Transport } from "./api";
import { CPI } from "./workflowModel";

export type CpiOrdinaryQuery = NonNullable<
  paths["/api/r5/cpi/query"]["get"]["parameters"]["query"]
>;
export type CpiOrdinaryResult =
  paths["/api/r5/cpi/query"]["get"]["responses"][200]["content"]["application/json"];
export type CpiOrdinaryFailure =
  paths["/api/r5/cpi/query"]["get"]["responses"][503]["content"]["application/json"];

export class CpiQueryFailure extends Error {
  constructor(readonly detail: CpiOrdinaryFailure) {
    super(detail.classification);
  }
}
export async function queryCpi(
  transport: Transport,
  query: CpiOrdinaryQuery,
  signal: AbortSignal,
): Promise<CpiOrdinaryResult> {
  const parameters = new URLSearchParams({ reference: query.reference });
  if (query.operation_id) parameters.set("operation_id", query.operation_id);
  try {
    const response = await transport(`/api/r5/cpi/query?${parameters}`, {
      method: "GET",
      cache: "no-store",
      credentials: "same-origin",
      signal,
    });
    const body = await response.json();
    if (!response.ok) {
      if (
        body &&
        ["UNKNOWN", "NOT_AVAILABLE", "REJECTED"].includes(body.status) &&
        typeof body.classification === "string" &&
        /^[a-z_]{1,80}$/.test(body.classification) &&
        (body.local_reason == null || typeof body.local_reason === "string")
      )
        throw new CpiQueryFailure(body);
      throw new Error("invalid_query_failure");
    }
    // Keep the returned scope and durability consistent before presenting data.
    if (
      body?.status !== "OK" ||
      body.mode !== "ordinary" ||
      body.reference !== query.reference ||
      body.definition_ref?.identity !== CPI.identity ||
      body.definition_ref?.revision !== CPI.revision ||
      !Array.isArray(body.measurements) ||
      body.measurements.length !== 4 ||
      !Array.isArray(body.evidence_grades) ||
      !Number.isFinite(Date.parse(body.decision_at)) ||
      (body.channel === "local"
        ? body.durable !== true ||
          !body.version_identity ||
          !body.publication_identity
        : body.channel !== "upstream" ||
          body.durable !== false ||
          body.version_identity !== null ||
          body.publication_identity !== null ||
          !query.operation_id ||
          body.operation_id !== query.operation_id ||
          !body.source_ref ||
          !body.binding_ref ||
          !Number.isFinite(Date.parse(body.obtained_at)))
    )
      throw new Error("invalid_query_result");
    return body;
  } catch (error) {
    if (error instanceof CpiQueryFailure) throw error;
    throw new CpiQueryFailure({
      status: "UNKNOWN",
      classification: "query_response_unavailable",
      local_reason: null,
    });
  }
}
