// Deterministic worker observations for workflow tests. Never controls a process.
export function workerFixture() {
  const online = new Set<string>();
  return (asset: string, action?: string) => {
    if (action !== undefined) {
      if (action !== "start" && action !== "recover")
        throw new Error("unexpected_test_worker_action");
      online.add(asset);
    }
    const running = online.has(asset);
    return {
      registered: true,
      online: running,
      reason: running ? "worker_online" : "worker_service_stopped",
      service_state: running ? "active" : "inactive",
      process_id: running ? 123 : null,
      worker: {
        state: running ? "idle" : "stopped",
        last_heartbeat_at: null,
      },
    };
  };
}
