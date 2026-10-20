import type { components, operations, paths } from "./api.generated";

export type Catalog = components["schemas"]["CatalogDTO"];
export type Directory = components["schemas"]["DirectoryDTO"];
export type DirectoryEntry = Directory["entries"][number];
export type ReadResult = components["schemas"]["ReadDTO"];
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
  query: paths[P]["get"]["parameters"]["query"],
  signal: AbortSignal,
): Promise<paths[P]["get"]["responses"][200]["content"]["application/json"]> {
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
  return body as paths[P]["get"]["responses"][200]["content"]["application/json"];
}
