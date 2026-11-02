import type { Claim, Evidence, Release, Records } from "./MacroReleaseRecords";
import { weekValid, dayValid } from "./claimsTimeSeriesAdapter";
import { isDecimalString } from "./decimalValue";
type ClaimsEvidence = {
  event_id: string;
  source_version: { value: string; precision: "date"; basis: string };
  source_url: string;
  evidence_id: string;
  raw_sha256: string;
  raw_locator: string;
  obtained_at: string;
  available_at: string;
  parser_revision: string;
  evidence_kind: string;
  actual_available: boolean;
  official_actual_publication_at: null;
  claims: {
    series_id: string;
    week_ending: string;
    statistic: string;
    role: string;
    value: string | null;
    value_identity: string | null;
    unit: string;
    confirmation_state: string;
    evidence: {
      page: number | null;
      fragment: string | null;
      reference_header: string;
    };
  }[];
};
export function normalizeClaimsReleases(v: {
  read_mode: string;
  as_of: string;
  events: ClaimsEvidence[];
}): Records {
  if (
    !v ||
    !["event", "source_history"].includes(v.read_mode) ||
    !Number.isFinite(Date.parse(v.as_of)) ||
    !Array.isArray(v.events)
  )
    throw new Error("claims_release_contract_invalid");
  const events: Release[] = v.events.map((e) => {
    if (
      !/^dol-claims-\d{4}-\d{2}-\d{2}$/.test(e.event_id) ||
      !dayValid(e.source_version?.value) ||
      e.source_version.precision !== "date" ||
      !Number.isFinite(Date.parse(e.available_at)) ||
      !Array.isArray(e.claims) ||
      !e.claims.every(
        (c) =>
          weekValid(c.week_ending) &&
          c.unit === "Number" &&
          (c.value === null || isDecimalString(c.value)) &&
          ["confirmed", "unknown", "conflict"].includes(c.confirmation_state),
      )
    )
      throw new Error("claims_release_contract_invalid");
    const claims: Claim[] = e.claims.map((c) => ({
      ...c,
      reference_week: c.week_ending,
      adjustment: ["ICNSA", "CCNSA"].includes(c.series_id) ? "NSA" : "SA",
      continuous_version: null,
      locator: {
        kind: "official_report_fragment",
        page: c.evidence.page,
        reference_header: c.evidence.reference_header,
        excerpt: `第${c.evidence.page ?? "未知"}页 · ${c.evidence.reference_header} · ${c.evidence.fragment ?? "原前值未说明，unknown"}`,
      },
    }));
    const evidence: Evidence = {
      ...e,
      source_version: { ...e.source_version, source: "DOL-ETA" },
      claims,
      retention: {
        kind: e.evidence_kind,
        exact_original_retained: true,
        coverage: "已保存材料；不推造更早发布日期",
      },
    };
    return {
      ...e,
      ...evidence,
      reference_weeks: [...new Set(e.claims.map((c) => c.week_ending))]
        .sort()
        .reverse(),
      actual_at: null,
      state: e.actual_available ? "confirmed" : "unknown",
      missing: [
        "no_verified_actual_publication_cutoff",
        "named_consensus_unavailable",
      ],
      evidence: [evidence],
    };
  });
  return {
    events,
    as_of: v.as_of,
    read_mode: v.read_mode as Records["read_mode"],
    as_of_basis: "available",
    provider_requests: 0,
    persisted: false,
    missing: [],
  };
}
