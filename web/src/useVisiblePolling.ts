import { useEffect, useRef, useState } from "react";

// Read-only requests have their own lifecycle; mutations never receive this signal.
export function useVisiblePolling<T>(
  load: (signal: AbortSignal, publish: (data: T) => void) => Promise<T>,
  keys: readonly unknown[],
  enabled = true,
  canRead: () => boolean = () => true,
  polling = true,
) {
  const loader = useRef(load);
  loader.current = load;
  const allowed = useRef(canRead);
  allowed.current = canRead;
  const sequence = useRef(0);
  const [state, setState] = useState<{
    data: T | null;
    busy: boolean;
    failure: string | null;
    stale: boolean;
    requestKeys: readonly unknown[];
  }>({
    data: null,
    busy: enabled,
    failure: null,
    stale: false,
    requestKeys: [],
  });
  const refresh = useRef<() => void>(() => {});
  const previousScope = useRef<readonly unknown[]>([]);
  useEffect(() => {
    let disposed = false;
    let running = false;
    let resume = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    const scope = [...keys, enabled];
    const sameScope =
      scope.length === previousScope.current.length &&
      scope.every((key, i) => Object.is(key, previousScope.current[i]));
    previousScope.current = scope;
    setState((old) =>
      sameScope
        ? { ...old, busy: enabled, requestKeys: scope }
        : {
            data: null,
            busy: enabled,
            failure: null,
            stale: false,
            requestKeys: scope,
          },
    );
    const visible = () => document.visibilityState !== "hidden";
    const schedule = () => {
      clearTimeout(timer);
      if (!disposed && enabled && visible() && polling)
        timer = setTimeout(run, 5000);
    };
    const run = async () => {
      if (disposed || !enabled || (polling && !visible())) return;
      if (running) {
        resume = true;
        return;
      }
      if (!allowed.current()) {
        schedule();
        return;
      }
      running = true;
      resume = false;
      controller = new AbortController();
      const request = controller;
      const version = sequence.current;
      setState((s) => ({ ...s, busy: true }));
      try {
        const data = await loader.current(request.signal, (data) => {
          if (
            !disposed &&
            !request.signal.aborted &&
            version === sequence.current
          )
            setState({
              data,
              busy: true,
              failure: null,
              stale: false,
              requestKeys: scope,
            });
        });
        if (
          !disposed &&
          !request.signal.aborted &&
          version === sequence.current
        )
          setState({
            data,
            busy: false,
            failure: null,
            stale: false,
            requestKeys: scope,
          });
      } catch {
        if (
          !disposed &&
          !request.signal.aborted &&
          version === sequence.current
        )
          setState((s) => ({
            ...s,
            busy: false,
            failure: "read_failed",
            stale: true,
          }));
      } finally {
        running = false;
        if (!disposed) {
          setState((s) => ({ ...s, busy: false }));
          if (resume && visible()) void run();
          else schedule();
        }
      }
    };
    const visibility = () => {
      clearTimeout(timer);
      if (visible()) void run();
      else {
        resume = false;
        controller?.abort();
      }
    };
    refresh.current = () => {
      clearTimeout(timer);
      void run();
    };
    if (polling) document.addEventListener("visibilitychange", visibility);
    void run();
    return () => {
      disposed = true;
      clearTimeout(timer);
      controller?.abort();
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [...keys, enabled, polling]);
  const requested = [...keys, enabled];
  const matches =
    requested.length === state.requestKeys.length &&
    requested.every((key, i) => Object.is(key, state.requestKeys[i]));
  return {
    ...state,
    ...(!matches
      ? { data: null, failure: null, busy: enabled, stale: false }
      : {}),
    refresh: () => refresh.current(),
    invalidate: () => {
      sequence.current += 1;
    },
    commit: (data: T) => {
      sequence.current += 1;
      setState((old) => ({
        ...old,
        data,
        busy: false,
        failure: null,
        stale: false,
      }));
    },
  };
}
