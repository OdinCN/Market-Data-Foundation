import {
  formatBeijingTime,
  beijingInstantKey,
  toBeijingInput,
  toBeijingIso,
  beijingInputToIso,
} from "./beijingTime";
import { DateField } from "./DateField";
import {
  pceLabels,
  readPce,
  PCE_REFERENCE,
  type PceEvidence,
  type PceRead,
} from "./pce";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  request,
  type Catalog,
  type Query,
  type ReadMode,
  type ReadResult,
  type Transport,
} from "./api";
import {
  type Asset,
  type Component,
  type Interface,
  type Service,
  keyOf,
  PCE_OPERATION,
} from "./workflowModel";
import type { Navigate, Route } from "./workflowRoute";
import { Fact, Tabs, Technical, WorkflowTable } from "./WorkflowTable";
import { failureName } from "./workflowHooks";
export function AccessPage({
  components,
  selected,
  choose,
  navigate,
}: {
  components: Component[];
  selected: Component | undefined;
  choose: (id: string) => void;
  navigate: Navigate;
}) {
  return (
    <>
      <div className="toolbar">
        <h2>接入组件目录</h2>
        <button
          className="primary"
          disabled
          title="当前 API 没有组件登记处理器"
        >
          ＋ 添加组件
        </button>
        <small>组件登记处理器未接入</small>
      </div>
      <WorkflowTable
        label="接入组件"
        columns={["接入组件", "上游来源", "接入方式", "绑定状态"]}
        rows={components.map((c) => ({
          id: c.id,
          search: `${c.label} ${c.upstreams.join(" ")}`,
          cells: [
            c.label,
            c.upstreams.join(" / "),
            c.id === "Macro"
              ? "Python / 官方协议"
              : c.id === "TDX"
                ? "Go Driver"
                : "API Adapter",
            <span className="chip">绑定待核实</span>,
          ],
        }))}
        selected={selected?.id}
        onSelect={choose}
      >
        {selected && (
          <>
            <h3>{selected.label}</h3>
            <p className="muted">{selected.upstreams.join(" · ")}</p>
            <dl>
              <Fact name="关联接口">
                {selected.interfaces.length} 个具名接口 / 操作
              </Fact>
              <Fact name="当前配置">执行绑定未核实；合同存在不代表激活</Fact>
              <Fact name="版本与来源">
                {[
                  ...new Set(
                    selected.interfaces
                      .map((i) => i.dto.source_contract?.revision)
                      .filter(Boolean),
                  ),
                ].join(" · ") || "版本未提供"}
              </Fact>
              <Fact name="运行归属">
                {selected.id === "KD"
                  ? "Owner 独立管理，只观察"
                  : "当前实例记录见平台管理"}
              </Fact>
            </dl>
            <button
              className="primary"
              onClick={() => navigate("interfaces", { component: selected.id })}
            >
              查看注册接口 →
            </button>
            <Technical>
              <p>实现关联</p>
              {selected.implementations.map((i) => (
                <p key={i}>
                  <code>{i}</code>
                </p>
              ))}
              {selected.interfaces.map((i) => (
                <p key={i.id}>
                  {i.source} · {i.variant} ·{" "}
                  {i.dto.source_contract?.build_revision ?? "构建版本未提供"}
                </p>
              ))}
            </Technical>
          </>
        )}
      </WorkflowTable>
    </>
  );
}
export function InterfacesPage({
  interfaces,
  selected,
  route,
  navigate,
}: {
  interfaces: Interface[];
  selected: Interface | undefined;
  route: Route;
  navigate: Navigate;
}) {
  const [realtime, setRealtime] = useState(false);
  const component = route.fields.get("component");
  const rows = interfaces.filter(
    (i) => !realtime && (!component || component === i.component),
  );
  return (
    <>
      <Tabs
        label="接口类型"
        values={[
          ["api", "API / 查询接口"],
          ["realtime", "实时 / Socket / WS"],
        ]}
        value={realtime ? "realtime" : "api"}
        onChange={(tab) => {
          setRealtime(tab === "realtime");
          navigate(
            "interfaces",
            { operation: null, asset: null, asset_revision: null },
            true,
          );
        }}
      />
      <div className="toolbar">
        <h2>
          {realtime ? "实时接口" : "已登记操作"}
          {component && ` · ${component}`}
        </h2>
        <button className="primary" disabled title="未提供接口注册处理器">
          ＋ 注册接口
        </button>
        {component && (
          <button
            onClick={() => navigate("interfaces", { component: null }, true)}
          >
            全部组件
          </button>
        )}
      </div>
      <WorkflowTable
        key={`${component}:${realtime}`}
        label="注册接口"
        columns={["接口名称", "组件 / 来源", "用途 / 变体", "状态"]}
        empty={
          realtime
            ? "实时 / Socket / WS 注册能力尚未提供；TDX 会话与 KD 资格需独立核实。"
            : "暂无已登记操作"
        }
        rows={rows.map((i) => ({
          id: i.id,
          search: `${i.label} ${i.source} ${i.variant} ${i.dto.operation} ${i.id}`,
          cells: [
            i.label,
            `${i.component} / ${i.source}`,
            i.variant,
            <span className="chip">
              {i.dto.code_delivery === "DELIVERED" ? "合同已知" : "候选待验证"}
            </span>,
          ],
        }))}
        selected={selected?.id}
        onSelect={(id) =>
          navigate(
            "interfaces",
            { operation: id, asset: null, asset_revision: null },
            true,
          )
        }
      >
        {selected &&
          !realtime &&
          (!component || component === selected.component) && (
            <>
              <h3>{selected.label}</h3>
              <p className="muted">
                {selected.source} · {selected.variant}
              </p>
              <dl>
                <Fact name="具名操作">{selected.dto.operation}</Fact>
                <Fact name="请求参数">
                  {selected.pce
                    ? "2026-07；T20807 → T20804；Year=2025,2026；Monthly；lines 1/25"
                    : selected.dto.source_contract
                      ? "按具名来源的参考期 / 范围合同；当前目录未公开完整参数 schema"
                      : "当前候选尚无运行描述符，参数不可核实"}
                </Fact>
                <Fact name="返回与单位">
                  {selected.pce
                    ? "PCE 总体 / 核心环比与同比，季调，单位百分比"
                    : selected.asset?.identity.includes("cpi")
                      ? selected.dto.component_identity.endsWith("cpi_history")
                        ? "四条 CPI 指数；按历史合同推导四项变动百分比"
                        : "四项 CPI 变动指标，单位百分比"
                      : "以具名领域合同为准；当前目录未公开完整字段 / 单位 schema"}
                </Fact>
                <Fact name="当前资格">
                  {selected.pce
                    ? "开发测试，仅支持 2026-07；来源资格尚未通过"
                    : "来源资格与激活状态待核实"}
                </Fact>
              </dl>
              <h4>接口测试结果</h4>
              <p className="muted">
                {selected.pce
                  ? "请从关联资产进入采集任务提交；真实来源由 Host 单次执行。"
                  : "尚未执行。当前无受控 Source 测试端点；真实来源测试需另行显式授权。"}
              </p>
              <button disabled title="没有已授权的 Source 测试处理器">
                显式测试接口
              </button>
              <div className="actions">
                <button
                  className="primary"
                  onClick={() =>
                    navigate("assets", {
                      asset: selected.asset?.identity ?? null,
                      asset_revision: selected.asset?.revision ?? null,
                      operation: selected.id,
                    })
                  }
                >
                  查看关联资产 →
                </button>
              </div>
              {!selected.asset && (
                <p role="status">无已准入 Definition，关联资产映射不可用。</p>
              )}
              <Technical>
                <dl>
                  <Fact name="接口身份">{selected.id}</Fact>
                  <Fact name="Source 变体">
                    {selected.dto.source_contract?.variant ?? "未提供"}
                  </Fact>
                  <Fact name="绑定候选">
                    {selected.dto.binding_contract
                      ? keyOf(selected.dto.binding_contract)
                      : "未提供"}
                  </Fact>
                  <Fact name="时间语义">
                    {selected.dto.source_contract?.temporal_contract}
                  </Fact>
                  <Fact name="返回范围">
                    {selected.dto.source_contract?.completeness_contract}
                  </Fact>
                  <Fact name="采集模式">
                    {selected.dto.source_contract?.acquisition_mode}
                  </Fact>
                  <Fact name="分页 / 新鲜度">
                    {selected.dto.source_contract?.pagination_contract} /{" "}
                    {selected.dto.source_contract?.freshness_contract}
                  </Fact>
                  <Fact name="资格分类">
                    {selected.dto.source_qualification} /{" "}
                    {selected.dto.current_binding}
                  </Fact>
                </dl>
                {selected.dto.reasons.map((r) => (
                  <p key={r}>{r}</p>
                ))}
                {selected.dto.binding_contract?.limitations.map((r) => (
                  <p key={r}>{r}</p>
                ))}
                <p>{selected.dto.evidence_refs.join(" · ")}</p>
                {selected.pce && (
                  <p>
                    {JSON.stringify(selected.pce.binding)} ·{" "}
                    {JSON.stringify(selected.pce.parameters)}
                  </p>
                )}
              </Technical>
            </>
          )}
      </WorkflowTable>
    </>
  );
}
export function AssetsPage({
  assets,
  selected,
  source,
  navigate,
}: {
  assets: Asset[];
  selected: Asset | undefined;
  source: Interface | undefined;
  navigate: Navigate;
}) {
  return (
    <>
      <div className="toolbar">
        <h2>逻辑资产目录</h2>
        <span className="muted">登记、持有、发布与读取分别核实</span>
      </div>
      <WorkflowTable
        label="数据资产"
        columns={["数据资产", "关联来源", "本地持有", "读取状态"]}
        rows={assets.map((a) => ({
          id: keyOf(a.key),
          search: `${a.label} ${a.interfaces.map((i) => i.source).join(" ")} ${keyOf(a.key)}`,
          cells: [
            a.label,
            [...new Set(a.interfaces.map((i) => i.source))].join(" / "),
            a.holding,
            a.readability,
          ],
        }))}
        selected={selected && keyOf(selected.key)}
        onSelect={(id) => {
          const a = assets.find((a) => keyOf(a.key) === id)!;
          navigate("assets", {
            asset: a.key.identity,
            asset_revision: a.key.revision,
          });
        }}
      >
        {selected && (
          <>
            <h3>{selected.label}</h3>
            <p className="muted">逻辑数据资产 · 已知 Definition</p>
            <dl>
              <Fact name="本地材料">
                {selected.holding}；材料清单未由当前 API 单独提供
              </Fact>
              <Fact name="数据版本">
                {selected.pce
                  ? `${selected.pce.assets.versions.length} 个已存储版本（读取资格单独核实）`
                  : selected.versions
                    ? `${selected.versions.versions.length} 个当前查询可见版本`
                    : "版本状态待确认（未绑定持有读取范围）"}
              </Fact>
              <Fact name="发布状态">{selected.publication}</Fact>
              <Fact name="读取资格">{selected.readability}</Fact>
              <Fact name="存储策略">{selected.storage}</Fact>
            </dl>
            {selected.pce && (
              <>
                <p className="muted">
                  开发测试，仅支持 2026-07；总体 /
                  核心环比与同比均为季调百分比。已有发布记录不代表本次采集成功。
                </p>
                {selected.pce.assets.versions.map((v) => (
                  <div className="version" key={v.version_identity}>
                    <b>{v.version_identity}</b>
                    <p>冻结时间：{formatBeijingTime(v.frozen_at)}</p>
                    {v.publications.map((p) => (
                      <p key={p.publication_identity}>
                        {p.publication_identity} · {p.decision} ·{" "}
                        {formatBeijingTime(p.usable_at)}
                      </p>
                    ))}
                    <button
                      onClick={() =>
                        navigate("services", {
                          asset: selected.key.identity,
                          asset_revision: selected.key.revision,
                          operation: selected.interfaces[0]?.id ?? null,
                          version: v.version_identity,
                          reference: PCE_REFERENCE,
                          as_of: selected.pce!.assets.as_of,
                        })
                      }
                    >
                      精确读取此版本 →
                    </button>
                  </div>
                ))}
              </>
            )}
            <h4>关联接口 / 来源变体</h4>
            <div className="linked-interfaces">
              {selected.interfaces.map((i) => (
                <button
                  key={i.id}
                  aria-pressed={source?.id === i.id}
                  onClick={() =>
                    navigate("assets", {
                      asset: selected.key.identity,
                      asset_revision: selected.key.revision,
                      operation: i.id,
                    })
                  }
                >
                  {i.source} · {i.variant}
                </button>
              ))}
            </div>
            {!source && selected.interfaces.length > 1 && (
              <p className="muted">
                采集前请明确选择来源变体；不会按显示名称自动选取。
              </p>
            )}
            <div className="actions">
              <button
                className="primary"
                onClick={() =>
                  navigate("collection", {
                    asset: selected.key.identity,
                    asset_revision: selected.key.revision,
                    operation: source?.id ?? null,
                    tab: "new",
                  })
                }
              >
                采集任务 →
              </button>
              <button
                onClick={() =>
                  navigate("services", {
                    asset: selected.key.identity,
                    asset_revision: selected.key.revision,
                    operation: source?.id ?? null,
                  })
                }
              >
                查看数据服务 →
              </button>
            </div>
            <Technical>
              {selected.pce && (
                <p>
                  {JSON.stringify(selected.pce.catalog.binding)} ·
                  source_qualified=false
                </p>
              )}
              <dl>
                <Fact name="Definition">{keyOf(selected.key)}</Fact>
                <Fact name="查询分类">
                  {selected.catalog?.classification ?? "持有范围未绑定"}
                </Fact>
                <Fact name="证据模式">
                  {selected.catalog?.evidence_mode ?? "CODE_CONTRACTS_ONLY"}
                </Fact>
              </dl>
              {selected.versions?.versions.map((v) => (
                <div key={v.identity} className="version">
                  <b>{v.identity}</b>
                  <p>
                    {v.completeness} · {v.selection_state}
                  </p>
                  {v.publications.map((p) => (
                    <p key={p.identity}>
                      {p.identity} · {p.decision} ·{" "}
                      {formatBeijingTime(p.usable_at)}
                    </p>
                  ))}
                  <button
                    onClick={() =>
                      navigate("services", {
                        asset: selected.key.identity,
                        asset_revision: selected.key.revision,
                        operation: source?.id ?? null,
                        version: v.identity,
                        reference: selected.catalog!.reference,
                        as_of: selected.catalog!.as_of,
                      })
                    }
                  >
                    精确读取此版本 →
                  </button>
                </div>
              ))}
            </Technical>
          </>
        )}
      </WorkflowTable>
    </>
  );
}
export function ServicesPage({
  services,
  asset,
  selected,
  catalog,
  route,
  navigate,
  query,
  setQuery,
  transport,
  generation,
}: {
  services: Service[];
  asset: Asset | undefined;
  selected: Service | undefined;
  catalog: Catalog | null;
  route: Route;
  navigate: Navigate;
  query: Query;
  setQuery: (q: Query) => void;
  transport: Transport;
  generation: number;
}) {
  const namedExact =
    !!(asset?.catalog || asset?.pce) &&
    !!route.fields.get("version") &&
    route.fields.has("reference") &&
    route.fields.has("as_of");
  return (
    <>
      <div className="toolbar">
        <h2>自动开放的合格资产</h2>
        <span className="muted">仅显示当前查询已经核实的本地读取服务</span>
      </div>
      <WorkflowTable
        label="数据服务"
        columns={["服务资产", "数据来源", "版本", "可用性"]}
        empty="当前没有满足读取资格的数据服务。"
        rows={services.map((s) => ({
          id: s.id,
          search: `${s.asset.label} ${s.id}`,
          cells: [
            s.asset.label,
            s.asset.interfaces
              .map((i) => i.source)
              .filter((s, i, a) => a.indexOf(s) === i)
              .join(" / "),
            s.version,
            <span className="chip good">可本地读取</span>,
          ],
        }))}
        selected={selected?.id}
        onSelect={(id) => {
          const s = services.find((s) => s.id === id)!;
          navigate("services", {
            asset: s.asset.key.identity,
            asset_revision: s.asset.key.revision,
            operation: route.fields.get("operation"),
          });
        }}
      >
        {asset ? (
          <>
            <h3>{asset.label}</h3>
            <dl>
              <Fact name="服务状态">
                {selected
                  ? asset.pce
                    ? "可本地读取（开发测试）"
                    : "当前查询已合格"
                  : "读取暂不可用"}
              </Fact>
              <Fact name="原因">
                {selected
                  ? "本地版本与 Publication 已核实"
                  : asset.catalog?.classification ===
                      "capability_not_configured"
                    ? "未绑定本地读取范围，持有与发布状态待确认"
                    : asset.catalog
                      ? "当前参考期 / 时点未满足读取条件"
                      : "该资产尚无通用 Web Reader 绑定"}
              </Fact>
            </dl>
            {asset.pce && (
              <p className="muted">
                开发测试，仅支持
                2026-07；本地可读取不改变来源资格。请用执行记录的版本核验本次产出。
              </p>
            )}
            {(selected || namedExact || asset.catalog?.asset || asset.pce) && (
              <LocalReader
                key={keyOf(asset.key)}
                transport={transport}
                query={query}
                setQuery={setQuery}
                route={route}
                catalog={catalog}
                pce={asset.pce}
                selectPceVersion={(version) =>
                  navigate("services", {
                    asset: asset.key.identity,
                    asset_revision: asset.key.revision,
                    operation: asset.interfaces[0]?.id ?? null,
                    version,
                    reference: PCE_REFERENCE,
                    as_of: asset.pce!.assets.as_of,
                  })
                }
                generation={generation}
              />
            )}
            <button
              onClick={() =>
                navigate("assets", {
                  asset: asset.key.identity,
                  asset_revision: asset.key.revision,
                  operation: route.fields.get("operation"),
                })
              }
            >
              返回同一资产 →
            </button>
          </>
        ) : undefined}
      </WorkflowTable>
    </>
  );
}
export function LocalReader({
  transport,
  query,
  setQuery,
  route,
  catalog,
  generation,
  pce,
}: {
  selectCpiVersion?: (version: string) => void;
  selectPceVersion?: (version: string) => void;
  pce?: PceEvidence;
  transport: Transport;
  query: Query;
  setQuery: (q: Query, version?: string | null) => void;
  route: Route;
  catalog: Catalog | null;
  generation: number;
}) {
  const isPce =
    route.fields.get("asset") === "mdf.macro.bea.pce.headline-core" ||
    route.fields.get("operation") === PCE_OPERATION;
  const [draft, setDraft] = useState(query);
  const [mode, setMode] = useState<ReadMode>(
    route.fields.has("version") ? "exact_version" : "as_of",
  );
  const [version, setVersion] = useState(
    route.fields.get("version") ??
      pce?.reading?.version_identity ??
      catalog?.service?.version_identity ??
      "",
  );
  const [pceResult, setPceResult] = useState<PceRead | null>(null);
  const [result, setResult] = useState<ReadResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const submitted = useRef<string | null>(null);
  const scopeKey = (q: Query, v: string | null) =>
    JSON.stringify([
      route.fields.get("asset"),
      route.fields.get("asset_revision"),
      route.fields.get("operation"),
      q.reference,
      beijingInstantKey(q.as_of),
      v,
    ]);
  const scope = scopeKey(query, route.fields.get("version"));
  const clear = () => {
    abort.current?.abort();
    setBusy(false);
    setResult(null);
    setPceResult(null);
    setError(null);
  };
  useLayoutEffect(() => {
    if (submitted.current !== scope) clear();
  }, [scope]);
  useLayoutEffect(() => {
    submitted.current = null;
    clear();
  }, [generation]);
  useEffect(() => () => abort.current?.abort(), []);
  useEffect(() => {
    setDraft(query);
    const named = route.fields.get("version");
    if (named) {
      setMode("exact_version");
      setVersion(named);
    }
  }, [query.reference, query.as_of, route.fields.get("version")]);
  useEffect(() => {
    if (!route.fields.has("version") && !version)
      setVersion(
        pce?.reading?.version_identity ??
          catalog?.service?.version_identity ??
          "",
      );
  }, [pce?.reading?.version_identity, catalog?.service?.version_identity]);
  const valid =
    /^\d{4}-(0[1-9]|1[0-2])$/.test(draft.reference ?? "") &&
    /(?:Z|[+-]\d{2}:\d{2})$/.test(draft.as_of ?? "") &&
    !!toBeijingInput(draft.as_of ?? "");
  const exact = mode === "exact_version";
  const exactAdmitted =
    /^[a-zA-Z0-9:._-]{1,256}$/.test(version) &&
    (!!pce?.assets.versions.some((v) => v.version_identity === version) ||
      !!catalog?.asset?.versions.some((v) => v.identity === version) ||
      (!route.fields.has("operation") &&
        route.fields.get("version") === version &&
        route.fields.get("reference") === query.reference &&
        beijingInstantKey(route.fields.get("as_of")) ===
          beijingInstantKey(query.as_of)));
  const canRead =
    valid &&
    (exact ? exactAdmitted : !!(pce?.reading || catalog?.service)) &&
    (!isPce || !!pce);
  const versions = isPce
    ? (pce?.assets.versions.map((v) => ({
        identity: v.version_identity,
        at:
          v.publications.find((p) => p.decision === "PUBLISH")?.usable_at ??
          v.publications[0]?.usable_at,
      })) ?? [])
    : (catalog?.asset?.versions.map((v) => ({
        identity: v.identity,
        at:
          v.publications.find((p) => p.decision === "PUBLISH")?.usable_at ??
          v.publications[0]?.usable_at,
      })) ?? []);
  async function read() {
    if (!canRead || busy) return;
    clear();
    const controller = new AbortController();
    abort.current = controller;
    setBusy(true);
    // Keep fractional precision from legacy links while changing only the offset.
    const requested = {
      ...draft,
      as_of: toBeijingIso(draft.as_of!),
    };
    submitted.current = scopeKey(requested, exact ? version : null);
    setQuery(requested, exact ? version : null);
    try {
      if (isPce) {
        const value = await readPce(
          transport,
          requested.reference!,
          requested.as_of!,
          exact ? version : undefined,
          controller.signal,
        );
        if (
          !pce?.assets.versions.some(
            (v) => v.version_identity === value.version_identity,
          )
        )
          throw new Error("pce_version_mismatch");
        if (!controller.signal.aborted) setPceResult(value);
      } else {
        const value = await request(
          transport,
          "/api/r5/cpi/read",
          {
            ...requested,
            mode,
            ...(exact ? { version_identity: version } : {}),
          },
          controller.signal,
        );
        if (
          route.fields.has("operation") &&
          (!catalog?.asset?.versions.some(
            (v) => v.identity === value.version_identity,
          ) ||
            value.asset_identity !== catalog.definition.identity ||
            value.definition_revision !== catalog.definition.revision)
        )
          throw new Error("operation_version_mismatch");
        if (!controller.signal.aborted) setResult(value);
      }
    } catch (e) {
      if (!controller.signal.aborted) setError(failureName(e));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  const beijingValue = toBeijingInput(draft.as_of ?? "");
  return (
    <section aria-label="本地读取" className="reader">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (!event.currentTarget.checkValidity()) return;
          void read();
        }}
      >
        <DateField
          label="参考月"
          type="month"
          value={draft.reference ?? ""}
          onChange={(value) => {
            clear();
            setDraft({ ...draft, reference: value });
          }}
        />
        <details className="query-history-options" open={exact || undefined}>
          <summary>历史查询选项</summary>
          <p className="small">
            可回看某一时刻系统已可用的数据。此时间不是数据所属月份；保留默认值即可查询当前已保存的数据。
          </p>
          <DateField
            label="数据可用截至时间（北京时间）"
            type="datetime-local"
            step="1"
            value={beijingValue}
            onChange={(value) => {
              clear();
              const v = value;
              setDraft({
                ...draft,
                as_of: beijingInputToIso(v),
              });
            }}
          />
          <label>
            读取方式
            <select
              value={mode}
              onChange={(e) => {
                clear();
                setMode(e.target.value as ReadMode);
              }}
            >
              <option value="as_of">按可用时间查询</option>
              <option value="exact_version">指定已保存版本</option>
            </select>
          </label>
          {exact && (
            <label>
              保存版本
              <select
                value={version}
                onChange={(e) => {
                  clear();
                  setVersion(e.target.value);
                }}
              >
                <option value="">选择保存版本</option>
                {version && !versions.some((v) => v.identity === version) && (
                  <option value={version}>链接指定的保存版本</option>
                )}
                {versions.map((v, i) => (
                  <option key={v.identity} value={v.identity}>
                    版本 {i + 1}
                    {v.at ? ` · ${formatBeijingTime(v.at)}` : ""}
                  </option>
                ))}
              </select>
            </label>
          )}
        </details>
        <button type="submit" className="primary" disabled={!canRead || busy}>
          {busy ? "正在查询…" : "读取本地数据"}
        </button>
      </form>
      {!canRead && (
        <p className="small">
          {!valid
            ? "请填写有效月份与时间。"
            : exact
              ? "请选择需要查询的保存版本。"
              : "当前条件下尚无可确认可读的本地数据，请核对月份与历史时点。"}
        </p>
      )}
      {error && (
        <div role="alert">
          <p>
            {error === "pce_reference_mismatch"
              ? "当前 PCE 仅保存了 2026-07 的数据，请选择该月份。"
              : "本次查询未完成；请核对月份、历史时点或保存版本。"}
          </p>
          <p className="small">
            错误代码：<span>{error}</span>
          </p>
        </div>
      )}
      {pceResult && (
        <section aria-label="读取结果">
          <p role="status">读取成功 · 本地读取（开发测试）</p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>指标</th>
                  <th>数值</th>
                  <th>单位</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(pceLabels).map(([id, label]) => (
                  <tr key={id}>
                    <td>{label}</td>
                    <td>{pceResult.measurements[id].value}</td>
                    <td>%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <details className="provenance">
            <summary>数据来源与版本</summary>
            <dl>
              <Fact name="数据提供方">BEA</Fact>
              <Fact name="保存版本">{pceResult.version_identity}</Fact>
              <Fact name="发布记录">{pceResult.publication_identity}</Fact>
            </dl>
            <p>本地数据可读取；BEA 来源查询尚未完成验证。</p>
          </details>
        </section>
      )}
      {result && (
        <section aria-label="读取结果">
          <p role="status">
            读取成功 ·{" "}
            {result.evidence_mode === "OFFLINE_FIXTURE"
              ? "离线合成 fixture 验证"
              : "本地读取"}
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>指标</th>
                  <th>数值</th>
                  <th>单位</th>
                </tr>
              </thead>
              <tbody>
                {result.measurements.map((m) => (
                  <tr key={m.measure_identity}>
                    <td>{m.name}</td>
                    <td>{m.value ?? "未提供"}</td>
                    <td>{m.unit}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <details className="provenance">
            <summary>数据来源与版本</summary>
            <dl>
              <Fact name="数据提供方">BLS</Fact>
              <Fact name="保存版本">{result.version_identity}</Fact>
              <Fact name="发布记录">{result.publication_identity}</Fact>
            </dl>
          </details>
        </section>
      )}
    </section>
  );
}
