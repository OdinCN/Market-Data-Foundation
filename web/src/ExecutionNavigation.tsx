import { createContext, useContext, type ReactNode } from "react";
import type { Transport } from "./api";
import type { CollectionRegistration } from "./collectionRegistry";
import { readCpiStrategy } from "./cpiTimeSeriesApi";
import { useVisiblePolling } from "./useVisiblePolling";
import { HISTORY_OPERATION, keyOf, sameKey, type Key } from "./workflowModel";
import type { Navigate } from "./workflowRoute";
import type { RuntimeFamily } from "./RuntimeFamily";

type Binding = { id: string; relation: "associated" | "asset-strategy" };
const Context = createContext<{
  asset: string;
  job: string;
  binding: Binding | null;
  open?: () => void;
} | null>(null);

/** Every normal execution detail inherits navigation. A declared endpoint is
 * not a verified target; stale/failed reads must never create a plan link. */
export function ExecutionNavigation({
  asset,
  job,
  registration,
  family,
  associatedPlan,
  transport,
  navigate,
  generation,
  children,
}: {
  asset?: Key;
  job: string | null;
  registration?: CollectionRegistration;
  family?: RuntimeFamily;
  associatedPlan?: string;
  transport: Transport;
  navigate: Navigate;
  generation: number;
  children: ReactNode;
}) {
  const strategy = sameKey(asset, registration?.asset)
    ? registration?.strategy
    : null;
  const read = useVisiblePolling(
    async (signal) => {
      if (family && sameKey(family.asset, asset)) {
        const data = await family.readPeriodic(transport, signal);
        const entry = family.periodic(
          data,
          {
            transport,
            navigate,
            generation,
            route: {
              view: "collection",
              invalid: false,
              fields: new URLSearchParams({
                tab: "periodic",
                schedule: strategy!.id,
              }),
            },
          },
          () => {},
        );
        if (entry.row.id !== strategy!.id) throw Error("strategy_unverified");
        return true;
      }
      if (registration?.profile) {
        await readCpiStrategy(transport, signal, registration.profile);
        // The periodic workbench also has a real registered preset row before
        // its enable intent is persisted. It remains a readable asset policy.
        return true;
      }
      const response = await transport(strategy!.path, { signal });
      if (!response.ok) throw Error("strategy_unavailable");
      const value = await response.json();
      if (
        typeof value?.enabled !== "boolean" ||
        (registration?.protocol === "financial"
          ? value.status !== "OK" ||
            !Array.isArray(value.tasks) ||
            typeof value.schedule_semantics !== "string"
          : registration?.protocol === "tdx" ||
              registration?.protocol === "roster"
            ? typeof value.worker_online !== "boolean"
            : true)
      )
        throw Error("strategy_unverified");
      return true;
    },
    [transport, generation, asset && keyOf(asset), job, strategy?.path, family],
    !!asset && !!job && !!strategy,
  );
  const binding: Binding | null = associatedPlan
    ? { id: associatedPlan, relation: "associated" }
    : read.data === true && !read.stale && strategy
      ? { id: strategy.id, relation: "asset-strategy" }
      : null;
  return (
    <Context.Provider
      value={
        asset && job
          ? {
              asset: keyOf(asset),
              job,
              binding,
              open: binding
                ? () =>
                    navigate("collection", {
                      asset: asset.identity,
                      asset_revision: asset.revision,
                      operation:
                        binding.relation === "associated"
                          ? HISTORY_OPERATION
                          : null,
                      tab: "periodic",
                      periodic_asset: keyOf(asset),
                      schedule: binding.id,
                      job,
                    })
                : undefined,
            }
          : null
      }
    >
      {children}
    </Context.Provider>
  );
}

export function useExecutionNavigation(asset: string, job: string) {
  const value = useContext(Context);
  return value?.asset === asset && value.job === job ? value : null;
}

export function PeriodicNavigation({
  asset,
  id,
  job,
  navigate,
}: {
  asset: Key;
  id: string;
  job?: string | null;
  navigate: Navigate;
}) {
  const fields = {
    asset: asset.identity,
    asset_revision: asset.revision,
    operation: null,
  };
  return (
    <div
      className="actions"
      aria-label="周期操作"
      data-periodic-id={id}
      data-periodic-asset={keyOf(asset)}
    >
      {job && (
        <button
          onClick={() =>
            navigate("collection", {
              ...fields,
              tab: "executions",
              execution_schedule: "all",
              schedule: null,
              job,
            })
          }
        >
          返回此执行记录
        </button>
      )}
      <button
        onClick={() =>
          navigate("collection", {
            ...fields,
            tab: "executions",
            execution_asset: keyOf(asset),
            execution_schedule: id,
            schedule: null,
            job: null,
          })
        }
      >
        查看执行记录
      </button>
      <button
        onClick={() =>
          navigate("assets", {
            ...fields,
            tab: "local",
            package: null,
            job: job ?? null,
          })
        }
      >
        查看资产数据
      </button>
    </div>
  );
}
