import type { components, operations, paths as R5Paths } from "./api.generated";
import type {
  components as ManagementComponents,
  paths as ManagementPaths,
} from "./management.generated";

type paths = R5Paths & {
  "/api/r3/management": ManagementPaths["/management"];
};
export type Management = ManagementComponents["schemas"]["ManagementDTO"];

export type Catalog = components["schemas"]["CatalogDTO"];
export type Directory = components["schemas"]["DirectoryDTO"];
export type DirectoryEntry = Directory["entries"][number];
export type Observability =
  operations["runtime_observability"]["responses"][200]["content"]["application/json"];
export type Observation = Observability["evidence_observation"];
export type ObjectRef = Observability["instances"][number]["related"];
export type ReadResult = components["schemas"]["ReadDTO"];
export type Jobs = components["schemas"]["JobsDTO"];
export type Job = Jobs["jobs"][number];
export type JobIntent = components["schemas"]["Intent"];
export type ReadMode = ReadResult["mode"];
export type Query = operations["cpi_catalog"]["parameters"]["query"];
export type Transport = typeof fetch;

export class ApiFailure extends Error {
  constructor(
    readonly classification: string,
    readonly status: "UNKNOWN" | "NOT_AVAILABLE" = "UNKNOWN",
  ) {
    super("Read unavailable");
  }
}

export async function request<P extends keyof paths>(
  transport: Transport,
  path: P,
  query: NonNullable<paths[P]["get"]>["parameters"]["query"],
  signal: AbortSignal,
): Promise<
  NonNullable<paths[P]["get"]>["responses"][200]["content"]["application/json"]
> {
  const fields: Record<string, string> = {};
  for (const [key, value] of Object.entries(query ?? {})) {
    if (typeof value === "string") fields[key] = value;
  }
  const response = await transport(`${path}?${new URLSearchParams(fields)}`, {
    method: "GET",
    signal,
    cache: "no-store",
  });
  const body = await response.json();
  if (!response.ok) {
    // Only bounded machine classifications are displayed, never raw server errors.
    const classification =
      typeof body.classification === "string" &&
      /^[a-z_]{1,80}$/.test(body.classification)
        ? body.classification
        : "request_failed";
    throw new ApiFailure(
      classification,
      body.status === "NOT_AVAILABLE" ? "NOT_AVAILABLE" : "UNKNOWN",
    );
  }
  if (
    !body ||
    typeof body !== "object" ||
    !["OK", "UNKNOWN", "NOT_AVAILABLE"].includes(body.status)
  ) {
    throw new ApiFailure("invalid_response");
  }
  return body as NonNullable<
    paths[P]["get"]
  >["responses"][200]["content"]["application/json"];
}

export async function submitJob(
  transport: Transport,
  path: string,
  intent: JobIntent | Record<string, never>,
) {
  const response = await transport(path, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(intent),
  });
  const body = await response.json();
  if (!response.ok)
    throw new ApiFailure(
      typeof body?.classification === "string" &&
      /^[a-z_]{1,80}$/.test(body.classification)
        ? body.classification
        : "request_failed",
      body?.status === "NOT_AVAILABLE" ? "NOT_AVAILABLE" : "UNKNOWN",
    );
  if (body?.status !== "OK" || body?.acceptance !== "DURABLE")
    throw new ApiFailure("invalid_response");
  return body as components["schemas"]["AcceptedDTO"];
}

// Explicit PCE transport contract within the existing API layer. No CPI JobDTO,
// release slot, qualification or successful read is synthesized.
export async function pceRequest<T>(
  transport: Transport,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await transport(path, {
    method: "GET",
    cache: "no-store",
    ...init,
  });
  if (!response.ok) throw new ApiFailure("pce_request_unavailable");
  const body = await response.json();
  if (!body || typeof body !== "object")
    throw new ApiFailure("invalid_pce_response");
  return body as T;
}
