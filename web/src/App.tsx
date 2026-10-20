import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiFailure,
  request,
  type Catalog,
  type Directory,
  type Query,
  type ReadMode,
  type ReadResult,
  type Transport,
} from "./api";
import { SourceDirectory } from "./SourceDirectory";
import "./style.css";

const menus = [
  "运行总览",
  "接入管理",
  "接口注册",
  "数据资产",
  "采集任务",
  "数据服务",
  "平台管理",
];
const paths = [
  "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  "M4 9h16v6H4z M8 5v4 M16 15v4 M5 19h14",
  "M8 6 3 12l5 6 M16 6l5 6-5 6 M14 3l-4 18",
  "M12 3 21 8l-9 5-9-5 9-5z M3 13l9 5 9-5 M3 18l9 4 9-4",
  "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z M12 7v5l3 2",
  "M13 2l-3 9h7l-6 11 1-9H5l8-11z",
  "M12 9a3 3 0 1 1 0 6 3 3 0 0 1 0-6z M12 2v3m0 14v3M22 12h-3M5 12H2",
];
const activePages = [1, 2, 3, 5];
const readLabels: Record<string, string> = {
  core_mom: "核心 CPI 环比",
  core_yoy: "核心 CPI 同比",
  headline_mom: "CPI 环比",
  headline_yoy: "CPI 同比",
};
const defaultQuery = (): Query => ({
  reference: new Date().toISOString().slice(0, 7),
  as_of: new Date().toISOString(),
});
const errorName = (error: unknown) =>
  error instanceof ApiFailure ? error.classification : "request_failed";
function route() {
  const hash = new URLSearchParams(location.hash.slice(1));
  const page = Number(hash.get("page") ?? 2);
  return {
    page: Number.isInteger(page) && page >= 0 && page < 7 ? page : 2,
    identity: hash.get("asset"),
    revision: hash.get("asset_revision"),
    operation: hash.get("operation"),
  };
}
function Chip({
  children,
  good = false,
}: {
  children: React.ReactNode;
  good?: boolean;
}) {
  return <span className={`chip ${good ? "good" : ""}`}>{children}</span>;
}

export default function App({
  transport = fetch,
  initialQuery,
}: {
  transport?: Transport;
  initialQuery?: Query;
}) {
  const [page, setPage] = useState(route);
  const [collapsed, setCollapsed] = useState(false);
  const [dark, setDark] = useState(false);
  const [draft, setDraft] = useState<Query>(initialQuery ?? defaultQuery());
  const [query, setQuery] = useState(draft);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [directory, setDirectory] = useState<Directory | null>(null);
  const [directoryFailure, setDirectoryFailure] = useState<string | null>(null);
  const [directoryBusy, setDirectoryBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [poll, setPoll] = useState(false);
  const [generation, setGeneration] = useState(0);
  const [mode, setMode] = useState<ReadMode>("as_of");
  const [version, setVersion] = useState("");
  const [preview, setPreview] = useState<ReadResult | null>(null);
  const [readFailure, setReadFailure] = useState<{
    status: string;
    classification: string;
  } | null>(null);
  const [reading, setReading] = useState(false);
  const readAbort = useRef<AbortController | null>(null);
  const mountedQuery = useRef(0);
  const refresh = useCallback(() => setGeneration((value) => value + 1), []);

  useEffect(() => {
    const onHash = () => setPage(route());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  useEffect(() => {
    readAbort.current?.abort();
    setReading(false);
    setPreview(null);
    setReadFailure(null);
  }, [page.page, page.identity, page.revision, page.operation]);
  useEffect(() => {
    const controller = new AbortController();
    setDirectory(null);
    setDirectoryFailure(null);
    setDirectoryBusy(true);
    request(transport, "/api/r5/sources", {}, controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setDirectory(data);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setDirectoryFailure(errorName(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setDirectoryBusy(false);
      });
    return () => controller.abort();
  }, [transport, generation]);
  useEffect(() => {
    if (!poll) return;
    const timer = window.setInterval(refresh, 30000);
    return () => window.clearInterval(timer);
  }, [poll, refresh]);
  useEffect(() => {
    const controller = new AbortController();
    mountedQuery.current += 1;
    readAbort.current?.abort();
    setReading(false);
    setPreview(null);
    setReadFailure(null);
    setCatalog(null);
    setBusy(true);
    setFailure(null);
    request(transport, "/api/r5/cpi", query, controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setCatalog(data);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setFailure(errorName(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => {
      controller.abort();
      readAbort.current?.abort();
    };
  }, [transport, query, generation]);

  function navigate(
    next: number,
    identity = page.identity ??
      (page.operation ? null : (catalog?.definition.identity ?? null)),
    operation = page.operation,
    revision = page.revision,
  ) {
    readAbort.current?.abort();
    setReading(false);
    setPreview(null);
    setReadFailure(null);
    const hash = new URLSearchParams({ page: String(next) });
    if (identity) hash.set("asset", identity);
    if (operation) hash.set("operation", operation);
    if (revision) hash.set("asset_revision", revision);
    location.hash = hash.toString();
    setPage({ page: next, identity, operation, revision });
  }
  async function read() {
    readAbort.current?.abort();
    const controller = new AbortController();
    readAbort.current = controller;
    const epoch = mountedQuery.current;
    setReading(true);
    setPreview(null);
    setReadFailure(null);
    try {
      const data = await request(
        transport,
        "/api/r5/cpi/read",
        {
          ...query,
          mode,
          ...(mode === "exact_version" ? { version_identity: version } : {}),
        },
        controller.signal,
      );
      if (!controller.signal.aborted && epoch === mountedQuery.current)
        setPreview(data);
    } catch (error) {
      if (!controller.signal.aborted)
        setReadFailure({
          classification: errorName(error),
          status: error instanceof ApiFailure ? error.status : "UNKNOWN",
        });
    } finally {
      if (!controller.signal.aborted) setReading(false);
    }
  }
  function changeRead() {
    readAbort.current?.abort();
    setReading(false);
    setPreview(null);
    setReadFailure(null);
  }

  const selected = directory?.entries.find((entry) =>
    page.operation
      ? entry.identity === page.operation &&
        (page.identity === null || entry.asset?.identity === page.identity) &&
        (page.revision === null || entry.asset?.revision === page.revision)
      : page.identity !== catalog?.definition.identity &&
        entry.asset?.identity === page.identity &&
        (page.revision === null || entry.asset?.revision === page.revision),
  );
  const missingOperation = Boolean(directory && page.operation && !selected);
  const showCpi =
    page.page !== 1 &&
    !missingOperation &&
    ((!page.operation && !selected) ||
      selected?.web_read === "CPI_REQUEST_QUALIFIED");
  const matches =
    !missingOperation &&
    catalog &&
    (!page.identity || page.identity === catalog.definition.identity) &&
    (!page.revision || page.revision === catalog.definition.revision);
  const implemented = activePages.includes(page.page);
  const canRead = Boolean(
    (page.page === 3 || page.page === 5) &&
      showCpi &&
      matches &&
      catalog?.asset &&
      (page.page === 3 || catalog?.service),
  );
  return (
    <div
      className={`shell ${dark ? "dark" : ""} ${collapsed ? "collapsed" : ""}`}
    >
      <aside>
        <a className="brand" href="#page=2" aria-label="Market Data Foundation">
          <img src="/mdf-data-hub.png" alt="" />
          {!collapsed && (
            <span>
              Market Data
              <br />
              Foundation
            </span>
          )}
        </a>
        <nav aria-label="一级导航">
          {menus.map((name, index) => (
            <button
              type="button"
              key={name}
              title={name}
              aria-label={name}
              aria-current={page.page === index ? "page" : undefined}
              onClick={() => navigate(index)}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d={paths[index]} />
              </svg>
              {!collapsed && (
                <>
                  <span>{name}</span>
                  <small>{String(index + 1).padStart(2, "0")}</small>
                </>
              )}
            </button>
          ))}
        </nav>
        <button
          type="button"
          className="collapse"
          onClick={() => setCollapsed(!collapsed)}
          aria-label={collapsed ? "展开侧栏" : "收起侧栏"}
        >
          {collapsed ? "»" : "« 收起侧栏"}
        </button>
      </aside>
      <div className="workspace">
        <header>
          <span>
            Market Data Foundation{" "}
            <span className="muted"> / {menus[page.page]}</span>
          </span>
          <div className="actions">
            <Chip>只读</Chip>
            <button
              type="button"
              onClick={() => setDark(!dark)}
              aria-label="切换主题"
            >
              {dark ? "浅色" : "深色"}
            </button>
            <button
              type="button"
              onClick={refresh}
              disabled={busy || directoryBusy}
            >
              刷新
            </button>
          </div>
        </header>
        <main>
          <div className="heading">
            <div>
              <p className="eyebrow">MARKET DATA FOUNDATION</p>
              <h1>{menus[page.page]}</h1>
              <p className="muted">
                {implemented
                  ? "具名来源合同目录 · Definition / Version / Publication · CPI 本地读取"
                  : "设计入口已保留，业务能力尚未实现"}
              </p>
            </div>
            <Chip>{implemented ? "具名只读目录" : "未实现"}</Chip>
          </div>
          {!implemented ? (
            <section className="panel placeholder">
              <h2>本模块尚未实现</h2>
              {page.page === 4 && (
                <div className="tabs">新建采集 / 周期任务 / 执行记录</div>
              )}
              {page.page === 6 && (
                <div className="tabs">运行实例 / 数据库与存储 / 日志与诊断</div>
              )}
              <p>运行状态 UNKNOWN。当前开放来源合同目录与 CPI 本地只读流程。</p>
            </section>
          ) : (
            <>
              <SourceDirectory
                directory={directory}
                failure={directoryFailure}
                busy={directoryBusy}
                page={page.page}
                selected={selected}
                missing={missingOperation}
                onSelect={(entry) =>
                  navigate(
                    page.page,
                    entry.asset?.identity ?? null,
                    entry.identity,
                    entry.asset?.revision ?? null,
                  )
                }
                onNavigate={(next, entry) =>
                  navigate(
                    next,
                    entry.asset?.identity ?? null,
                    entry.identity,
                    entry.asset?.revision ?? null,
                  )
                }
              />
              {page.operation && (
                <button
                  type="button"
                  onClick={() =>
                    navigate(
                      page.page,
                      catalog?.definition.identity ?? null,
                      null,
                      catalog?.definition.revision ?? null,
                    )
                  }
                >
                  {page.page === 1 ? "清除目录选择" : "返回 CPI 本地读取"}
                </button>
              )}
              {showCpi && (
                <>
                  {selected && (
                    <p className="notice">
                      以下为 CPI 资产的独立请求级核验，Version
                      血缘为准；不证明上方来源当前绑定。
                    </p>
                  )}
                  <section className="panel filters">
                    <form
                      onSubmit={(event) => {
                        event.preventDefault();
                        setQuery({ ...draft });
                      }}
                    >
                      <label>
                        报告月份
                        <input
                          aria-label="报告月份"
                          type="month"
                          required
                          value={draft.reference}
                          onChange={(e) =>
                            setDraft({ ...draft, reference: e.target.value })
                          }
                        />
                      </label>
                      <label className="instant">
                        读取时点（含时区）
                        <input
                          aria-label="读取时点"
                          required
                          value={draft.as_of}
                          onChange={(e) =>
                            setDraft({ ...draft, as_of: e.target.value })
                          }
                        />
                      </label>
                      <button type="submit" className="primary">
                        应用读取条件
                      </button>
                    </form>
                    <label className="poll">
                      <input
                        type="checkbox"
                        checked={poll}
                        onChange={(e) => setPoll(e.target.checked)}
                      />
                      自动刷新（30秒）
                    </label>
                  </section>
                  <div className="notice" role="status">
                    {busy
                      ? "正在核实本地只读证据…"
                      : failure
                        ? `UNKNOWN · ${failure}`
                        : catalog?.evidence_mode === "OFFLINE_FIXTURE"
                          ? "离线合成 fixture · 仅用于验证读取流程，不代表当前持有"
                          : catalog?.evidence_mode === "UNCONFIGURED"
                            ? "UNKNOWN · 未配置当前只读数据能力"
                            : "已授权只读范围 · 状态仅对本次读取条件有效"}
                  </div>
                  {catalog && (
                    <>
                      <section className="panel">
                        <div className="panel-title">
                          <h2>
                            {page.page === 2
                              ? "API / 查询接口"
                              : page.page === 3
                                ? "逻辑资产目录"
                                : "统一读取服务"}
                          </h2>
                          <Chip good={catalog.status === "OK"}>
                            {catalog.status}
                          </Chip>
                        </div>
                        {page.page === 2 && (
                          <p className="muted">
                            实时 / Socket / WS 尚未实现。登记合同不等于当前
                            Source 激活或持有。
                          </p>
                        )}
                        {!matches ? (
                          <p role="alert">
                            关联对象缺失：未找到请求的具名资产。
                          </p>
                        ) : page.page === 3 && !catalog.asset ? (
                          <p>关联资产不可核实 · {catalog.classification}</p>
                        ) : page.page === 5 && !catalog.service ? (
                          <p>
                            没有满足当前条件的数据服务 ·{" "}
                            {catalog.classification}
                          </p>
                        ) : (
                          <div className="table-wrap">
                            <table>
                              <thead>
                                <tr>
                                  <th>名称</th>
                                  <th>稳定标识</th>
                                  <th>
                                    {page.page === 2
                                      ? "合同登记"
                                      : "当前条件资格"}
                                  </th>
                                </tr>
                              </thead>
                              <tbody>
                                <tr className="selected">
                                  <td>{catalog.definition.title}</td>
                                  <td>
                                    <code>
                                      {page.page === 5
                                        ? catalog.service?.identity
                                        : catalog.definition.identity}
                                    </code>
                                  </td>
                                  <td>
                                    <Chip good={catalog.status === "OK"}>
                                      {page.page === 2
                                        ? "CANONICAL_CONTRACT"
                                        : catalog.asset?.local_holding}
                                    </Chip>
                                  </td>
                                </tr>
                              </tbody>
                            </table>
                          </div>
                        )}
                      </section>
                      {matches && (
                        <div className="detail-grid">
                          <section className="panel">
                            <div className="panel-title">
                              <h2>CPI 详情</h2>
                              <span className="muted">
                                {catalog.definition.revision}
                              </span>
                            </div>
                            <dl>
                              <dt>资产标识</dt>
                              <dd data-testid="asset-identity">
                                <code>{catalog.definition.identity}</code>
                              </dd>
                              <dt>报告月份</dt>
                              <dd>{catalog.reference}</dd>
                              <dt>读取时点</dt>
                              <dd>{catalog.as_of}</dd>
                              <dt>Source 健康 / Binding 激活</dt>
                              <dd>UNKNOWN / UNKNOWN</dd>
                              <dt>本地持有</dt>
                              <dd>
                                {catalog.asset?.local_holding ?? "UNKNOWN"}
                              </dd>
                              <dt>读取分类</dt>
                              <dd>
                                {catalog.classification ?? "当前条件已核验"}
                              </dd>
                            </dl>
                            <div className="actions">
                              {page.page === 2 && (
                                <button
                                  type="button"
                                  disabled={!catalog.asset}
                                  onClick={() =>
                                    navigate(3, catalog.asset!.identity)
                                  }
                                >
                                  查看关联资产
                                </button>
                              )}
                              {page.page === 3 && (
                                <button
                                  type="button"
                                  disabled={!catalog.service}
                                  onClick={() =>
                                    navigate(5, catalog.service!.asset_identity)
                                  }
                                >
                                  查看数据服务
                                </button>
                              )}
                              {page.page === 5 && (
                                <button
                                  type="button"
                                  onClick={() =>
                                    navigate(3, catalog.service?.asset_identity)
                                  }
                                >
                                  查看关联资产
                                </button>
                              )}
                            </div>
                            {page.page === 2 && !catalog.asset && (
                              <p className="muted">
                                当前证据不足，无法定位关联资产。
                              </p>
                            )}
                            {page.page === 3 && !catalog.service && (
                              <p className="muted">
                                未满足当前读取资格，数据服务不可用。
                              </p>
                            )}
                          </section>
                          {page.page !== 2 && (
                            <section className="panel">
                              <h2>Version / Publication</h2>
                              {catalog.asset?.versions.length ? (
                                catalog.asset.versions.map((item) => (
                                  <div className="version" key={item.identity}>
                                    <code>{item.identity}</code>
                                    <p>
                                      <Chip>{item.selection_state}</Chip>
                                    </p>
                                    <p className="muted">
                                      冻结：{item.frozen_at}
                                    </p>
                                    <p className="muted">
                                      覆盖：{item.completeness}
                                    </p>
                                    <p className="muted">
                                      版本血缘 Binding：
                                      {item.binding_identity ?? "UNKNOWN"}{" "}
                                      {item.binding_revision}
                                    </p>
                                    {item.publications.map((pub) => (
                                      <div key={pub.identity}>
                                        <code>{pub.identity}</code> ·{" "}
                                        {pub.decision}
                                        <p className="muted">
                                          决策：{pub.decision_at}
                                          <br />
                                          可用：{pub.usable_at}
                                          {pub.effective_at && (
                                            <>
                                              <br />
                                              生效：{pub.effective_at}
                                            </>
                                          )}
                                        </p>
                                        {pub.pending_clocks.length > 0 && (
                                          <p>
                                            尚未满足时钟：
                                            {pub.pending_clocks.join(", ")}
                                          </p>
                                        )}
                                      </div>
                                    ))}
                                  </div>
                                ))
                              ) : (
                                <p className="muted">没有可核实的版本</p>
                              )}
                            </section>
                          )}
                        </div>
                      )}
                    </>
                  )}
                  {canRead && (
                    <section className="panel">
                      <div className="panel-title">
                        <h2>
                          {page.page === 3 ? "本地数据预览" : "本地服务读取"}
                        </h2>
                        <Chip>whole_version</Chip>
                      </div>
                      <div className="read-controls">
                        <label>
                          读取方式
                          <select
                            aria-label="读取方式"
                            value={mode}
                            onChange={(e) => {
                              changeRead();
                              setMode(e.target.value as ReadMode);
                              setVersion(
                                catalog?.service?.version_identity ??
                                  catalog?.asset?.versions[0]?.identity ??
                                  "",
                              );
                            }}
                          >
                            <option value="as_of">as-of · 按时点</option>
                            <option value="exact_version">
                              exact · 精确版本
                            </option>
                          </select>
                        </label>
                        {mode === "exact_version" && (
                          <label className="instant">
                            版本标识
                            <input
                              aria-label="版本标识"
                              value={version}
                              onChange={(e) => {
                                changeRead();
                                setVersion(e.target.value);
                              }}
                            />
                          </label>
                        )}
                        <button
                          type="button"
                          className="primary"
                          disabled={
                            reading || (mode === "exact_version" && !version)
                          }
                          onClick={read}
                        >
                          {reading ? "读取中…" : "读取本地数据"}
                        </button>
                      </div>
                      <p className="muted">
                        预览和刷新仅核实本地材料，不触发采集。读取使用已应用条件：
                        {query.reference} / {query.as_of}
                      </p>
                      {readFailure && (
                        <p role="alert">
                          {readFailure.status} · {readFailure.classification}
                        </p>
                      )}
                      {preview && (
                        <>
                          <p role="status">
                            读取成功 · {preview.evidence_mode} ·{" "}
                            {preview.content_mode}
                          </p>
                          <p>
                            <code>{preview.version_identity}</code> /{" "}
                            <code>{preview.publication_identity}</code>
                          </p>
                          <div className="table-wrap">
                            <table>
                              <thead>
                                <tr>
                                  <th>指标</th>
                                  <th>系列</th>
                                  <th>报告期 / 比较期</th>
                                  <th>值</th>
                                  <th>单位</th>
                                  <th>可用性</th>
                                </tr>
                              </thead>
                              <tbody>
                                {preview.measurements.map((item) => (
                                  <tr key={item.measure_identity}>
                                    <td>
                                      {readLabels[item.name] ?? item.name}
                                    </td>
                                    <td>
                                      <code>{item.series_id}</code>
                                      <br />
                                      {item.seasonal_adjustment}
                                    </td>
                                    <td>
                                      {item.reference_period} /{" "}
                                      {item.comparison_period}
                                    </td>
                                    <td>{item.value ?? "NOT_AVAILABLE"}</td>
                                    <td>{item.unit}</td>
                                    <td>
                                      {item.availability}
                                      <br />
                                      {item.unavailable_reason}
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                          <p className="muted digest">
                            内容 SHA-256：{preview.content_sha256}
                          </p>
                        </>
                      )}
                    </section>
                  )}
                </>
              )}
            </>
          )}
        </main>
      </div>
    </div>
  );
}
