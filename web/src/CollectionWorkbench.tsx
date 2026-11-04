import { macroAssetLabel } from "./macroTimeSeriesProfile";
import {
  M3_PROFILE,
  JOLTS_PROFILE,
  CLAIMS_PROFILE,
  GDP_PROFILE,
  CPI_PROFILE,
  PPI_PROFILE,
  PCE_PROFILE,
  CONSUMPTION_PROFILE,
  EMPLOYMENT_PROFILE,
  RETAIL_PROFILE,
} from "./macroTimeSeriesProfile";
import { formatBeijingTime } from "./beijingTime";
import { DateField } from "./DateField";
import {
  pceRequest,
  pceBound,
  pceExecution,
  validPceBinding,
  PCE_REFERENCE,
  type PceJob,
  type PceJobs,
} from "./pce";
import { useEffect, useRef, useState } from "react";
import { request, submitJob, type Jobs, type Transport } from "./api";
import {
  type Asset,
  type CollectionContext,
  type Execution,
  type Interface,
  type Plans,
  M3,
  JOLTS,
  joltsOperationMatches,
  CLAIMS,
  claimsOperationMatches,
  GDP,
  CPI,
  PPI,
  PCE,
  CONSUMPTION,
  EMPLOYMENT,
  RETAIL,
  retailOperationMatches,
  consumptionOperationMatches,
  employmentOperationMatches,
  MANUAL_OPERATION,
  PCE_OPERATION,
  HISTORY_OPERATION,
  assetTitle,
  executions,
  keyOf,
  sameKey,
  stateLabel,
} from "./workflowModel";
import { failureName, useResource } from "./workflowHooks";
import type { Navigate, Route } from "./workflowRoute";
import { Fact, Tabs, Technical, WorkflowTable } from "./WorkflowTable";
import { readCpiExecutions, type CpiAvailability } from "./cpiTimeSeriesApi";
import {
  CpiExecutionDetail,
  CpiStrategyWorkbench,
  cpiActionText,
  cpiExecutionStateText,
} from "./CpiStrategyWorkbench";
import { useVisiblePolling } from "./useVisiblePolling";
import { CpiTimeSeriesCollection } from "./CpiTimeSeriesCollection";
const tabs = [
  ["new", "新建采集"],
  ["periodic", "周期任务"],
  ["executions", "执行记录"],
] as const;
const modes = ["一次性初始化", "指定范围回补"] as const;
const devIntentKey = "mdf.cpi.dev.manual-intents.v1";
function readDevIntents(): string[] {
  const stored = sessionStorage.getItem(devIntentKey);
  if (stored === null) return [];
  const intents: unknown = JSON.parse(stored);
  if (
    !Array.isArray(intents) ||
    !intents.every((intent) => typeof intent === "string" && intent.length > 0)
  )
    throw new Error("invalid_intent_reservation");
  return intents;
}
export function CollectionWorkbench({
  assets,
  asset,
  source,
  route,
  navigate,
  transport,
  generation,
  invalidIdentity,
  cpiAvailability,
  ppiAvailability,
  pceAvailability,
  employmentAvailability,
  retailAvailability,
  consumptionAvailability,
  m3Availability,
  joltsAvailability,
  claimsAvailability,
  gdpAvailability,
}: {
  assets: Asset[];
  asset: Asset | undefined;
  source: Interface | undefined;
  route: Route;
  navigate: Navigate;
  transport: Transport;
  generation: number;
  invalidIdentity: boolean;
  m3Availability?: CpiAvailability;
  joltsAvailability?: CpiAvailability;
  claimsAvailability?: CpiAvailability;
  gdpAvailability?: CpiAvailability;
  cpiAvailability?: CpiAvailability;
  ppiAvailability?: CpiAvailability;
  pceAvailability?: CpiAvailability;
  employmentAvailability?: CpiAvailability;
  retailAvailability?: CpiAvailability;
  consumptionAvailability?: CpiAvailability;
}) {
  const tab = tabs.some((t) => t[0] === route.fields.get("tab"))
    ? route.fields.get("tab")!
    : "new";
  // The route owns the creation target, even while its directory is loading.
  const selectedTarget = invalidIdentity
    ? ""
    : route.fields.has("asset") && route.fields.has("asset_revision")
      ? `${route.fields.get("asset")}@${route.fields.get("asset_revision")}`
      : asset
        ? keyOf(asset.key)
        : "";
  const [mode, setMode] = useState<string>(
    route.fields.get("mode") === "backfill" ? "指定范围回补" : "一次性初始化",
  );
  const [refresh, setRefresh] = useState(0);
  const [briefTarget, setBriefTarget] = useState<string | null>(null);
  const [start, setStart] = useState(
    sameKey(asset?.key, PCE) ? PCE_REFERENCE : "",
  );
  const [end, setEnd] = useState(sameKey(asset?.key, PCE) ? PCE_REFERENCE : "");
  const [frequency, setFrequency] = useState("daily");
  const [zone, setZone] = useState("Asia/Shanghai");
  const [time, setTime] = useState("09:00");
  const [day, setDay] = useState("1");
  const [month, setMonth] = useState("1");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const reservedIntents = useRef(new Set<string>());
  const rangeEdited = useRef(false);
  useEffect(() => setConfirmed(false), [start, end]);
  const writing = useRef(false);
  const active = useRef(true);
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
    };
  }, []);
  const context: CollectionContext | null = asset
    ? {
        asset,
        source:
          source ??
          (asset.interfaces.length === 1 ? asset.interfaces[0] : null),
        storage: asset.storage,
      }
    : null;
  const isPce = !!asset && sameKey(asset.key, PCE) && !invalidIdentity;
  useEffect(() => {
    if (isPce && !rangeEdited.current) {
      setStart(PCE_REFERENCE);
      setEnd(PCE_REFERENCE);
    }
  }, [isPce]);
  const pceCatalog = context?.source?.pce;
  const pceBindingMatches = isPce && pceBound(context?.source, pceCatalog);
  const isCpi = !!asset && sameKey(asset.key, CPI) && !invalidIdentity;
  const isPpi =
    !invalidIdentity &&
    !route.fields.has("operation") &&
    (sameKey(asset?.key, PPI) ||
      (route.fields.get("asset") === PPI.identity &&
        route.fields.get("asset_revision") === PPI.revision));
  const pceTarget =
    !invalidIdentity &&
    (isPce ||
      (route.fields.get("asset") === PCE.identity &&
        route.fields.get("asset_revision") === PCE.revision)) &&
    (!route.fields.has("operation") ||
      route.fields.get("operation") === PCE_OPERATION);
  const employmentTarget =
    !invalidIdentity &&
    (!route.fields.has("operation") ||
      (route.fields.get("operation") === source?.id &&
        employmentOperationMatches(source?.dto))) &&
    (sameKey(asset?.key, EMPLOYMENT) ||
      (route.fields.get("asset") === EMPLOYMENT.identity &&
        route.fields.get("asset_revision") === EMPLOYMENT.revision));
  const retailTarget =
    !invalidIdentity &&
    (!route.fields.has("operation") || retailOperationMatches(source?.dto)) &&
    (sameKey(asset?.key, RETAIL) ||
      (route.fields.get("asset") === RETAIL.identity &&
        route.fields.get("asset_revision") === RETAIL.revision));
  const consumptionTarget =
    !invalidIdentity &&
    (!route.fields.has("operation") ||
      (route.fields.get("operation") === source?.id &&
        consumptionOperationMatches(source?.dto))) &&
    (sameKey(asset?.key, CONSUMPTION) ||
      (route.fields.get("asset") === CONSUMPTION.identity &&
        route.fields.get("asset_revision") === CONSUMPTION.revision));
  const gdpTarget =
    !invalidIdentity &&
    !route.fields.has("operation") &&
    (sameKey(asset?.key, GDP) ||
      (route.fields.get("asset") === GDP.identity &&
        route.fields.get("asset_revision") === GDP.revision));
  const m3Target =
    !invalidIdentity &&
    !route.fields.has("operation") &&
    (sameKey(asset?.key, M3) ||
      (route.fields.get("asset") === M3.identity &&
        route.fields.get("asset_revision") === M3.revision));
  const newM3 = !!m3Availability?.data && !m3Availability.legacy;
  const newGdp = !!gdpAvailability?.data && !gdpAvailability.legacy;
  const joltsTarget =
    !invalidIdentity &&
    (!route.fields.has("operation") || joltsOperationMatches(source?.dto)) &&
    (sameKey(asset?.key, JOLTS) ||
      (route.fields.get("asset") === JOLTS.identity &&
        route.fields.get("asset_revision") === JOLTS.revision));
  const newJolts = !!joltsAvailability?.data && !joltsAvailability.legacy;
  const claimsTarget =
    !invalidIdentity &&
    (!route.fields.has("operation") || claimsOperationMatches(source?.dto)) &&
    (sameKey(asset?.key, CLAIMS) ||
      (route.fields.get("asset") === CLAIMS.identity &&
        route.fields.get("asset_revision") === CLAIMS.revision));
  const newClaims = !!claimsAvailability?.data && !claimsAvailability.legacy;
  const newConsumption =
    !!consumptionAvailability?.data && !consumptionAvailability.legacy;
  const newRetail = !!retailAvailability?.data && !retailAvailability.legacy;
  const newEmployment =
    !!employmentAvailability?.data && !employmentAvailability.legacy;
  const macroProfile = m3Target
    ? M3_PROFILE
    : joltsTarget
      ? JOLTS_PROFILE
      : claimsTarget
        ? CLAIMS_PROFILE
        : gdpTarget
          ? GDP_PROFILE
          : consumptionTarget
            ? CONSUMPTION_PROFILE
            : retailTarget
              ? RETAIL_PROFILE
              : employmentTarget
                ? EMPLOYMENT_PROFILE
                : pceTarget
                  ? PCE_PROFILE
                  : isPpi
                    ? PPI_PROFILE
                    : CPI_PROFILE;
  const macroAvailability = m3Target
    ? m3Availability
    : joltsTarget
      ? joltsAvailability
      : claimsTarget
        ? claimsAvailability
        : gdpTarget
          ? gdpAvailability
          : consumptionTarget
            ? consumptionAvailability
            : retailTarget
              ? retailAvailability
              : employmentTarget
                ? employmentAvailability
                : pceTarget
                  ? pceAvailability
                  : isPpi
                    ? ppiAvailability
                    : cpiAvailability;
  const newPpi =
    !!ppiAvailability && !ppiAvailability.legacy && !!ppiAvailability.data;
  const newPce =
    !!pceAvailability && !pceAvailability.legacy && !!pceAvailability.data;
  const newMacro = m3Target
    ? !!m3Availability && !m3Availability.legacy
    : joltsTarget
      ? !!joltsAvailability && !joltsAvailability.legacy
      : claimsTarget
        ? !!claimsAvailability && !claimsAvailability.legacy
        : gdpTarget
          ? !!gdpAvailability && !gdpAvailability.legacy
          : consumptionTarget
            ? !!consumptionAvailability && !consumptionAvailability.legacy
            : retailTarget
              ? !!retailAvailability && !retailAvailability.legacy
              : employmentTarget
                ? !!employmentAvailability && !employmentAvailability.legacy
                : pceTarget
                  ? !!pceAvailability &&
                    !pceAvailability.legacy &&
                    (!!pceAvailability.data || !!pceAvailability.failure)
                  : isPpi
                    ? !!ppiAvailability
                    : !!cpiAvailability &&
                      !cpiAvailability.legacy &&
                      (!!cpiAvailability.data || !!cpiAvailability.failure);
  const macroPending =
    !!macroAvailability &&
    !macroAvailability.legacy &&
    !macroAvailability.data &&
    !macroAvailability.failure;
  const newCpi =
    !!cpiAvailability &&
    !cpiAvailability.legacy &&
    (!!cpiAvailability.data || !!cpiAvailability.failure);
  const cpiTarget =
    !route.fields.has("operation") &&
    (isCpi ||
      (!invalidIdentity &&
        route.fields.get("asset") === CPI.identity &&
        route.fields.get("asset_revision") === CPI.revision &&
        !route.fields.has("operation")));
  const filterField = tab === "periodic" ? "periodic_asset" : "execution_asset";
  const filter = route.fields.get(filterField) ?? "all";
  const executionSchedule = route.fields.get("execution_schedule") ?? "all";
  const readRecords =
    !joltsTarget &&
    !claimsTarget &&
    !gdpTarget &&
    !isPpi &&
    (!pceTarget || (tab !== "new" && filter !== keyOf(PCE))) &&
    (!cpiAvailability || cpiAvailability.legacy) &&
    (tab === "new"
      ? isCpi
      : !invalidIdentity &&
        (filter === "all" ||
          filter === keyOf(CPI) ||
          (isCpi && route.fields.has("job"))));
  const pceReadBound =
    validPceBinding(pceCatalog?.binding) ||
    assets.some(
      (a) =>
        sameKey(a.key, PCE) &&
        (validPceBinding(a.pce?.catalog.binding) ||
          a.interfaces.some((i) => validPceBinding(i.pce?.binding))),
    );
  const readPceRecords =
    (!pceAvailability || pceAvailability.legacy) &&
    pceReadBound &&
    (isPce ||
      (tab === "executions" &&
        !invalidIdentity &&
        pceReadBound &&
        (filter === "all" || filter === keyOf(PCE))));
  const pceJobs = useVisiblePolling<PceJobs>(
    async (signal) => {
      const result = await pceRequest<PceJobs>(transport, "/api/pce/jobs", {
        signal,
      });
      if (
        !Array.isArray(result.jobs) ||
        result.jobs.some((j) => !validPceBinding(j.binding) || !j.job_id)
      )
        throw new Error("pce_jobs_binding_mismatch");
      return result;
    },
    [
      transport,
      generation,
      refresh,
      readPceRecords,
      tab,
      route.fields.get("job"),
    ],
    readPceRecords,
  );
  const jobId = route.fields.get("job");
  const scheduleId = route.fields.get("schedule");
  const jobs = useVisiblePolling<Jobs>(
    (signal) => request(transport, "/api/r5/cpi/jobs", {}, signal),
    [transport, generation, refresh, jobId, readRecords],
    readRecords,
  );
  const needsNamedJob =
    !!jobId &&
    readRecords &&
    !jobs.busy &&
    !jobs.data?.jobs.some((j) => j.job_identity === jobId);
  const namedJob = useResource<Jobs>(
    (signal) =>
      request(transport, "/api/r5/cpi/jobs", { job_identity: jobId! }, signal),
    [transport, generation, refresh, jobId, needsNamedJob],
    needsNamedJob,
  );
  const plans = useVisiblePolling<Plans>(
    (signal) => request(transport, "/api/r5/cpi/plans", undefined, signal),
    [transport, generation, refresh, readRecords],
    readRecords,
  );
  const jobsMatch =
    jobs.data?.status === "OK" &&
    jobs.data.asset_identity === CPI.identity &&
    jobs.data.asset_revision === CPI.revision;
  const plansMatch =
    plans.data?.status === "OK" &&
    plans.data.asset_identity === CPI.identity &&
    plans.data.asset_revision === CPI.revision;
  const planAsset = plansMatch
    ? {
        identity: plans.data!.asset_identity!,
        revision: plans.data!.asset_revision!,
      }
    : undefined;
  const all = [
    ...executions(
      [
        ...(jobsMatch ? jobs.data!.jobs : []),
        ...(namedJob.data?.status === "OK" &&
        namedJob.data.asset_identity === CPI.identity &&
        namedJob.data.asset_revision === CPI.revision
          ? namedJob.data.jobs.filter(
              (j) =>
                !jobs.data?.jobs.some(
                  (existing) => existing.job_identity === j.job_identity,
                ),
            )
          : []),
      ],
      plansMatch ? plans.data : null,
    ).filter((e) => sameKey(e.asset, CPI)),
    ...(pceJobs.data?.jobs ?? []).map(pceExecution),
  ];
  const cpiRecords = useVisiblePolling(
    (signal) => readCpiExecutions(transport, signal),
    [transport, generation, CPI_PROFILE.id],
    newCpi &&
      tab === "executions" &&
      !invalidIdentity &&
      (filter === "all" ||
        filter === keyOf(CPI) ||
        (!!route.fields.get("job") &&
          !isPpi &&
          !pceTarget &&
          !gdpTarget &&
          !joltsTarget &&
          !claimsTarget)),
  );
  const ppiRecords = useVisiblePolling(
    (signal) => readCpiExecutions(transport, signal, PPI_PROFILE),
    [transport, generation, PPI_PROFILE.id],
    newPpi &&
      tab === "executions" &&
      !invalidIdentity &&
      (filter === "all" || filter === keyOf(PPI)),
  );
  const pceRecords = useVisiblePolling(
    (signal) => readCpiExecutions(transport, signal, PCE_PROFILE),
    [transport, generation, PCE_PROFILE.id],
    newPce &&
      tab === "executions" &&
      !invalidIdentity &&
      (filter === "all" || filter === keyOf(PCE) || pceTarget),
  );
  const employmentRecords = useVisiblePolling(
    (signal) => readCpiExecutions(transport, signal, EMPLOYMENT_PROFILE),
    [transport, generation, EMPLOYMENT_PROFILE.id],
    newEmployment &&
      tab === "executions" &&
      !invalidIdentity &&
      (filter === "all" || filter === keyOf(EMPLOYMENT)),
  );
  const retailRecords = useVisiblePolling(
    (signal) => readCpiExecutions(transport, signal, RETAIL_PROFILE),
    [transport, generation, RETAIL_PROFILE.id],
    newRetail &&
      tab === "executions" &&
      !invalidIdentity &&
      (filter === "all" || filter === keyOf(RETAIL)),
  );
  const consumptionRecords = useVisiblePolling(
    (signal) => readCpiExecutions(transport, signal, CONSUMPTION_PROFILE),
    [transport, generation, CONSUMPTION_PROFILE.id],
    newConsumption &&
      tab === "executions" &&
      !invalidIdentity &&
      (filter === "all" || filter === keyOf(CONSUMPTION) || consumptionTarget),
  );
  const gdpRecords = useVisiblePolling(
    (signal) => readCpiExecutions(transport, signal, GDP_PROFILE),
    [transport, generation, GDP_PROFILE.id],
    newGdp &&
      tab === "executions" &&
      !invalidIdentity &&
      (filter === "all" || filter === keyOf(GDP) || gdpTarget),
  );
  const m3Records = useVisiblePolling(
    (signal) => readCpiExecutions(transport, signal, M3_PROFILE),
    [transport, generation, M3_PROFILE.id],
    newM3 &&
      tab === "executions" &&
      !invalidIdentity &&
      (filter === "all" || filter === keyOf(M3) || m3Target),
  );
  const joltsRecords = useVisiblePolling(
    (signal) => readCpiExecutions(transport, signal, JOLTS_PROFILE),
    [transport, generation, JOLTS_PROFILE.id],
    newJolts &&
      tab === "executions" &&
      !invalidIdentity &&
      (filter === "all" || filter === keyOf(JOLTS) || joltsTarget),
  );
  const claimsRecords = useVisiblePolling(
    (signal) => readCpiExecutions(transport, signal, CLAIMS_PROFILE),
    [transport, generation, CLAIMS_PROFILE.id],
    newClaims &&
      tab === "executions" &&
      !invalidIdentity &&
      (filter === "all" || filter === keyOf(CLAIMS) || claimsTarget),
  );
  const macroRecords = [
    ...(executionSchedule === "all" &&
    (filter === "all" || filter === keyOf(M3))
      ? (m3Records.data?.executions ?? []).map((e) => ({
          ...e,
          profile: M3_PROFILE,
          rowId: `m3:${e.id}`,
        }))
      : []),
    ...(executionSchedule === "all" &&
    (filter === "all" || filter === keyOf(GDP))
      ? (gdpRecords.data?.executions ?? []).map((e) => ({
          ...e,
          profile: GDP_PROFILE,
          rowId: `gdp:${e.id}`,
        }))
      : []),
    ...(executionSchedule === "all" &&
    (filter === "all" || filter === keyOf(JOLTS))
      ? (joltsRecords.data?.executions ?? []).map((e) => ({
          ...e,
          profile: JOLTS_PROFILE,
          rowId: `jolts:${e.id}`,
        }))
      : []),
    ...(executionSchedule === "all" &&
    (filter === "all" || filter === keyOf(CLAIMS))
      ? (claimsRecords.data?.executions ?? []).map((e) => ({
          ...e,
          profile: CLAIMS_PROFILE,
          rowId: `claims:${e.id}`,
        }))
      : []),
    ...(executionSchedule === "all" &&
    (filter === "all" || filter === keyOf(CONSUMPTION))
      ? (consumptionRecords.data?.executions ?? []).map((e) => ({
          ...e,
          profile: CONSUMPTION_PROFILE,
          rowId: `consumption:${e.id}`,
        }))
      : []),
    ...((executionSchedule === "all" || executionSchedule === "retail-auto") &&
    (filter === "all" || filter === keyOf(RETAIL))
      ? (retailRecords.data?.executions ?? []).map((e) => ({
          ...e,
          profile: RETAIL_PROFILE,
          rowId: `retail:${e.id}`,
        }))
      : []),
    ...(executionSchedule === "all" &&
    (filter === "all" || filter === keyOf(EMPLOYMENT))
      ? (employmentRecords.data?.executions ?? []).map((e) => ({
          ...e,
          profile: EMPLOYMENT_PROFILE,
          rowId: `employment:${e.id}`,
        }))
      : []),
    ...(executionSchedule === "all" &&
    (filter === "all" || filter === keyOf(CPI))
      ? (cpiRecords.data?.executions ?? []).map((e) => ({
          ...e,
          profile: CPI_PROFILE,
          rowId: e.id,
        }))
      : []),
    ...(executionSchedule === "all" &&
    (filter === "all" || filter === keyOf(PCE))
      ? (pceRecords.data?.executions ?? []).map((e) => ({
          ...e,
          profile: PCE_PROFILE,
          rowId: `pce:${e.id}`,
        }))
      : []),
    ...(executionSchedule === "all" &&
    (filter === "all" || filter === keyOf(PPI))
      ? (ppiRecords.data?.executions ?? []).map((e) => ({
          ...e,
          profile: PPI_PROFILE,
          rowId: `ppi:${e.id}`,
        }))
      : []),
  ];
  const cpiExecution = macroRecords.find(
    (e) =>
      e.id === jobId &&
      (!route.fields.has("asset") ||
        (route.fields.get("asset") === e.profile.asset.identity &&
          route.fields.get("asset_revision") === e.profile.asset.revision)),
  );
  const rows = all.filter(
    (e) =>
      (filter === "all" || keyOf(e.asset) === filter) &&
      (executionSchedule === "all" || e.schedule === executionSchedule),
  );
  const execution = jobId ? all.find((e) => e.id === jobId) : undefined;
  const plan = plansMatch
    ? plans.data?.plans.find((p) => p.identity === scheduleId)
    : undefined;
  const planFields = {
    asset: planAsset?.identity ?? null,
    asset_revision: planAsset?.revision ?? null,
    operation: HISTORY_OPERATION,
  };
  function recordState(kind: "plans" | "jobs") {
    if (filter === keyOf(M3))
      return m3Records.failure
        ? "M3执行记录读取失败"
        : m3Records.busy
          ? "正在读取M3执行记录…"
          : "当前没有M3执行记录";
    if (filter === keyOf(JOLTS))
      return joltsRecords.failure
        ? "JOLTS执行记录读取失败"
        : joltsRecords.busy
          ? "正在读取JOLTS执行记录…"
          : "当前已查询范围没有JOLTS执行记录";
    if (filter === keyOf(CLAIMS))
      return claimsRecords.failure
        ? "美国初请与续请执行记录读取失败"
        : claimsRecords.busy
          ? "正在读取美国初请与续请执行记录…"
          : "当前没有美国初请与续请执行记录";
    if (filter === keyOf(GDP))
      return gdpRecords.failure
        ? "GDP执行记录读取失败，当前记录尚未核实。"
        : gdpRecords.busy
          ? "正在读取GDP执行记录…"
          : !newGdp
            ? "GDP服务状态尚未核实，执行记录尚未查询。"
            : "当前已查询范围没有GDP执行记录。";
    if (filter === keyOf(EMPLOYMENT))
      return employmentRecords.failure
        ? "就业执行记录读取失败。"
        : employmentRecords.busy
          ? "正在读取就业执行记录…"
          : "当前范围没有就业执行记录。";
    if (filter === keyOf(PCE)) {
      if (pceAvailability && !pceAvailability.legacy) {
        if (!newPce) return "PCE 服务状态尚未核实，执行记录尚未查询。";
        if (pceRecords.busy) return "正在读取 PCE 执行记录…";
        if (pceRecords.failure)
          return "PCE 执行记录读取失败，当前记录尚未核实。";
        if (!pceRecords.data) return "PCE 执行记录尚未查询。";
        return "当前已查询范围没有执行记录。";
      }
      if (kind === "plans") return "PCE 开发测试只支持单次采集。";
      if (pceJobs.busy) return "正在回查采集记录…";
      return pceJobs.failure
        ? "采集记录读取失败，记录是否存在尚未核实。"
        : "当前已查询范围没有执行记录。";
    }
    if (
      newCpi &&
      kind === "jobs" &&
      (filter === "all" || filter === keyOf(CPI))
    ) {
      if (cpiRecords.busy || ppiRecords.busy) return "正在读取 CPI 执行记录…";
      if (cpiRecords.failure) return "CPI 执行记录读取失败，当前记录尚未核实。";
      return "当前已查询范围没有执行记录。";
    }
    if (
      kind === "jobs" &&
      (filter === keyOf(PPI) || (filter === "all" && newPpi))
    ) {
      if (!newPpi) return "PPI 服务状态尚未核实，执行记录尚未查询。";
      if (ppiRecords.busy) return "正在读取 PPI 执行记录…";
      if (ppiRecords.failure) return "PPI 执行记录读取失败，当前记录尚未核实。";
      if (!ppiRecords.data) return "PPI 执行记录尚未查询。";
      return "当前已查询范围没有执行记录。";
    }
    const resources = kind === "plans" ? [plans] : [jobs, plans];
    if (!readRecords)
      return "未选择读取范围或此资产尚未绑定读取能力 / 尚未查询。";
    if (resources.some((r) => r.busy)) return "正在回查采集记录…";
    if (resources.some((r) => r.failure))
      return "采集记录读取失败，记录是否存在尚未核实。";
    if (resources.some((r) => r.data?.evidence_mode === "UNCONFIGURED"))
      return "读取范围未配置，记录是否存在尚未核实。";
    if (!plansMatch || (kind === "jobs" && !jobsMatch))
      return "读取范围或记录状态尚未核实。";
    return kind === "plans"
      ? "当前已查询范围暂无周期任务。"
      : "当前已查询范围没有执行记录。";
  }
  useEffect(() => {
    if (route.fields.get("mode") === "backfill") setMode("指定范围回补");
  }, [route.fields.get("mode"), jobId]);
  const backingJob = jobId
    ? all.find((e) => e.id === jobId && sameKey(e.asset, asset?.key))
    : undefined;
  useEffect(() => {
    rangeEdited.current = false;
  }, [mode, jobId]);
  useEffect(() => {
    if (
      mode === "指定范围回补" &&
      backingJob?.reference &&
      !rangeEdited.current
    ) {
      setStart(backingJob.reference);
      setEnd(backingJob.reference);
    }
  }, [mode, backingJob?.id, backingJob?.reference]);
  useEffect(() => {
    setConfirmed(false);
    setNotice(null);
    setUncertain(false);
  }, [asset && keyOf(asset.key), context?.source?.id, mode, jobId]);
  const matches =
    isCpi &&
    jobs.data?.asset_identity === asset?.key.identity &&
    jobs.data?.asset_revision === asset?.key.revision;
  const scopeMatches =
    matches &&
    jobs.data?.reference &&
    start === jobs.data.reference &&
    end === start;
  const sourceMatches = isPce
    ? pceBindingMatches
    : context?.source?.id === MANUAL_OPERATION;
  const devCandidate = jobs.data?.storage_target === "mdf_b3_cpi_05_dev_r1";
  const devBindingMatches =
    !devCandidate ||
    (jobs.data?.source_operation_ref === context?.source?.id &&
      jobs.data?.binding_ref ===
        "mdf.binding-candidate.bls-v2.cpi.release-2026-09@p4-release-reference-2026-09-r1");
  const registered = isPce
    ? !!pceJobs.data?.jobs.length
    : devCandidate && !!jobs.data?.jobs.length;
  const pceBackfill = pceJobs.data?.backfill;
  const pceBackfillMode = isPce && mode === "指定范围回补";
  const cpiBackfill = jobs.data?.backfill;
  const cpiBackfillMode =
    isCpi &&
    mode === "指定范围回补" &&
    jobId === cpiBackfill?.submission?.parent_job_id;
  const successorOf = (id: string) =>
    all.find((e) => (e.pce?.parent_job_id ?? e.dto?.parent_job_id) === id);
  const successor = backingJob ? successorOf(backingJob.id) : undefined;
  const devIntent = isPce
    ? pceBackfillMode
      ? pceBackfill?.submission.job_identity
      : pceCatalog?.submission.job_identity
    : cpiBackfillMode
      ? cpiBackfill?.submission?.job_identity
      : devCandidate && sourceMatches
        ? jobs.data?.intent_identity
        : null;
  useEffect(() => {
    if (
      !isPce ||
      !devIntent ||
      !pceJobs.data?.jobs.some((j) => j.job_id === devIntent)
    )
      return;
    reservedIntents.current.add(devIntent);
    try {
      const intents = readDevIntents();
      if (!intents.includes(devIntent))
        sessionStorage.setItem(
          devIntentKey,
          JSON.stringify([...intents, devIntent]),
        );
    } catch {
      /* In-memory reservation and durable Job still prevent submission. */
    }
  }, [isPce, devIntent, pceJobs.data]);
  let devIntentLocked = false;
  if (devIntent) {
    try {
      devIntentLocked =
        (isPce &&
          !pceBackfillMode &&
          sessionStorage.getItem("mdf-pce-202607-submission-pending") !==
            null) ||
        reservedIntents.current.has(devIntent) ||
        readDevIntents().includes(devIntent);
    } catch {
      devIntentLocked = true;
    }
  }
  const pceScopeMatches =
    start === PCE_REFERENCE &&
    end === start &&
    (!route.fields.has("reference") ||
      route.fields.get("reference") === PCE_REFERENCE);
  const pceCanManual = !!(
    pceBindingMatches &&
    pceScopeMatches &&
    pceCatalog?.write_eligible &&
    pceJobs.data &&
    !registered &&
    pceCatalog.submission.job_identity
  );
  const canManual = isPce
    ? pceCanManual
    : !!(
        scopeMatches &&
        sourceMatches &&
        devBindingMatches &&
        !registered &&
        jobs.data?.status === "OK" &&
        jobs.data.write_eligible &&
        jobs.data.operator_identity &&
        jobs.data.intent_identity &&
        jobs.data.release_slot
      );
  // Backfill requires original failed history child and the server's existing range handler.
  const parent = plans.data?.plans.find((p) =>
    p.batches.some((b) => b.jobs.some((j) => j.job_identity === jobId)),
  );
  const pceBackfillScope = !!(
    pceBindingMatches &&
    pceScopeMatches &&
    pceCatalog?.write_eligible &&
    pceBackfill?.eligible &&
    backingJob?.state === "failed" &&
    backingJob.pce &&
    !backingJob.pce.parent_job_id &&
    validPceBinding(backingJob.pce.binding) &&
    validPceBinding(pceBackfill.submission.binding) &&
    pceBackfill.submission.reference === PCE_REFERENCE &&
    pceBackfill.submission.parent_job_id === backingJob.id &&
    backingJob.id === pceCatalog.submission.job_identity &&
    pceBackfill.submission.job_identity !== backingJob.id
  );
  const cpiBackfillScope = !!(
    cpiBackfillMode &&
    cpiBackfill?.eligible &&
    cpiBackfill.submission &&
    scopeMatches &&
    sourceMatches &&
    devBindingMatches &&
    backingJob?.state === "failed" &&
    !backingJob.dto?.parent_job_id &&
    cpiBackfill.submission.job_identity !== backingJob.id
  );
  const backfillScope = isPce
    ? pceBackfillScope
    : cpiBackfillMode
      ? cpiBackfillScope
      : !!(
          parent &&
          backingJob?.state === "failed" &&
          start === backingJob.reference &&
          end === start &&
          plans.data?.write_eligible &&
          plans.data.asset_identity === asset?.key.identity &&
          plans.data.asset_revision === asset?.key.revision &&
          context?.source?.dto.component_identity.endsWith("cpi_history")
        );
  const block = !asset
    ? "请先选择已登记资产。"
    : invalidIdentity
      ? "资产 / 修订 / 来源关联不匹配，未选择其他目标。"
      : !context?.source
        ? "请返回资产详情，明确选择来源变体。"
        : mode === "周期采集"
          ? "当前后端仅支持有界固定间隔计划，不能准确实现日 / 周 / 月 / 年日历规则；保存已阻止。"
          : mode === "指定范围回补"
            ? successor
              ? `关联回补任务已登记：${stateLabel(successor.state)}。请查看该任务及其结果，不会再创建后继。`
              : backingJob?.state === "outcome_unknown"
                ? "原执行效果未知，必须先核实，禁止盲目重试。"
                : (isPce || cpiBackfillMode) && devIntentLocked
                  ? "此回补意图已预留，请回查同一任务；禁止重复提交。"
                  : cpiBackfillMode && !cpiBackfillScope
                    ? backfillReason(
                        cpiBackfill?.reason ??
                          (scopeMatches
                            ? "binding_mismatch"
                            : "reference_mismatch"),
                      )
                    : isPce && !pceCatalog?.write_eligible
                      ? "当前为只读预览，请在开发测试服务提交。"
                      : isPce && !pceScopeMatches
                        ? "开发测试仅支持 2026-07；开始与结束须相同。"
                        : isPce && !pceBindingMatches
                          ? "来源与 PCE 绑定不匹配，当前不能提交。"
                          : isPce && pceBackfill?.reason
                            ? backfillReason(pceBackfill.reason)
                            : !backfillScope
                              ? "请选择同一资产已失败的历史执行及其原始参考月；当前范围回补处理器不支持其他记录。"
                              : null
            : isPce
              ? !pceBindingMatches
                ? "来源与 PCE 绑定不匹配，当前不能提交。"
                : !pceScopeMatches
                  ? "开发测试仅支持 2026-07；开始与结束须相同。"
                  : registered
                    ? "此采集已登记，请回查执行记录；不会创建第二次执行。"
                    : devIntentLocked
                      ? "此意图已预留或提交保护不可用，请回查同一任务；禁止重复提交。"
                      : !pceCatalog?.write_eligible
                        ? "当前为只读预览，请在开发测试服务提交。"
                        : !pceJobs.data
                          ? "持久任务状态尚未核实，请回查后再提交。"
                          : null
              : !sourceMatches
                ? "所选来源变体未绑定此人工执行器，不会切换到其他 CPI 来源。"
                : registered
                  ? "此 DEV 候选已登记，请回查执行记录；不会创建第二次执行。"
                  : devIntentLocked
                    ? "此意图已预留或提交保护不可用，请回查同一任务；禁止重复提交。"
                    : !devBindingMatches
                      ? "来源与 DEV 目标绑定不匹配，当前不能提交。"
                      : !matches ||
                          !jobs.data?.write_eligible ||
                          !jobs.data.operator_identity
                        ? "未绑定执行器或可信操作者，当前不能提交。"
                        : !scopeMatches
                          ? `此处理器仅支持 ${jobs.data?.reference ?? "已准入"} 参考月；开始与结束须相同。`
                          : null;
  async function submit() {
    if (
      writing.current ||
      !confirmed ||
      uncertain ||
      devIntentLocked ||
      block ||
      !(mode === "一次性初始化"
        ? canManual
        : mode === "指定范围回补" && backfillScope)
    )
      return;
    writing.current = true;
    setBusy(true);
    setNotice(null);
    try {
      let acceptedJob: string | null = null;
      if (mode === "一次性初始化") {
        if (devIntent) {
          // Reserve before any possible effect, including an unmount while the
          // POST is pending. Empty/stale GETs never authorize another POST.
          const intents = readDevIntents();
          if (intents.includes(devIntent))
            throw new Error("intent_already_reserved");
          reservedIntents.current.add(devIntent);
          const stored = JSON.stringify([...intents, devIntent]);
          sessionStorage.setItem(devIntentKey, stored);
          if (sessionStorage.getItem(devIntentKey) !== stored)
            throw new Error("intent_reservation_unverified");
          // Retain even after DURABLE: accepted intents are read-only. Only a
          // different named Host intent may submit; no new retry protocol.
        }
        if (isPce) {
          const receipt = await pceRequest<PceJob>(transport, "/api/pce/jobs", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(pceCatalog!.submission),
          });
          if (
            receipt.job_id !== devIntent ||
            !validPceBinding(receipt.binding) ||
            ![
              "pending",
              "running",
              "succeeded",
              "failed",
              "outcome_unknown",
              "cancelled",
            ].includes(receipt.state)
          )
            throw new Error("pce_receipt_identity_mismatch");
          acceptedJob = receipt.job_id;
        } else {
          const receipt = await submitJob(transport, "/api/r5/cpi/jobs", {
            job_identity: jobs.data!.intent_identity!,
            asset_identity: asset!.key.identity,
            asset_revision: asset!.key.revision,
            reference: start,
            release_slot: jobs.data!.release_slot!,
            mode: "manual_once",
            ...(devCandidate
              ? {
                  source_operation_ref: context!.source!.id,
                  binding_ref: jobs.data!.binding_ref!,
                  target_schema: jobs.data!.storage_target,
                  confirmed: true,
                }
              : {}),
          });
          if (receipt.job_identity !== jobs.data!.intent_identity)
            throw new Error("receipt_identity_mismatch");
          acceptedJob = receipt.job_identity;
        }
      } else if (cpiBackfillMode) {
        const intent = cpiBackfill!.submission!;
        const intents = readDevIntents();
        if (intents.includes(intent.job_identity))
          throw new Error("intent_already_reserved");
        reservedIntents.current.add(intent.job_identity);
        const stored = JSON.stringify([...intents, intent.job_identity]);
        sessionStorage.setItem(devIntentKey, stored);
        if (sessionStorage.getItem(devIntentKey) !== stored)
          throw new Error("intent_reservation_unverified");
        const receipt = await submitJob(
          transport,
          "/api/r5/cpi/jobs/backfill",
          intent,
        );
        if (receipt.job_identity !== intent.job_identity)
          throw new Error("receipt_identity_mismatch");
        acceptedJob = receipt.job_identity;
      } else if (pceBackfillMode) {
        const intent = pceBackfill!.submission;
        const intents = readDevIntents();
        if (intents.includes(intent.job_identity))
          throw new Error("intent_already_reserved");
        reservedIntents.current.add(intent.job_identity);
        const stored = JSON.stringify([...intents, intent.job_identity]);
        sessionStorage.setItem(devIntentKey, stored);
        if (sessionStorage.getItem(devIntentKey) !== stored)
          throw new Error("intent_reservation_unverified");
        const receipt = await pceRequest<PceJob>(
          transport,
          "/api/pce/jobs/backfill",
          {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(intent),
          },
        );
        if (
          receipt.job_id !== intent.job_identity ||
          receipt.parent_job_id !== intent.parent_job_id ||
          !validPceBinding(receipt.binding) ||
          ![
            "pending",
            "running",
            "succeeded",
            "failed",
            "outcome_unknown",
            "cancelled",
          ].includes(receipt.state)
        )
          throw new Error("pce_receipt_identity_mismatch");
        acceptedJob = receipt.job_id;
      } else {
        const response = await transport(
          `/api/r5/cpi/plans/${parent!.identity}/backfill`,
          {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ start, end }),
          },
        );
        const receipt = await response.json();
        if (
          !response.ok ||
          receipt.status !== "OK" ||
          receipt.acceptance !== "DURABLE" ||
          receipt.plan_identity !== parent!.identity
        )
          throw new Error("invalid_receipt");
      }
      if (active.current) {
        setNotice("意图已持久接收，执行结果以独立回查为准。");
        setConfirmed(false);
        setRefresh((n) => n + 1);
        navigate(
          "collection",
          {
            tab: "executions",
            job: acceptedJob,
            schedule: acceptedJob ? null : parent!.identity,
            execution_schedule: acceptedJob ? "all" : parent!.identity,
          },
          true,
        );
      }
    } catch (e) {
      if (active.current) {
        if (!devIntent) setUncertain(true);
        setNotice(
          `提交回执未核实，请回查同一任务；不会自动重试。${failureName(e)}`,
        );
      }
    } finally {
      writing.current = false;
      if (active.current) setBusy(false);
    }
  }
  const fields = {
    asset: asset?.key.identity ?? null,
    asset_revision: asset?.key.revision ?? null,
    operation: context?.source?.id ?? null,
  };
  const calendar = `${frequency === "daily" ? "每天" : frequency === "weekly" ? `每周 ${day}` : frequency === "monthly" ? `每月 ${day} 日` : `每年 ${month} 月 ${day} 日`} ${time}（${zone}）`;
  return (
    <>
      <Tabs
        label="采集任务页签"
        values={tabs}
        value={tab}
        onChange={(tab) => navigate("collection", { tab }, true)}
      />
      {tab !== "new" && (
        <div className="toolbar">
          <label>
            {tab === "periodic" ? "周期资产筛选" : "执行资产筛选"}
            <select
              value={filter}
              onChange={(e) =>
                navigate(
                  "collection",
                  { [filterField]: e.target.value, job: null },
                  true,
                )
              }
            >
              <option value="all">全部资产</option>
              {!assets.some((a) => sameKey(a.key, CLAIMS)) && (
                <option value={keyOf(CLAIMS)}>{assetTitle(CLAIMS)}</option>
              )}
              {!assets.some((a) => sameKey(a.key, GDP)) && (
                <option value={keyOf(GDP)}>{assetTitle(GDP)}</option>
              )}
              {!assets.some((a) => sameKey(a.key, EMPLOYMENT)) &&
                newEmployment && (
                  <option value={keyOf(EMPLOYMENT)}>
                    {assetTitle(EMPLOYMENT)}
                  </option>
                )}
              {!assets.some((a) => sameKey(a.key, PPI)) && (
                <option value={keyOf(PPI)}>{assetTitle(PPI)}</option>
              )}
              {!assets.some((a) => sameKey(a.key, PCE)) && (
                <option value={keyOf(PCE)}>{assetTitle(PCE)}</option>
              )}
              {assets.map((a) => (
                <option key={keyOf(a.key)} value={keyOf(a.key)}>
                  {assetTitle(a.key)}
                </option>
              ))}
              {filter !== "all" &&
                !assets.some((a) => keyOf(a.key) === filter) &&
                filter !== keyOf(GDP) &&
                filter !== keyOf(CLAIMS) &&
                filter !== keyOf(PPI) &&
                filter !== keyOf(PCE) &&
                !(filter === keyOf(EMPLOYMENT) && newEmployment) && (
                  <option value={filter}>{filter}（不可核实）</option>
                )}
            </select>
          </label>
          <button
            onClick={() =>
              navigate(
                "collection",
                {
                  [filterField]: "all",
                  ...(tab === "executions"
                    ? { execution_schedule: "all" }
                    : {}),
                },
                true,
              )
            }
          >
            清除筛选
          </button>
          {tab === "executions" && executionSchedule !== "all" && (
            <p>计划筛选：{executionSchedule}</p>
          )}
        </div>
      )}
      {notice && (
        <p role="status" className="inline-notice">
          {notice}
        </p>
      )}
      {((readRecords && (jobs.failure || plans.failure)) ||
        (readPceRecords && pceJobs.failure) ||
        cpiRecords.failure ||
        ppiRecords.failure ||
        pceRecords.failure ||
        employmentRecords.failure ||
        retailRecords.failure ||
        consumptionRecords.failure ||
        gdpRecords.failure ||
        claimsRecords.failure ||
        joltsRecords.failure) && (
        <div role="alert">
          采集记录已陈旧，保留上次数据并自动重读。
          {claimsRecords.failure && (
            <p>美国初请与续请执行记录读取失败，当前结果尚未核实。</p>
          )}
          {gdpRecords.failure && <p>GDP执行记录读取失败，当前结果尚未核实。</p>}
          {ppiRecords.failure && (
            <p>PPI 执行记录读取失败，当前记录尚未核实。</p>
          )}
          {pceRecords.failure && (
            <p>PCE 执行记录读取失败，当前结果尚未核实。</p>
          )}
          {cpiRecords.failure && (
            <p>CPI 执行记录读取失败，当前结果尚未核实。</p>
          )}
          {readPceRecords && pceJobs.failure && (
            <p>PCE 执行记录读取失败，当前结果尚未核实。</p>
          )}
          {readRecords && (jobs.failure || plans.failure) && (
            <p>CPI 既有采集记录读取失败，当前结果尚未核实。</p>
          )}
        </div>
      )}
      {tab === "new" &&
      (newMacro || macroPending) &&
      (cpiTarget ||
        isPpi ||
        pceTarget ||
        employmentTarget ||
        retailTarget ||
        consumptionTarget ||
        gdpTarget ||
        joltsTarget ||
        m3Target ||
        claimsTarget) ? (
        <>
          <label>
            采集目标
            <select
              value={selectedTarget}
              onChange={(e) => {
                const selected = assets.find(
                  (a) => keyOf(a.key) === e.target.value,
                );
                const selectedKey =
                  e.target.value === keyOf(M3)
                    ? M3
                    : e.target.value === keyOf(JOLTS)
                      ? JOLTS
                      : e.target.value === keyOf(CLAIMS)
                        ? CLAIMS
                        : e.target.value === keyOf(GDP)
                          ? GDP
                          : e.target.value === keyOf(CONSUMPTION)
                            ? CONSUMPTION
                            : e.target.value === keyOf(RETAIL)
                              ? RETAIL
                              : e.target.value === keyOf(EMPLOYMENT)
                                ? EMPLOYMENT
                                : e.target.value === keyOf(PCE)
                                  ? PCE
                                  : e.target.value === keyOf(PPI)
                                    ? PPI
                                    : e.target.value === keyOf(CPI)
                                      ? CPI
                                      : selected?.key;
                navigate("collection", {
                  asset: selectedKey?.identity ?? null,
                  asset_revision: selectedKey?.revision ?? null,
                  operation: null,
                  tab: "new",
                });
              }}
            >
              {!assets.some((a) => sameKey(a.key, CLAIMS)) &&
                macroProfile.id !== "claims" && (
                  <option value={keyOf(CLAIMS)}>{assetTitle(CLAIMS)}</option>
                )}
              {!assets.some((a) => sameKey(a.key, GDP)) &&
                macroProfile.id !== "gdp" && (
                  <option value={keyOf(GDP)}>{assetTitle(GDP)}</option>
                )}
              {!assets.some((a) => sameKey(a.key, macroProfile.asset)) && (
                <option value={keyOf(macroProfile.asset)}>
                  {macroAssetLabel(macroProfile)}
                </option>
              )}
              {!assets.some((a) => sameKey(a.key, EMPLOYMENT)) &&
                macroProfile.id !== "employment" &&
                newEmployment && (
                  <option value={keyOf(EMPLOYMENT)}>
                    {assetTitle(EMPLOYMENT)}
                  </option>
                )}
              {!assets.some((a) => sameKey(a.key, CPI)) &&
                macroProfile.id !== "cpi" && (
                  <option value={keyOf(CPI)}>{assetTitle(CPI)}</option>
                )}
              {!assets.some((a) => sameKey(a.key, PPI)) &&
                macroProfile.id !== "ppi" && (
                  <option value={keyOf(PPI)}>{assetTitle(PPI)}</option>
                )}
              {!assets.some((a) => sameKey(a.key, PCE)) &&
                macroProfile.id !== "pce" && (
                  <option value={keyOf(PCE)}>{assetTitle(PCE)}</option>
                )}
              {assets.map((a) => (
                <option key={keyOf(a.key)} value={keyOf(a.key)}>
                  {assetTitle(a.key)}
                </option>
              ))}
            </select>
          </label>
          {macroPending ? (
            <p role="status">正在核实 {macroProfile.label} 采集服务…</p>
          ) : (
            <CpiTimeSeriesCollection
              profile={macroProfile}
              transport={transport}
              availability={macroAvailability!}
              tab={tab}
              generation={generation}
              onExecution={(job) => {
                setBriefTarget(job);
                navigate("collection", {
                  tab: "executions",
                  asset: macroProfile.asset.identity,
                  asset_revision: macroProfile.asset.revision,
                  operation: null,
                  execution_asset: keyOf(macroProfile.asset),
                  execution_schedule: "all",
                  schedule: null,
                  mode: null,
                  job,
                });
              }}
            />
          )}
        </>
      ) : tab === "new" ? (
        <div className="workbench collection-form">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (!e.currentTarget.checkValidity()) return;
              void submit();
            }}
          >
            <h2>新建采集</h2>
            {isPce && (
              <p className="muted">
                BEA PCE 开发测试，仅支持 2026-07。登记后由 Host 单次执行。
              </p>
            )}
            <div className="field-grid">
              <label>
                采集目标
                <select
                  value={selectedTarget}
                  onChange={(e) => {
                    const a = assets.find(
                      (a) => keyOf(a.key) === e.target.value,
                    );
                    const selectedKey =
                      e.target.value === keyOf(CLAIMS)
                        ? CLAIMS
                        : e.target.value === keyOf(GDP)
                          ? GDP
                          : e.target.value === keyOf(EMPLOYMENT)
                            ? EMPLOYMENT
                            : e.target.value === keyOf(PCE)
                              ? PCE
                              : e.target.value === keyOf(PPI)
                                ? PPI
                                : e.target.value === keyOf(CPI)
                                  ? CPI
                                  : a?.key;
                    navigate("collection", {
                      asset: selectedKey?.identity ?? null,
                      asset_revision: selectedKey?.revision ?? null,
                      operation: null,
                      tab: "new",
                    });
                  }}
                >
                  <option value="">选择一个已登记资产</option>
                  {!assets.some((a) => sameKey(a.key, CLAIMS)) && (
                    <option value={keyOf(CLAIMS)}>{assetTitle(CLAIMS)}</option>
                  )}
                  {!assets.some((a) => sameKey(a.key, GDP)) && (
                    <option value={keyOf(GDP)}>{assetTitle(GDP)}</option>
                  )}
                  {!assets.some((a) => sameKey(a.key, EMPLOYMENT)) && (
                    <option value={keyOf(EMPLOYMENT)}>
                      {assetTitle(EMPLOYMENT)}
                    </option>
                  )}
                  {!assets.some((a) => sameKey(a.key, CPI)) && (
                    <option value={keyOf(CPI)}>{assetTitle(CPI)}</option>
                  )}
                  {!assets.some((a) => sameKey(a.key, PPI)) && (
                    <option value={keyOf(PPI)}>{assetTitle(PPI)}</option>
                  )}
                  {!assets.some((a) => sameKey(a.key, PCE)) && (
                    <option value={keyOf(PCE)}>{assetTitle(PCE)}</option>
                  )}
                  {assets.map((a) => (
                    <option key={keyOf(a.key)} value={keyOf(a.key)}>
                      {assetTitle(a.key)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                关联数据源 / Operation
                <output>
                  {context?.source
                    ? `${context.source.source} · ${context.source.variant} · ${context.source.dto.operation}`
                    : "未选择具名来源变体"}
                </output>
              </label>
            </div>
            <div className="readonly">
              <b>存储策略</b>
              <p>{context?.storage ?? "继承资产配置，尚未选择资产"}</p>
              <button type="button" onClick={() => navigate("assets", fields)}>
                查看资产 →
              </button>
            </div>
            <h4>采集方式</h4>
            <div className="mode-pills">
              {modes.map((m) => (
                <button
                  key={m}
                  type="button"
                  aria-pressed={mode === m}
                  onClick={() => setMode(m)}
                >
                  {m}
                </button>
              ))}
            </div>
            {mode === "周期采集" ? (
              <>
                <div className="field-grid">
                  <label>
                    执行周期
                    <select
                      value={frequency}
                      onChange={(e) => setFrequency(e.target.value)}
                    >
                      {[
                        ["daily", "每天"],
                        ["weekly", "每周"],
                        ["monthly", "每月"],
                        ["yearly", "每年"],
                      ].map(([v, l]) => (
                        <option key={v} value={v}>
                          {l}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    时区
                    <select
                      value={zone}
                      onChange={(e) => setZone(e.target.value)}
                    >
                      <option value="Asia/Shanghai">
                        北京时间（Asia/Shanghai）
                      </option>
                    </select>
                  </label>
                  {frequency === "yearly" && (
                    <label>
                      月份
                      <input
                        type="number"
                        min="1"
                        max="12"
                        value={month}
                        onChange={(e) => setMonth(e.target.value)}
                      />
                    </label>
                  )}
                  {frequency !== "daily" && (
                    <label>
                      {frequency === "weekly" ? "星期（1–7）" : "几号触发"}
                      <input
                        type="number"
                        min="1"
                        max={frequency === "weekly" ? 7 : 31}
                        value={day}
                        onChange={(e) => setDay(e.target.value)}
                      />
                    </label>
                  )}
                  <label>
                    具体执行时间（北京时间）
                    <input
                      type="text"
                      inputMode="numeric"
                      placeholder="HH:mm"
                      pattern="(?:[01][0-9]|2[0-3]):[0-5][0-9]"
                      value={time}
                      onChange={(e) => setTime(e.target.value)}
                    />
                  </label>
                </div>
                <p className="readonly">
                  规则预览：{calendar}；缺少对应日期时跳过该次触发。
                </p>
              </>
            ) : (
              <>
                {mode === "指定范围回补" && (
                  <label>
                    关联失败 / 异常执行
                    <select
                      value={jobId ?? ""}
                      onChange={(e) => {
                        const selected = all.find(
                          (job) =>
                            job.id === e.target.value &&
                            sameKey(job.asset, asset?.key),
                        );
                        // Choosing a parent explicitly chooses its existing binding;
                        // never substitute another source from the asset directory.
                        rangeEdited.current = false;
                        setStart(selected?.reference ?? "");
                        setEnd(selected?.reference ?? "");
                        setConfirmed(false);
                        navigate(
                          "collection",
                          {
                            job: selected?.id ?? null,
                            operation: selected?.operation ?? null,
                            reference: selected?.reference ?? null,
                            ...(selected ? {} : { as_of: null }),
                            tab: "new",
                            mode: "backfill",
                          },
                          true,
                        );
                      }}
                    >
                      <option value="">选择原始执行记录</option>
                      {all
                        .filter(
                          (e) =>
                            sameKey(e.asset, asset?.key) &&
                            (isPce
                              ? e.id ===
                                  pceBackfill?.submission.parent_job_id &&
                                e.state === "failed" &&
                                !e.pce?.parent_job_id
                              : ["failed", "outcome_unknown"].includes(
                                  e.state,
                                )),
                        )
                        .map((e) => (
                          <option key={e.id} value={e.id}>
                            {e.id} · {stateLabel(e.state)}
                          </option>
                        ))}
                    </select>
                  </label>
                )}
                <div className="field-grid">
                  <DateField
                    label="开始参考月"
                    type="month"
                    min={isPce ? PCE_REFERENCE : undefined}
                    max={isPce ? PCE_REFERENCE : undefined}
                    value={start}
                    onChange={(value) => {
                      rangeEdited.current = true;
                      setConfirmed(false);
                      setStart(value);
                    }}
                  />
                  <DateField
                    label="结束参考月"
                    type="month"
                    min={isPce ? PCE_REFERENCE : undefined}
                    max={isPce ? PCE_REFERENCE : undefined}
                    value={end}
                    onChange={(value) => {
                      rangeEdited.current = true;
                      setConfirmed(false);
                      setEnd(value);
                    }}
                  />
                </div>
                <p className="muted">
                  Macro 按已登记参考期限定范围，不将任意自然日转换为可执行窗口。
                </p>
              </>
            )}
            {block && (
              <p role="status" className="availability">
                {block}
              </p>
            )}
            {successor && mode === "指定范围回补" && (
              <Successor execution={successor} navigate={navigate} />
            )}
            <label className="confirm">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              我明确提交此目标的采集意图
            </label>
            <div className="actions">
              <button
                className="primary"
                disabled={
                  !!block ||
                  !confirmed ||
                  busy ||
                  uncertain ||
                  !(mode === "一次性初始化" ? canManual : backfillScope)
                }
              >
                {mode === "周期采集" ? "保存周期任务" : "创建采集任务"}
              </button>
              <button
                type="button"
                onClick={() => {
                  rangeEdited.current = false;
                  setStart(isPce ? PCE_REFERENCE : "");
                  setEnd(isPce ? PCE_REFERENCE : "");
                  setMode("一次性初始化");
                  setConfirmed(false);
                }}
              >
                重置表单
              </button>
              {(uncertain || devIntentLocked) && (
                <button
                  type="button"
                  onClick={() =>
                    navigate(
                      "collection",
                      {
                        tab: "executions",
                        job: devIntent ?? null,
                      },
                      true,
                    )
                  }
                >
                  回查同一任务 →
                </button>
              )}
            </div>
          </form>
          <aside className="detail-pane" aria-label="本次采集摘要">
            <h3>本次采集摘要</h3>
            <dl>
              <Fact name="采集目标">{asset?.label ?? "未选择"}</Fact>
              <Fact name="来源变体">
                {context?.source
                  ? `${context.source.source} · ${context.source.variant}`
                  : "未选择"}
              </Fact>
              <Fact name="采集方式">{mode}</Fact>
              <Fact name={mode === "周期采集" ? "执行规则" : "范围"}>
                {mode === "周期采集"
                  ? calendar
                  : `${start || "未设置"} 至 ${end || "未设置"}`}
              </Fact>
              <Fact name="存储目标">{context?.storage ?? "继承资产配置"}</Fact>
              {devCandidate && (
                <Fact name="DEV 写入目标">
                  mdf_b3_cpi_05_dev_r1 · CPI 2026-09 · 单次执行
                </Fact>
              )}
              <Fact name="任务接收状态">尚未提交 / 以持久回执为准</Fact>
            </dl>
            <p className="muted">
              一次性与回补提交后进入执行记录；周期规则需后端准确支持后才能保存。
            </p>
            <Technical>
              <p>{asset && keyOf(asset.key)}</p>
              <p>{context?.source?.id}</p>
              <p>
                {jobs.data?.evidence_mode} · {jobs.data?.classification}
              </p>
              <p>
                Operator：
                {(isPce
                  ? pceCatalog?.binding.operator
                  : jobs.data?.operator_identity) ?? "未绑定"}
              </p>
              <p>
                存储：
                {(isPce
                  ? pceCatalog?.binding.target.schema
                  : jobs.data?.storage_target) ?? "未核实"}
              </p>
              {!isPce && (
                <p>release slot：{jobs.data?.release_slot ?? "未提供"}</p>
              )}
              {isPce && (
                <p>
                  {JSON.stringify(pceCatalog?.binding)} · source_qualified=false
                </p>
              )}
            </Technical>
          </aside>
        </div>
      ) : tab === "periodic" &&
        (newCpi ||
          newPpi ||
          newPce ||
          newEmployment ||
          newRetail ||
          newConsumption ||
          newM3 ||
          (filter === keyOf(M3) && m3Availability) ||
          newJolts ||
          (filter === keyOf(JOLTS) && joltsAvailability) ||
          newClaims ||
          (filter === keyOf(CLAIMS) && claimsAvailability) ||
          newGdp ||
          (filter === keyOf(GDP) && gdpAvailability) ||
          (filter === keyOf(RETAIL) && retailAvailability) ||
          (filter === keyOf(CONSUMPTION) && consumptionAvailability)) ? (
        <CpiStrategyWorkbench
          transport={transport}
          tenthProfile={
            (newM3 || m3Target || filter === keyOf(M3)) && m3Availability
              ? M3_PROFILE
              : undefined
          }
          tenthAvailability={m3Availability}
          tenthVisible={
            !invalidIdentity && (filter === "all" || filter === keyOf(M3))
          }
          ninthProfile={
            (newJolts || joltsTarget || filter === keyOf(JOLTS)) &&
            joltsAvailability
              ? JOLTS_PROFILE
              : undefined
          }
          ninthAvailability={joltsAvailability}
          ninthVisible={
            !invalidIdentity && (filter === "all" || filter === keyOf(JOLTS))
          }
          eighthProfile={
            (newClaims || claimsTarget || filter === keyOf(CLAIMS)) &&
            claimsAvailability
              ? CLAIMS_PROFILE
              : undefined
          }
          eighthAvailability={claimsAvailability}
          eighthVisible={
            !invalidIdentity && (filter === "all" || filter === keyOf(CLAIMS))
          }
          seventhProfile={
            (newGdp || gdpTarget || filter === keyOf(GDP)) && gdpAvailability
              ? GDP_PROFILE
              : undefined
          }
          seventhAvailability={gdpAvailability}
          seventhVisible={
            !invalidIdentity && (filter === "all" || filter === keyOf(GDP))
          }
          availability={
            cpiAvailability ?? {
              data: null,
              legacy: false,
              busy: false,
              failure: null,
            }
          }
          additionalProfile={ppiAvailability ? PPI_PROFILE : undefined}
          additionalAvailability={ppiAvailability}
          additionalVisible={
            !invalidIdentity && (filter === "all" || filter === keyOf(PPI))
          }
          thirdProfile={
            pceAvailability && !pceAvailability.legacy ? PCE_PROFILE : undefined
          }
          thirdAvailability={pceAvailability}
          thirdVisible={
            !invalidIdentity && (filter === "all" || filter === keyOf(PCE))
          }
          fifthProfile={
            (newRetail || filter === keyOf(RETAIL)) && retailAvailability
              ? RETAIL_PROFILE
              : undefined
          }
          fifthAvailability={retailAvailability}
          sixthProfile={
            (newConsumption ||
              consumptionTarget ||
              filter === keyOf(CONSUMPTION)) &&
            consumptionAvailability
              ? CONSUMPTION_PROFILE
              : undefined
          }
          sixthAvailability={consumptionAvailability}
          sixthVisible={filter === "all" || filter === keyOf(CONSUMPTION)}
          fifthVisible={
            !invalidIdentity && (filter === "all" || filter === keyOf(RETAIL))
          }
          fourthProfile={newEmployment ? EMPLOYMENT_PROFILE : undefined}
          fourthAvailability={employmentAvailability}
          fourthVisible={
            !invalidIdentity &&
            (filter === "all" || filter === keyOf(EMPLOYMENT))
          }
          selectedId={scheduleId}
          generation={generation}
          visible={
            !invalidIdentity && (filter === "all" || filter === keyOf(CPI))
          }
          selected={scheduleId === "cpi-auto"}
          onSelect={(id) => navigate("collection", { schedule: id }, true)}
        />
      ) : tab === "periodic" ? (
        <>
          <div className="toolbar">
            <h2>当前设定的周期任务</h2>
          </div>
          <WorkflowTable
            label="周期任务"
            columns={["计划", "采集目标", "规则", "状态"]}
            empty={recordState("plans")}
            selected={plan?.identity}
            onSelect={(schedule) =>
              navigate(
                "collection",
                { ...planFields, schedule, job: null },
                true,
              )
            }
            rows={(plans.data?.plans ?? [])
              .filter(
                () =>
                  plansMatch &&
                  (filter === "all" || keyOf(planAsset!) === filter),
              )
              .map((p) => ({
                id: p.identity,
                search: `${p.identity} ${assetTitle(planAsset!)}`,
                cells: [
                  p.spec.name,
                  assetTitle(planAsset!),
                  `既有有界间隔计划 · ${p.spec.timezone}`,
                  stateLabel(p.state),
                ],
              }))}
          >
            {plan && (
              <>
                <h3>{plan.spec.name}</h3>
                <dl>
                  <Fact name="计划身份">{plan.identity}</Fact>
                  <Fact name="采集目标">{assetTitle(planAsset!)}</Fact>
                  <Fact name="参考期范围">
                    {plan.spec.start} 至 {plan.spec.end}
                  </Fact>
                  <Fact name="完成批次">
                    {plan.completed_batches} / {plan.total_batches}
                  </Fact>
                  <Fact name="下次到期">
                    {formatBeijingTime(plan.next_due)}
                  </Fact>
                </dl>
                <button
                  onClick={() =>
                    navigate(
                      "collection",
                      {
                        ...planFields,
                        tab: "executions",
                        job: null,
                        execution_schedule: plan!.identity,
                      },
                      true,
                    )
                  }
                >
                  查看执行记录 →
                </button>
                <button onClick={() => navigate("assets", planFields)}>
                  查看关联资产 →
                </button>
              </>
            )}
          </WorkflowTable>
        </>
      ) : (
        <>
          <div className="toolbar">
            <h2>采集执行记录</h2>
            {!newCpi && !newPpi && !newPce && !newEmployment && !newRetail && (
              <button onClick={() => setRefresh((n) => n + 1)}>
                回查执行记录
              </button>
            )}
            {executionSchedule !== "all" && (
              <button
                onClick={() =>
                  navigate("collection", { execution_schedule: "all" }, true)
                }
              >
                全部计划
              </button>
            )}
          </div>
          <WorkflowTable
            label="执行记录"
            className="execution-records"
            columns={["任务名称", "创建时间（北京时间）", "采集范围", "结果"]}
            pageSize={20}
            locateId={
              (m3Target ||
                joltsTarget ||
                claimsTarget ||
                gdpTarget ||
                consumptionTarget ||
                retailTarget ||
                employmentTarget ||
                isPpi ||
                (pceTarget && newPce)) &&
              (briefTarget || jobId)
                ? `${macroProfile.id}:${briefTarget || jobId}`
                : briefTarget
            }
            selected={cpiExecution?.rowId ?? execution?.id}
            empty={recordState("jobs")}
            onSelect={(job) => {
              setBriefTarget(null);
              const macro = macroRecords.find((e) => e.rowId === job);
              navigate(
                "collection",
                macro && (macro.profile.id !== "cpi" || isPpi || pceTarget)
                  ? {
                      job: macro.id,
                      asset: macro.profile.asset.identity,
                      asset_revision: macro.profile.asset.revision,
                      operation: null,
                    }
                  : { job: macro?.id ?? job },
                true,
              );
            }}
            rows={[
              ...rows.map((e) => ({
                id: e.id,
                search: `${e.id} ${e.attempt ?? ""} ${assetTitle(e.asset)} ${stateLabel(e.state)}`,
                cells: [
                  `${sameKey(e.asset, PCE) ? "PCE" : "CPI"} · ${e.schedule ? "自动阶段" : (e.pce?.parent_job_id ?? e.dto?.parent_job_id) ? "范围回补" : "初始化采集"}`,
                  <ExecutionCreatedAt value={e.dto?.created_at} />,
                  <span className="execution-range">{e.reference}</span>,
                  <span className="execution-result">
                    {stateLabel(e.state)}
                  </span>,
                ],
              })),
              ...macroRecords.map((e) => ({
                id: e.rowId,
                search: `${e.id} ${macroAssetLabel(e.profile)} ${e.origin !== "manual" ? "自动策略" : "手动采集"} ${cpiActionText(e.origin !== "manual" ? (e.stage ?? e.action) : e.action)} ${e.start ?? ""} ${e.end ?? ""} ${cpiExecutionStateText(e.state, e.result, e.profile, e)}`,
                cells: [
                  `${e.profile.label} · ${e.origin !== "manual" ? "自动阶段 · " : ""}${cpiActionText(e.origin !== "manual" ? (e.stage ?? e.action) : e.action)}`,
                  <ExecutionCreatedAt value={e.created_at} />,
                  <span className="execution-range">
                    <span>{e.start ?? "未记录"}</span>
                    <span>至</span>
                    <span>{e.end ?? "未记录"}</span>
                  </span>,
                  <span className="execution-result">
                    {cpiExecutionStateText(e.state, e.result, e.profile, e)}
                  </span>,
                ],
              })),
            ]}
          >
            {cpiExecution ? (
              <CpiExecutionDetail
                execution={cpiExecution}
                profile={cpiExecution.profile}
              />
            ) : execution ? (
              <ExecutionDetail
                execution={execution}
                navigate={navigate}
                successor={successorOf(execution.id)}
                eligible={
                  execution.state === "failed" &&
                  (execution.pce
                    ? !!pceBackfill?.eligible &&
                      execution.id === pceBackfill.submission.parent_job_id
                    : execution.schedule
                      ? !!plans.data?.write_eligible
                      : !!cpiBackfill?.eligible &&
                        execution.id === cpiBackfill.submission?.parent_job_id)
                }
                reason={
                  execution.pce
                    ? pceBackfill?.reason
                    : execution.schedule
                      ? "history_plan_read_only"
                      : cpiBackfill?.reason
                }
              />
            ) : null}
          </WorkflowTable>
          {jobId &&
            !jobs.busy &&
            !cpiRecords.busy &&
            !ppiRecords.busy &&
            !pceRecords.busy &&
            !employmentRecords.busy &&
            !retailRecords.busy &&
            !consumptionRecords.busy &&
            !execution &&
            !cpiExecution && (
              <p role="status">
                请求的具名执行记录当前不可核实，未改选其他任务。
              </p>
            )}
        </>
      )}
    </>
  );
}
function ExecutionCreatedAt({ value }: { value?: string | null }) {
  const formatted = formatBeijingTime(value, "未记录").replace(" 北京时间", "");
  if (formatted === "未记录") return <span>未记录</span>;
  const [date, time] = formatted.split(" ");
  return (
    <time className="execution-created">
      <span>{date}</span>
      <span>{time}</span>
    </time>
  );
}
function ExecutionDetail({
  execution: e,
  navigate,
  successor,
  eligible,
  reason,
}: {
  execution: Execution;
  navigate: Navigate;
  successor?: Execution;
  eligible: boolean;
  reason?: string | null;
}) {
  const fields = {
    asset: e.asset.identity,
    asset_revision: e.asset.revision,
    operation: e.operation,
  };
  return (
    <>
      <h3>执行详情</h3>
      <dl>
        <Fact name="Job / Attempt">
          {e.id} / {e.attempt ?? "尚未执行"}
        </Fact>
        <Fact name="关联资产">{assetTitle(e.asset)}</Fact>
        <Fact name="参考期">{e.reference}</Fact>
        <Fact name="执行结果">{stateLabel(e.state)}</Fact>
        {(e.dto?.stage ?? e.pce?.stage) && (
          <Fact name="执行阶段回报">
            {e.dto?.stage ?? e.pce?.stage} ·{" "}
            {e.dto?.classification ?? e.pce?.classification ?? "无错误分类"}
          </Fact>
        )}
        <Fact name="关联计划">{e.schedule ?? "无 / 人工任务"}</Fact>
        <Fact name="材料 / 版本 / 发布">
          {e.version ? e.version : "尚无版本 / 发布回执"}
        </Fact>
        {e.publication && <Fact name="发布记录">{e.publication}</Fact>}
      </dl>
      {e.state === "outcome_unknown" && (
        <p role="status">原执行效果未知，必须先核实，禁止盲目重试。</p>
      )}
      {successor && <Successor execution={successor} navigate={navigate} />}
      {e.state === "failed" && !successor && !eligible && (
        <p role="status">{backfillReason(reason)}</p>
      )}
      <div className="actions">
        <button onClick={() => navigate("assets", fields)}>
          查看关联资产 →
        </button>
        {(e.pce?.parent_job_id ?? e.dto?.parent_job_id) && (
          <button
            onClick={() =>
              navigate("collection", {
                ...fields,
                tab: "executions",
                job: (e.pce?.parent_job_id ?? e.dto?.parent_job_id)!,
              })
            }
          >
            查看原失败执行 →
          </button>
        )}
        {e.schedule && (
          <button
            onClick={() =>
              navigate("collection", {
                ...fields,
                tab: "periodic",
                schedule: e.schedule,
              })
            }
          >
            查看周期任务 →
          </button>
        )}
        {eligible && !successor && (
          <button
            onClick={() =>
              navigate("collection", {
                ...fields,
                tab: "new",
                job: e.id,
                mode: "backfill",
              })
            }
          >
            指定范围回补 →
          </button>
        )}
        {e.version && e.publication && e.readAsOf && (
          <button
            onClick={() =>
              navigate("services", {
                ...fields,
                version: e.version,
                reference: e.reference,
                as_of: e.readAsOf,
              })
            }
          >
            精确读取结果 →
          </button>
        )}
      </div>
      {(e.pce?.parent_job_id ?? e.dto?.parent_job_id) && (
        <p>原失败 Job：{e.pce?.parent_job_id ?? e.dto?.parent_job_id}</p>
      )}
      {e.pce?.events?.length || e.dto?.history.length ? (
        <ol aria-label="执行阶段回报">
          {e.pce?.events?.map((event, index) => (
            <li key={`pce-${index}`}>
              {formatBeijingTime(event.updated_at)} · {stateLabel(event.state)}{" "}
              · {event.stage} · {event.attempt_id ?? "尚无尝试"} ·{" "}
              {event.classification ?? "无错误分类"}
            </li>
          ))}
          {e.dto?.history.map((event, index) => (
            <li key={`cpi-${index}`}>
              {formatBeijingTime(event.updated_at)} · {stateLabel(event.state)}{" "}
              · {event.stage} · {event.attempt_identity ?? "尚无尝试"} ·{" "}
              {event.classification ?? "无错误分类"}
            </li>
          ))}
        </ol>
      ) : null}
    </>
  );
}

function backfillReason(reason?: string | null): string {
  if (reason?.endsWith("exact_two_scope_migration_required"))
    return "测试库尚未准入这笔回补任务，请由 Host 核对并完成受控迁移，再回查资格；不能直接重试原任务。";
  if (reason?.endsWith("successor_slot_used"))
    return "唯一回补任务已登记，请回查执行记录核实其状态及结果；不能创建第三笔。";
  if (reason?.includes("read_only"))
    return "当前为只读，不能创建回补；请在开发测试服务回查资格，仍可查看失败历史。";
  if (
    reason?.includes("effects_unresolved") ||
    reason?.includes("outcome_unknown")
  )
    return "原执行效果尚未核实，请先由 Host 核实持久结果；禁止再次提交。";
  if (reason === "reference_mismatch")
    return "开发测试仅支持 CPI 2026-09，开始与结束须相同；请恢复原参考月。";
  if (reason === "cpi_pg_schema_or_permission_mismatch")
    return "测试库身份或写入权限未通过核对；请由 Host 核实 schema 与权限后回查资格。";
  if (reason?.includes("failed_attempt_required"))
    return "原失败 Attempt 的持久记录不完整；请由 Host 核对任务、Attempt 与事件，暂不能创建回补。";
  if (reason?.includes("storage_unavailable"))
    return "持久任务存储暂不可读取；请回查任务并由 Host 核实存储状态，不能据此重试。";
  if (reason?.includes("parent_missing"))
    return "原失败任务尚未查到，请回查具名任务后再核实回补资格。";
  if (reason?.includes("not_failed") || reason?.includes("failure_mismatch"))
    return "当前任务不符合已核实失败 INIT 的回补条件；请查看失败历史并由 Host 核实。";
  return "当前未配置可执行的回补能力或资格尚未核实；请回查原任务，由 Host 核实绑定与执行条件。仍可查看失败历史。";
}
function Successor({
  execution: e,
  navigate,
}: {
  execution: Execution;
  navigate: Navigate;
}) {
  const fields = {
    asset: e.asset.identity,
    asset_revision: e.asset.revision,
    operation: e.operation,
  };
  return (
    <div className="readonly">
      <p>
        关联回补任务：{e.id} · {stateLabel(e.state)}
      </p>
      <button
        type="button"
        onClick={() =>
          navigate(
            "collection",
            { ...fields, tab: "executions", job: e.id },
            true,
          )
        }
      >
        查看回补任务 →
      </button>
      {e.version && e.publication && e.readAsOf && (
        <button
          type="button"
          onClick={() =>
            navigate("services", {
              ...fields,
              version: e.version,
              reference: e.reference,
              as_of: e.readAsOf,
            })
          }
        >
          查看回补成功结果 →
        </button>
      )}
    </div>
  );
}
