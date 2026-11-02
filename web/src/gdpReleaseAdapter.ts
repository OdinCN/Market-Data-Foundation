import type { Claim, Evidence, Release, Records } from "./MacroReleaseRecords";
import { quarterValid } from "./QuarterField";
import { isDecimalString } from "./decimalValue";

type GdpClaim = Omit<Claim, "reference_month"> & {
  reference_quarter: string;
  estimate_kind: string;
  confirmation_state: "confirmed" | "unavailable" | "conflict";
};
export type GdpReleaseRecord = Omit<Evidence, "claims"> & {
  event_id: string;
  reference_quarter: string;
  reference_quarters: string[];
  estimate_kind: string;
  actual_at: null;
  claims: GdpClaim[];
  missing: string[];
  missing_scopes: NonNullable<Release["missing_scopes"]>;
  previous_estimate_revision_pp: string | null;
  surprise_missing: string[];
  evidence_ids?: string[];
};
export type GdpReleaseResponse = {
  read_mode: "event" | "source_history";
  as_of: string;
  events: GdpReleaseRecord[];
  missing: string[];
};
export function normalizeGdpReleases(value: GdpReleaseResponse): Records {
  if (
    !value ||
    !["event", "source_history"].includes(value.read_mode) ||
    !Number.isFinite(Date.parse(value.as_of)) ||
    !Array.isArray(value.events) ||
    !Array.isArray(value.missing)
  )
    throw new Error("gdp_release_contract_invalid");
  const events: Release[] = value.events.map((event) => {
    if (
      !event ||
      !quarterValid(event.reference_quarter) ||
      !Array.isArray(event.reference_quarters) ||
      !event.reference_quarters.every(quarterValid) ||
      typeof event.estimate_kind !== "string" ||
      event.actual_at !== null ||
      event.source_version?.precision !== "date" ||
      !Number.isFinite(Date.parse(event.available_at)) ||
      !Array.isArray(event.claims) ||
      !Array.isArray(event.missing) ||
      !Array.isArray(event.missing_scopes) ||
      !event.claims.every(
        (claim) =>
          quarterValid(claim.reference_quarter) &&
          (claim.value === null || isDecimalString(claim.value)) &&
          claim.unit === "percent" &&
          claim.continuous_version === null &&
          typeof claim.estimate_kind === "string" &&
          ["confirmed", "unavailable", "conflict"].includes(
            claim.confirmation_state,
          ),
      )
    )
      throw new Error("gdp_release_contract_invalid");
    const claims: Claim[] = event.claims.map((claim) => ({
      ...claim,
      surprise_missing: event.surprise_missing,
    }));
    // GDP event mode aggregates claims and evidence IDs but exposes only the selected
    // material's complete proof. Do not fabricate proofs for the remaining IDs.
    return {
      ...event,
      claims,
      state: event.claims.some(
        (claim) =>
          claim.role === "actual" && claim.confirmation_state === "confirmed",
      )
        ? "confirmed"
        : "unknown",
      evidence: [{ ...event, claims }],
    };
  });
  return {
    ...value,
    events,
    as_of_basis: "available",
    provider_requests: 0,
    persisted: false,
  };
}
