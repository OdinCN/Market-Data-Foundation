import { FINANCIAL } from "./FinancialDailyWorkbench";
import { macroAssetLabel } from "./macroTimeSeriesProfile";
import { CPI_PROFILE, type MacroProfile } from "./macroTimeSeriesProfile";
import { useState, type ReactNode } from "react";
import type { Packages, RuntimeRevision } from "./packageApi";
import type { Navigate, Route } from "./workflowRoute";
import { Fact, WorkflowTable } from "./WorkflowTable";
import { assetTitle, sameKey } from "./workflowModel";
const connected = (value?: RuntimeRevision["plan"]) =>
  value?.allowed === true
    ? "已接入"
    : value?.allowed === false
      ? "未接入"
      : "待核实";
const eligible = (value: RuntimeRevision["local_read"]) =>
  value.allowed === true
    ? "允许"
    : value.allowed === false
      ? "不可用"
      : "待核实";
export function RuntimeAssets({
  data,
  busy,
  failure,
  route,
  navigate,
  reader,
  ordinary,
  reference,
  cpi,
  cpiAction,
  financial,
  family,
  profile = CPI_PROFILE,
}: {
  data: Packages | null;
  busy: boolean;
  failure: string | null;
  route: Route;
  navigate: Navigate;
  reader: ReactNode;
  ordinary: ReactNode;
  reference: ReactNode;
  cpi?: ReactNode;
  cpiAction?: ReactNode;
  financial?: ReactNode;
  family?: { title: string; content: ReactNode };
  profile?: MacroProfile;
}) {
  const [expanded, setExpanded] = useState(false);
  const filter = route.fields.get("package");
  const packages =
    data?.packages.filter((p) => !filter || p.package_id === filter) ?? [];
  const rows = [
    ...new Set(packages.flatMap((p) => p.assets.map((a) => a.identity))),
  ].map((identity) => {
    const owners = packages.filter((p) =>
      p.assets.some((a) => a.identity === identity),
    );
    const revisions = owners.flatMap((p) =>
      p.assets
        .filter((a) => a.identity === identity)
        .flatMap((a) => a.revisions),
    );
    const title =
      owners
        .flatMap((p) => p.capabilities)
        .find((c) => c.definition_ref.identity === identity)?.title ?? identity;
    return {
      identity,
      owners,
      revisions,
      title: assetTitle({ identity, revision: "" }, title),
    };
  });
  const selected = rows.find((r) => r.identity === route.fields.get("asset"));
  const cpiSelected =
    !!cpi &&
    route.fields.get("asset") === profile.asset.identity &&
    (!route.fields.has("asset_revision") ||
      route.fields.get("asset_revision") === profile.asset.revision) &&
    (!filter ||
      packages.some(
        (p) =>
          p.capabilities.some((c) =>
            sameKey(c.definition_ref, profile.asset),
          ) ||
          p.assets.some((a) =>
            a.revisions.some((r) => sameKey(r.definition_ref, profile.asset)),
          ),
      ));
  const financialSelected =
    !!financial && route.fields.get("asset") === FINANCIAL.identity;
  const revision = route.fields.has("asset_revision")
    ? selected?.revisions.find(
        (r) => r.definition_ref.revision === route.fields.get("asset_revision"),
      )
    : selected?.revisions.length === 1
      ? selected.revisions[0]
      : undefined;
  const providers = [
    ...new Set(
      revision?.source_entries
        .filter(
          (source) =>
            source.source_contract &&
            source.binding_contract &&
            source.code_delivery === "DELIVERED",
        )
        .map((source) => source.source_name) ?? [],
    ),
  ];
  const unknown = packages.some((p) => p.history_completeness !== "COMPLETE");
  return (
    <>
      <div className="toolbar asset-toolbar">
        <h2>数据资产目录</h2>
        <div className="asset-filter">
          <label>
            接入包筛选
            <select
              aria-label="接入包筛选"
              value={filter ?? "all"}
              onChange={(e) =>
                navigate(
                  "assets",
                  {
                    package: e.target.value === "all" ? null : e.target.value,
                    asset: null,
                    asset_revision: null,
                    operation: null,
                    version: null,
                    tab: null,
                  },
                  true,
                )
              }
            >
              <option value="all">全部接入包</option>
              {data?.packages.map((p) => (
                <option key={p.package_id} value={p.package_id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          {filter && (
            <button onClick={() => navigate("assets", { package: null }, true)}>
              清除包筛选
            </button>
          )}
        </div>
      </div>
      {busy && <p role="status">正在读取运行资产目录…</p>}
      {failure && (
        <div role="alert">
          数据资产读取失败，请刷新重试。当前列表不能证明没有已保存的数据。
        </div>
      )}
      {data && filter && !packages.length && (
        <p role="alert">此接入包不在当前可见目录中。</p>
      )}
      {data && (
        <>
          {unknown && (
            <p role="status" className="asset-notice">
              部分接入包的本地数据尚未核实；这里仅显示已确认的资产。
            </p>
          )}
          <WorkflowTable
            className={
              (cpiSelected || financialSelected || !!family) && expanded
                ? "cpi-asset-expanded"
                : undefined
            }
            label="数据资产"
            columns={["数据资产", "接入包", "可见原因"]}
            empty={
              unknown
                ? "当前没有可确认的目录条目；历史持有仍待核实。"
                : "当前筛选下没有可见资产。"
            }
            rows={rows.map((r) => ({
              id: r.identity,
              search: `${r.title} ${r.owners.map((p) => p.name).join(" ")}`,
              cells: [
                r.title,
                r.owners.map((p) => p.name).join(" / "),
                r.revisions.some((v) => v.capability_enabled)
                  ? "能力已启用"
                  : r.revisions.some((v) =>
                        v.visibility_basis?.includes("RETAINED_HISTORY"),
                      )
                    ? "保留历史"
                    : r.revisions.some((v) =>
                          v.visibility_basis?.includes("REGISTERED_MANUAL"),
                        )
                      ? "已登记人工通路"
                      : "可见依据待核实",
              ],
            }))}
            selected={selected?.identity}
            onSelect={(identity) =>
              navigate(
                "assets",
                {
                  asset: identity,
                  asset_revision:
                    rows.find((r) => r.identity === identity)?.revisions
                      .length === 1
                      ? rows.find((r) => r.identity === identity)!.revisions[0]
                          .definition_ref.revision
                      : null,
                  operation: null,
                  version: null,
                  tab: null,
                  // Legacy navigation seeds a monthly reference/as_of globally;
                  // selecting the daily asset is an ordinary query, not history.
                  ...(identity === FINANCIAL.identity
                    ? { reference: null, as_of: null }
                    : {}),
                },
                true,
              )
            }
          >
            {selected && (
              <>
                <div className="toolbar cpi-asset-title">
                  <h3>{selected.title}</h3>
                  {(cpiSelected || financialSelected || !!family) && (
                    <>
                      {cpiAction}
                      <button
                        aria-expanded={expanded}
                        onClick={() => setExpanded(!expanded)}
                      >
                        {expanded ? "收起窗体" : "展开窗体"}
                      </button>
                    </>
                  )}
                </div>
                {family?.content}
                {cpiSelected && cpi}
                {financialSelected && (
                  <>
                    <p className="small">
                      可用能力：人工采集{connected(revision?.collect)}；周期采集
                      {connected(revision?.plan)}
                      。能力不代表策略已开启或执行器在线，实际自动采集状态见周期任务。
                    </p>
                    {financial}
                  </>
                )}
                {!family &&
                  !cpiSelected &&
                  !financialSelected &&
                  selected.revisions.length > 1 && (
                    <label>
                      数据定义
                      <select
                        aria-label="数据定义"
                        value={revision?.definition_ref.revision ?? ""}
                        onChange={(e) =>
                          navigate(
                            "assets",
                            {
                              asset_revision: e.target.value || null,
                              operation: null,
                              version: null,
                              tab: null,
                            },
                            true,
                          )
                        }
                      >
                        <option value="">请选择数据定义</option>
                        {[
                          ...new Set(
                            selected.revisions.map(
                              (r) => r.definition_ref.revision,
                            ),
                          ),
                        ].map((r) => (
                          <option key={r} value={r}>
                            {r}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                {!family && !cpiSelected && !financialSelected && revision && (
                  <>
                    <dl>
                      <Fact name="能力状态">
                        {revision.capability_enabled
                          ? "已启用"
                          : "已停用；保留历史单独判断"}
                      </Fact>
                      <Fact name="目录权限">
                        {revision.metadata_view_permission === "allowed"
                          ? "允许查看历史目录"
                          : revision.metadata_view_permission === "denied"
                            ? "历史目录查看被拒绝，不能据此推断持有"
                            : "历史目录权限待核实，不能据此推断持有"}
                      </Fact>
                      <Fact name="本地持有">
                        {revision.local_holding === "PRESENT"
                          ? "明确持有"
                          : revision.local_holding === "ABSENT"
                            ? "明确无持有"
                            : "未知"}
                      </Fact>
                      <Fact name="内容读取">
                        {eligible(revision.local_read)}
                      </Fact>
                      <Fact name="上游读取资格">
                        {eligible(revision.upstream_read)}
                      </Fact>
                      <Fact name="采集资格">{eligible(revision.collect)}</Fact>
                    </dl>
                    <p className="muted">
                      数据提供方：{providers.join(" / ") || "尚未接入"}
                    </p>
                    {route.fields.has("operation") && (
                      <button
                        onClick={() =>
                          navigate(
                            "assets",
                            { operation: null, version: null },
                            true,
                          )
                        }
                      >
                        使用资产默认查询
                      </button>
                    )}
                    {ordinary}
                    <button
                      onClick={() =>
                        navigate("collection", {
                          asset: revision.definition_ref.identity,
                          asset_revision: revision.definition_ref.revision,
                          operation: route.fields.get("operation"),
                          tab: "executions",
                        })
                      }
                    >
                      查看采集任务 →
                    </button>
                    {revision.local_read.allowed === true ? (
                      <details
                        className="historical-read"
                        open={
                          !ordinary ||
                          route.fields.has("version") ||
                          route.fields.get("tab") === "local" ||
                          route.fields.has("as_of")
                        }
                      >
                        <summary>
                          {ordinary ? "查询已保存的历史数据" : "查询本地数据"}
                        </summary>
                        {reader || <p>该资产的本地读取页面尚未适配。</p>}
                      </details>
                    ) : (
                      <p role="status">
                        {revision.local_read.allowed === false
                          ? "当前内容读取不可用。"
                          : "当前内容读取资格尚未确认。"}
                        持有历史不等于允许读取内容。
                      </p>
                    )}
                    {revision.local_read.allowed === null &&
                      (route.fields.get("tab") === "local" ||
                        route.fields.has("version")) &&
                      reader && (
                        <section aria-label="独立本地读取">
                          <p>
                            目录汇总的读取资格尚未核实；此本地读取入口将独立核验权限、版本与时点。
                          </p>
                          {reader}
                        </section>
                      )}
                  </>
                )}
              </>
            )}
          </WorkflowTable>
        </>
      )}
      {financialSelected && !selected && !busy && (data || failure) && (
        <section aria-label="日频登记资产读取">
          <h3>金融条件日频（十项）</h3>
          <p>当前目录持有尚未确认；本地查询将独立核验，未据此声称已有数据。</p>
          {financial}
        </section>
      )}
      {cpiSelected && !selected && !busy && (data || failure) && (
        <section>
          <div className="toolbar cpi-asset-title">
            <h3>{macroAssetLabel(profile)}</h3>
            {cpiAction}
          </div>
          {cpi}
        </section>
      )}
      {!family &&
        !cpiSelected &&
        !financialSelected &&
        !revision &&
        (!filter ||
          packages.some((p) =>
            p.capabilities.some(
              (c) => c.definition_ref.identity === route.fields.get("asset"),
            ),
          )) &&
        route.fields.has("asset") &&
        route.fields.has("asset_revision") &&
        reference && (
          <section aria-label="独立本地读取">
            <h3>已有合同与本地版本入口</h3>
            <p>
              此入口保留已有合同和本地读取深链；本地读取仍由原 Reader 核验。
              它不证明该资产已出现在当前运行目录或接入包已配置。
            </p>
            {reference}
          </section>
        )}
    </>
  );
}
