export const FINANCIAL_PREFIX = "/api/r5/financial/timeseries";

// One builder for the real local read and the documentation's current request.
export function financialQuerySuffix({
  start,
  end,
  series,
  cutoff,
  offset = 0,
}: {
  start: string;
  end: string;
  series: string;
  cutoff: string;
  offset?: number;
}) {
  const params = new URLSearchParams({
    start,
    end,
    series,
    offset: String(offset),
    limit: "200",
  });
  if (cutoff) params.set("as_of", new Date(cutoff).toISOString());
  return `/query?${params}`;
}
