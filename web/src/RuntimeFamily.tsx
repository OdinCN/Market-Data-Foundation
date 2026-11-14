import { useState, type ComponentType, type ReactNode } from "react";
import { Tabs } from "./WorkflowTable";
import type { Transport } from "./api";
import type { Navigate, Route } from "./workflowRoute";
import type { Row } from "./WorkflowTable";
import { keyOf, sameKey } from "./workflowModel";
import { useVisiblePolling } from "./useVisiblePolling";

export type FamilyProps = {
  transport: Transport;
  route: Route;
  navigate: Navigate;
  generation: number;
  stale?: boolean;
};
export type FamilyExecution = {
  id: string;
  createdAt: string;
  title: string;
  search: string;
  scope?: string;
  start?: string;
  end?: string;
  result: ReactNode;
  schedule?: string;
};
export type PeriodicEntry = {
  row: Row;
  detail: ReactNode;
  refresh: () => void;
};
// Families provide domain facts and leaf controls. The shared workbench owns
// catalog selection, tabs, filters, ordering, pagination and detail placement.
export type RuntimeFamily = {
  asset: { identity: string; revision: string };
  title: string;
  Query: ComponentType<FamilyProps>;
  ApiHelp: ComponentType<FamilyProps>;
  Collection: ComponentType<FamilyProps>;
  ExecutionDetail: ComponentType<FamilyProps>;
  readExecutions: (
    transport: Transport,
    signal: AbortSignal,
  ) => Promise<FamilyExecution[]>;
  readPeriodic: (transport: Transport, signal: AbortSignal) => Promise<unknown>;
  periodic: (
    data: unknown,
    props: FamilyProps,
    refresh: () => void,
  ) => PeriodicEntry;
};
export const familyRowId = (family: RuntimeFamily, id: string) =>
  `${keyOf(family.asset)}:${id}`;
export function useFamilyRecords(
  families: readonly RuntimeFamily[],
  props: FamilyProps,
  invalid: boolean,
) {
  const tab = props.route.fields.get("tab");
  const filter =
    props.route.fields.get(
      tab === "periodic" ? "periodic_asset" : "execution_asset",
    ) ?? "all";
  const selected = invalid
    ? undefined
    : families.find((f) =>
        sameKey(f.asset, {
          identity: props.route.fields.get("asset") ?? "",
          revision: props.route.fields.get("asset_revision") ?? "",
        }),
      );
  const scope = families.filter(
    (f) => filter === "all" || filter === keyOf(f.asset),
  );
  const signature = scope.map((f) => keyOf(f.asset)).join("|");
  const executions = useVisiblePolling(
    async (signal) =>
      (
        await Promise.all(
          scope.map(async (family) => ({
            family,
            records: await family.readExecutions(props.transport, signal),
          })),
        )
      ).flatMap(({ family, records }) =>
        records.map((record) => ({ family, ...record })),
      ),
    [props.transport, props.generation, tab, signature],
    !invalid && tab === "executions" && scope.length > 0,
  );
  const periodic = useVisiblePolling(
    async (signal) =>
      Promise.all(
        scope.map(async (family) => ({
          family,
          data: await family.readPeriodic(props.transport, signal),
        })),
      ),
    [props.transport, props.generation, tab, signature],
    !invalid && tab === "periodic" && scope.length > 0,
  );
  return {
    selected,
    executions,
    periodic,
    periodicEntries: (periodic.data ?? []).map(({ family, data }) =>
      family.periodic(
        data,
        { ...props, stale: periodic.stale },
        periodic.refresh,
      ),
    ),
  };
}

export function FamilyAssetReader({
  family,
  ...props
}: FamilyProps & { family: RuntimeFamily }) {
  const [tab, setTab] = useState("query");
  return (
    <section
      aria-label={`${family.title} 范围查询`}
      className="ordinary-query cpi-timeseries macro-range-query"
    >
      <h3>{family.title} · 范围查询</h3>
      <Tabs
        label={`${family.title} 消费者页签`}
        values={[
          ["query", "查询数据"],
          ["api", "API说明"],
        ]}
        value={tab}
        onChange={setTab}
      />
      <div hidden={tab !== "query"}>
        <family.Query {...props} />
      </div>
      <div hidden={tab !== "api"}>
        <family.ApiHelp {...props} />
      </div>
    </section>
  );
}
