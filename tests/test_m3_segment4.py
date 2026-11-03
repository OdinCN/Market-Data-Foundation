"""M3 asset, bounded jobs, source deadline and existing-host integration."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_data_foundation.web.m3_product_host import M3ProductHost
from market_data_foundation.web.m3_query_api import PREFIX, timeseries_router
from market_data_foundation.web.package_catalog import PackageCatalog
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from tests.test_m3_capital_goods import no_network  # noqa: F401
from tests.test_m3_segment3 import AT, service


class Packages:
    environment_id = "test"
    catalog = PackageCatalog()
    store = SimpleNamespace(
        read=lambda *args: SimpleNamespace(enabled_capability_ids=["m3"])
    )

    def credential_states(self, *args, **kwargs):
        return [SimpleNamespace(source_id="Census", credential_state="configured")]

    def load_credential(self, *args, **kwargs):
        return "synthetic0123456789abcdef"


def host(tmp_path):
    s, wire = service()
    return (
        M3ProductHost(
            packages=Packages(),
            read_factory=lambda: s,
            query_factory=lambda: s,
            write_factory=lambda **kwargs: s,
            job_directory=tmp_path / "jobs",
            allow_read=True,
            allow_source=True,
            allow_collect=True,
            clock=lambda: AT + timedelta(seconds=1),
        ),
        s,
        wire,
    )


def test_asset_catalog_names_m3_and_keeps_census_capability_specific():
    c = PackageCatalog()
    cap = next(x for x in c.capabilities("macro") if x.capability_id == "m3")
    assert cap.title == "美国耐用品与核心资本品" and cap.required_sources == ["Census"]
    assert "M3" in cap.source_relations[0].release_calendar
    assert c.entries["macro", "m3"][0].binding_contract.silent_fallback is False


def test_job_resume_uses_same_collection_and_keeps_coverage_separate(tmp_path):
    h, s, wire = host(tmp_path)
    command = {
        "request_id": str(uuid4()),
        "action": "update",
        "start": "2024-12",
        "end": "2025-09",
    }
    first = h.submit(command)
    assert (
        first["state"] == "failed" and first["classification"] == "m3_resume_required"
    )
    assert (
        first["result"]["remaining_requests"] == 9
        and first["result"]["provider_requests"] == 6
    )
    first_collection = first["collection_id"]
    second = h.submit(command, resume=True)
    assert (
        second["collection_id"] == first_collection
        and second["result"]["remaining_requests"] == 3
    )
    third = h.submit(command, resume=True)
    assert third["state"] == "complete" and third["result"]["remaining_requests"] == 0
    assert third["result"]["readback"]["verified"] and len(wire.calls) == 15
    assert h.submit(command, resume=True) == third and len(wire.calls) == 15
    assert len(h.executions()["executions"]) == 1


def test_completed_job_with_provider_null_does_not_loop(tmp_path):
    h, s, wire = host(tmp_path)
    original = wire.fetch

    def fetch(req):
        snap = original(req)
        import json

        from market_data_foundation.historical.m3_capital_goods import parse_response

        raw = json.loads(snap.raw)
        raw[1][4] = None
        return parse_response(
            json.dumps(raw).encode(), req, obtained_at=snap.obtained_at
        )

    wire.fetch = fetch
    command = {
        "request_id": str(uuid4()),
        "action": "init",
        "start": "2025-01",
        "end": "2025-02",
    }
    result = h.submit(command)
    assert result["state"] == "complete" and result["result"]["remaining_requests"] == 0
    assert (
        not result["result"]["coverage_complete"]
        and result["result"]["completion"] == "source_coverage_partial"
    )
    assert h.submit(command, resume=True) == result and len(wire.calls) == 3


def test_endpoint_jobs_use_existing_origin_and_durable_receipts(tmp_path):
    h, s, wire = host(tmp_path)
    app = FastAPI()
    app.include_router(timeseries_router(h))
    headers = {"Origin": CANDIDATE_ORIGIN, "Content-Type": "application/json"}
    command = {
        "request_id": str(uuid4()),
        "action": "init",
        "start": "2025-01",
        "end": "2025-02",
    }
    with TestClient(
        app, base_url=CANDIDATE_ORIGIN, client=("192.168.10.25", 1234)
    ) as http:
        assert http.get(PREFIX + "/status").status_code == 200
        assert http.post(PREFIX + "/jobs", json=command).status_code == 403
        assert http.post(
            PREFIX + "/jobs", json=command, headers=headers
        ).status_code in (200, 202)
        import time

        for _ in range(100):
            jobs = http.get(PREFIX + "/jobs").json()["jobs"]
            if jobs and jobs[0]["state"] != "running":
                break
            time.sleep(0.01)
        assert (
            jobs[0]["state"] == "complete" and jobs[0]["result"]["readback"]["verified"]
        )
        assert (
            http.get(PREFIX + "/executions").json()["executions"][0]["job_id"]
            == command["request_id"]
        )


def test_temporary_source_has_observation_but_no_persisted_availability():
    s, wire = service()
    current = s.query_range("2025-01", "2025-02", allow_upstream=True)
    assert not current["persisted"] and not s.store.rows and not s.store.batches
    assert current["coverage"] == {"expected": 6, "available": 6}
    assert all(
        p["origin"] == "upstream"
        and p["observed_at"] == AT.isoformat()
        and p["available_at"] is None
        and p["revision"] is None
        for p in current["points"]
    )
    history = s.query_range(
        "2025-01", "2025-02", as_of=(AT + timedelta(days=1)).isoformat()
    )
    assert history["provider_requests"] == 0 and history["coverage"]["available"] == 0
    assert len(wire.calls) == 3


def test_query_deadline_bounds_all_fragments_without_losing_partial(monkeypatch):
    from market_data_foundation.historical import m3_capital_goods_timeseries as module
    from market_data_foundation.historical.m3_capital_goods_source import (
        M3CandidateSource,
    )

    s, wire = service()
    clock = [0.0]
    timeouts = []

    class Timed(M3CandidateSource):
        def __init__(self):
            pass

        def fetch(self, request, *, timeout=20):
            timeouts.append(timeout)
            clock[0] += 6
            return wire.fetch(request)

    s.source = Timed()
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    result = s.query_range("2025-01", "2025-04", allow_upstream=True)
    assert timeouts == [15.0, 9.0, 3.0]
    assert result["provider_requests"] == 3 and result["budget_exhausted"]
    assert result["coverage"] == {"expected": 12, "available": 6}
    assert len(result["missing"]) == 6
    assert result["source_failure"]["classification"] == "census_m3_query_deadline"
    assert not s.store.rows


def test_integrated_preview_admits_manual_job_and_refuses_origin(tmp_path, monkeypatch):
    from market_data_foundation.web.cpi_timeseries_host import create_timeseries_preview
    from tests import test_readonly_host as old

    _, previous, *_ = old.setup(tmp_path, monkeypatch)
    h, _, _ = host(tmp_path)
    app = create_timeseries_preview(
        previous.settings,
        enabled=True,
        connector=previous.connector,
        schedule_path=tmp_path / "cpi-schedule.json",
        job_directory=tmp_path / "cpi-jobs",
        m3_query_host=h,
    )
    command = {
        "request_id": str(uuid4()),
        "action": "update",
        "start": "2025-01",
        "end": "2025-02",
    }
    with TestClient(
        app, base_url=CANDIDATE_ORIGIN, client=("192.168.10.25", 1234)
    ) as http:
        assert http.post(PREFIX + "/jobs", json=command).status_code == 405
        headers = {"Origin": CANDIDATE_ORIGIN, "Content-Type": "application/json"}
        assert http.post(
            PREFIX + "/jobs", json=command, headers=headers
        ).status_code in (200, 202)
