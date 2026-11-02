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
  evidence?: ClaimsEvidence[];
  claims: {
    series_id: string;
    week_ending: string;
    statistic: string;
    role: string;
    value: string | null;
    value_identity: string | null;
    unit: string;
    confirmation_state: string;
    evidence_id?: string | null;
    evidence_ids?: string[];
    evidence: {
      page: number | null;
      fragment: string | null;
      reference_header: string;
    } | null;
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
  const validEvidence = (e: ClaimsEvidence) =>
    e &&
    /^dol-claims-\d{4}-\d{2}-\d{2}$/.test(e.event_id) &&
    dayValid(e.source_version?.value) &&
    e.source_version.precision === "date" &&
    typeof e.evidence_id === "string" &&
    e.evidence_id.length > 0 &&
    /^[a-f0-9]{64}$/.test(e.raw_sha256) &&
    Number.isFinite(Date.parse(e.obtained_at)) &&
    Number.isFinite(Date.parse(e.available_at)) &&
    Date.parse(e.obtained_at) <= Date.parse(e.available_at) &&
    Date.parse(e.available_at) <= Date.parse(v.as_of) &&
    Array.isArray(e.claims) &&
    e.claims.every(
      (c) =>
        weekValid(c.week_ending) &&
        c.unit === "Number" &&
        (c.value === null || isDecimalString(c.value)) &&
        ["confirmed", "unknown", "conflict"].includes(c.confirmation_state) &&
        (c.confirmation_state === "conflict" ||
          (typeof c.evidence === "object" && c.evidence !== null)),
    );
  const identities = v.events.map((e) =>
    v.read_mode === "source_history"
      ? `${e.event_id}:${e.evidence_id}`
      : e.event_id,
  );
  if (new Set(identities).size !== identities.length)
    throw new Error("claims_release_contract_invalid");
  const events: Release[] = v.events.map((e) => {
    if (!validEvidence(e)) throw new Error("claims_release_contract_invalid");
    const normalizeClaim = (
      c: ClaimsEvidence["claims"][number],
      proofId?: string,
    ): Claim => ({
      ...c,
      evidence_id: c.evidence_id ?? proofId,
      reference_week: c.week_ending,
      adjustment: ["ICNSA", "CCNSA"].includes(c.series_id) ? "NSA" : "SA",
      continuous_version: null,
      locator: {
        kind: "official_report_fragment",
        page:
          c.confirmation_state === "conflict"
            ? null
            : (c.evidence?.page ?? null),
        reference_header:
          c.confirmation_state === "conflict"
            ? "多材料冲突，分别查看原始材料"
            : (c.evidence?.reference_header ?? "原文位置未知"),
        excerpt:
          c.confirmation_state === "conflict"
            ? "未选择单一材料作为结论；各份材料的原文和值分别保留。"
            : `第${c.evidence?.page ?? "未知"}页 · ${c.evidence?.reference_header ?? "原文位置未知"} · ${c.evidence?.fragment ?? "原前值未说明"}`,
      },
    });
    const proofs = v.read_mode === "event" ? e.evidence : [e];
    if (
      !proofs?.length ||
      proofs.some((p) => !validEvidence(p) || p.event_id !== e.event_id) ||
      new Set(proofs.map((p) => p.evidence_id)).size !== proofs.length
    )
      throw new Error("claims_release_contract_invalid");
    const evidence: Evidence[] = proofs.map((p) => ({
      ...p,
      source_version: { ...p.source_version, source: "DOL-ETA" },
      claims: p.claims.map((c) => normalizeClaim(c, p.evidence_id)),
      retention: {
        kind: p.evidence_kind,
        exact_original_retained: true,
        coverage: "已保存材料；不推造更早发布日期",
      },
    }));
    const claims = e.claims.map((c) => {
      const proofId =
        v.read_mode === "source_history" ? e.evidence_id : c.evidence_id;
      if (c.confirmation_state === "conflict") return normalizeClaim(c);
      const proof = proofs.find((p) => p.evidence_id === proofId);
      const original = proof?.claims.find(
        (p) =>
          p.series_id === c.series_id &&
          p.week_ending === c.week_ending &&
          p.role === c.role &&
          p.value === c.value,
      );
      if (!original) throw new Error("claims_release_contract_invalid");
      return normalizeClaim(
        { ...c, evidence: original.evidence },
        proofId ?? undefined,
      );
    });
    if (
      claims.some(
        (c) =>
          c.evidence_id &&
          !evidence.some((p) => p.evidence_id === c.evidence_id),
      )
    )
      throw new Error("claims_release_contract_invalid");
    if (
      claims.some((c) =>
        c.evidence_ids?.some(
          (id) => !evidence.some((p) => p.evidence_id === id),
        ),
      )
    )
      throw new Error("claims_release_contract_invalid");
    return {
      ...evidence[0],
      ...e,
      source_version: { ...e.source_version, source: "DOL-ETA" },
      claims,
      reference_weeks: [...new Set(e.claims.map((c) => c.week_ending))]
        .sort()
        .reverse(),
      actual_at: null,
      state: e.actual_available ? "confirmed" : "unknown",
      missing: [
        "no_verified_actual_publication_cutoff",
        "named_consensus_unavailable",
        ...(!e.actual_available ? ["event_actual_unconfirmed"] : []),
      ],
      evidence,
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
