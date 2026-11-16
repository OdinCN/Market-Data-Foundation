import { ExecutionNavigation, PeriodicNavigation } from "./ExecutionNavigation";
import { executionOrigin } from "./executionOrigin";
import {
  CollectionBriefs,
  CollectionFormLock,
  ExecutionDetailShell,
} from "./CollectionLifecycle";
import {
  collectionRegistrations,
  collectionTargets,
  legacyCollection,
  readonlyCollections,
} from "./collectionRegistry";
import { MACRO_PROFILES } from "./macroTimeSeriesProfile";
import { useCollectionSubmit } from "./useCollectionSubmit";
import { ExecutionBriefCard } from "./DailyFormControls";
import {
  familyRowId,
  useFamilyRecords,
  type RuntimeFamily,
} from "./RuntimeFamily";
import { claimsExecutionPeriod } from "./claimsPeriod";
import { executionCreatedOrder } from "./executionOrder";
import {
  financialJobTitle,
  financialJobSearch,
  financialJobResult,
} from "./financialJobPresentation";
import { useFinancialPeriodic } from "./FinancialPeriodic";
import {
  FINANCIAL,
  FinancialDailyWorkbench,
  financialCall,
  type FinancialJob,
} from "./FinancialDailyWorkbench";
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
  financialRegistered = false,
  families = [],
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
  financialRegistered?: boolean;
  families?: readonly RuntimeFamily[];
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
  // Browsing another execution must not replace the operator's creation draft.
  // Execution URLs still carry their own canonical asset and job identity.
  const creationContext = useRef<Record<string, string | null> | null>(null);
  if (tab === "new")
    creationContext.current = {
      asset: route.fields.get("asset"),
      asset_revision: route.fields.get("asset_revision"),
      operation: route.fields.get("operation"),
      job: route.fields.get("job"),
      mode: route.fields.get("mode"),
    };
  // The route owns the creation target, even while its directory is loading.
  const selectedTarget = invalidIdentity
    ? ""
    : route.fields.has("asset") && route.fields.has("asset_revision")
      ? `${route.fields.get("asset")}@${route.fields.get("asset_revision")}`
      : asset
        ? keyOf(asset.key)
        : "";
  const collectionRegistered = collectionRegistrations(families).some(
    (entry) => keyOf(entry.asset) === selectedTarget,
  );
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
  const submissionScope = `${selectedTarget}:${route.fields.get("operation") ?? ""}:${mode}:${route.fields.get("job") ?? ""}`;
  const submission = useCollectionSubmit(submissionScope);
  const activeSubmissionScope = useRef(submissionScope);
  activeSubmissionScope.current = submissionScope;
  // Refreshing server offers must not erase or replace the submitted identity.
  const legacyPinned = useRef<{
    scope: string;
    id: string;
    key: string;
    variant: string;
    start: string;
    end: string;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const acceptedReceiptScope = useRef<{
    asset: string;
    operation: string | undefined;
    job: string | null;
    plan: string | null;
  } | null>(null);
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
  const macroProfile =
    MACRO_PROFILES.find((p) =>
      sameKey(
        p.asset,
        asset?.key ?? {
          identity: route.fields.get("asset") ?? "",
          revision: route.fields.get("asset_revision") ?? "",
        },
      ),
    ) ?? CPI_PROFILE;
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
  const financialTarget =
    !invalidIdentity &&
    sameKey(asset?.key, FINANCIAL) &&
    !route.fields.has("operation");
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
  // A creation-target change is not a binding revocation. Keep this generation's
  // verified, capability-specific read binding for the global history list;
  // every returned job still passes validPceBinding. Writes use fresh context.
  const pceHistoryBinding = useRef({ transport, generation, verified: false });
  if (
    pceHistoryBinding.current.transport !== transport ||
    pceHistoryBinding.current.generation !== generation
  )
    pceHistoryBinding.current = { transport, generation, verified: false };
  if (pceReadBound) pceHistoryBinding.current.verified = true;
  const pceHistoryBound = pceHistoryBinding.current.verified;
  const pceStatusFailure =
    tab === "executions" &&
    !invalidIdentity &&
    (filter === "all" || filter === keyOf(PCE) || pceTarget) &&
    pceAvailability?.failure;
  const readPceRecords =
    pceHistoryBound &&
    (!pceAvailability || pceAvailability.legacy) &&
    ((tab === "new" && isPce && (!pceAvailability || pceAvailability.legacy)) ||
      (tab === "executions" &&
        !invalidIdentity &&
        (filter === "all" ||
          filter === keyOf(PCE) ||
          (isPce && route.fields.has("job")))));
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
    [transport, generation, refresh, readPceRecords, tab],
    readPceRecords,
  );
  const financialPeriodic = useFinancialPeriodic(
    transport,
    financialRegistered && tab === "periodic",
    navigate,
  );
  const financialRecords = useVisiblePolling(
    (signal) =>
      financialCall<{ jobs: FinancialJob[] }>(transport, "/jobs", signal),
    [transport, generation, refresh, filter, tab],
    tab === "executions" &&
      financialRegistered &&
      !invalidIdentity &&
      (filter === "all" || filter === keyOf(FINANCIAL)),
  );
  const familyProps = { transport, route, navigate, generation };
  const familyRecords = useFamilyRecords(
    families,
    familyProps,
    invalidIdentity,
  );
  const selectedFamily = familyRecords.selected;
  const familyRows = (familyRecords.executions.data ?? []).filter(
    (row) => executionSchedule === "all" || row.schedule === executionSchedule,
  );
  const jobId = route.fields.get("job");
  const financialExecution =
    financialTarget && jobId
      ? financialRecords.data?.jobs.find((j) => j.request_id === jobId)
      : undefined;
  const scheduleId = route.fields.get("schedule");
  const jobs = useVisiblePolling<Jobs>(
    (signal) => request(transport, "/api/r5/cpi/jobs", {}, signal),
    [transport, generation, refresh, readRecords],
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
    ...(readPceRecords ? (pceJobs.data?.jobs ?? []) : []).map(pceExecution),
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
  const macroReaders = {
    cpi: cpiRecords,
    ppi: ppiRecords,
    pce: pceRecords,
    employment: employmentRecords,
    retail: retailRecords,
    consumption: consumptionRecords,
    gdp: gdpRecords,
    m3: m3Records,
    jolts: joltsRecords,
    claims: claimsRecords,
  };
  const macroRecords = [
    ...(filter === "all" || filter === keyOf(M3)
      ? (m3Records.data?.executions ?? []).map((e) => ({
          ...e,
          profile: M3_PROFILE,
          rowId: `m3:${e.id}`,
        }))
      : []),
    ...(filter === "all" || filter === keyOf(GDP)
      ? (gdpRecords.data?.executions ?? []).map((e) => ({
          ...e,
          profile: GDP_PROFILE,
          rowId: `gdp:${e.id}`,
        }))
      : []),
    ...(filter === "all" || filter === keyOf(JOLTS)
      ? (joltsRecords.data?.executions ?? []).map((e) => ({
          ...e,
          profile: JOLTS_PROFILE,
          rowId: `jolts:${e.id}`,
        }))
      : []),
    ...(filter === "all" || filter === keyOf(CLAIMS)
      ? (claimsRecords.data?.executions ?? []).map((e) => ({
          ...e,
          profile: CLAIMS_PROFILE,
          rowId: `claims:${e.id}`,
        }))
      : []),
    ...(filter === "all" || filter === keyOf(CONSUMPTION)
      ? (consumptionRecords.data?.executions ?? []).map((e) => ({
          ...e,
          profile: CONSUMPTION_PROFILE,
          rowId: `consumption:${e.id}`,
        }))
      : []),
    ...(filter === "all" || filter === keyOf(RETAIL)
      ? (retailRecords.data?.executions ?? []).map((e) => ({
          ...e,
          profile: RETAIL_PROFILE,
          rowId: `retail:${e.id}`,
        }))
      : []),
    ...(filter === "all" || filter === keyOf(EMPLOYMENT)
      ? (employmentRecords.data?.executions ?? []).map((e) => ({
          ...e,
          profile: EMPLOYMENT_PROFILE,
          rowId: `employment:${e.id}`,
        }))
      : []),
    ...(filter === "all" || filter === keyOf(CPI)
      ? (cpiRecords.data?.executions ?? []).map((e) => ({
          ...e,
          profile: CPI_PROFILE,
          rowId: e.id,
        }))
      : []),
    ...(filter === "all" || filter === keyOf(PCE)
      ? (pceRecords.data?.executions ?? []).map((e) => ({
          ...e,
          profile: PCE_PROFILE,
          rowId: `pce:${e.id}`,
        }))
      : []),
    ...(filter === "all" || filter === keyOf(PPI)
      ? (ppiRecords.data?.executions ?? []).map((e) => ({
          ...e,
          profile: PPI_PROFILE,
          rowId: `ppi:${e.id}`,
        }))
      : []),
  ].filter(
    (e) =>
      executionSchedule === "all" ||
      ((e.origin === "automatic" ||
        e.origin === "schedule" ||
        e.origin === "periodic") &&
        executionSchedule === `${e.profile.id}-auto`),
  );
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
  const execution = jobId
    ? all.find((e) => e.id === jobId && (!asset || sameKey(e.asset, asset.key)))
    : undefined;
  const plan = plansMatch
    ? plans.data?.plans.find((p) => p.identity === scheduleId)
    : undefined;
  const planFields = {
    asset: planAsset?.identity ?? null,
    asset_revision: planAsset?.revision ?? null,
    operation: HISTORY_OPERATION,
  };
  function recordState(kind: "plans" | "jobs") {
    const family = families.find(
      (candidate) => keyOf(candidate.asset) === filter,
    );
    if (kind === "jobs" && family) {
      const records = familyRecords.executions;
      if (records.failure) return `${family.title}执行记录读取失败。`;
      if (records.busy || !records.data)
        return `正在读取${family.title}执行记录…`;
      return "当前筛选范围内没有执行记录。";
    }
    if (kind === "jobs" && filter === keyOf(FINANCIAL)) {
      if (!financialRegistered)
        return "金融日频人工接入尚未核实，执行记录尚未查询。";
      if (financialRecords.busy) return "正在读取金融日频执行记录…";
      if (financialRecords.failure)
        return "金融日频执行记录读取失败，当前结果尚未核实。";
      return financialRecords.data
        ? "当前已查询范围没有金融日频执行记录。"
        : "金融日频执行记录尚未查询。";
    }

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
    setUncertain(false);
  }, [asset && keyOf(asset.key), context?.source?.id, mode, jobId]);
  useEffect(() => {
    const receipt = acceptedReceiptScope.current;
    if (
      !receipt ||
      !["new", "executions"].includes(tab) ||
      receipt.asset !== (asset && keyOf(asset.key)) ||
      receipt.operation !== context?.source?.id ||
      (tab === "executions" &&
        (receipt.job !== jobId || receipt.plan !== scheduleId))
    ) {
      setNotice(null);
      acceptedReceiptScope.current = null;
    }
  }, [
    asset && keyOf(asset.key),
    context?.source?.id,
    mode,
    jobId,
    scheduleId,
    tab,
  ]);
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
  const pinnedLegacy =
    legacyPinned.current?.scope === submissionScope
      ? legacyPinned.current
      : null;
  const legacyVariant =
    pinnedLegacy?.variant ??
    (mode === "一次性初始化"
      ? "legacy-once"
      : !isPce && !cpiBackfillMode
        ? "legacy-plan-backfill"
        : "legacy-backfill");
  const legacyRegistration = legacyCollection(
    asset?.key,
    legacyVariant,
    families,
  );
  const unavailableCollection = readonlyCollections.find((r) =>
    sameKey(r.asset, asset?.key),
  );
  const planBackfill = legacyVariant === "legacy-plan-backfill";
  const legacyIdentity =
    (planBackfill
      ? parent?.identity
      : (devIntent ?? jobs.data?.intent_identity)) ??
    pinnedLegacy?.id ??
    null;
  // Independent failed children of the same plan can each need a retry.
  const legacyIntentKey =
    (legacyIdentity === pinnedLegacy?.id ? pinnedLegacy.key : null) ??
    (planBackfill && legacyIdentity && jobId
      ? JSON.stringify([legacyIdentity, jobId])
      : legacyIdentity);
  const briefIdentity = pinnedLegacy?.id ?? legacyIdentity;
  const briefIntentKey = pinnedLegacy?.key ?? legacyIntentKey;
  let legacyLocked = false;
  try {
    legacyLocked =
      !!legacyIntentKey &&
      (reservedIntents.current.has(legacyIntentKey) ||
        readDevIntents().includes(legacyIntentKey));
  } catch {
    legacyLocked = true;
  }
  type LegacyReceipt = {
    job: string | null;
    plan: string | null;
    state: string;
    rawState?: string;
  };
  async function readLegacy(id: string): Promise<LegacyReceipt> {
    if (isPce) {
      const response = await pceRequest<PceJobs>(transport, "/api/pce/jobs");
      const job = response.jobs.find(
        (j) => j.job_id === id && validPceBinding(j.binding),
      );
      if (!job) throw Error("receipt_unknown");
      return {
        job: id,
        plan: null,
        state: stateLabel(job.state),
        rawState: job.state,
      };
    }
    if (legacyVariant === "legacy-plan-backfill") {
      const response = await request(
        transport,
        "/api/r5/cpi/plans",
        undefined,
        new AbortController().signal,
      );
      const plan = response.plans.find((p) => p.identity === id);
      const batch = plan?.batches.find(
        (b) =>
          b.reference === (pinnedLegacy?.start ?? start) &&
          b.jobs.some((j) => j.job_identity === jobId),
      );
      // An existing parent alone predates this POST. Require this failed
      // child's retry to be queued or a later attempt in the very same batch.
      if (
        !plan ||
        !batch ||
        !(
          plan.retry_queue.includes(batch.key) ||
          batch.jobs.at(-1)?.job_identity !== jobId
        )
      )
        throw Error("receipt_unknown");
      return {
        job: null,
        plan: id,
        state: stateLabel(plan.state),
        rawState: plan.state,
      };
    }
    const response = await request(
      transport,
      "/api/r5/cpi/jobs",
      {
        job_identity: id,
      },
      new AbortController().signal,
    );
    const job = response.jobs.find((j) => j.job_identity === id);
    if (!job) throw Error("receipt_unknown");
    return {
      job: id,
      plan: null,
      state: stateLabel(job.state),
      rawState: job.state,
    };
  }
  function recallLegacy() {
    if (!briefIdentity || !briefIntentKey) return;
    void submission
      .recall(
        briefIntentKey,
        () => readLegacy(briefIdentity),
        (r) => (r.job ?? r.plan) === briefIdentity,
      )
      .then(() => {
        setUncertain(false);
        setRefresh((n) => n + 1);
      })
      .catch(() => setNotice("原请求尚未核实；保留编号，不重复提交。"));
  }
  const legacyTracking = useVisiblePolling(
    () => readLegacy(briefIdentity!),
    [transport, generation, submissionScope, briefIdentity],
    tab === "new" &&
      !busy &&
      !!legacyRegistration &&
      !!briefIdentity &&
      (legacyLocked || devIntentLocked),
  );
  const legacyReceipt =
    legacyTracking.data ??
    (submission.attempt?.id === briefIntentKey &&
    submission.attempt?.state === "accepted"
      ? (submission.attempt.receipt as LegacyReceipt)
      : null);
  async function submit() {
    if (
      writing.current ||
      !confirmed ||
      uncertain ||
      devIntentLocked ||
      legacyLocked ||
      !legacyIdentity ||
      !legacyIntentKey ||
      !legacyRegistration ||
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
      legacyPinned.current = {
        scope: submissionScope,
        id: legacyIdentity,
        key: legacyIntentKey,
        variant: legacyVariant,
        start,
        end,
      };
      reservedIntents.current.add(legacyIntentKey);
      const intents = readDevIntents();
      if (!intents.includes(legacyIntentKey)) {
        const stored = JSON.stringify([...intents, legacyIntentKey]);
        sessionStorage.setItem(devIntentKey, stored);
        if (sessionStorage.getItem(devIntentKey) !== stored)
          throw Error("intent_reservation_unverified");
      }
      const result = await submission.submit(
        legacyIntentKey,
        async () => {
          let acceptedJob: string | null = null;
          if (mode === "一次性初始化") {
            if (isPce) {
              const receipt = await pceRequest<PceJob>(
                transport,
                "/api/pce/jobs",
                {
                  method: "POST",
                  credentials: "same-origin",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify(pceCatalog!.submission),
                },
              );
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
          return {
            job: acceptedJob,
            plan: acceptedJob ? null : parent!.identity,
            state: "意图已持久接收，执行结果以回读为准",
          };
        },
        () => readLegacy(legacyIdentity!),
        (value) => (value.job ?? value.plan) === legacyIdentity,
      );
      const acceptedJob = result.job;
      if (active.current && activeSubmissionScope.current === submissionScope) {
        acceptedReceiptScope.current = {
          asset: keyOf(asset!.key),
          operation: context?.source?.id,
          job: acceptedJob,
          plan: acceptedJob ? null : parent!.identity,
        };
        setNotice("意图已持久接收，执行结果以独立回查为准。");
        setConfirmed(false);
        setRefresh((n) => n + 1);
      }
    } catch (e) {
      if (active.current && activeSubmissionScope.current === submissionScope) {
        if (!devIntent) setUncertain(true);
        setNotice(
          `提交回执未核实，请回查同一任务；不会自动重试。${failureName(e)}`,
        );
      }
    } finally {
      if (activeSubmissionScope.current === submissionScope) {
        writing.current = false;
        if (active.current) setBusy(false);
      }
    }
  }
  useEffect(() => {
    writing.current = false;
    setBusy(false);
    setConfirmed(false);
    setUncertain(false);
  }, [submissionScope]);
  const fields = {
    asset: asset?.key.identity ?? null,
    asset_revision: asset?.key.revision ?? null,
    operation: context?.source?.id ?? null,
  };
  const calendar = `${frequency === "daily" ? "每天" : frequency === "weekly" ? `每周 ${day}` : frequency === "monthly" ? `每月 ${day} 日` : `每年 ${month} 月 ${day} 日`} ${time}（${zone}）`;
  const familyAssetTitle = (key: { identity: string; revision: string }) =>
    families.find((f) => sameKey(f.asset, key))?.title ?? assetTitle(key);
  const targetControl = (
    <label>
      采集目标
      <select
        aria-label="采集目标"
        value={selectedTarget}
        onChange={(e) => {
          const key =
            assets.find((a) => keyOf(a.key) === e.target.value)?.key ??
            collectionTargets(families).find(
              (k) => keyOf(k) === e.target.value,
            );
          navigate("collection", {
            asset: key?.identity ?? null,
            asset_revision: key?.revision ?? null,
            operation: null,
            tab: "new",
            job: null,
            ...(sameKey(key, FINANCIAL)
              ? { reference: null, as_of: null }
              : {}),
          });
        }}
      >
        <option value="">选择一个已登记资产</option>
        {[
          ...assets,
          ...collectionTargets(families)
            .filter((k) => !assets.some((a) => sameKey(a.key, k)))
            .map((key) => ({ key })),
        ].map((a) => (
          <option key={keyOf(a.key)} value={keyOf(a.key)}>
            {familyAssetTitle(a.key)}
          </option>
        ))}
      </select>
    </label>
  );
  return (
    <>
      <Tabs
        label="采集任务页签"
        values={tabs}
        value={tab}
        onChange={(next) =>
          navigate(
            "collection",
            { ...(next === "new" ? creationContext.current : {}), tab: next },
            true,
          )
        }
      />
      {tab !== "new" && (
        <div className="toolbar">
          <label>
            {tab === "periodic" ? "周期资产筛选" : "执行资产筛选"}
            <select
              aria-label={tab === "periodic" ? "周期资产筛选" : "执行资产筛选"}
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
                  {familyAssetTitle(a.key)}
                </option>
              ))}
              {families
                .filter((f) => !assets.some((a) => sameKey(a.key, f.asset)))
                .map((f) => (
                  <option key={keyOf(f.asset)} value={keyOf(f.asset)}>
                    {f.title}
                  </option>
                ))}
              {filter !== "all" &&
                !assets.some((a) => keyOf(a.key) === filter) &&
                !families.some((f) => keyOf(f.asset) === filter) &&
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
        pceStatusFailure ||
        cpiRecords.failure ||
        ppiRecords.failure ||
        pceRecords.failure ||
        employmentRecords.failure ||
        retailRecords.failure ||
        consumptionRecords.failure ||
        gdpRecords.failure ||
        claimsRecords.failure ||
        joltsRecords.failure ||
        financialRecords.failure ||
        familyRecords.executions.failure ||
        familyRecords.periodic.failure) && (
        <div role="alert">
          采集记录已陈旧，保留上次数据并自动重读。
          {(familyRecords.executions.failure ||
            familyRecords.periodic.failure) && (
            <p>登记资产任务读取失败，当前记录尚未核实。</p>
          )}
          {financialRecords.failure && (
            <p>金融日频执行记录读取失败，当前结果尚未核实。</p>
          )}
          {claimsRecords.failure && (
            <p>美国初请与续请执行记录读取失败，当前结果尚未核实。</p>
          )}
          {gdpRecords.failure && <p>GDP执行记录读取失败，当前结果尚未核实。</p>}
          {ppiRecords.failure && (
            <p>PPI 执行记录读取失败，当前记录尚未核实。</p>
          )}
          {pceStatusFailure && (
            <p>PCE 服务状态读取失败，当前执行记录尚未核实。</p>
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
      {tab === "new" && selectedFamily?.collection && collectionRegistered ? (
        <>
          {targetControl}
          <selectedFamily.Collection
            key={keyOf(selectedFamily.asset)}
            {...familyProps}
          />
        </>
      ) : tab === "new" && financialTarget && collectionRegistered ? (
        <>
          {targetControl}
          <FinancialDailyWorkbench
            route={route}
            navigate={navigate}
            transport={transport}
            generation={generation}
            embedded
          />
        </>
      ) : tab === "new" &&
        collectionRegistered &&
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
          {targetControl}
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
            <CollectionFormLock
              locked={busy || legacyLocked || devIntentLocked}
            >
              <h2>新建采集</h2>
              {isPce && (
                <p className="muted">
                  BEA PCE 开发测试，仅支持 2026-07。登记后由 Host 单次执行。
                </p>
              )}
              <div className="field-grid">
                {targetControl}
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
                <button
                  type="button"
                  onClick={() => navigate("assets", fields)}
                >
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
                    Macro
                    按已登记参考期限定范围，不将任意自然日转换为可执行窗口。
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
                    legacyLocked ||
                    !legacyRegistration ||
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
              </div>
            </CollectionFormLock>
          </form>
          <CollectionBriefs>
            {briefIdentity &&
              (submission.attempt || legacyLocked || devIntentLocked) && (
                <>
                  <ExecutionBriefCard
                    id={briefIdentity}
                    disabled={!legacyReceipt}
                    createdAt=""
                    current
                    title={asset?.label ?? "采集任务"}
                    start={pinnedLegacy?.start ?? start}
                    end={pinnedLegacy?.end ?? end}
                    result={
                      (!uncertain ? legacyReceipt?.state : null) ??
                      (busy
                        ? "正在提交，尚未核实"
                        : "回执待核实，保留原请求编号")
                    }
                    onSelect={() => {
                      if (legacyReceipt)
                        navigate("collection", {
                          ...fields,
                          tab: legacyReceipt.job ? "executions" : "periodic",
                          job: legacyReceipt.job,
                          schedule: legacyReceipt.plan,
                          execution_schedule: legacyReceipt.job
                            ? "all"
                            : legacyReceipt.plan,
                        });
                    }}
                  />
                  {!legacyReceipt &&
                    (uncertain || legacyLocked || legacyTracking.stale) && (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={recallLegacy}
                      >
                        核实原请求
                      </button>
                    )}
                </>
              )}
            {legacyReceipt && (
              <p className="small" data-collection-exception="server-offer">
                此兼容入口使用服务提供的一次性编号；没有新的服务准入编号时不另建重复任务。可继续查看原记录或按服务资格回补。
              </p>
            )}
            <h3 id="legacy-collection-summary">本次采集摘要</h3>
            <dl aria-labelledby="legacy-collection-summary">
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
              {unavailableCollection && (
                <p role="status">{unavailableCollection.reason}</p>
              )}
              {devCandidate && (
                <Fact name="DEV 写入目标">
                  mdf_b3_cpi_05_dev_r1 · CPI 2026-09 · 单次执行
                </Fact>
              )}
              <Fact name="任务接收状态">
                {legacyReceipt
                  ? "已接收"
                  : legacyLocked
                    ? "回执待核实"
                    : "尚未提交"}
              </Fact>
            </dl>
            <p className="muted">
              一次性与回补提交后保留本页，主动点击已确认简报才进入执行记录；周期规则需后端准确支持后才能保存。
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
          </CollectionBriefs>
        </div>
      ) : tab === "periodic" &&
        (familyRecords.periodicEntries.length > 0 ||
          families.some((family) => filter === keyOf(family.asset)) ||
          financialRegistered ||
          newCpi ||
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
          navigation={(id) => {
            const candidates = collectionRegistrations(families).filter(
              (r) => r.strategy?.id === id,
            );
            const registration =
              candidates.length === 1 ? candidates[0] : undefined;
            return (
              registration && (
                <PeriodicNavigation
                  asset={registration.asset}
                  id={id}
                  job={sameKey(asset?.key, registration.asset) ? jobId : null}
                  navigate={navigate}
                />
              )
            );
          }}
          supplements={familyRecords.periodicEntries}
          supplement={
            financialRegistered &&
            !invalidIdentity &&
            (filter === "all" || filter === keyOf(FINANCIAL))
              ? financialPeriodic
              : undefined
          }
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
          onSelect={(id) => {
            const candidates = collectionRegistrations(families).filter(
              (r) => r.strategy?.id === id,
            );
            const target =
              candidates.length === 1 ? candidates[0].asset : undefined;
            if (target)
              navigate(
                "collection",
                {
                  schedule: id,
                  asset: target.identity,
                  asset_revision: target.revision,
                  operation: null,
                  job: sameKey(asset?.key, target) ? jobId : null,
                },
                true,
              );
          }}
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
                {jobId &&
                  plan.batches.some((b) =>
                    b.jobs.some((j) => j.job_identity === jobId),
                  ) && (
                    <button
                      onClick={() =>
                        navigate("collection", {
                          ...planFields,
                          tab: "executions",
                          schedule: null,
                          job: jobId,
                        })
                      }
                    >
                      返回此执行记录
                    </button>
                  )}
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
              selectedFamily && jobId
                ? familyRowId(selectedFamily, jobId)
                : financialTarget && jobId
                  ? `financial:${jobId}`
                  : (cpiExecution?.rowId ?? execution?.id ?? briefTarget)
            }
            selected={
              selectedFamily && jobId
                ? familyRowId(selectedFamily, jobId)
                : financialExecution
                  ? `financial:${financialExecution.request_id}`
                  : (cpiExecution?.rowId ?? execution?.id)
            }
            empty={recordState("jobs")}
            onSelect={(job) => {
              setBriefTarget(null);
              const familyRow = familyRows.find(
                (row) => familyRowId(row.family, row.id) === job,
              );
              if (familyRow) {
                navigate(
                  "collection",
                  {
                    asset: familyRow.family.asset.identity,
                    asset_revision: familyRow.family.asset.revision,
                    operation: null,
                    job: familyRow.id,
                  },
                  true,
                );
                return;
              }
              const financial = financialRecords.data?.jobs.find(
                (j) => `financial:${j.request_id}` === job,
              );
              if (financial) {
                navigate(
                  "collection",
                  {
                    asset: FINANCIAL.identity,
                    asset_revision: FINANCIAL.revision,
                    operation: null,
                    job: financial.request_id,
                  },
                  true,
                );
                return;
              }
              const macro = macroRecords.find((e) => e.rowId === job);
              const legacy = rows.find((e) => e.id === job);
              navigate(
                "collection",
                macro
                  ? {
                      job: macro.id,
                      asset: macro.profile.asset.identity,
                      asset_revision: macro.profile.asset.revision,
                      operation: null,
                    }
                  : legacy
                    ? {
                        job,
                        asset: legacy.asset.identity,
                        asset_revision: legacy.asset.revision,
                        operation: legacy.operation,
                      }
                    : { job },
                true,
              );
            }}
            rows={[
              ...familyRows.map((row) => ({
                id: familyRowId(row.family, row.id),
                execution: { id: row.id, asset: keyOf(row.family.asset) },
                createdAt: row.createdAt,
                search: row.search,
                cells: [
                  row.title,
                  <ExecutionCreatedAt value={row.createdAt} />,
                  row.scope ?? (
                    <span className="execution-range">
                      <span>{row.start ?? "未记录"}</span>
                      <span>至</span>
                      <span>{row.end ?? "未记录"}</span>
                    </span>
                  ),
                  <span className="execution-result">{row.result}</span>,
                ],
              })),
              ...(financialRecords.data?.jobs ?? [])
                .filter(
                  (j) =>
                    executionSchedule === "all" ||
                    (executionSchedule === "financial-auto" &&
                      ["automatic", "schedule", "periodic"].includes(
                        j.origin ?? "",
                      )),
                )
                .map((j) => ({
                  id: `financial:${j.request_id}`,
                  execution: { id: j.request_id, asset: keyOf(FINANCIAL) },
                  createdAt: j.created_at,
                  search: financialJobSearch(j),
                  cells: [
                    financialJobTitle(j),
                    <ExecutionCreatedAt value={j.created_at} />,
                    <span className="execution-range">
                      <span>{j.command.start}</span>
                      <span>至</span>
                      <span>{j.command.end}</span>
                    </span>,
                    <span className="execution-result">
                      {financialJobResult(j)}
                    </span>,
                  ],
                })),
              ...rows.map((e) => ({
                id: e.id,
                execution: { id: e.id, asset: keyOf(e.asset) },
                createdAt: e.dto?.created_at,
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
                execution: { id: e.id, asset: keyOf(e.profile.asset) },
                createdAt: e.created_at,
                search: `${e.id} ${macroAssetLabel(e.profile)} ${executionOrigin(e.origin)} ${cpiActionText(["automatic", "schedule"].includes(e.origin ?? "") ? (e.stage ?? e.action) : e.action)} ${e.start ?? ""} ${e.end ?? ""} ${cpiExecutionStateText(e.state, e.result, e.profile, e)}${e.profile.id === "claims" ? " " + claimsExecutionPeriod(e.result) : ""}`,
                cells: [
                  <>
                    {`${e.profile.label} · ${executionOrigin(e.origin)} · ${cpiActionText(["automatic", "schedule"].includes(e.origin ?? "") ? (e.stage ?? e.action) : e.action)}`}
                    {e.profile.id === "claims" && (
                      <span className="small">
                        {claimsExecutionPeriod(e.result)}
                      </span>
                    )}
                  </>,
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
            ].sort(executionCreatedOrder)}
          >
            <ExecutionNavigation
              asset={
                selectedFamily?.asset ??
                (financialTarget
                  ? FINANCIAL
                  : (cpiExecution?.profile.asset ?? execution?.asset))
              }
              job={jobId}
              family={selectedFamily}
              registration={collectionRegistrations(families).find(
                (r) =>
                  sameKey(
                    r.asset,
                    selectedFamily?.asset ??
                      (financialTarget
                        ? FINANCIAL
                        : (cpiExecution?.profile.asset ?? execution?.asset)),
                  ) &&
                  (execution
                    ? r.protocol.startsWith("legacy")
                    : !r.protocol.startsWith("legacy")),
              )}
              associatedPlan={
                execution?.schedule &&
                plansMatch &&
                !plans.stale &&
                plans.data?.plans.filter(
                  (p) => p.identity === execution.schedule,
                ).length === 1
                  ? execution.schedule
                  : undefined
              }
              transport={transport}
              navigate={navigate}
              generation={generation}
            >
              {selectedFamily && jobId ? (
                <selectedFamily.ExecutionDetail
                  key={`${keyOf(selectedFamily.asset)}:${jobId}`}
                  {...familyProps}
                />
              ) : financialTarget && jobId ? (
                <FinancialDailyWorkbench
                  key={`financial:${jobId}`}
                  route={route}
                  navigate={navigate}
                  transport={transport}
                  generation={generation}
                  embedded
                  detailOnly
                />
              ) : cpiExecution ? (
                <CpiExecutionDetail
                  execution={cpiExecution}
                  key={`${keyOf(cpiExecution.profile.asset)}:${cpiExecution.id}`}
                  profile={cpiExecution.profile}
                  transport={transport}
                  canCollect={
                    macroAvailability?.data?.web_capabilities.collect ===
                      true && !macroAvailability.failure
                  }
                  onResult={() =>
                    navigate("assets", {
                      asset: cpiExecution.profile.asset.identity,
                      asset_revision: cpiExecution.profile.asset.revision,
                      operation: null,
                      tab: "local",
                      package: null,
                      job: cpiExecution.id,
                    })
                  }
                  onRefresh={macroReaders[cpiExecution.profile.id].refresh}
                  actionsDisabled={
                    macroReaders[cpiExecution.profile.id].busy ||
                    macroReaders[cpiExecution.profile.id].stale
                  }
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
                          execution.id ===
                            cpiBackfill.submission?.parent_job_id)
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
            </ExecutionNavigation>
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
            !cpiExecution &&
            !financialTarget &&
            !selectedFamily && (
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
      <ExecutionDetailShell
        id={e.id}
        assetKey={keyOf(e.asset)}
        asset={assetTitle(e.asset)}
        source={e.pce ? "BEA · 兼容入口" : "BLS · 兼容入口"}
        trigger={e.schedule ? "自动策略（历史计划批次）" : "触发来源待核实"}
        action={
          e.pce?.parent_job_id || e.dto?.parent_job_id
            ? "指定范围回补"
            : e.schedule
              ? "历史计划采集"
              : "一次性初始化"
        }
        start={e.reference}
        end={e.reference}
        createdAt={e.dto?.created_at}
        startedAt={e.dto?.started_at}
        finishedAt={e.dto?.finished_at}
        result={stateLabel(e.state)}
        classification={e.dto?.classification ?? e.pce?.classification}
        onResult={() =>
          navigate("assets", {
            ...fields,
            tab: "local",
            job: e.id,
            package: null,
          })
        }
        onResume={
          eligible && !successor
            ? () =>
                navigate("collection", {
                  ...fields,
                  tab: "new",
                  job: e.id,
                  mode: "backfill",
                })
            : undefined
        }
        resumeLabel="指定范围回补 →"
      >
        <dl>
          <Fact name="Job / Attempt">
            {e.id} / {e.attempt ?? "尚未执行"}
          </Fact>
          {e.pce?.updated_at && (
            <Fact name="最近阶段时间（北京时间）">
              {formatBeijingTime(e.pce.updated_at)}
            </Fact>
          )}
          <Fact name="参考期">{e.reference}</Fact>
          {(e.dto?.stage ?? e.pce?.stage) && (
            <Fact name="执行阶段回报">
              {e.dto?.stage ?? e.pce?.stage} ·{" "}
              {e.dto?.classification ?? e.pce?.classification ?? "无错误分类"}
            </Fact>
          )}
          <Fact name="关联计划">
            {e.schedule ?? "未记录（不据此推断触发来源）"}
          </Fact>
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
                {formatBeijingTime(event.updated_at)} ·{" "}
                {stateLabel(event.state)} · {event.stage} ·{" "}
                {event.attempt_id ?? "尚无尝试"} ·{" "}
                {event.classification ?? "无错误分类"}
              </li>
            ))}
            {e.dto?.history.map((event, index) => (
              <li key={`cpi-${index}`}>
                {formatBeijingTime(event.updated_at)} ·{" "}
                {stateLabel(event.state)} · {event.stage} ·{" "}
                {event.attempt_identity ?? "尚无尝试"} ·{" "}
                {event.classification ?? "无错误分类"}
              </li>
            ))}
          </ol>
        ) : null}
      </ExecutionDetailShell>
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
