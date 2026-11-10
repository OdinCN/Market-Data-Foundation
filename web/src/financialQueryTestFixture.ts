// Deterministic HTTP fixture only; never a provider or persistent DEV observation.
import example from "./financial-query-example.json";
export function financialResponse(
  path: string,
  extra: Record<string, unknown> = {},
) {
  const p = new URL(path, "http://fixture.invalid").searchParams;
  const selected = (p.get("series") ?? "").split(","),
    start = Date.parse(p.get("start") + "T00:00:00Z"),
    end = Date.parse(p.get("end") + "T00:00:00Z");
  const total = ((end - start) / 86400000 + 1) * selected.length,
    offset = Number(p.get("offset") ?? 0),
    limit = Number(p.get("limit") ?? 40);
  return {
    status: "OK",
    rows: Array.from(
      { length: Math.max(0, Math.min(limit, total - offset)) },
      (_, n) => {
        const i = offset + n,
          sid = selected[i % selected.length];
        return {
          ...example.rows[0],
          series_id: sid,
          observation_date: new Date(
            end - Math.floor(i / selected.length) * 86400000,
          )
            .toISOString()
            .slice(0, 10),
          value: "5.0000",
          state: "observed",
          provider_extra: { controlled_fixture: true },
          basis_points: sid.startsWith("BAML") ? "500.0000" : null,
          origin: "controlled_fixture",
          ...(p.has("as_of") &&
          Date.parse(p.get("as_of")!) <
            Date.parse(example.rows[1].available_at!)
            ? {
                value: null,
                basis_points: null,
                revision: null,
                available_at: null,
                observed_at: null,
                raw_locator: null,
                metadata_locator: null,
                state: "not_known_at_cutoff",
                origin: null,
              }
            : {}),
          attribution: "Controlled fixture; no official numeric acquisition",
        };
      },
    ),
    as_of: p.get("as_of") ?? "2026-11-05T14:00:00+00:00",
    total,
    offset,
    next_offset: offset + limit < total ? offset + limit : null,
    provider_requests: 0,
    query_mode: p.has("as_of")
      ? "history"
      : p.get("local_only") === "true"
        ? "local"
        : "current",
    upstream: { provider_requests: 0, cache_hits: 0, issues: [] },
    coverage: { series: [] },
    ...extra,
  };
}
