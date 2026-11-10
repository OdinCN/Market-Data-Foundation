import { expect, it } from "vitest";
import {
  autoCollectionDiagnosis,
  type DiagnosisEntry,
} from "./autoCollectionDiagnosis";
const now = Date.parse("2026-11-04T18:50:00Z");
function entry(targets: Record<string, unknown>[] = []): DiagnosisEntry {
  return {
    data: {
      exists: true,
      enabled: true,
      history_start: "1992-01",
      worker: { state: "idle", last_heartbeat_at: null },
      next_due: null,
      next_release_at: "2026-11-17T13:30:00Z",
      policy: {},
      control: {
        registered: true,
        online: true,
        service_state: "active",
        process_id: 123,
        reason: "worker_online",
        worker: { state: "idle", last_heartbeat_at: null },
      },
      schedule: {
        calendar_status: "fresh",
        targets: [
          {
            reference_month: "2026-10",
            release_at: "2026-11-17T13:30:00Z",
            target: { state: "pending" },
          },
          ...targets,
        ],
      },
    },
    saving: false,
    uncertain: false,
    resource: { stale: false },
  };
}
it("keeps the real retail far TBA isolated from current release and does not use manual success as a diagnosis", () => {
  const v = entry([
    {
      reference_month: "2027-12",
      release_at: null,
      publication_status: "TBA",
      calendar_execution_blocked: true,
      calendar_validation: "calendar_fallback_not_covered",
      target: { state: "pending", attempts: 0 },
      history: { state: "blocked", attempts: 0 },
    },
  ]);
  v.data!.last_result = { state: "complete", origin: "manual" };
  const d = autoCollectionDiagnosis(v, now);
  expect(d.state).toBe("待发布");
  expect(d.summary).toContain("2026-10");
  expect(d.summary).toContain("2026-11-17 21:30:00");
  expect(d.issues).toHaveLength(0);
  expect(d.advisories[0].period).toBe("2027-12");
  expect(d.advisories[0].impact).toContain("不影响2026-10");
});
it.each([
  { reference_month: "2026-10", release_at: null, publication_status: "TBA" },
  {
    reference_month: "2027-12",
    release_at: "2027-12-15T13:30:00Z",
    publication_status: "scheduled",
  },
  {
    reference_month: "2027-12",
    release_at: null,
    publication_status: "TBA",
    target: { attempts: 1, state: "pending" },
  },
])(
  "does not downgrade current, known conflicting, or attempted blocked periods: %j",
  (target) => {
    const d = autoCollectionDiagnosis(
      entry([
        {
          ...target,
          calendar_execution_blocked: true,
          calendar_validation: "calendar_conflict",
        },
      ]),
      now,
    );
    expect(d.state).toBe("需处理");
    expect(d.issues[0].reason).toContain("冲突");
    expect(d.issues[0].targetKey).toBeTruthy();
    expect(d.advisories).toHaveLength(0);
  },
);
it("retains a historical failure separately from a healthy upcoming plan and successful unrelated receipt", () => {
  const v = entry([
    {
      reference_month: "2026-08",
      release_at: "2026-09-15T12:30:00Z",
      target: { state: "failed", classification: "provider_http_403" },
    },
  ]);
  v.data!.last_result = { state: "complete", origin: "manual" };
  const d = autoCollectionDiagnosis(v, now);
  expect(d.state).toBe("待发布；历史需处理");
  expect(d.issues[0].summary).toContain("2026-08");
  expect(d.issues[0].reason).toContain("HTTP403");
  expect(d.issues[0].impact).toContain("不以其他期次或手动成功");
});
it.each([
  ["outcome_unknown", null, "待核实", "回执"],
  ["failed", "2026-11-05T01:00:00Z", "重试中", "09:00:00"],
  ["failed", null, "需处理", "失败分类"],
])(
  "explains %s with period, impact and next step",
  (state, retry, stateText, next) => {
    const v = entry([
      {
        reference_month: "2026-11",
        release_at: "2026-11-04T18:00:00Z",
        target: {
          state,
          next_attempt_at: retry,
          classification: state === "failed" ? "provider_http_429" : undefined,
        },
      },
    ]);
    const d = autoCollectionDiagnosis(v, now);
    expect(d.state).toBe(stateText);
    expect(d.issues[0].period).toBe("2026-11");
    expect(d.issues[0].impact).toBeTruthy();
    expect(d.issues[0].next).toContain(next!);
    expect(d.listReason).toBe(d.issues[0].summary);
  },
);
it("keeps service/source/plan failures distinct without invented repair actions", () => {
  const v = entry();
  v.data!.control!.online = false;
  v.data!.control!.reason = "worker_heartbeat_stale";
  expect(autoCollectionDiagnosis(v, now).issues[0].reason).toContain("心跳");
  v.data!.control = null;
  expect(autoCollectionDiagnosis(v, now).issues[0].next).toContain(
    "不重复启动",
  );
  v.data!.control = {
    registered: true,
    online: true,
    reason: "worker_online",
    service_state: "active",
    process_id: 1,
    worker: { state: "idle", last_heartbeat_at: null },
  };
  v.data!.schedule = null;
  expect(autoCollectionDiagnosis(v, now).issues[0].reason).toContain(
    "计划读取",
  );
});

it("does not hide an unresolved historical failure when automatic collection is paused", () => {
  const v = entry([
    {
      reference_month: "2026-08",
      release_at: "2026-09-16T12:30:00Z",
      target: { state: "failed", classification: "provider_http_403" },
    },
  ]);
  v.data!.enabled = false;
  const d = autoCollectionDiagnosis(v, now);
  expect(d.state).toBe("已暂停");
  expect(d.issues[0].period).toBe("2026-08");
});

it.each([
  [{ calendar_validation: "calendar_fallback_conflict" }, "备用日历与已保留"],
  [
    {
      status: "cancelled",
      calendar_validation: "calendar_fallback_not_covered",
    },
    "移除",
  ],
  [{ calendar_validation: "calendar_conflict" }, "冲突"],
  [
    {
      calendar_validation: "calendar_fallback_not_covered",
      state: "calendar_removed",
    },
    "移除",
  ],
  [
    {
      calendar_validation: "calendar_fallback_not_covered",
      cancellation_evidence: { source: "official" },
    },
    "移除",
  ],
  [
    {
      calendar_validation: "calendar_fallback_not_covered",
      calendar_cancelled_after_completion: true,
    },
    "完成后",
  ],
  [
    {
      calendar_validation: "calendar_fallback_not_covered",
      calendar_changed_after_completion: true,
    },
    "完成后",
  ],
  [{ calendar_validation: "calendar_moved_after_execution" }, "已有执行记录"],
  [
    { calendar_validation: "gdp_calendar_moved_after_execution" },
    "已有执行记录",
  ],
  [
    {
      publication_status: "cancelled",
      calendar_validation: "calendar_fallback_not_covered",
    },
    "移除",
  ],
] as const)(
  "explicit calendar problem outranks unchanged TBA/null/no-attempt placeholder: %j",
  (problem, reason) => {
    const d = autoCollectionDiagnosis(
      entry([
        {
          reference_month: "2027-12",
          publication_status: "TBA",
          release_at: null,
          calendar_execution_blocked: true,
          target: { state: "pending", attempts: 0 },
          history: { state: "blocked", attempts: 0 },
          ...problem,
        },
      ]),
      now,
    );
    expect(d.state).toBe("需处理");
    expect(d.advisories).toHaveLength(0);
    expect(d.issues[0].period).toBe("2027-12");
    expect(d.issues[0].reason).toContain(reason);
    expect(d.issues[0].impact).toContain("不影响2026-10");
    expect(d.issues[0].next).toContain("不自动重放");
    expect(d.summary).toContain("2026-11-17 21:30:00");
  },
);

it("shows the stopped employment October target even with an online worker and a December next release", () => {
  const v = entry([]);
  v.data!.schedule!.targets = [
    {
      reference_month: "2026-10",
      release_at: "2026-11-06T13:30:00Z",
      target_available: false,
      target: {
        state: "awaiting_provider",
        attempts: 1,
        next_attempt_at: null,
        classification: "schedule_actual_http_unknown",
      },
      auto: {
        state: "stopped",
        actual_http: 1,
        reserved_http: 1,
        next_attempt_at: null,
        classification: "schedule_actual_http_unknown",
      },
      history: { state: "blocked", attempts: 0 },
    },
    {
      reference_month: "2026-11",
      release_at: "2026-12-04T13:30:00Z",
      target: { state: "pending" },
    },
  ];
  const d = autoCollectionDiagnosis(v, Date.parse("2026-11-10T06:00:00Z"));
  expect(d.state).toContain("待核实");
  expect(d.state).not.toContain("重试中");
  expect(d.listReason).toContain("2026-10");
  expect(d.listReason).toContain("自动重试已停止");
  expect(d.issues[0].next).toContain("未知预留不释放");
});

it("does not mistake an explicit verified recovery for the original stopped automatic success", () => {
  const v = entry([]);
  v.data!.schedule!.targets = [
    {
      reference_month: "2026-09",
      release_at: "2026-10-02T13:30:00Z",
      target_available: true,
      target: { state: "complete", recovered_by_job: "new-job" },
      auto: {
        state: "stopped",
        actual_http: 1,
        reserved_http: 1,
        classification: "schedule_actual_http_unknown",
      },
      recovery: {
        state: "confirmed",
        kind: "operator_job_not_natural_capture",
        job_id: "new-job",
      },
      history: { state: "blocked", attempts: 0 },
    },
    {
      reference_month: "2026-10",
      release_at: "2026-11-17T13:30:00Z",
      target: { state: "pending" },
    },
  ];
  const d = autoCollectionDiagnosis(v, now);
  expect(d.state).toBe("待发布");
  expect(d.issues).toEqual([]);
});

it.each([
  "cpi_timeseries_storage_outcome_unknown",
  "schedule_release_window_expired",
])(
  "explains a stopped %s without pretending another retry is scheduled",
  (code) => {
    const v = entry([
      {
        reference_month: "2026-09",
        release_at: "2026-10-01T13:30:00Z",
        target: {
          state: "awaiting_provider",
          attempts: 1,
          classification: code,
        },
        auto: { state: "stopped", classification: code },
        history: { state: "blocked", attempts: 0 },
      },
    ]);
    const d = autoCollectionDiagnosis(v, now);
    expect(d.state).not.toContain("重试中");
    expect(d.listReason).toContain("2026-09");
    expect(d.issues[0].reason).toContain("停止");
  },
);
