"""Offline shared-runtime scheduling with the real six-input service and journal."""

from datetime import timedelta

import pytest

from market_data_foundation.historical import macro_release_acquisition
from market_data_foundation.historical.bls_timeseries_profile import CPI_PROFILE
from market_data_foundation.historical.census_retail_live import HttpResult
from market_data_foundation.historical.cpi_request_control import CpiRequestControl
from market_data_foundation.historical.macro_release_records import ReleaseRecords
from market_data_foundation.historical.retail_release_scheduler import (
    CensusControlledTransport,
    RetailReleaseScheduler,
)
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from market_data_foundation.web.retail_timeseries import PREFIX
from tests.test_census_retail_release_schedule import table
from tests.test_census_retail_timeseries import (
    AT,
    encode,
    no_network,  # noqa: F401
    payload,
    response,
)
from tests.test_cpi_timeseries_web_host import client
from tests.test_macro_release_chain import Raw, material
from tests.test_retail_timeseries_web import retail

RAW = table([("September 2026", "October 15, 2026"), ("October 2026", "TBA")]).encode()


def bound(tmp_path, monkeypatch):
    app, runtime, data, wire, calls = retail(tmp_path, monkeypatch)
    host = runtime.retail_timeseries
    raw, expected_url = material("census-retail.html")
    data.raw_store = Raw(CPI_PROFILE)
    ReleaseRecords(data).prepare()
    host.test_material_calls = []

    def fetch_material(url, family):
        assert url == expected_url and family == "retail"
        host.test_material_calls.append((url, family))
        return raw

    monkeypatch.setattr(
        macro_release_acquisition, "fetch_official_material", fetch_material
    )
    now = [AT - timedelta(days=20)]
    host.clock = host.scheduler.clock = host.auto_strategy.clock = lambda: now[0]
    data.clock = lambda: now[0]
    original_write, original_read = host.write_factory, host.read_factory

    def write(**kwargs):
        service = original_write(**kwargs)
        service.clock = service.source._clock = lambda: now[0]
        return service

    def read():
        service = original_read()
        service.clock = lambda: now[0]
        return service

    host.write_factory, host.read_factory = write, read
    fetched = []
    host.scheduler.calendar_fetcher = lambda: fetched.append(now[0]) or RAW
    host.scheduler.import_calendar(RAW, now[0])
    host.update_strategy({"enabled": True})
    return app, host, data, wire, calls, now, fetched


def test_read_only_strategy_routes_and_real_paused_heartbeat(tmp_path, monkeypatch):
    app, runtime, data, wire, calls = retail(tmp_path, monkeypatch)
    host = runtime.retail_timeseries
    with client(app) as http:
        for path in ("strategy", "schedule", "executions"):
            assert http.get(PREFIX + "/" + path).status_code == 200
        assert not host.auto_strategy.get()["exists"]
        assert not host.scheduler.state_path.exists()
        assert not host.auto_strategy.strategy_path.exists()
        assert not calls and not wire.calls
        assert (
            http.put(
                PREFIX + "/strategy",
                json={"enabled": True},
                headers={"Origin": CANDIDATE_ORIGIN},
            ).status_code
            == 200
        )
        assert (
            http.post(
                PREFIX + "/strategy",
                json={"enabled": False},
                headers={"Origin": CANDIDATE_ORIGIN},
            ).status_code
            == 200
        )
    assert host.auto_strategy.tick()["action"] == "paused"
    view = host.strategy()
    assert view["worker"]["state"] == "paused"
    assert view["worker"]["last_heartbeat_at"]
    assert not wire.calls and not data.batches
    assert host.auto_strategy.strategy_path.name == "retail-strategy.json"


def test_real_target_history_inputs_readback_restart_and_reschedule(
    tmp_path, monkeypatch
):
    _, host, data, wire, _, now, _ = bound(tmp_path, monkeypatch)
    assert host.auto_strategy.tick()["action"] == "not_due"
    assert not wire.calls
    now[0] = AT
    assert host.auto_strategy.tick()["action"] == "target"
    target = host.schedule()["targets"][0]
    assert target["target"]["state"] == "complete"
    result = target["target"]["result"]
    assert result["readback"]["verified"] is True
    assert result["readback"]["point_count"] == 6
    assert result["readback"]["buffer_point_count"] == 72
    assert len(data.read_range("2026-09", "2026-09")) == 6
    first_job = host.executions()["executions"][0]
    assert first_job["origin"] == "automatic" and first_job["stage"] == "target"
    assert first_job["request_id"] == first_job["collection_id"]
    proof = first_job["result"]["release_material"]
    assert proof["event_ids"] == ["census:advance-retail:2026-10-15"]
    assert proof["evidence_ids"] and proof["transport_calls"] == 1
    assert len(host.test_material_calls) == 1
    count = len(wire.calls)
    host.scheduler = RetailReleaseScheduler(
        host,
        host.scheduler.state_path,
        clock=lambda: now[0],
        calendar_fetcher=lambda: RAW,
    )
    host.auto_strategy.scheduler = host.scheduler
    moved = table(
        [("September 2026", "November 2, 2026"), ("October 2026", "TBA")]
    ).encode()
    host.scheduler.import_calendar(moved, now[0])
    assert host.schedule()["targets"][0]["target"]["state"] == "complete"
    assert host.auto_strategy.tick()["action"] == "not_due"
    assert len(wire.calls) == count
    # Return the completed event's date; only one small history check remains.
    host.scheduler.import_calendar(RAW, now[0])
    now[0] += timedelta(seconds=4)
    assert host.auto_strategy.tick()["action"] == "history"
    history = host.schedule()["targets"][0]["history"]
    assert history["state"] == "complete"
    assert history["update_start"] == "2025-10"
    assert host.auto_strategy.tick()["action"] == "not_due"
    assert len(wire.calls) == count + 1
    assert len(host.test_material_calls) == 1  # Retained event, no refetch.


def test_missing_target_retries_new_response_and_tba_never_fabricated(
    tmp_path, monkeypatch
):
    _, host, _, wire, _, now, _ = bound(tmp_path, monkeypatch)
    now[0] = AT
    wire.outcomes = [response(encode(payload("2025-09", "2026-08")))]
    assert host.auto_strategy.tick()["action"] == "target"
    target, unresolved = host.schedule()["targets"]
    assert target["target"]["state"] == "awaiting_provider"
    assert unresolved["release_at"] is None and unresolved["calendar_execution_blocked"]
    before = len(wire.calls)
    assert host.auto_strategy.tick()["action"] == "not_due"
    assert len(wire.calls) == before
    now[0] += timedelta(seconds=20)
    wire.outcomes = None
    host.auto_strategy.tick()
    latest = host.schedule()["targets"][0]
    assert latest["target"]["state"] == "complete"
    jobs = host.executions()["executions"]
    assert len({j["collection_id"] for j in jobs}) == 2
    assert len(wire.calls) == before + 1


def test_calendar_failure_valid_cache_expiry_pause_and_no_false_refresh(
    tmp_path, monkeypatch
):
    _, host, _, wire, _, now, fetched = bound(tmp_path, monkeypatch)
    now[0] = AT
    host.scheduler.refresh_calendar()
    acquired = host.schedule()["calendar"]["acquired_at"]

    def fail():
        raise TimeoutError("unsafe provider detail")

    host.scheduler.calendar_fetcher = fail
    now[0] += timedelta(hours=7)
    host.auto_strategy.tick()
    snapshot = host.schedule()
    assert snapshot["calendar_status"] == "fresh"
    assert snapshot["calendar"]["acquired_at"] == acquired
    assert snapshot["calendar"]["error"] == "census_retail_calendar_failure"
    assert "unsafe" not in str(snapshot)
    count = len(wire.calls)
    now[0] += timedelta(days=2)
    assert host.auto_strategy.tick()["action"] == "calendar_unavailable"
    assert len(wire.calls) == count
    host.update_strategy({"enabled": False})
    host.scheduler.calendar_fetcher = lambda: pytest.fail("paused calendar IO")
    assert host.auto_strategy.tick()["action"] == "paused"
    assert len(wire.calls) == count and fetched


def test_unknown_interruption_blocks_replay_and_budget_is_bounded(
    tmp_path, monkeypatch
):
    _, host, _, wire, _, now, _ = bound(tmp_path, monkeypatch)
    now[0] = AT
    state = host.scheduler._read()
    state["targets"][0]["target"].update(state="running", attempts=1)
    host.scheduler._save(state)
    assert host.auto_strategy.tick()["action"] == "execution_interrupted"
    for _ in range(3):
        host.auto_strategy.tick()
    assert not wire.calls
    state = host.scheduler._read()
    state["targets"][0]["target"].update(state="pending", attempts=10)
    host.scheduler._save(state)
    assert host.auto_strategy.tick()["action"] == "budget_exhausted"
    assert not wire.calls


def test_census_admission_shares_calendar_data_429_and_network_budget(tmp_path):
    now = [AT]
    control = CpiRequestControl(tmp_path / "census-control.json", clock=lambda: now[0])

    class Transport:
        calls = 0

        def get(self):
            self.calls += 1
            return HttpResult(429, b"", {"Retry-After": "7200"})

    transport = Transport()
    wrapped = CensusControlledTransport(transport, control)
    assert wrapped.get().status == 429
    with pytest.raises(Exception) as error:
        wrapped.get()
    assert error.value.classification == "census_retail_request_cooldown"
    assert error.value.retry_at == AT + timedelta(hours=2)
    assert transport.calls == 1
    now[0] += timedelta(hours=2)
    transport.get = lambda: (_ for _ in ()).throw(TimeoutError())
    with pytest.raises(TimeoutError):
        wrapped.get()
    import json

    assert len(json.loads(control.state_path.read_text())["requests"]) == 2


@pytest.mark.parametrize(
    "category,kind",
    [
        ("44X72", "SM"),
        ("441", "SM"),
        ("444", "SM"),
        ("447", "SM"),
        ("722", "SM"),
        ("44X72", "MPCSM"),
    ],
)
def test_every_raw_target_input_is_required(tmp_path, monkeypatch, category, kind):
    _, host, _, wire, _, now, _ = bound(tmp_path, monkeypatch)
    now[0] = AT
    rows = payload("2025-09", "2026-09")
    header = rows[0]
    rows = [rows[0]] + [
        row
        for row in rows[1:]
        if not (
            row[header.index("time")] == "2026-09"
            and row[header.index("category_code")] == category
            and row[header.index("data_type_code")] == kind
        )
    ]
    wire.outcomes = [response(encode(rows))]
    host.auto_strategy.tick()
    assert host.schedule()["targets"][0]["target"]["state"] == "awaiting_provider"
    assert host.schedule()["targets"][0]["history"]["state"] == "blocked"


def test_429_retry_after_and_failure_do_not_complete_target(tmp_path, monkeypatch):
    _, host, _, wire, _, now, _ = bound(tmp_path, monkeypatch)
    now[0] = AT
    wire.outcomes = [HttpResult(429, b"", {"Retry-After": "7200"})]
    host.auto_strategy.tick()
    target = host.schedule()["targets"][0]["target"]
    assert target["state"] == "awaiting_provider"
    assert target["next_attempt_at"] == (AT + timedelta(hours=2)).isoformat()
    assert target["result"]["provider_attempts"] == 1
    assert target["result"]["readback"]["verified"] is False
    now[0] += timedelta(minutes=30)
    host.auto_strategy.tick()
    assert len(wire.calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("retail_schedule_path", "absent/schedule.json"),
        ("retail_request_control_path", "jobs"),
        ("retail_request_control_path", "retail-strategy.json"),
    ],
)
def test_runtime_rejects_shared_state_paths(tmp_path, monkeypatch, field, value):
    with pytest.raises(ValueError, match="retail_independent_control_paths_required"):
        retail(tmp_path, monkeypatch, **{field: tmp_path / value})


@pytest.mark.parametrize("tba", [False, True])
def test_explicit_same_identity_resume_reconciles_unknown_without_new_source(
    tmp_path, monkeypatch, tba
):
    from market_data_foundation.historical.retail_timeseries_store import (
        RetailStorageError,
    )

    _, host, data, wire, _, now, _ = bound(tmp_path, monkeypatch)
    now[0] = AT
    original = data.save_batch
    data.save_batch = lambda *args: (_ for _ in ()).throw(
        RetailStorageError("retail_commit_unknown")
    )
    host.auto_strategy.tick()
    stage = host.schedule()["targets"][0]["target"]
    assert stage["state"] == "unknown"
    assert len(wire.calls) == 1
    host.auto_strategy.tick()
    assert len(wire.calls) == 1
    job = host.journal.read(stage["request_id"])
    data.save_batch = original
    command = {key: job[key] for key in ("request_id", "action", "start", "end")}
    resumed = host.submit(command, resume=True)
    assert resumed["state"] == "complete"
    assert resumed["origin"] == "automatic" and resumed["stage"] == "target"
    assert len(wire.calls) == 1
    if tba:
        unresolved = table(
            [("September 2026", "TBA"), ("October 2026", "TBA")]
        ).encode()
        host.scheduler.import_calendar(unresolved, now[0])
        assert host.auto_strategy.tick()["action"] == "not_due"
        assert host.schedule()["targets"][0]["target"]["state"] == "unknown"
        assert len(wire.calls) == 1
        host.scheduler.import_calendar(RAW, now[0])
    assert host.auto_strategy.tick()["action"] == "recovered"
    assert host.schedule()["targets"][0]["target"]["state"] == "complete"
    assert len(wire.calls) == 1


def test_shared_worker_requires_retail_opt_in_paths_and_selects_retail(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from tests.test_ppi_worker_history import arguments, worker_module

    module = worker_module()
    args = arguments("retail") + [
        "--retail-schedule-path",
        "/tmp/retail/schedule.json",
        "--retail-job-directory",
        "/tmp/retail/jobs",
        "--retail-artifacts-path",
        "/tmp/artifacts/retail",
        "--retail-request-control-path",
        "/tmp/retail/census-control.json",
    ]
    parsed = module.build_parser().parse_args(args)
    parsed.opt_in_retail_worker = False
    with pytest.raises(ValueError, match="explicit_retail_worker_opt_in_required"):
        module.build_runtime(parsed)
    runtime = SimpleNamespace(retail_timeseries=SimpleNamespace(auto_strategy=object()))
    constructor, run = Mock(return_value=runtime), Mock()
    monkeypatch.setattr(module, "TimeSeriesRuntime", constructor)
    monkeypatch.setattr(module, "run_worker", run)
    assert module.main(args) == 0
    assert constructor.call_args.kwargs["retail_allow_collect"] is True
    assert constructor.call_args.kwargs["allow_collect"] is False
    assert constructor.call_args.kwargs["ppi_enabled"] is False
    assert constructor.call_args.kwargs["pce_enabled"] is False
    assert constructor.call_args.kwargs["employment_enabled"] is False
    assert run.call_args.args[0] is runtime.retail_timeseries.auto_strategy


def test_public_retail_openapi_is_current_and_strategy_has_typed_body():
    import json
    from pathlib import Path

    from market_data_foundation.web.retail_timeseries import create_app

    document = create_app().openapi()
    committed = Path(__file__).resolve().parents[1] / "web/src/retail.openapi.json"
    assert json.loads(committed.read_text()) == document
    paths = document["paths"]
    for method in ("get", "put", "post"):
        assert method in paths[PREFIX + "/strategy"]
    body = paths[PREFIX + "/strategy"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert body["required"] == ["enabled"]
    assert body["properties"]["enabled"]["type"] == "boolean"
    assert body["additionalProperties"] is False
