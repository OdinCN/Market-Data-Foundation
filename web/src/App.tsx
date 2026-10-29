import { beijingToday, nowBeijingIso, toBeijingIso } from "./beijingTime";
import { useEffect, useMemo, useState } from "react";
import {
  request,
  type Catalog,
  type Directory,
  type Jobs,
  type Management,
  type Observability,
  type Query,
  type Transport,
} from "./api";
import { InterfacesPage, LocalReader } from "./WorkflowCatalog";
import { PackageSources } from "./PackageSources";
import { RuntimeAssets } from "./RuntimeAssets";
import { CpiOrdinaryQuery } from "./CpiOrdinaryQuery";
import { CpiTimeSeries } from "./CpiTimeSeries";
import { useCpiAvailability } from "./cpiTimeSeriesApi";
import { getPackages, type Packages } from "./packageApi";
import {
  pceRequest,
  readPce,
  validPceBinding,
  PCE_REFERENCE,
  type PceCatalog,
  type PceAssets,
  type PceEvidence,
} from "./pce";
import { CollectionWorkbench } from "./CollectionWorkbench";
import { OverviewPage, PlatformPage } from "./WorkflowRuntime";
import { Technical } from "./WorkflowTable";
import { useResource } from "./workflowHooks";
import {
  CPI,
  PCE,
  PCE_OPERATION,
  catalogForOperation,
  project,
  sameKey,
} from "./workflowModel";
import { menus, route, type Navigate } from "./workflowRoute";
import "./style.css";
const icons = [
  "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  "M4 9h16v6H4z M8 5v4 M16 15v4 M5 19h14",
  "M12 3 21 8l-9 5-9-5 9-5z M3 13l9 5 9-5 M3 18l9 4 9-4",
  "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z M12 7v5l3 2",
  "M12 9a3 3 0 1 1 0 6 3 3 0 0 1 0-6z M12 2v3m0 14v3M22 12h-3M5 12H2",
];
// Query fields form one scope: partial links must never inherit a prior period.
function appRoute() {
  const page = route();
  page.invalid ||= page.fields.has("reference") !== page.fields.has("as_of");
  return page;
}
export default function App({
  transport = fetch,
  initialQuery,
}: {
  transport?: Transport;
  initialQuery?: Query;
}) {
  const [page, setPage] = useState(appRoute);
  const [collapsed, setCollapsed] = useState(false);
  const [dark, setDark] = useState(false);
  const [generation, setGeneration] = useState(0);
  const [defaultQuery] = useState<Query>(() => ({
    reference: initialQuery?.reference ?? beijingToday().slice(0, 7),
    as_of: initialQuery?.as_of ?? nowBeijingIso(),
  }));
  const reference = page.fields.get("reference");
  const asOf = page.fields.get("as_of");
  const activeReference =
    reference ??
    (page.fields.get("asset") === PCE.identity ||
    page.fields.get("operation") === PCE_OPERATION
      ? PCE_REFERENCE
      : defaultQuery.reference);
  const sourceAsOf = asOf ?? defaultQuery.as_of;
  const activeAsOf = toBeijingIso(sourceAsOf ?? "") || sourceAsOf;
  const query = useMemo<Query>(
    () => ({ reference: activeReference, as_of: activeAsOf }),
    [activeReference, activeAsOf],
  );
  useEffect(() => {
    const update = () => setPage(appRoute());
    window.addEventListener("hashchange", update);
    window.addEventListener("popstate", update);
    return () => {
      window.removeEventListener("hashchange", update);
      window.removeEventListener("popstate", update);
    };
  }, []);
  const navigate: Navigate = (view, fields = {}, preserve = false) => {
    const hash =
      preserve && !page.invalid
        ? new URLSearchParams(page.fields)
        : new URLSearchParams();
    if (view === "collection" && page.view === "collection" && !page.invalid) {
      for (const key of [
        "execution_asset",
        "periodic_asset",
        "execution_schedule",
      ])
        if (!hash.has(key) && page.fields.has(key))
          hash.set(key, page.fields.get(key)!);
    }
    if (
      ["assets", "access", "collection", "services", "interfaces"].includes(
        view,
      ) &&
      !hash.has("package") &&
      page.fields.has("package")
    )
      hash.set("package", page.fields.get("package")!);
    hash.delete("page");
    hash.set("ui", "r5");
    hash.set(
      "view",
      view === "services"
        ? "assets"
        : view === "interfaces"
          ? "platform"
          : view,
    );
    if (view === "services") hash.set("tab", "local");
    if (view === "interfaces") hash.set("tab", "diagnostics");
    if (!hash.has("reference") && query.reference)
      hash.set("reference", query.reference);
    if (!hash.has("as_of") && query.as_of) hash.set("as_of", query.as_of);
    for (const [key, value] of Object.entries(fields))
      value === null ? hash.delete(key) : hash.set(key, value);
    if (
      (fields.asset === PCE.identity || fields.operation === PCE_OPERATION) &&
      !("reference" in fields)
    )
      hash.set("reference", PCE_REFERENCE);
    location.hash = hash.toString();
    setPage(appRoute());
    document.querySelector("main")?.scrollIntoView?.({ block: "start" });
  };
  const directory = useResource<Directory>(
    (signal) => request(transport, "/api/r5/sources", {}, signal),
    [transport, generation, page.invalid],
    !page.invalid,
  );
  const packages = useResource<Packages>(
    (signal) => getPackages(transport, signal),
    [transport, generation, page.invalid],
    !page.invalid && ["access", "assets"].includes(page.view),
  );
  const management = useResource<Management>(
    (signal) => request(transport, "/api/r3/management", undefined, signal),
    [transport, generation, page.invalid],
    !page.invalid,
  );
  const observes = page.view === "overview" || page.view === "platform";
  const observation = useResource<Observability>(
    (signal) => request(transport, "/api/r5/observability", undefined, signal),
    [transport, generation, observes, page.invalid],
    observes && !page.invalid,
  );
  const summaryJobs = useResource<Jobs>(
    (signal) =>
      request(
        transport,
        "/api/r5/cpi/jobs",
        page.fields.get("job") ? { job_identity: page.fields.get("job")! } : {},
        signal,
      ),
    [
      transport,
      generation,
      page.view === "overview",
      page.invalid,
      page.fields.get("job"),
    ],
    page.view === "overview" && !page.invalid,
  );
  const identity = page.fields.get("asset");
  const revision = page.fields.get("asset_revision");
  const operation = page.fields.get("operation");
  const operationEntry = directory.data?.entries.find(
    (e) => e.identity === operation,
  );
  const key =
    identity && revision ? { identity, revision } : operationEntry?.asset;
  const wantsCatalog =
    (page.view === "collection" ||
      (page.view === "assets" && !!(identity || operation))) &&
    (!operation || sameKey(operationEntry?.asset, CPI)) &&
    ((!identity && !operation) || sameKey(key, CPI));
  const cpiAvailability = useCpiAvailability(
    transport,
    generation,
    !page.invalid &&
      ["assets", "collection"].includes(page.view) &&
      (!operation ||
        (!!operationEntry?.asset &&
          (!identity || identity === operationEntry.asset.identity) &&
          (!revision || revision === operationEntry.asset.revision))),
    page.view === "collection",
  );
  const catalog = useResource<Catalog>(
    (signal) => request(transport, "/api/r5/cpi", query, signal),
    [transport, generation, query, wantsCatalog, page.view, page.invalid],
    wantsCatalog && !page.invalid && cpiAvailability.legacy,
  );
  const readCatalog = operation
    ? catalogForOperation(catalog.data, operationEntry)
    : catalog.data;
  const pceCatalog = useResource<PceCatalog>(
    async (signal) => {
      const c = await pceRequest<PceCatalog>(transport, "/api/pce/catalog", {
        signal,
      });
      if (
        c.source_qualified !== false ||
        !validPceBinding(c.binding) ||
        !validPceBinding(c.submission?.binding)
      )
        throw new Error("pce_binding_mismatch");
      return c;
    },
    [transport, generation, page.invalid],
    !page.invalid,
  );
  const wantsPce =
    ["assets", "services"].includes(page.view) &&
    sameKey(key, PCE) &&
    (!operation || operation === PCE_OPERATION);
  const pceEvidence = useResource<PceEvidence>(
    async (signal) => {
      const inventory = await pceRequest<PceAssets>(
        transport,
        "/api/pce/assets",
        { signal },
      );
      if (
        !validPceBinding(inventory.binding) ||
        !Array.isArray(inventory.versions)
      )
        throw new Error("pce_inventory_mismatch");
      let reading = null;
      try {
        reading = await readPce(
          transport,
          key ? query.reference! : PCE_REFERENCE,
          query.as_of!,
          undefined,
          signal,
        );
      } catch {
        /* Inventory is not proof of readability. */
      }
      return { catalog: pceCatalog.data!, assets: inventory, reading };
    },
    [transport, generation, query, wantsPce, pceCatalog.data, page.view],
    wantsPce && !!pceCatalog.data && !page.invalid,
  );
  const model = project(
    directory.data,
    readCatalog,
    management.data,
    pceCatalog.data,
    pceEvidence.data,
  );
  const source = model.interfaces.find((i) => i.id === operation);
  const asset = model.assets.find((a) => sameKey(a.key, key));
  const invalidIdentity = !!(
    directory.data &&
    ((operation && !source) ||
      (identity &&
        ((revision && !asset) ||
          (!revision &&
            (page.view !== "assets" ||
              !!operation ||
              page.fields.has("version"))))) ||
      (revision && !identity) ||
      (source &&
        identity &&
        (!source.asset ||
          !sameKey(source.asset, { identity, revision: revision! }))) ||
      (page.fields.has("component") &&
        !model.components.some((c) => c.id === page.fields.get("component"))))
  );
  const missingMapping =
    !!source &&
    !source.asset &&
    ["assets", "collection", "services"].includes(page.view);
  const selectedAsset =
    invalidIdentity || missingMapping || (operation && !source)
      ? undefined
      : asset;
  const menu = menus.find((m) => m[0] === page.view)!;
  const busy =
    cpiAvailability.busy ||
    packages.busy ||
    directory.busy ||
    catalog.busy ||
    pceEvidence.busy ||
    management.busy ||
    observation.busy;
  const localReader =
    selectedAsset &&
    (!sameKey(selectedAsset.key, CPI) || cpiAvailability.legacy) &&
    (sameKey(selectedAsset.key, CPI) || sameKey(selectedAsset.key, PCE)) ? (
      <LocalReader
        key={`${identity}:${revision}:${operation}`}
        catalog={readCatalog}
        pce={selectedAsset.pce}
        route={page}
        query={query}
        transport={transport}
        generation={generation}
        selectCpiVersion={(version) => navigate("assets", { version }, true)}
        selectPceVersion={(version) =>
          navigate(
            "assets",
            {
              version,
              reference: PCE_REFERENCE,
              as_of: selectedAsset.pce!.assets.as_of,
            },
            true,
          )
        }
        setQuery={(q, version) => {
          const hash = new URLSearchParams(page.fields);
          if (version) hash.set("version", version);
          else hash.delete("version");
          hash.set("reference", q.reference!);
          hash.set("as_of", q.as_of!);
          history.replaceState(null, "", `#${hash}`);
          setPage(appRoute());
        }}
      />
    ) : null;
  const ordinaryQuery =
    selectedAsset &&
    sameKey(selectedAsset.key, CPI) &&
    cpiAvailability.legacy ? (
      <CpiOrdinaryQuery
        key={`ordinary:${identity}:${revision}:${operation ?? ""}:${query.reference ?? ""}`}
        transport={transport}
        initialReference={query.reference ?? ""}
        operationId={operation ?? null}
        entries={selectedAsset.interfaces.map((entry) => entry.dto)}
        readExact={(result) =>
          navigate(
            "assets",
            {
              asset: result.definition_ref.identity,
              asset_revision: result.definition_ref.revision,
              operation: result.operation_id ?? null,
              version: result.version_identity ?? null,
              reference: result.reference,
              as_of: result.decision_at,
              tab: "local",
            },
            true,
          )
        }
      />
    ) : null;
  return (
    <div
      className={`shell ${dark ? "dark" : ""} ${collapsed ? "collapsed" : ""}`}
    >
      <aside className="sidebar">
        <a
          className="brand"
          href="#ui=r5&view=overview"
          aria-label="Market Data Foundation"
        >
          <img src="/mdf-data-hub.png" alt="" />
          <span>
            Market Data
            <br />
            Foundation
          </span>
        </a>
        <nav aria-label="一级导航">
          {menus.map(([view, name], index) => (
            <button
              type="button"
              key={view}
              title={name}
              aria-label={name}
              aria-current={page.view === view ? "page" : undefined}
              onClick={() =>
                navigate(view, {
                  asset: identity,
                  asset_revision: revision,
                  operation,
                  version: page.fields.get("version"),
                  package: page.fields.get("package"),
                })
              }
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d={icons[index]} />
              </svg>
              <span className="nav-label">{name}</span>
            </button>
          ))}
        </nav>
        <button
          className="collapse"
          aria-label={collapsed ? "展开侧栏" : "收起侧栏"}
          onClick={() => setCollapsed(!collapsed)}
        >
          {collapsed ? "»" : "« 收起侧栏"}
        </button>
        <small className="scope-indicator">R5 · 开发候选</small>
      </aside>
      <div className="workspace">
        <header>
          <span className="header-context">
            MDF 控制台 <span className="muted"> / {menu[1]}</span>
          </span>
          <div className="header-actions">
            <button
              aria-label="切换主题"
              aria-pressed={dark}
              onClick={() => setDark(!dark)}
            >
              {dark ? "☀ 亮色" : "☾ 深色"}
            </button>
            {(page.view !== "collection" ||
              cpiAvailability.legacy ||
              invalidIdentity ||
              (!cpiAvailability.data && !cpiAvailability.failure) ||
              ((page.fields.get("tab") ?? "new") === "new" &&
                !sameKey(key, CPI))) && (
              <button
                aria-label="刷新"
                disabled={page.invalid || busy}
                onClick={() => setGeneration((g) => g + 1)}
              >
                ↻ 刷新
              </button>
            )}
          </div>
        </header>
        <main>
          <div className="heading">
            <h1>{menu[1]}</h1>
            <p>{menu[2]}</p>
          </div>
          {page.invalid ? (
            <p role="alert" className="inline-notice">
              无法定位此链接：路由或关联参数无效。请选择一级导航继续。
            </p>
          ) : (
            <>
              {directory.failure && (
                <div role="alert">
                  目录暂不可读取。<Technical>{directory.failure}</Technical>
                </div>
              )}
              {invalidIdentity && (
                <p role="alert">
                  关联对象缺失或身份冲突：资产、修订与来源必须精确匹配，未改选其他对象。
                </p>
              )}
              {missingMapping && (
                <p role="status">
                  该接口无已准入
                  Definition，关联资产映射不可用，未创建虚构资产。
                </p>
              )}
              {page.view === "overview" && (
                <OverviewPage
                  data={observation.data}
                  jobs={summaryJobs.data}
                  failure={observation.failure}
                  navigate={navigate}
                />
              )}
              {page.view !== "overview" && (
                <section className="content-panel">
                  {page.view === "access" && (
                    <PackageSources
                      data={packages.data}
                      busy={packages.busy}
                      failure={packages.failure}
                      route={page}
                      navigate={navigate}
                      transport={transport}
                      changed={() => setGeneration((g) => g + 1)}
                    />
                  )}
                  {page.view === "assets" && (
                    <RuntimeAssets
                      data={packages.data}
                      busy={packages.busy}
                      failure={packages.failure}
                      route={page}
                      navigate={navigate}
                      cpi={
                        !cpiAvailability.legacy &&
                        (!!cpiAvailability.data || !!cpiAvailability.failure) &&
                        !invalidIdentity &&
                        !missingMapping &&
                        identity === CPI.identity &&
                        (!revision || revision === CPI.revision) &&
                        (!operation || sameKey(selectedAsset?.key, CPI)) ? (
                          <>
                            <CpiTimeSeries
                              transport={transport}
                              availability={cpiAvailability}
                            />
                          </>
                        ) : undefined
                      }
                      cpiAction={
                        <button
                          onClick={() =>
                            navigate("collection", {
                              asset: CPI.identity,
                              asset_revision: CPI.revision,
                              operation: null,
                              tab: "new",
                            })
                          }
                        >
                          采集数据
                        </button>
                      }
                      reference={
                        selectedAsset ? (
                          <>
                            <h3>{selectedAsset.label}</h3>
                            <p>
                              代码合同参考；运行接入与资产可见性以上方目录为准。
                            </p>
                            <div className="linked-interfaces">
                              {selectedAsset.interfaces.map((entry) => (
                                <button
                                  key={entry.id}
                                  aria-pressed={source?.id === entry.id}
                                  onClick={() =>
                                    navigate(
                                      "assets",
                                      { operation: entry.id, version: null },
                                      true,
                                    )
                                  }
                                >
                                  {entry.source} · {entry.variant}
                                </button>
                              ))}
                            </div>
                            {ordinaryQuery}
                            <button
                              onClick={() =>
                                navigate("collection", {
                                  asset: selectedAsset.key.identity,
                                  asset_revision: selectedAsset.key.revision,
                                  operation,
                                  tab: "new",
                                })
                              }
                            >
                              采集任务 →
                            </button>
                            {!localReader && (
                              <p>
                                {selectedAsset.catalog?.classification ===
                                "capability_not_configured"
                                  ? "未绑定本地读取范围，持有与发布状态待确认"
                                  : "该资产尚无通用 Web Reader 绑定或当前读取范围不可核实。"}
                              </p>
                            )}
                            {localReader}
                          </>
                        ) : null
                      }
                      reader={localReader}
                      ordinary={ordinaryQuery}
                    />
                  )}
                  {page.view === "collection" && (
                    <CollectionWorkbench
                      key={`${identity}:${revision}:${operation}`}
                      assets={model.assets}
                      asset={selectedAsset}
                      source={invalidIdentity ? undefined : source}
                      invalidIdentity={invalidIdentity || missingMapping}
                      route={page}
                      navigate={navigate}
                      transport={transport}
                      generation={generation}
                      cpiAvailability={cpiAvailability}
                    />
                  )}
                  {page.view === "platform" && (
                    <PlatformPage
                      data={observation.data}
                      management={management.data}
                      requestedAt={management.startedAt}
                      instances={model.instances}
                      route={page}
                      navigate={navigate}
                      failure={management.failure ?? observation.failure}
                    />
                  )}
                  {page.view === "platform" &&
                    page.fields.get("tab") === "diagnostics" &&
                    !page.fields.has("diagnostic") &&
                    !page.fields.has("job") && (
                      <details className="technical" open>
                        <summary>接口实现目录（开发诊断）</summary>
                        <p>此目录记录代码合同，不代表来源已经接通。</p>
                        <InterfacesPage
                          interfaces={model.interfaces}
                          selected={invalidIdentity ? undefined : source}
                          route={page}
                          navigate={navigate}
                        />
                      </details>
                    )}
                  {(directory.busy || catalog.busy) && (
                    <p className="small" role="status">
                      正在读取当前范围…
                    </p>
                  )}
                  {pceEvidence.failure && (
                    <p role="status">PCE 持有与读取范围暂不可核实。</p>
                  )}
                  {catalog.failure && (
                    <div role="status">
                      持有与读取范围暂不可核实。
                      <Technical>{catalog.failure}</Technical>
                    </div>
                  )}
                </section>
              )}
            </>
          )}
          <footer>MDF 统一平台 · R5 产品流程审核候选</footer>
        </main>
      </div>
    </div>
  );
}
