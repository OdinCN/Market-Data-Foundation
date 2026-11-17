import { keyOf, sameKey, type Key } from "./workflowModel";
import type { Route } from "./workflowRoute";

/** An explicit all-assets filter survives record selection. Without a filter,
 * an asset/source deep link reads that exact asset and version only. */
export function collectionReadScope(
  route: Route,
  invalid = false,
  resolved?: Key,
) {
  const fields = route.fields;
  const field =
    fields.get("tab") === "periodic" ? "periodic_asset" : "execution_asset";
  const target = fields.has("asset") || fields.has("operation");
  const selected =
    fields.get("asset") && fields.get("asset_revision")
      ? {
          identity: fields.get("asset")!,
          revision: fields.get("asset_revision")!,
        }
      : target
        ? resolved
        : undefined;
  const filter = fields.get(field) ?? (selected ? keyOf(selected) : "all");
  const includes = (key: Key) =>
    !invalid && !route.invalid && (filter === "all" || filter === keyOf(key));
  return {
    field,
    filter,
    selected,
    includes,
    reads: (key: Key) =>
      includes(key) ||
      (!invalid &&
        !route.invalid &&
        !!fields.get("job") &&
        sameKey(selected, key)),
  };
}
