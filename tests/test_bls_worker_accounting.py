"""Real BLS service/worker/scheduler accounting; all source and storage IO isolated."""

import copy
import json
import socket
from datetime import UTC, datetime, timedelta

import pytest

from market_data_foundation.historical.bls_cpi_direct_live import HttpResult
from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    EMPLOYMENT_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.historical.cpi_release_schedule import CpiReleaseScheduler
from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesService
from market_data_foundation.historical.cpi_timeseries_cli import WorkerService
from market_data_foundation.historical.cpi_timeseries_source import BlsRangeSource
from market_data_foundation.historical.cpi_timeseries_store import (
    CpiStorageError,
    InMemoryTimeSeriesStore,
)
from tests.test_employment_web import setup as employment_setup


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("deterministic accounting tests must not open a socket")

    monkeypatch.setattr(socket.socket, "connect", forbidden)


class Clock:
    def __init__(self):
        self.at = datetime(2026, 11, 10, 13, 30, tzinfo=UTC)

    def __call__(self):
        return self.at


class Transport:
    def __init__(self, profile, mode):
        self.profile, self.mode, self.calls = profile, mode, 0

    def post(self, *args, **kwargs):
        self.calls += 1
        if self.mode == "timeout":
            raise TimeoutError("must not expose this raw error")
        if self.mode == "429":
            return HttpResult(429, b"", {"retry-after": "300"})
        if self.mode == "unknown":
            raise AssertionError("unknown is injected after the service boundary")
        points = (
            []
            if self.mode == "unpublished"
            else [
                dict(
                    year="2026",
                    period="M10",
                    periodName="October",
                    value="-" if self.mode == "null" else "100",
                    footnotes=[{}],
                )
            ]
        )
        return HttpResult(
            200,
            json.dumps(
                dict(
                    status="REQUEST_SUCCEEDED",
                    Results=dict(
                        series=[
                            dict(seriesID=s, data=points)
                            for s in self.profile.series_ids
                        ]
                    ),
                )
            ).encode(),
            {},
        )


def system(tmp_path, profile, mode="ok", *, factory=None):
    clock = Clock()
    store = InMemoryTimeSeriesStore(profile=profile, clock=clock)
    transport = Transport(profile, mode)
    source = BlsRangeSource(
        profile=profile,
        registered=False,
        transport=transport,
        clock=clock,
        max_transport_attempts=1,
    )
    proxy = WorkerService(
        factory or (lambda: CpiTimeSeriesService(store, source, profile=profile)),
        profile=profile,
    )
    scheduler = CpiReleaseScheduler(
        proxy,
        tmp_path / "schedule.json",
        profile=profile,
        clock=clock,
        auto_mode=True,
        history_start="2026-09",
        calendar_fetcher=None,
        reference_fetcher=None,
        signal_fetcher=None,
    )
    raw = b"""<table><tr><th>Reference Month</th><th>Release Date</th>
<th>Release Time</th></tr><tr><td>October 2026</td><td>Nov. 10, 2026</td>
<td>08:30 AM</td></tr></table>"""
    scheduler.import_calendar(raw, acquired_at=clock.at)
    return scheduler, store, transport, clock, proxy


def item(scheduler):
    return next(
        t for t in scheduler.status()["targets"] if t["reference_month"] == "2026-10"
    )


def test_real_host_admission_is_known_zero_and_does_not_stop_as_unknown(
    tmp_path, monkeypatch
):
    _, runtime, _, _, connections = employment_setup(tmp_path, monkeypatch)
    host = runtime.employment_timeseries
    host.allow_collect = False
    scheduler, _, transport, _, _ = system(tmp_path / "isolated", EMPLOYMENT_PROFILE)
    scheduler.service = host.scheduler.service
    scheduler.due_check()
    target = item(scheduler)
    assert target["auto"]["classification"] == "collection_not_enabled"
    assert target["auto"]["actual_http"] == 0
    assert target["auto"].get("reserved_http", 0) == 0
    assert target["auto"]["last_error"]["phase"] == "admission"
    assert not connections and transport.calls == 0


@pytest.mark.parametrize(
    "profile", [CPI_PROFILE, PPI_PROFILE, EMPLOYMENT_PROFILE], ids=lambda p: p.key
)
@pytest.mark.parametrize("mode", ["ok", "unpublished", "null", "429", "timeout"])
def test_real_service_counts_and_due_result(tmp_path, profile, mode):
    scheduler, store, transport, clock, _ = system(tmp_path, profile, mode)
    result = scheduler.due_check()
    target = item(scheduler)
    assert transport.calls == 1
    assert target["auto"]["actual_http"] == 1
    assert target["auto"].get("reserved_http", 0) == 0
    assert target["auto"].get("classification") != "schedule_actual_http_unknown"
    if mode == "ok":
        assert result["action"] == "updated"
        assert target["target_available"] is True
        assert store.status()["observation_count"] == len(profile.series_ids)
    else:
        assert target["target_available"] is False
        assert target["auto"]["next_attempt_at"]
        if mode == "429":
            assert target["auto"]["http_status"] == 429
            assert datetime.fromisoformat(
                target["auto"]["next_attempt_at"]
            ) >= clock.at + timedelta(seconds=300)
        assert scheduler.due_check()["action"] == "not_due"
        assert transport.calls == 1


def test_true_unknown_http_stays_stopped_and_preserves_reservation(tmp_path):
    scheduler, _, transport, _, proxy = system(tmp_path, CPI_PROFILE)

    def unknown(*args, **kwargs):
        raise RuntimeError("unclassified operation; no confirmed HTTP count")

    proxy.update_range = unknown
    scheduler.due_check()
    target = item(scheduler)
    assert target["auto"]["state"] == "stopped"
    assert target["auto"]["actual_http"] == target["auto"]["reserved_http"] == 1
    assert target["auto"]["last_error"]["classification"] == "schedule_update_failed"
    original = copy.deepcopy(target)
    scheduler.due_check()
    assert item(scheduler) == original
    assert transport.calls == 0


def test_storage_unknown_keeps_count_but_never_blindly_retries_source(tmp_path):
    scheduler, store, transport, clock, _ = system(tmp_path, CPI_PROFILE)

    def fail(*args, **kwargs):
        raise CpiStorageError("cpi_timeseries_storage_outcome_unknown")

    store.save_batches = fail
    scheduler.due_check()
    target = item(scheduler)
    assert target["auto"]["actual_http"] == 1
    assert target["auto"]["state"] == "stopped"
    assert target["auto"]["classification"] == "cpi_timeseries_storage_outcome_unknown"
    assert target["auto"]["next_attempt_at"] is None
    clock.at += timedelta(minutes=10)
    scheduler.due_check()
    assert transport.calls == 1


def test_material_or_cleanup_failure_does_not_erase_numeric_receipt(tmp_path):
    scheduler, store, transport, _, proxy = system(tmp_path, CPI_PROFILE)
    old_factory = proxy.factory

    def factory():
        service = old_factory()

        def close():
            raise RuntimeError("private cleanup diagnostic")

        service.close = close
        return service

    def material(*args):
        raise RuntimeError("private callback diagnostic")

    proxy.factory = factory
    proxy.material_capture = material
    result = proxy.update_range("2026-10", "2026-10", include_buffer=False)
    assert result["source_request_count"] == 1
    assert result["saved_points"] == len(CPI_PROFILE.series_ids)
    assert result["release_material"]["state"] == "unknown"
    assert result["release_material"]["provider_requests"] is None
    assert result["service_cleanup"]["state"] == "failed"
    assert "private" not in json.dumps(result)
    assert transport.calls == 1 and store.status()["observation_count"]


def verified_recovery_job(
    scheduler, clock, request_id="7b72b4e5-9c4d-43fd-a1eb-2ebbb571f36e"
):
    from market_data_foundation.web.cpi_timeseries_jobs import verify_readback

    clock.at += timedelta(seconds=10)
    written = scheduler.service.update_range(
        "2026-10", "2026-10", series_ids=scheduler.series_ids, include_buffer=False
    )
    read = scheduler.service.query_range(
        "2026-10",
        "2026-10",
        series_ids=scheduler.series_ids,
        allow_upstream=False,
        vintage_mode="available",
    )
    command = dict(
        request_id=request_id, action="update", start="2026-10", end="2026-10"
    )
    verified, result = verify_readback(
        written, read, command, profile=scheduler.profile
    )
    assert verified
    return dict(
        command,
        job_id=request_id,
        asset=scheduler.profile.key,
        state="complete",
        numeric_result=written,
        result=result,
        started_at=clock.at.isoformat(),
        finished_at=clock.at.isoformat(),
    )


def stop_without_replay(scheduler):
    saved = scheduler.service.update_range
    scheduler.service.update_range = lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("unknown original attempt")
    )
    scheduler.due_check()
    scheduler.service.update_range = saved
    return copy.deepcopy(item(scheduler))


def test_recovery_preserves_original_attempt_and_does_not_replay_history(
    tmp_path,
):
    scheduler, _, transport, clock, _ = system(tmp_path, EMPLOYMENT_PROFILE)
    original = stop_without_replay(scheduler)
    job = verified_recovery_job(scheduler, clock)
    result = scheduler.confirm_target_recovery(
        "2026-10",
        job,
        evidence_locator="isolated/verified-new-job.json",
        diagnostic_http=1,
    )
    recovered = item(scheduler)
    assert recovered["target_available"] is True
    assert recovered["auto"] == original["auto"]
    assert recovered["recovery"]["previous_target"] == original["target"]
    assert recovered["recovery"]["previous_auto"]["reserved_http"] == 1
    assert recovered["recovery"]["kind"] == "operator_job_not_natural_capture"
    assert recovered["history"] == original["history"]
    assert (
        scheduler.confirm_target_recovery(
            "2026-10", job, evidence_locator="isolated/verified-new-job.json"
        )["targets"]
        == result["targets"]
    )
    assert scheduler.due_check()["action"] == "not_due"
    assert transport.calls == 1
    from market_data_foundation.web.cpi_timeseries_policy import automatic_executions

    execution = automatic_executions(scheduler.status(), profile=EMPLOYMENT_PROFILE)[0]
    assert execution["state"] == "stopped"
    assert execution["classification"] == "schedule_actual_http_unknown"
    assert execution["finished_at"] is None


@pytest.mark.parametrize(
    "mutation",
    ["wrong_id", "wrong_asset", "partial", "write_unknown", "values_changed", "budget"],
)
def test_recovery_rejects_unproved_or_over_budget_receipts_without_state_change(
    tmp_path, mutation
):
    from market_data_foundation.historical.cpi_release_schedule import CpiScheduleError

    scheduler, store, _, clock, _ = system(tmp_path, EMPLOYMENT_PROFILE)
    stop_without_replay(scheduler)
    job = verified_recovery_job(scheduler, clock)
    if mutation == "wrong_id":
        job["request_id"] = "6f0b241a-f5b3-47f6-b9f1-113806dc165d"
    if mutation == "wrong_asset":
        job["asset"] = "cpi"
    if mutation == "partial":
        job["result"]["readback"]["verified"] = False
    if mutation == "write_unknown":
        job["state"] = "unknown"
    if mutation == "values_changed":
        job["numeric_result"]["points"][0]["value"] = "999"
    if mutation == "budget":
        state = scheduler._read()
        state["targets"]["2026-10"]["auto"]["actual_http"] = 30
        scheduler._save(state)
    original = scheduler.state_path.read_bytes()
    with pytest.raises(CpiScheduleError):
        scheduler.confirm_target_recovery(
            "2026-10", job, evidence_locator="isolated/receipt.json"
        )
    assert scheduler.state_path.read_bytes() == original
    assert store.status()["observation_count"] == 18


def test_source_credential_rejection_is_known_zero_through_real_service(tmp_path):
    scheduler, _, transport, _, proxy = system(tmp_path, EMPLOYMENT_PROFILE)
    source = proxy.factory().source
    source.registered = True
    source._credential_loader = lambda: None
    scheduler.due_check()
    target = item(scheduler)
    assert target["auto"]["actual_http"] == 0
    assert target["auto"].get("reserved_http", 0) == 0
    assert target["auto"]["classification"] == "bls_live_credential_invalid"
    assert target["auto"]["next_attempt_at"] is not None
    assert transport.calls == 0


def test_unclassified_factory_failure_does_not_guess_zero(tmp_path):
    scheduler, _, transport, _, proxy = system(tmp_path, EMPLOYMENT_PROFILE)

    def failure():
        raise RuntimeError("unknown construction effect")

    proxy.factory = failure
    scheduler.due_check()
    target = item(scheduler)
    assert target["auto"]["actual_http"] == target["auto"]["reserved_http"] == 1
    assert target["auto"]["state"] == "stopped"
    assert target["auto"]["last_error"]["phase"] == "service_factory"
    assert target["auto"]["last_error"]["provider_attempts"] is None
    assert transport.calls == 0
