"""No live storage/provider: real scheduler and job-journal recovery contracts."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_data_foundation.web import financial_daily_api as api
from market_data_foundation.web.cpi_timeseries_jobs import WebError
from market_data_foundation.web.financial_daily_jobs import FinancialJobs, plan
from market_data_foundation.web.financial_daily_schedule import (
    GROUPS,
    MAX_ATTEMPTS,
    FinancialSchedule,
    slots,
)

AT = datetime(2026, 11, 5, 14, 20, tzinfo=UTC)  # 09:20 ET, after two groups.


class Jobs:
    def __init__(self, *, fail=False):
        self.records = {}
        self.calls = []
        self.fail = fail
        self.journal = SimpleNamespace(read=self.records.get)

    def submit(self, command, **kwargs):
        self.calls.append(("submit", command.copy(), kwargs))
        return self._result(command)

    def resume(self, request_id):
        command = self.records[request_id]["command"]
        self.calls.append(("resume", command.copy(), {}))
        return self._result(command)

    def _result(self, command):
        record = dict(
            command=command,
            state="partial" if self.fail else "complete",
            provider_requests=len(self.calls),
            steps=[
                dict(
                    state="failed" if self.fail else "complete",
                    series_id="DFEDTARL",
                    classification="financial_http_503",
                    retry_at=(AT + timedelta(minutes=30)).isoformat()
                    if self.fail
                    else None,
                )
            ],
        )
        self.records[command["request_id"]] = record
        return record


def test_read_and_paused_tick_never_create_policy_or_request_source(tmp_path):
    schedule = FinancialSchedule(tmp_path / "none", clock=lambda: AT)
    jobs = Jobs()
    assert not schedule.get()["enabled"] and not schedule.directory.exists()
    assert schedule.tick(jobs) == {"state": "paused"}
    assert not jobs.calls and not schedule.path.exists()


def test_source_slots_respect_dst_weekend_and_separate_times():
    assert [(g, t.hour, t.minute) for g, t in slots(AT)] == [
        ("policy", 14, 10),
        ("overnight", 14, 15),
    ]
    summer = datetime(2026, 7, 6, 13, 20, tzinfo=UTC)
    assert slots(summer)[0][1].hour == 13
    weekend = datetime(2026, 11, 7, 22, tzinfo=UTC)
    assert [g for g, _ in slots(weekend)] == ["policy"]
    assert all(t > weekend for _, t in slots(weekend, future=True))


def test_enable_pause_restart_replays_same_slot_without_new_job(tmp_path):
    schedule = FinancialSchedule(tmp_path, clock=lambda: AT)
    jobs = Jobs()
    schedule.enabled(True)
    first = schedule.tick(jobs)
    assert first["group"] == "policy" and first["state"] == "complete"
    assert jobs.calls[0][2]["selected"] == GROUPS["policy"][0]
    assert jobs.calls[0][2]["origin"] == "periodic"
    assert jobs.calls[0][1]["start"] == "2026-10-23"
    assert jobs.calls[0][1]["end"] == "2026-11-05"
    schedule.enabled(False)
    assert schedule.tick(jobs)["state"] == "paused" and len(jobs.calls) == 1
    schedule = FinancialSchedule(tmp_path, clock=lambda: AT)
    schedule.enabled(True)
    second = schedule.tick(jobs)
    assert second["group"] == "overnight"
    assert schedule.tick(jobs)["state"] == "not_due" and len(jobs.calls) == 2
    assert schedule.get()["last_successful_collection_at"] == AT.isoformat()


def test_partial_cooldown_resumes_same_job_and_keeps_other_group_independent(tmp_path):
    clock = [AT]
    schedule = FinancialSchedule(tmp_path, clock=lambda: clock[0])
    jobs = Jobs(fail=True)
    schedule.enabled(True)
    failed = schedule.tick(jobs)
    assert failed["state"] == "failed" and failed["attempts"] == 1
    assert schedule.get()["last_successful_collection_at"] is None
    schedule.tick(jobs)  # independent overnight group during policy cooldown
    assert len(jobs.calls) == 2
    assert schedule.tick(jobs)["state"] == "not_due"
    clock[0] = AT + timedelta(minutes=31)
    jobs.fail = False
    schedule = FinancialSchedule(tmp_path, clock=lambda: clock[0])
    resumed = schedule.tick(jobs)
    assert (
        jobs.calls[-1][0] == "resume" and resumed["request_id"] == failed["request_id"]
    )
    assert resumed["attempts"] == 2 and resumed["state"] == "complete"


def test_repeated_failure_exhausts_per_slot_without_infinite_upstream_calls(tmp_path):
    clock = [AT]
    schedule = FinancialSchedule(tmp_path, clock=lambda: clock[0])
    schedule.enabled(True)
    jobs = Jobs(fail=True)
    for _ in range(10):
        schedule.tick(jobs)
        clock[0] += timedelta(minutes=5)
    assert len(jobs.calls) == 2 * MAX_ATTEMPTS
    assert all(t["attempts"] == MAX_ATTEMPTS for t in schedule.get()["tasks"])
    assert all(t["state"] == "failed" for t in schedule.get()["tasks"])


def test_inflight_shared_job_lock_does_not_consume_source_attempt(tmp_path):
    schedule = FinancialSchedule(tmp_path, clock=lambda: AT)
    schedule.enabled(True)
    jobs = Jobs()
    jobs.submit = lambda *a, **k: (_ for _ in ()).throw(
        WebError("financial_jobs_busy", 409)
    )
    assert schedule.tick(jobs)["attempts"] == 0
    assert not jobs.calls


def test_single_tick_lock_blocks_duplicate_executor(tmp_path):
    schedule = FinancialSchedule(tmp_path, clock=lambda: AT)
    with (
        schedule.lock("tick.lock"),
        pytest.raises(WebError, match="financial_schedule_busy"),
    ):
        schedule.tick(Jobs())


def test_revisions_use_source_group_and_derived_legs_order_without_credit_archive():
    command = dict(action="update", start="2026-10-23", end="2026-11-05")
    steps = plan(command, GROUPS["treasury"][0])
    assert [s["series_id"] for s in steps] == ["DGS2", "DGS10", "DFII10", "T10YIE"]
    assert steps[-1]["source"] == "derived"
    assert all(s["source"] == "fred" for s in plan(command, GROUPS["credit"][0]))


def test_periodic_job_resume_keeps_original_identity_and_selection(
    tmp_path, monkeypatch
):
    from tests.test_financial_daily_jobs import command

    host = FinancialJobs(tmp_path)
    c = command()
    record = dict(
        request_id=c["request_id"],
        command=c,
        state="partial",
        origin="periodic",
        selected_series=["DGS2"],
        schedule_slot="treasury:known",
    )
    host.journal.save(record)
    calls = []
    monkeypatch.setattr(host, "submit", lambda *a, **k: calls.append((a, k)))
    host.resume(c["request_id"])
    assert calls[0][1]["selected"] == ("DGS2",)
    assert (
        calls[0][1]["origin"] == "periodic"
        and calls[0][1]["schedule_slot"] == "treasury:known"
    )


def test_strategy_get_is_read_only_private_writes_and_unknown_failure(
    tmp_path, monkeypatch
):
    class Control:
        def __init__(self, *a, **k):
            pass

        def inspect(self, *a):
            return dict(
                online=False, registered=True, reason="worker_service_not_started"
            )

        def act(self, *a):
            raise WebError("worker_service_action_failed", 503)

    monkeypatch.setattr(api, "DevWorkerControl", Control)
    monkeypatch.setattr(api, "bound_fred_credential", lambda: None)
    options = api.FinancialOptions(
        tmp_path / "jobs", True, True, tmp_path / "state", tmp_path / "window", True
    )
    app = FastAPI()
    app.include_router(api.financial_router(options))
    with TestClient(
        app, base_url="http://192.168.10.20:18761", client=("127.0.0.1", 50000)
    ) as client:
        assert client.get(api.PREFIX + "/strategy").json()["enabled"] is False
        assert not (tmp_path / "state").exists()
        command = dict(enabled=True)
        assert (
            client.post(
                api.PREFIX + "/strategy",
                json=command,
                headers={"host": "odin.pub:18761", "origin": "http://odin.pub:18761"},
            ).status_code
            == 403
        )
        response = client.post(
            api.PREFIX + "/strategy",
            json=command,
            headers={"origin": "http://192.168.10.20:18761"},
        )
        assert (
            response.status_code == 503
            and response.json()["classification"] == "worker_service_action_failed"
        )
        assert not (tmp_path / "state/schedule.json").exists()
        assert (
            client.post(
                api.PREFIX + "/strategy",
                json={"enabled": False, "unit": "other"},
                headers={"origin": "http://192.168.10.20:18761"},
            ).status_code
            == 400
        )


def test_scheduler_retries_normal_job_retained_commit_without_refetch(
    tmp_path, monkeypatch
):
    from market_data_foundation.historical.financial_daily import FinancialError
    from market_data_foundation.web import financial_daily_jobs as module
    from tests.test_financial_daily import payload
    from tests.test_financial_daily_jobs import Raw

    clock = [AT]
    schedule = FinancialSchedule(tmp_path / "state", clock=lambda: clock[0])
    source = SimpleNamespace(request_count=0)
    batch = payload("DFEDTARL")
    fetched = []
    committed = []
    fail = [True]

    def fetch(*a, **k):
        fetched.append(a)
        source.request_count += 1
        return batch

    source.fetch = fetch

    def save(value):
        if fail[0]:
            raise FinancialError("financial_storage_outcome_unknown")
        committed.append(value.batch_id)
        return dict(batch_id=value.batch_id, inserted_versions=1)

    store = SimpleNamespace(
        raw_store=Raw(),
        save=save,
        record_attempt=lambda *a, **k: None,
        close=lambda: None,
    )
    monkeypatch.setattr(module, "coverage", lambda *a, **k: dict(series=[]))
    monkeypatch.setitem(GROUPS, "policy", (("DFEDTARL",), ("09:10",), False))
    jobs = FinancialJobs(
        tmp_path / "jobs",
        service_factory=lambda **k: SimpleNamespace(source=source, store=store),
    )
    schedule.enabled(True)
    first = schedule.tick(jobs)
    assert first["state"] == "failed" and len(fetched) == 1
    before = jobs.journal.read(first["request_id"])
    fail[0] = False
    clock[0] += timedelta(minutes=6)
    after = schedule.tick(jobs)
    assert after["request_id"] == first["request_id"] and after["state"] == "complete"
    record = jobs.journal.read(first["request_id"])
    assert record["resume_count"] == 1 and record["origin"] == "periodic"
    assert record["steps"][0]["retained"] == before["steps"][0]["retained"]
    assert len(fetched) == 1 and committed == [batch.batch_id]


def test_publication_waiting_through_real_job_and_store_is_not_storage_unknown(
    tmp_path, monkeypatch
):
    from market_data_foundation.historical.financial_daily import FinancialError
    from market_data_foundation.web import financial_daily_jobs as module
    from tests.test_financial_daily_jobs import attempt_store

    at = AT.replace(minute=11)  # Only the policy slot has become due.
    schedule = FinancialSchedule(tmp_path / "state", clock=lambda: at)
    store = attempt_store()
    calls = []

    class Source:
        request_count = 0

        def fetch(self, *args, **kwargs):
            calls.append(args)
            raise FinancialError("financial_origin_future_window_not_acquired")

    monkeypatch.setattr(module, "coverage", lambda *a, **k: dict(series=[]))
    monkeypatch.setitem(GROUPS, "policy", (("DFEDTARL",), ("09:10",), False))
    jobs = FinancialJobs(
        tmp_path / "jobs",
        service_factory=lambda **k: SimpleNamespace(source=Source(), store=store),
    )
    schedule.enabled(True)
    task = schedule.tick(jobs)
    receipt = jobs.journal.read(task["request_id"])
    assert receipt["state"] == "partial" and task["job_state"] == "partial"
    assert task["provider_requests"] == 0
    assert task["failures"][0]["state"] == "waiting_publication"
    assert task["failures"][0]["classification"] == (
        "financial_origin_future_window_not_acquired"
    )
    assert store.connection.attempts[0][4:6] == (
        "failed",
        "financial_origin_future_window_not_acquired",
    )
    assert schedule.tick(jobs)["state"] == "not_due"
    assert len(calls) == 1 and schedule.get()["last_successful_collection_at"] is None


def test_financial_write_admission_is_applied_in_composed_web(tmp_path, monkeypatch):
    from tests.test_cpi_timeseries_web_host import client, setup

    app, *_ = setup(
        tmp_path,
        monkeypatch,
        financial_options=api.FinancialOptions(
            tmp_path / "jobs", True, True, tmp_path / "state", tmp_path / "window", True
        ),
    )
    with client(app) as http:
        private = http.post(
            api.PREFIX + "/strategy",
            json={"enabled": False},
            headers={"Origin": "http://192.168.10.20:18761"},
        )
        assert private.status_code == 200 and private.json()["enabled"] is False
        public = http.post(
            api.PREFIX + "/strategy",
            json={"enabled": True},
            headers={"Host": "odin.pub:18761", "Origin": "http://odin.pub:18761"},
        )
        assert (
            public.status_code == 405
        )  # composed public Host rejects sensitive methods
        assert not (tmp_path / "state/schedule.json").exists()


def test_product_resume_of_exhausted_job_is_reconciled_while_paused_without_fetch(
    tmp_path,
):
    clock = [AT]
    schedule = FinancialSchedule(tmp_path, clock=lambda: clock[0])
    schedule.enabled(True)
    jobs = Jobs(fail=True)
    for _ in range(10):
        schedule.tick(jobs)
        clock[0] += timedelta(minutes=5)
    assert all(t["attempts"] == MAX_ATTEMPTS for t in schedule.get()["tasks"])
    original_calls = len(jobs.calls)
    fixed = schedule.get()["tasks"][0]
    # Normal product resume is independent of the scheduler.
    jobs.records[fixed["request_id"]].update(
        state="complete", finished_at=clock[0].isoformat()
    )
    schedule.enabled(False)
    assert schedule.tick(jobs)["state"] == "paused"
    matched = next(
        t for t in schedule.get()["tasks"] if t["request_id"] == fixed["request_id"]
    )
    assert (
        matched["state"] == "complete"
        and matched["completed_at"] == clock[0].isoformat()
    )
    assert matched["attempts"] == MAX_ATTEMPTS and len(jobs.calls) == original_calls


@pytest.mark.parametrize(
    "at,policy,treasury",
    [
        # At each DST boundary, use the NEXT dated slot, not a fixed UTC offset.
        (
            "2026-03-07T23:00:00+00:00",
            "2026-03-08T13:10:00+00:00",
            "2026-03-09T22:30:00+00:00",
        ),
        (
            "2026-10-31T23:00:00+00:00",
            "2026-11-01T14:10:00+00:00",
            "2026-11-02T23:30:00+00:00",
        ),
    ],
)
def test_read_projection_exposes_dated_next_group_checks_without_writes(
    tmp_path, at, policy, treasury
):
    schedule = FinancialSchedule(
        tmp_path / "absent", clock=lambda: datetime.fromisoformat(at)
    )
    view = schedule.get()
    next_checks = {v["group"]: v["at"] for v in view["next_group_checks"]}
    assert next_checks["policy"] == policy
    assert next_checks["treasury"] == treasury
    assert set(next_checks) == set(GROUPS)
    assert view["enabled"] is False and view["next_check_at"] is None
    assert view["updated_at"] is None
    assert not schedule.directory.exists()
