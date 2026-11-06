export const FINANCIAL_PREFIX = "/api/r5/financial/timeseries";

// One builder for current local-first reads and explicit cutoff history.
export function financialQuerySuffix({
  start,
  end,
  series,
  cutoff,
  history = Boolean(cutoff),
  localOnly = false,
  offset = 0,
}: {
  start: string;
  end: string;
  series: string;
  cutoff: string;
  history?: boolean;
  localOnly?: boolean;
  offset?: number;
}) {
  const params = new URLSearchParams({
    start,
    end,
    series,
    offset: String(offset),
    limit: "200",
    local_only: String(localOnly || history),
  });
  if (history) {
    if (!cutoff) throw new Error("financial_history_cutoff_required");
    params.set("as_of", new Date(cutoff).toISOString());
  }
  return `/query?${params}`;
}
