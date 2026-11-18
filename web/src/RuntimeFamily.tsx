import type { CollectionContract } from "./collectionRegistry";
import { useRef, useState, type ComponentType, type ReactNode } from "react";
import { Tabs, Technical } from "./WorkflowTable";
import type { Transport } from "./api";
import type { Navigate, Route } from "./workflowRoute";
import type { Row } from "./WorkflowTable";
import { keyOf, sameKey } from "./workflowModel";
import { useVisiblePolling } from "./useVisiblePolling";
import { collectionReadScope } from "./collectionReadScope";

export type FamilyProps = {
  transport: Transport;
  route: Route;
  navigate: Navigate;
  generation: number;
  stale?: boolean;
  execution?: FamilyExecution;
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
  facts?: unknown;
};
export type RecordReadError = {
  request_id: string;
  classification: string;
  cause_classification?: string;
};
// Preserve the existing array reader contract, with optional batch diagnostics.
export type FamilyExecutionRecords = FamilyExecution[] & {
  readErrors?: RecordReadError[];
};
export function recordsWithReadErrors(
  records: FamilyExecution[],
  readErrors: RecordReadError[] = [],
): FamilyExecutionRecords {
  return Object.assign(records, { readErrors });
}
export function RecordReadErrors({
  errors = [],
}: {
  errors?: RecordReadError[];
}) {
  if (!errors.length) return null;
  return (
    <div role="alert" aria-label="采集记录局部读取失败">
      <p>
        部分采集记录不可读；以下原号的资产、来源和状态尚未核实。健康记录可继续查看。
      </p>
      <ul>
        {errors.map((error) => (
          <li key={`${error.request_id}:${error.classification}`}>
            <Technical>{error.request_id}</Technical> · {error.classification}
            {error.cause_classification && ` · ${error.cause_classification}`}
          </li>
        ))}
      </ul>
    </div>
  );
}
export type PeriodicEntry = {
  title?: ReactNode;
  row: Row;
  detail: ReactNode;
  refresh: () => void;
};
// Families provide domain facts and leaf controls. The shared workbench owns
// catalog selection, tabs, filters, ordering, pagination and detail placement.
export type RuntimeFamily = {
  asset: { identity: string; revision: string };
  title: string;
  collection: CollectionContract | null;
  Query: ComponentType<FamilyProps>;
  ApiHelp: ComponentType<FamilyProps>;
  Collection: ComponentType<FamilyProps>;
  ExecutionDetail: ComponentType<FamilyProps>;
  readExecutions: (
    transport: Transport,
    signal: AbortSignal,
  ) => Promise<FamilyExecutionRecords>;
  readPeriodic: (transport: Transport, signal: AbortSignal) => Promise<unknown>;
  periodic: (
    data: unknown,
    props: FamilyProps,
    refresh: () => void,
  ) => PeriodicEntry;
};
export const familyRowId = (family: RuntimeFamily, id: string) =>
  `${keyOf(family.asset)}:${id}`;

function useFamilyRead<T>(
  scope: readonly RuntimeFamily[],
  props: FamilyProps,
  enabled: boolean,
  read: (family: RuntimeFamily, signal: AbortSignal) => Promise<T>,
) {
  type Entry = { family: RuntimeFamily; data: T };
  const signature = scope.map((f) => keyOf(f.asset)).join("|");
  const retained = useRef(new Map<string, Entry>());
  const owner = useRef({
    transport: props.transport,
    generation: props.generation,
  });
  if (
    owner.current.transport !== props.transport ||
    owner.current.generation !== props.generation
  ) {
    retained.current.clear();
    owner.current = {
      transport: props.transport,
      generation: props.generation,
    };
  }
  const resource = useVisiblePolling<{ values: Entry[]; failures: string[] }>(
    async (signal, publish) => {
      const values = new Map(
        [...retained.current].filter(([key]) =>
          scope.some((f) => keyOf(f.asset) === key),
        ),
      );
      const failures = new Set<string>();
      const snapshot = () => ({
        values: [...values.values()],
        failures: [...failures],
      });
      await Promise.all(
        scope.map(async (family) => {
          const key = keyOf(family.asset);
          try {
            const data = await read(family, signal);
            if (signal.aborted) return;
            const entry = { family, data };
            values.set(key, entry);
            retained.current.set(key, entry);
          } catch {
            if (signal.aborted) return;
            failures.add(key);
          }
          publish(snapshot());
        }),
      );
      return snapshot();
    },
    [
      props.transport,
      props.generation,
      props.route.fields.get("tab"),
      signature,
    ],
    enabled && scope.length > 0,
  );
  return {
    ...resource,
    failure:
      resource.failure ??
      (resource.data?.failures.length ? "family_read_failed" : null),
    stale: resource.stale || !!resource.data?.failures.length,
  };
}
export function useFamilyRecords(
  families: readonly RuntimeFamily[],
  props: FamilyProps,
  invalid: boolean,
) {
  const tab = props.route.fields.get("tab");
  const readScope = collectionReadScope(props.route, invalid);
  const selected = invalid
    ? undefined
    : families.find((f) =>
        sameKey(f.asset, {
          identity: props.route.fields.get("asset") ?? "",
          revision: props.route.fields.get("asset_revision") ?? "",
        }),
      );
  const scope = families.filter((f) => readScope.reads(f.asset));
  const readExecutions = useFamilyRead(
    scope,
    props,
    !invalid && tab === "executions",
    (family, signal) => family.readExecutions(props.transport, signal),
  );
  const executions = {
    ...readExecutions,
    readErrors: [
      ...new Map(
        (readExecutions.data?.values ?? []).flatMap(({ data }) =>
          (data.readErrors ?? []).map(
            (error) =>
              [`${error.request_id}:${error.classification}`, error] as const,
          ),
        ),
      ).values(),
    ],
    data: readExecutions.data
      ? readExecutions.data.values.flatMap(({ family, data }) =>
          data.map((record) => ({ family, ...record })),
        )
      : null,
  };
  const periodic = useFamilyRead(
    scope,
    props,
    !invalid && tab === "periodic",
    (family, signal) => family.readPeriodic(props.transport, signal),
  );
  return {
    selected,
    executions,
    periodic,
    periodicEntries: (periodic.data?.values ?? []).map(({ family, data }) => ({
      title: family.title,
      ...family.periodic(
        data,
        { ...props, stale: periodic.stale },
        periodic.refresh,
      ),
    })),
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
