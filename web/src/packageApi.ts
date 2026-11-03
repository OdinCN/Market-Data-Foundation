import type { paths } from "./api.generated";
import type { Transport } from "./api";

export type Packages =
  paths["/api/r5/packages"]["get"]["responses"][200]["content"]["application/json"];
export type Package =
  paths["/api/r5/packages/{package_id}"]["get"]["responses"][200]["content"]["application/json"];
export type SavePackage =
  paths["/api/r5/packages/{package_id}"]["put"]["requestBody"]["content"]["application/json"];
export type PackageFailure =
  paths["/api/r5/packages/{package_id}"]["put"]["responses"][409]["content"]["application/json"];
export type RuntimeAsset = Package["assets"][number];
export type RuntimeRevision = RuntimeAsset["revisions"][number];

export class PackageRequestFailure extends Error {
  constructor(readonly detail: PackageFailure) {
    super(detail.classification);
  }
}
async function exchange<T>(
  transport: Transport,
  path: string,
  init: RequestInit,
): Promise<T> {
  const writing = init.method === "PUT";
  try {
    const response = await transport(path, {
      cache: "no-store",
      credentials: "same-origin",
      ...init,
    });
    const body = await response.json();
    if (!response.ok) {
      if (
        body &&
        ["UNKNOWN", "UNAVAILABLE", "REJECTED", "CONFLICT"].includes(
          body.status,
        ) &&
        ["NONE", "UNKNOWN"].includes(body.effect) &&
        typeof body.classification === "string" &&
        /^[a-z_]{1,80}$/.test(body.classification)
      )
        throw new PackageRequestFailure(body);
      throw new Error("invalid_response");
    }
    if (
      !body ||
      typeof body !== "object" ||
      (body.status !== undefined && body.status !== "OK")
    )
      throw new Error("invalid_response");
    return body as T;
  } catch (error) {
    if (error instanceof PackageRequestFailure) throw error;
    throw new PackageRequestFailure({
      status: "UNKNOWN",
      classification: writing
        ? "config_commit_outcome_unknown"
        : "package_read_unavailable",
      effect: writing ? "UNKNOWN" : "NONE",
    });
  }
}
export const getPackages = (transport: Transport, signal: AbortSignal) =>
  exchange<Packages>(transport, "/api/r5/packages", { method: "GET", signal });
export const getPackage = (
  transport: Transport,
  id: string,
  signal: AbortSignal,
) =>
  exchange<Package>(transport, `/api/r5/packages/${encodeURIComponent(id)}`, {
    method: "GET",
    signal,
  });
export const putPackage = (
  transport: Transport,
  id: string,
  command: SavePackage,
) =>
  exchange<Package>(transport, `/api/r5/packages/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(command),
  });

export const verifyPackageSource = (
  transport: Transport,
  id: string,
  source: string,
  revision: string,
) =>
  exchange<Package>(
    transport,
    `/api/r5/packages/${encodeURIComponent(id)}/sources/${encodeURIComponent(source)}/verify`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: revision }),
    },
  );
