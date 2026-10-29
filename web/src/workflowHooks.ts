import { useEffect, useState } from "react";
import { ApiFailure } from "./api";
import { PackageRequestFailure } from "./packageApi";
import { current, monotonicNow, receive } from "./Observability";
import type { Observation, Observability } from "./api";
import type { Snapshot } from "./Observability";
export const failureName = (e: unknown) =>
  e instanceof PackageRequestFailure
    ? e.detail.classification
    : e instanceof ApiFailure
      ? e.classification
      : "request_failed";
export function useResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
  keys: readonly unknown[],
  enabled = true,
) {
  const [state, setState] = useState<{
    data: T | null;
    failure: string | null;
    busy: boolean;
    startedAt: number;
    requestKeys: readonly unknown[];
  }>({
    data: null,
    failure: null,
    busy: false,
    startedAt: NaN,
    requestKeys: [],
  });
  useEffect(() => {
    const controller = new AbortController();
    const startedAt = monotonicNow();
    const requestKeys = [...keys, enabled];
    setState({
      data: null,
      failure: null,
      busy: enabled,
      startedAt,
      requestKeys,
    });
    if (enabled)
      void load(controller.signal)
        .then((data) => {
          if (!controller.signal.aborted)
            setState({
              data,
              failure: null,
              busy: false,
              startedAt,
              requestKeys,
            });
        })
        .catch((e) => {
          if (!controller.signal.aborted)
            setState({
              data: null,
              failure: failureName(e),
              busy: false,
              startedAt,
              requestKeys,
            });
        });
    return () => controller.abort();
  }, [...keys, enabled]);
  const requested = [...keys, enabled];
  const matches =
    requested.length === state.requestKeys.length &&
    requested.every((key, i) => Object.is(key, state.requestKeys[i]));
  // A changed route/query/generation cannot render old evidence for even one commit.
  return matches
    ? state
    : {
        data: null,
        failure: null,
        busy: enabled,
        startedAt: NaN,
        requestKeys: requested,
      };
}
export function useObservation(data: Observability | null) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  useEffect(() => {
    setSnapshot(data ? receive(data) : null);
  }, [data]);
  useEffect(() => {
    const timer = setInterval(() => {
      const now = monotonicNow();
      setSnapshot((previous) =>
        previous && previous.checkedAt !== null
          ? {
              ...previous,
              checkedAt:
                Number.isFinite(now) && now >= previous.checkedAt ? now : null,
            }
          : previous,
      );
    }, 1000);
    return () => clearInterval(timer);
  }, []);
  return (stamp?: Observation) =>
    !!(
      data &&
      snapshot?.data === data &&
      stamp &&
      current(data.evidence_observation, snapshot) &&
      current(stamp, snapshot)
    );
}
