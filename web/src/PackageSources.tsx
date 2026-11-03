import { useEffect, useRef, useState, type ReactNode } from "react";
import type { Transport } from "./api";
import { formatBeijingTime } from "./beijingTime";
import {
  getPackage,
  putPackage,
  verifyPackageSource,
  PackageRequestFailure,
  type Package,
  type Packages,
  type SavePackage,
} from "./packageApi";
import type { Navigate, Route } from "./workflowRoute";
import { Fact, Tabs, WorkflowTable } from "./WorkflowTable";

// A route change must not release an unresolved PUT and permit another save.
// Retain only nonsecret intent across route changes, never a request body or Key.
type SaveIntent = {
  expected_revision: string | null;
  enabled_capability_ids: string[];
  replaced_sources: string[];
};
type SaveReceipt = {
  intent: SaveIntent;
  done: Promise<void>;
  unknown: boolean;
};
const saves = new WeakMap<Transport, Map<string, SaveReceipt>>();
function receipts(transport: Transport) {
  let map = saves.get(transport);
  if (!map) {
    map = new Map();
    saves.set(transport, map);
  }
  return map;
}

function PackageDialog({
  children,
  title,
  busy,
  onClose,
}: {
  children: ReactNode;
  title: string;
  busy: boolean;
  onClose: () => void;
}) {
  const element = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = element.current!;
    const previous = document.activeElement as HTMLElement | null;
    const nativeModal = typeof dialog.showModal === "function";
    if (nativeModal) dialog.showModal();
    else dialog.setAttribute("open", ""); // jsdom; browsers use the native modal.
    dialog.querySelector<HTMLButtonElement>("button")?.focus();
    return () => {
      if (nativeModal && dialog.open) dialog.close();
      if (previous?.isConnected) previous.focus();
      else
        document
          .querySelector<HTMLButtonElement>("[data-package-action]")
          ?.focus();
    };
  }, []);
  return (
    <dialog
      ref={element}
      className="package-dialog"
      aria-modal="true"
      aria-labelledby="package-dialog-title"
      tabIndex={-1}
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          if (!busy) onClose();
        } else if (event.key === "Tab") {
          const items = [
            ...element.current!.querySelectorAll<HTMLElement>(
              "button, input, select, textarea, a[href], summary, [tabindex]",
            ),
          ].filter((item) => !item.matches(":disabled") && item.tabIndex >= 0);
          const first = items[0],
            last = items.at(-1);
          if (event.shiftKey && document.activeElement === first && last) {
            event.preventDefault();
            last.focus();
          } else if (
            !event.shiftKey &&
            document.activeElement === last &&
            first
          ) {
            event.preventDefault();
            first.focus();
          }
        }
      }}
    >
      <div className="package-dialog-header">
        <h2 id="package-dialog-title">接入包配置 · {title}</h2>
        <button disabled={busy} aria-label="关闭包配置" onClick={onClose}>
          关闭
        </button>
      </div>
      {children}
    </dialog>
  );
}

export function PackageSources({
  data,
  busy,
  failure,
  route,
  navigate,
  transport,
  changed,
}: {
  data: Packages | null;
  busy: boolean;
  failure: string | null;
  route: Route;
  navigate: Navigate;
  transport: Transport;
  changed: () => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const id = route.fields.get("package");
  const selected = data?.packages.find((p) => p.package_id === id);
  return (
    <>
      <h2>数据源接入包</h2>
      {busy && <p role="status">正在读取接入包…</p>}
      {failure && <div role="alert">接入包读取失败，请刷新重试。</div>}
      <WorkflowTable
        label="数据源接入包"
        empty={
          busy
            ? "正在读取接入包…"
            : failure
              ? "当前目录状态未知。"
              : "当前可见目录没有接入包。"
        }
        columns={["接入包", "配置", "启用能力", "所需来源"]}
        rows={(data?.packages ?? []).map((p) => ({
          id: p.package_id,
          search: `${p.name} ${p.domain}`,
          cells: [
            p.name,
            p.configuration_state === "PRESENT" ? "已接入" : "尚未接入",
            `${p.enabled_capability_ids.length} 项`,
            p.required_sources.join(" / ") || "当前无需来源",
          ],
        }))}
        selected={selected?.package_id}
        onSelect={(packageId) =>
          navigate("access", { package: packageId, tab: "capabilities" })
        }
      >
        {selected && (
          <section aria-label="所选接入包">
            <h3>{selected.name}</h3>
            <p className="muted">{selected.description}</p>
            <button
              data-package-action
              className="primary"
              aria-haspopup="dialog"
              onClick={() => {
                navigate("access", { tab: "capabilities" }, true);
                setEditing(selected.package_id);
              }}
            >
              {selected.configuration_state === "PRESENT"
                ? "管理此包"
                : "接入此包"}
            </button>
          </section>
        )}
      </WorkflowTable>
      {id && editing === id && (
        <PackageEditor
          key={id}
          id={id}
          transport={transport}
          changed={changed}
          tab={route.fields.get("tab") ?? "capabilities"}
          setTab={(tab) => navigate("access", { tab }, true)}
          navigate={navigate}
          onClose={() => setEditing(null)}
        />
      )}
      {data && id && !selected && (
        <p role="alert">此接入包不在当前可见目录中，未改选其他接入包。</p>
      )}
    </>
  );
}
function PackageEditor({
  id,
  transport,
  changed,
  tab,
  setTab,
  navigate,
  onClose,
}: {
  id: string;
  transport: Transport;
  changed: () => void;
  tab: string;
  setTab: (tab: string) => void;
  navigate: Navigate;
  onClose: () => void;
}) {
  const [snapshot, setSnapshot] = useState<Package | null>(null);
  const [enabled, setEnabled] = useState<string[]>([]);
  const [credentialDrafts, setCredentialDrafts] = useState<
    Record<string, string>
  >({});
  const [phase, setPhase] = useState<
    "loading" | "ready" | "saving" | "failed" | "conflict" | "unknown"
  >("loading");
  const [notice, setNotice] = useState("");
  const [saveSource, setSaveSource] = useState<string | null>(null);
  const [diagnostic, setDiagnostic] = useState("");
  const [verifying, setVerifying] = useState<string | null>(null);
  const lock = useRef(false);
  const active = useRef(true);
  const readRequest = useRef<AbortController | null>(null);
  const attempted = useRef<SaveIntent | null>(null);
  async function reread() {
    if (lock.current) return;
    readRequest.current?.abort();
    const controller = new AbortController();
    readRequest.current = controller;
    setPhase("loading");
    setCredentialDrafts({});
    setDiagnostic("");
    try {
      const receipt = receipts(transport).get(id);
      if (receipt) {
        await receipt.done;
        if (!active.current || controller.signal.aborted) return;
        if (receipt.unknown) attempted.current = receipt.intent;
      }
      const result = await getPackage(transport, id, controller.signal);
      if (!active.current || controller.signal.aborted) return;
      const previous = attempted.current;
      receipts(transport).delete(id);
      setSnapshot(result);
      setEnabled([...result.enabled_capability_ids]);
      setPhase("ready");
      if (previous) {
        const matches =
          result.enabled_capability_ids.length ===
            previous.enabled_capability_ids.length &&
          result.enabled_capability_ids.every((c) =>
            previous.enabled_capability_ids.includes(c),
          );
        setNotice(
          (matches
            ? "已重读：当前启用集合与待核对内容一致。请核对当前配置。"
            : "已重读当前配置，与待核对内容不同；请核对后重新编辑。") +
            (previous.replaced_sources.length
              ? "凭据仅显示配置状态，不能据此确认刚才输入的值；再次替换需重新输入。"
              : ""),
        );
        attempted.current = null;
        changed();
      }
    } catch (error) {
      if (!active.current || controller.signal.aborted) return;
      setPhase(attempted.current ? "unknown" : "failed");
      setDiagnostic(
        error instanceof PackageRequestFailure
          ? error.detail.classification
          : "package_read_unavailable",
      );
    }
  }
  useEffect(() => {
    active.current = true;
    void reread();
    return () => {
      active.current = false;
      readRequest.current?.abort();
    };
  }, [id, transport]);
  const required = snapshot
    ? [
        ...new Set(
          snapshot.capabilities
            .filter((c) => enabled.includes(c.capability_id))
            .flatMap((c) => c.required_sources),
        ),
      ].sort()
    : [];
  const replacementSources = required.filter(
    (source) => source in credentialDrafts,
  );
  const invalidCredential = replacementSources.some(
    (source) => !credentialDrafts[source].trim(),
  );
  const dirty =
    !!snapshot &&
    (enabled.length !== snapshot.enabled_capability_ids.length ||
      enabled.some((c) => !snapshot.enabled_capability_ids.includes(c)) ||
      replacementSources.length > 0);
  async function save(source: string | null = null) {
    if (
      !snapshot ||
      lock.current ||
      receipts(transport).has(id) ||
      phase !== "ready" ||
      !dirty ||
      invalidCredential ||
      snapshot.write_eligible !== true ||
      (snapshot.configuration_state === "ABSENT" && enabled.length === 0)
    )
      return;
    lock.current = true;
    setSaveSource(source);
    readRequest.current?.abort();
    const command: SavePackage = {
      expected_revision: snapshot.config_revision,
      enabled_capability_ids: [...enabled],
      source_changes: required.map((source_id) =>
        source_id in credentialDrafts
          ? {
              source_id,
              credential_action: "replace" as const,
              credential_value: credentialDrafts[source_id],
            }
          : { source_id, credential_action: "keep" as const },
      ),
    };
    const intent: SaveIntent = {
      expected_revision: snapshot.config_revision,
      enabled_capability_ids: [...enabled],
      replaced_sources: [...replacementSources],
    };
    let finish = () => {};
    const receipt: SaveReceipt = {
      intent,
      unknown: false,
      done: new Promise<void>((resolve) => {
        finish = resolve;
      }),
    };
    receipts(transport).set(id, receipt);
    setPhase("saving");
    setCredentialDrafts({});
    setNotice("");
    setDiagnostic("");
    try {
      const result = await putPackage(transport, id, command);
      receipts(transport).delete(id);
      if (!active.current) return;
      setSnapshot(result);
      setEnabled([...result.enabled_capability_ids]);
      setPhase("ready");
      setNotice("配置已保存；尚未发起来源请求或采集。");
      changed();
    } catch (error) {
      const detail =
        error instanceof PackageRequestFailure ? error.detail : null;
      receipt.unknown = !detail || detail.effect === "UNKNOWN";
      if (!receipt.unknown) receipts(transport).delete(id);
      if (!active.current) return;
      setDiagnostic(detail?.classification ?? "config_commit_outcome_unknown");
      if (detail?.effect === "NONE" && detail.status !== "CONFLICT")
        setCredentialDrafts(
          Object.fromEntries(
            intent.replaced_sources.map((source) => [source, ""]),
          ),
        );
      if (!detail || detail.effect === "UNKNOWN") {
        attempted.current = intent;
        setPhase("unknown");
      } else if (detail.status === "CONFLICT") setPhase("conflict");
      else {
        setPhase("ready");
        setNotice(
          "保存被拒绝或确认未提交，本次修改未生效；可检查后再保存。" +
            (intent.replaced_sources.length
              ? "凭据输入已清除；请重新输入，或明确取消本次替换。"
              : ""),
        );
      }
    } finally {
      // Avoid retaining raw values in a completed async operation's request DTO.
      for (const change of command.source_changes ?? [])
        if (change.credential_action === "replace")
          change.credential_value = "";
      lock.current = false;
      finish();
    }
  }
  async function verify(source: string) {
    if (
      !snapshot?.config_revision ||
      lock.current ||
      dirty ||
      phase !== "ready"
    )
      return;
    lock.current = true;
    setVerifying(source);
    setSaveSource(null);
    setDiagnostic("");
    try {
      const result = await verifyPackageSource(
        transport,
        id,
        source,
        snapshot.config_revision,
      );
      if (!active.current) return;
      setSnapshot(result);
      changed();
    } catch (error) {
      if (active.current) {
        const conflict =
          error instanceof PackageRequestFailure &&
          error.detail.status === "CONFLICT";
        setSnapshot((previous) =>
          previous
            ? {
                ...previous,
                source_verifications: previous.source_verifications?.map(
                  (item) =>
                    conflict || item.source_id === source
                      ? {
                          source_id: item.source_id,
                          current: false,
                          checked_at: null,
                          request_status: "not_run",
                          authentication: "unknown",
                          http_status: null,
                          reason: conflict
                            ? "configuration_changed"
                            : "unexpected_exception",
                        }
                      : item,
                ),
              }
            : previous,
        );
        if (conflict) setPhase("conflict");
        setDiagnostic(
          error instanceof PackageRequestFailure
            ? error.detail.classification
            : "verification_unavailable",
        );
      }
    } finally {
      lock.current = false;
      if (active.current) setVerifying(null);
    }
  }
  if (!snapshot)
    return (
      <PackageDialog title={id} busy={false} onClose={onClose}>
        <div role="status">
          {phase === "loading"
            ? "正在读取包配置…"
            : phase === "unknown"
              ? "提交结果未知，配置可能已生效。请先重读核对，不能直接重复保存。"
              : "配置读取失败，当前状态未知。"}
          {phase !== "loading" && (
            <button onClick={() => void reread()}>重新读取配置</button>
          )}
          {diagnostic && <p className="small">错误代码：{diagnostic}</p>}
        </div>
      </PackageDialog>
    );
  const blocked = phase !== "ready" || verifying !== null;
  const reason =
    snapshot.write_eligible !== true
      ? snapshot.write_reasons.includes("readonly_host")
        ? "当前服务仅支持查看，保存配置尚未开放。"
        : "当前账号无法保存配置，请核对访问权限。"
      : snapshot.configuration_state === "ABSENT" && enabled.length === 0
        ? "首次接入至少选择一项能力。"
        : invalidCredential
          ? "请填写新凭据，或取消本次替换。"
          : !dirty
            ? "尚无待保存的修改。"
            : blocked
              ? "请等待保存完成，或先重读核对当前配置。"
              : "";
  const capabilityChanges = snapshot.capabilities.filter(
    (cap) =>
      enabled.includes(cap.capability_id) !==
      snapshot.enabled_capability_ids.includes(cap.capability_id),
  ).length;
  const localFeedback =
    tab === "sources" && saveSource !== null && required.includes(saveSource);
  const saveFeedback = (
    <>
      {notice && <p role="status">{notice}</p>}
      {phase === "saving" && <p role="status">正在保存，请勿重复提交…</p>}
      {phase === "loading" && <p role="status">正在重读核对…</p>}
      {phase === "failed" && <p role="alert">配置读取失败，当前状态未知。</p>}
      {phase === "conflict" && (
        <p role="alert">配置已被其他操作更新，本次未生效。请重读后再编辑。</p>
      )}
      {phase === "unknown" && (
        <p role="alert">
          提交结果未知，配置可能已生效。请先重读核对，不能直接重复保存。
        </p>
      )}
      {diagnostic && <p className="small">错误代码：{diagnostic}</p>}
    </>
  );
  return (
    <PackageDialog
      title={snapshot.name}
      busy={phase === "saving" || verifying !== null}
      onClose={onClose}
    >
      <section aria-label="接入包配置">
        <p className="muted">选择需要启用的能力，来源配置会随选择更新。</p>
        {snapshot.write_eligible !== true && (
          <p role="status" className="package-save-note">
            {reason}
          </p>
        )}
        <Tabs
          label="接入包设置"
          values={[
            ["capabilities", "能力配置"],
            ["sources", "来源配置"],
          ]}
          value={tab}
          onChange={setTab}
        />
        {tab === "capabilities" ? (
          <fieldset
            className="package-capabilities"
            disabled={blocked || snapshot.write_eligible !== true}
          >
            <legend>启用能力</legend>
            {snapshot.capabilities.map((c) => (
              <label className="capability-choice" key={c.capability_id}>
                <input
                  type="checkbox"
                  checked={enabled.includes(c.capability_id)}
                  onChange={(e) => {
                    setNotice("");
                    const next = e.target.checked
                      ? [...enabled, c.capability_id]
                      : enabled.filter((v) => v !== c.capability_id);
                    setEnabled(next);
                    const sources = new Set(
                      snapshot.capabilities
                        .filter((cap) => next.includes(cap.capability_id))
                        .flatMap((cap) => cap.required_sources),
                    );
                    setCredentialDrafts((old) =>
                      Object.fromEntries(
                        Object.entries(old).filter(([source]) =>
                          sources.has(source),
                        ),
                      ),
                    );
                  }}
                />
                {c.title}
              </label>
            ))}
          </fieldset>
        ) : (
          <>
            <p>
              默认使用已保存的凭据；需要更换时再输入新值。保存后不会回显完整凭据。
            </p>
            <p>
              本次选择所需来源：{required.join(" / ") || "无"}
              。切换能力不会访问来源。
            </p>
            <p>
              验证结果保留于本次服务运行期间；服务重启后需重新验证。来源资格不由此验证评定，采集是否可执行请查看本包资产的当前准入。
            </p>
            <section aria-label="能力与来源关系">
              <h3>
                {snapshot.package_id === "macro"
                  ? "九族与来源关系"
                  : "能力与来源关系"}
              </h3>
              {snapshot.capabilities.map((cap) => (
                <details key={cap.capability_id}>
                  <summary>
                    {cap.title} · 数值来源：
                    {cap.source_relations?.length
                      ? [
                          ...new Set(
                            cap.source_relations.map(
                              (relation) => relation.source_id,
                            ),
                          ),
                        ].join(" / ")
                      : "未装配"}{" "}
                    ·{" "}
                    {enabled.includes(cap.capability_id)
                      ? "本次选择启用"
                      : "本次未启用"}
                  </summary>
                  <p>
                    资产：{cap.definition_ref.identity} /{" "}
                    {cap.definition_ref.revision}
                  </p>
                  {cap.source_relations?.length ? (
                    distinctRelations(cap.source_relations).map(
                      (relation, index) => (
                        <dl key={index}>
                          <Fact name="数值通路">
                            {relation.source_id === relation.numeric_path
                              ? relation.numeric_path
                              : `${relation.source_id} · ${relation.numeric_path}`}
                          </Fact>
                          <Fact name="原始机构">
                            {relation.original_institution}
                          </Fact>
                          <Fact name="发布日历">
                            {relation.release_calendar}
                          </Fact>
                          <Fact name="备用通路">{relation.backup_status}</Fact>
                        </dl>
                      ),
                    )
                  ) : (
                    <p>来源关系未装配。</p>
                  )}
                </details>
              ))}
            </section>
            {required.map((id) => {
              const state = snapshot.credential_states.find(
                (c) => c.source_id === id,
              );
              const verification = snapshot.source_verifications?.find(
                (v) => v.source_id === id,
              );
              return (
                <fieldset key={id}>
                  <legend>{id}</legend>
                  <dl>
                    <Fact name="凭据状态">
                      {state?.credential_state === "configured"
                        ? "已配置（仅表示存在）"
                        : state?.credential_state === "missing"
                          ? "缺少配置"
                          : "配置状态未知"}
                    </Fact>
                  </dl>
                  <dl aria-label={`${id} 验证结果`}>
                    <Fact name="已保存配置验证">
                      {verification?.reason
                        ? (verificationReasons[verification.reason] ??
                          verification.reason)
                        : "尚未验证"}
                    </Fact>
                    <Fact name="请求结果">
                      {verification?.request_status === "succeeded"
                        ? "请求成功"
                        : verification?.request_status === "failed"
                          ? "请求失败"
                          : "未发起请求"}
                    </Fact>
                    <Fact name="认证">
                      {verification?.authentication === "confirmed"
                        ? "认证已确认"
                        : verification?.authentication === "rejected"
                          ? "认证被拒绝（来源明确返回认证错误）"
                          : "认证未知（本次检查未确认认证结论）"}
                    </Fact>
                    <Fact name="验证时间">
                      {verification?.checked_at
                        ? formatBeijingTime(verification.checked_at)
                        : "尚未验证"}
                    </Fact>
                    <Fact name="HTTP">
                      {verification?.http_status ?? "未取得响应"}
                    </Fact>
                  </dl>
                  <p>
                    来源资格与采集执行权限另行判定；验证不会启用能力、采集或影响已保存数据读取。30秒内重复验证复用当前结果。
                  </p>
                  <button
                    disabled={
                      blocked ||
                      dirty ||
                      !snapshot.config_revision ||
                      snapshot.write_eligible !== true
                    }
                    onClick={() => void verify(id)}
                  >
                    {verifying === id ? "正在验证…" : `验证已保存的 ${id} 配置`}
                  </button>
                  {dirty && <p>请先保存或取消修改，再验证当前已保存配置。</p>}
                  {state?.replace_supported === true ? (
                    id in credentialDrafts ? (
                      <div
                        className="credential-replacement"
                        aria-label={`${id} 待提交凭据`}
                      >
                        <label>
                          {id} 新凭据
                          <input
                            type="password"
                            disabled={
                              blocked || snapshot.write_eligible !== true
                            }
                            autoComplete="new-password"
                            spellCheck={false}
                            maxLength={4096}
                            value={credentialDrafts[id]}
                            onChange={(event) => {
                              setNotice("");
                              setDiagnostic("");
                              setCredentialDrafts((old) => ({
                                ...old,
                                [id]: event.target.value,
                              }));
                            }}
                          />
                        </label>
                        <p className="small">
                          此入口会一并保存整个接入包的所有待提交修改：能力启停{" "}
                          {capabilityChanges} 项变更；凭据替换{" "}
                          {replacementSources.join(" / ") || "无"}
                          。保存不会发起上游验证。
                        </p>
                        <div className="actions">
                          <button
                            className="primary"
                            disabled={!!reason}
                            title={reason}
                            onClick={() => void save(id)}
                          >
                            保存整包配置（{id}入口）
                          </button>
                          <button
                            disabled={
                              blocked || snapshot.write_eligible !== true
                            }
                            onClick={() => {
                              setCredentialDrafts((old) => {
                                const next = { ...old };
                                delete next[id];
                                return next;
                              });
                              setSaveSource(id);
                              setNotice(
                                `已取消 ${id} 本次替换，未保存；其他待提交修改仍需整包保存。`,
                              );
                              setDiagnostic("");
                            }}
                          >
                            取消替换 {id} 凭据
                          </button>
                        </div>
                        {reason && <p className="small">不能保存：{reason}</p>}
                      </div>
                    ) : (
                      <button
                        disabled={blocked || snapshot.write_eligible !== true}
                        onClick={() =>
                          setCredentialDrafts((old) => ({
                            ...old,
                            [id]: "",
                          }))
                        }
                      >
                        替换 {id} 凭据
                      </button>
                    )
                  ) : (
                    <p>当前环境未装配此来源的凭据替换。</p>
                  )}
                  {localFeedback && saveSource === id && (
                    <div aria-label={`${id} 保存反馈`}>
                      {saveFeedback}
                      {(phase === "conflict" || phase === "unknown") && (
                        <button onClick={() => void reread()}>
                          重读核对整包配置
                        </button>
                      )}
                    </div>
                  )}
                </fieldset>
              );
            })}
          </>
        )}
        <p className="small">
          两个页签共用一次保存；停用能力保留已有凭据与历史。配置不代表来源可执行。
        </p>
        {!localFeedback && saveFeedback}
        <div className="actions">
          <button
            className="primary"
            disabled={!!reason}
            title={reason}
            onClick={() => void save()}
          >
            保存配置
          </button>
          <button
            disabled={phase === "saving" || verifying !== null}
            onClick={onClose}
          >
            取消
          </button>
          <button
            disabled={
              phase === "saving" || phase === "loading" || verifying !== null
            }
            onClick={() => void reread()}
          >
            重读核对配置
          </button>
          <button
            disabled={phase === "saving" || verifying !== null}
            onClick={() => navigate("assets", { package: snapshot.package_id })}
          >
            查看本包资产 →
          </button>
        </div>
        {reason && snapshot.write_eligible === true && (
          <p className="small">{reason}</p>
        )}
      </section>
    </PackageDialog>
  );
}

const verificationReasons: Record<string, string> = {
  not_checked: "尚未验证",
  configuration_changed: "配置或凭据已变化，请重新验证",
  credential_missing: "缺少凭据",
  verification_not_assembled: "验证通路未装配",
  source_not_admitted: "当前来源未获准请求",
  request_succeeded: "验证请求成功",
  http_forbidden: "HTTP 403：访问被拒绝，不能据此判定 Key 错误",
  http_unauthorized: "HTTP 401：未获授权",
  rate_limited: "来源限流或配额已用尽",
  http_failure: "来源 HTTP 请求失败",
  timeout: "来源请求超时",
  transport_failure: "网络传输失败",
  protocol_failure: "响应协议错误",
  request_invalid: "请求无效",
  unexpected_exception: "检查未完成，原因未确认",
  business_error: "来源返回业务错误",
  response_invalid: "响应未符合验证合同",
  admission_denied: "共享来源请求准入暂未允许",
  credential_storage_unavailable: "凭据存储不可用",
};

// Presentation only: canonical entries and distinct relationship fields stay intact.
function distinctRelations(
  relations: NonNullable<Package["capabilities"][number]["source_relations"]>,
) {
  const seen = new Set<string>();
  return relations.filter((relation) => {
    const key = JSON.stringify([
      relation.source_id,
      relation.numeric_path,
      relation.original_institution,
      relation.release_calendar,
      relation.backup_status,
    ]);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
