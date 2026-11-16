/** Test oracle for isolated protocol fixtures, independent of product registry
 * strategy declarations and rendered controls. New protocols add one adapter;
 * an adapter may return null for a genuinely policy-free API. No TDX fallback. */
type Row = {
  protocol: string;
  path: string;
  profile?: string | { id: string };
};
type Exchange = (request: {
  path: string;
  method?: string;
}) => Promise<{ status: number; body: any }>;
type Policy = { id: string; path: string } | null;
type Adapter = (row: Row, exchange: Exchange) => Promise<Policy>;
export function protocolExecutionOrigins(row: Row) {
  const profile =
    typeof row.profile === "string" ? row.profile : row.profile?.id;
  if (row.protocol === "macro") {
    const strict = ["gdp", "claims"].includes(profile ?? "");
    return { automatic: strict ? "schedule" : "automatic", unknown: !strict };
  }
  if (row.protocol === "financial")
    return { automatic: "automatic", unknown: true };
  if (["tdx", "roster"].includes(row.protocol))
    return { automatic: "periodic", unknown: true };
  throw Error("missing execution evidence adapter: " + row.protocol);
}
const adapters: Record<string, Adapter> = {
  macro: async (row, exchange) => {
    const path = row.path.replace(/\/jobs$/, "/strategy");
    const value = await exchange({ path });
    if (value.status !== 200) return null;
    if (
      typeof value.body.exists !== "boolean" ||
      typeof value.body.enabled !== "boolean"
    )
      throw Error("invalid Macro policy evidence");
    const profile =
      typeof row.profile === "string" ? row.profile : row.profile?.id;
    if (!profile) throw Error("missing Macro protocol profile");
    return { id: `${profile}-auto`, path };
  },
  financial: async (row, exchange) => {
    const path = row.path.replace(/\/jobs$/, "/strategy");
    const value = await exchange({ path });
    if (value.status !== 200) return null;
    if (value.body.status !== "OK" || !Array.isArray(value.body.tasks))
      throw Error("invalid financial policy evidence");
    return { id: "financial-auto", path };
  },
  tdx: async (_row, exchange) => tdxPolicy("daily", exchange),
  roster: async (_row, exchange) => tdxPolicy("roster", exchange),
};
async function tdxPolicy(
  kind: "daily" | "roster",
  exchange: Exchange,
): Promise<Policy> {
  const path = "/api/r5/tdx/daily/strategy";
  const value = await exchange({ path });
  if (value.status !== 200) return null;
  if (
    typeof value.body.enabled !== "boolean" ||
    typeof value.body.worker_online !== "boolean"
  )
    throw Error("invalid TDX policy evidence");
  return { id: `tdx-${kind}-auto`, path };
}
export async function expectedPolicy(
  row: Row,
  exchange: Exchange,
  protocols = adapters,
): Promise<Policy> {
  const adapter = protocols[row.protocol];
  if (!adapter) throw Error(`missing policy evidence adapter: ${row.protocol}`);
  return adapter(row, exchange);
}
