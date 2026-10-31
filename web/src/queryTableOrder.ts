// Presentation only: copy before sorting; equal months keep their input order.
export function newestMonthFirst<T>(
  rows: readonly T[],
  month: (row: T) => string | null | undefined,
): T[] {
  return [...rows].sort((a, b) =>
    (month(b) ?? "").localeCompare(month(a) ?? ""),
  );
}
