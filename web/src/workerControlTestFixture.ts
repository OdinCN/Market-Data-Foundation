// Deterministic frontend transport observation; never a live process claim.
export const onlineWorkerFixture = {
  registered: true,
  online: true,
  reason: "worker_online",
  process_id: 123,
  service_state: "active",
  worker: { state: "idle", last_heartbeat_at: null },
};
