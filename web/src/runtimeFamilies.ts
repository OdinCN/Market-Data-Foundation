import type { RuntimeFamily } from "./RuntimeFamily";
import { TDX_FAMILY } from "./TdxDailyFamily";
import { TDX_ROSTER_FAMILY } from "./TdxRosterFamily";

// New non-Macro families register leaf adapters here; App and the global
// workbench render them through the same path without family-specific routes.
export const RUNTIME_FAMILIES: readonly RuntimeFamily[] = [
  TDX_FAMILY,
  TDX_ROSTER_FAMILY,
];
