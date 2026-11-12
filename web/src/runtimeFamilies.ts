import type { RuntimeFamily } from "./RuntimeFamily";
import { TDX_FAMILY } from "./TdxDailyFamily";

// New non-Macro families register leaf adapters here; App and the global
// workbench render them through the same path without family-specific routes.
export const RUNTIME_FAMILIES: readonly RuntimeFamily[] = [TDX_FAMILY];
