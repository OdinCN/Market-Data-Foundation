// Missing provenance is a fact to preserve, never evidence of manual/automatic.
export function executionOrigin(origin?: string | null) {
  if (origin === "manual") return "手动采集";
  if (["automatic", "schedule", "periodic"].includes(origin ?? ""))
    return "自动策略";
  return "触发来源待核实";
}
