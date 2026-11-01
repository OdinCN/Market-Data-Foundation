export const menus = [
  ["overview", "运行总览", "平台运行与资源概览"],
  ["access", "数据源管理", "接入包、能力与来源配置"],
  ["assets", "数据资产", "逻辑资产、本地持有与版本"],
  ["collection", "采集任务", "初始化、增量采集与执行回查"],
  ["platform", "平台管理", "运行实例、存储配置与诊断"],
] as const;
export type View = (typeof menus)[number][0] | "interfaces" | "services";
export type Route = { view: View; fields: URLSearchParams; invalid: boolean };
export function route(): Route {
  const fields = new URLSearchParams(location.hash.slice(1));
  const known = [
    "ui",
    "view",
    "page",
    "asset",
    "execution_asset",
    "periodic_asset",
    "execution_schedule",
    "asset_revision",
    "release_event",
    "release_evidence",
    "operation",
    "reference",
    "as_of",
    "version",
    "tab",
    "job",
    "diagnostic",
    "component",
    "package",
    "instance",
    "schedule",
    "mode",
  ];
  let invalid = [...fields.keys()].some(
    (key) => !known.includes(key) || fields.getAll(key).length !== 1,
  );
  for (const [key, value] of fields) {
    if (["ui", "view", "page"].includes(key)) continue;
    if (key === "reference") invalid ||= !/^\d{4}-(0[1-9]|1[0-2])$/.test(value);
    else if (key === "as_of")
      invalid ||=
        !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(
          value,
        ) || !Number.isFinite(Date.parse(value));
    else
      invalid ||=
        value.length > (key === "operation" ? 512 : 256) ||
        !/^[a-zA-Z0-9@:._/-]+$/.test(value);
  }
  let view: View = "overview";
  if (fields.has("ui") || fields.has("view")) {
    const legacy = ["interfaces", "services"].includes(
      fields.get("view") ?? "",
    );
    const menu = menus.find((m) => m[0] === fields.get("view"));
    invalid ||=
      fields.get("ui") !== "r5" || (!menu && !legacy) || fields.has("page");
    view = legacy ? (fields.get("view") as View) : (menu?.[0] ?? "overview");
  } else if (fields.has("page")) {
    const pages: View[] = [
      "overview",
      "access",
      "interfaces",
      "assets",
      "collection",
      "services",
      "platform",
      "platform",
      "interfaces",
    ];
    invalid ||= !/^[0-8]$/.test(fields.get("page")!);
    view = pages[Number(fields.get("page"))] ?? "overview";
    if (fields.get("page") === "8" && !fields.has("tab"))
      fields.set("tab", "realtime");
  } else invalid ||= fields.size > 0;
  if (view === "services") {
    view = "assets";
    fields.set("tab", "local");
  }
  if (view === "interfaces") {
    view = "platform";
    fields.set("tab", "diagnostics");
  }
  fields.set("view", view);
  fields.set("ui", "r5");
  fields.delete("page");
  if (fields.has("tab")) {
    const tabs: Record<View, string[]> = {
      overview: ["diagnostics"],
      access: ["capabilities", "sources"],
      interfaces: ["api", "realtime"],
      assets: ["local"],
      collection: ["new", "periodic", "executions"],
      services: [],
      platform: ["instances", "storage", "diagnostics"],
    };
    invalid ||= !tabs[view].includes(fields.get("tab")!);
  }
  if (fields.has("mode"))
    invalid ||= view !== "collection" || fields.get("mode") !== "backfill";
  if (view === "collection") {
    // Legacy deep links become visible filters on their own tab only.
    const selected =
      fields.get("asset") && fields.get("asset_revision")
        ? `${fields.get("asset")}@${fields.get("asset_revision")}`
        : "all";
    for (const [tab, field] of [
      ["executions", "execution_asset"],
      ["periodic", "periodic_asset"],
    ])
      if (!fields.has(field))
        fields.set(field, fields.get("tab") === tab ? selected : "all");
    if (!fields.has("execution_schedule"))
      fields.set(
        "execution_schedule",
        fields.get("tab") === "executions"
          ? (fields.get("schedule") ?? "all")
          : "all",
      );
  }
  return { view, fields, invalid };
}
export type Navigate = (
  view: View,
  fields?: Record<string, string | null>,
  preserve?: boolean,
) => void;
