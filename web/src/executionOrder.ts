/** Only observed creation times determine cross-asset order; unknowns remain last. */
export function executionCreatedOrder(
  a: { id: string; createdAt?: string | null },
  b: { id: string; createdAt?: string | null },
) {
  const stamp = (v?: string | null) =>
    v && Number.isFinite(Date.parse(v)) ? Date.parse(v) : -Infinity;
  return stamp(b.createdAt) - stamp(a.createdAt) || 0;
}
