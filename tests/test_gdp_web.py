"""GDP HTTP/runtime contracts with synthetic services, no source or private DB."""

import importlib.util
from datetime import timedelta
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from market_data_foundation.web.cpi_timeseries_host import create_timeseries_preview
from market_data_foundation.web.gdp_host import PREFIX
from market_data_foundation.web.gdp_runtime import GdpOptions
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from tests import test_readonly_host as old
from tests.test_gdp_chain import service
from tests.test_gdp_schedule import AT, event, ics

HEADERS = {"Origin": CANDIDATE_ORIGIN, "Content-Type": "application/json"}


def setup(tmp_path, monkeypatch, **options):
    _, previous, _, _, _, _ = old.setup(tmp_path, monkeypatch, enabled_caps=("gdp",))
    s, wire, clock, _ = service()
    clock[0] = AT
    s.close = lambda: None
    calls = []

    def read():
        calls.append("read")
        return s

    def write(*, credential_loader):
        assert credential_loader()
        calls.append("write")
        return s

    def query(*, credential_loader):
        assert credential_loader()
        calls.append("query")
        return s

    config = dict(
        schedule_path=tmp_path / "gdp-schedule.json",
        job_directory=tmp_path / "gdp-jobs",
        read_factory=read,
        write_factory=write,
        query_factory=query,
        allow_read=True,
        allow_collect=True,
        allow_source=True,
        calendar_fetcher=lambda: ics(event()),
        history_start="2026-Q3",
    )
    config.update(options)
    app = create_timeseries_preview(
        previous.settings,
        enabled=True,
        connector=previous.connector,
        clock=lambda: clock[0],
        schedule_path=tmp_path / "cpi-schedule.json",
        job_directory=tmp_path / "cpi-jobs",
        gdp_options=GdpOptions(**config),
    )
    return app, app.state.timeseries_runtime.gdp_timeseries, s, wire, clock, calls


def client(app):
    return TestClient(app, base_url=CANDIDATE_ORIGIN, client=("192.168.10.25", 1234))


def command():
    return dict(request_id=str(uuid4()), action="init", start="2026-Q3", end="2026-Q3")


def test_gets_have_no_network_write_or_worker_liveness(tmp_path, monkeypatch):
    app, host, _, wire, _, calls = setup(tmp_path, monkeypatch)
    assert calls == [] and wire.calls == []
    with client(app) as http:
        for operation in (
            "status",
            "strategy",
            "schedule",
            "executions",
            "jobs",
            "api-help",
        ):
            response = http.get(PREFIX + "/" + operation)
            assert response.status_code == 200, response.text
        result = http.get(PREFIX + "/query?start=2026-Q2&end=2026-Q3").json()
        assert result["local_only"] is True and len(result["points"]) == 16
        assert (
            http.get(PREFIX + "/releases?reference_quarter=2026-Q3").status_code == 200
        )
        strategy = http.get(PREFIX + "/strategy").json()
        assert not strategy["enabled"] and strategy["worker"]["state"] == "not_started"
    assert not wire.calls
    assert (
        not host.scheduler.state_path.exists() and not host.journal.directory.exists()
    )


@pytest.mark.parametrize(
    "query",
    [
        "start=2026-07&end=2026-09",
        "start=2026-Q3&end=2026-Q2",
        "start=2026-Q2&end=2026-Q3&basket=all",
        "start=2026-Q2&end=2026-Q3&local_only=1",
        "start=2026-Q2&end=2026-Q3&series=FAKE",
        "start=2026-Q2&end=2026-Q3&start=2026-Q1",
        "start=2026-Q2&end=2026-Q3&query_mode=history",
        "start=2026-Q2&end=2026-Q3&as_of=2026-11-02T12:00:00",
        "start=2026-Q2&end=2026-Q3&as_of_basis=official",
    ],
)
def test_invalid_requests_are_rejected_before_services(tmp_path, monkeypatch, query):
    app, _, _, wire, _, calls = setup(tmp_path, monkeypatch)
    with client(app) as http:
        response = http.get(PREFIX + "/query?" + query)
    assert response.status_code == 400, response.text
    assert calls == [] and not wire.calls


def test_current_passthrough_is_nonpersisted_history_stays_local(tmp_path, monkeypatch):
    app, _, s, wire, clock, _ = setup(tmp_path, monkeypatch)
    with client(app) as http:
        current = http.get(PREFIX + "/query?start=2026-Q3&end=2026-Q3&local_only=false")
        assert current.status_code == 200, current.text
        assert current.json()["coverage"]["available"] == 8
        assert s.status()["observation_count"] == 0
        n = len(wire.calls)
        history = http.get(
            PREFIX + "/query",
            params=dict(
                start="2026-Q3",
                end="2026-Q3",
                local_only="false",
                query_mode="history",
                as_of=clock[0].isoformat(),
            ),
        )
        assert history.status_code == 200 and history.json()["local_only"]
        assert len(wire.calls) == n


def test_async_collect_receipt_and_readback(tmp_path, monkeypatch):
    app, host, s, _, _, _ = setup(tmp_path, monkeypatch)
    entered, release, done = Event(), Event(), Event()
    init = s.init_range

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        try:
            return init(*args, **kwargs)
        finally:
            done.set()

    s.init_range = held
    cmd = command()
    with client(app) as http:
        try:
            ack = http.post(PREFIX + "/jobs", json=cmd, headers=HEADERS)
            assert ack.status_code == 202, ack.text
            assert entered.wait(2)
            jobs = http.get(PREFIX + "/jobs").json()["jobs"]
            assert jobs[0]["state"] == "running"
        finally:
            release.set()
        assert done.wait(3)
    # Acquiring the execution lock waits only for the current tiny journal tail.
    import fcntl

    with (host.journal.directory / "execution.lock").open() as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
    job = host.executions()["executions"][0]
    assert job["state"] == "complete" and job["result"]["readback"]["verified"]
    assert host.submit(cmd)["state"] == "complete"


def test_strategy_does_not_fabricate_heartbeat_or_collect(tmp_path, monkeypatch):
    app, host, _, wire, _, _ = setup(tmp_path, monkeypatch)
    with client(app) as http:
        response = http.put(
            PREFIX + "/strategy", json={"enabled": True}, headers=HEADERS
        )
        assert response.status_code == 200, response.text
        assert response.json()["worker"]["state"] == "not_started"
        assert not wire.calls
        assert (
            http.put(
                PREFIX + "/strategy", json={"enabled": False}, headers=HEADERS
            ).status_code
            == 200
        )
    assert host.auto_strategy.tick()["action"] == "paused"
    assert host.strategy()["worker"]["state"] == "paused"


def test_permissions_and_origin_are_independent(tmp_path, monkeypatch):
    app, _, _, wire, _, _ = setup(
        tmp_path, monkeypatch, allow_collect=False, allow_source=False
    )
    with client(app) as http:
        assert http.get(PREFIX + "/query?start=2026-Q3&end=2026-Q3").status_code == 200
        assert (
            http.get(
                PREFIX + "/query?start=2026-Q3&end=2026-Q3&local_only=false"
            ).status_code
            == 403
        )
        assert (
            http.post(PREFIX + "/jobs", json=command(), headers=HEADERS).status_code
            == 403
        )
        assert http.post(PREFIX + "/jobs", json=command()).status_code == 405
        assert (
            http.put(
                PREFIX + "/strategy", json={"enabled": True}, headers=HEADERS
            ).status_code
            == 403
        )
    assert not wire.calls


def test_history_calendar_cannot_look_ahead(tmp_path, monkeypatch):
    app, host, _, _, clock, _ = setup(tmp_path, monkeypatch)
    host.scheduler.refresh_calendar()
    with client(app) as http:
        result = http.get(
            PREFIX + "/query",
            params=dict(
                start="2026-Q3",
                end="2026-Q3",
                query_mode="history",
                as_of=(clock[0] - timedelta(seconds=1)).isoformat(),
            ),
        ).json()
    assert result["publication_events"] == []


def test_catalog_and_runtime_bindings(tmp_path, monkeypatch):
    app, host, _, _, _, _ = setup(tmp_path, monkeypatch)
    assert host.asset_metadata()["asset_id"] == "mdf.macro.bea.gdp-growth"
    with client(app) as http:
        package = http.get("/api/r5/packages/macro")
        assert package.status_code == 200, package.text
        gdp = next(
            a
            for a in package.json()["assets"]
            if a["identity"] == "mdf.macro.bea.gdp-growth"
        )
        assert gdp["revisions"][0]["collect"]["allowed"]
        help_doc = http.get(PREFIX + "/api-help").json()
        schema = app.openapi()["paths"][PREFIX + "/query"]
        assert set(help_doc["parameters"]) == {
            p["name"] for p in schema["get"]["parameters"]
        }


def test_isolation_and_worker_explicit_opt_in(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="gdp_independent_paths"):
        setup(tmp_path, monkeypatch, job_directory=tmp_path / "cpi-jobs")
    path = Path(__file__).parents[1] / "scripts/mdf_cpi_timeseries_worker.py"
    spec = importlib.util.spec_from_file_location("gdp_worker_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from types import SimpleNamespace

    with pytest.raises(ValueError, match="explicit_gdp_worker_opt_in_required"):
        module.build_runtime(SimpleNamespace(asset="gdp", opt_in_gdp_worker=False))


def test_write_openapi_describes_quarterly_commands(tmp_path, monkeypatch):
    app, _, _, _, _, _ = setup(tmp_path, monkeypatch)
    paths = app.openapi()["paths"]
    job = paths[PREFIX + "/jobs"]["post"]["requestBody"]
    schema = job["content"]["application/json"]["schema"]
    assert set(schema["required"]) == {"request_id", "action", "start", "end"}
    assert schema["additionalProperties"] is False
    assert schema["properties"]["action"]["enum"] == ["init", "update"]
    assert schema["properties"]["start"]["pattern"] == "^[0-9]{4}-Q[1-4]$"
    strategy = paths[PREFIX + "/strategy"]["put"]["requestBody"]
    schema = strategy["content"]["application/json"]["schema"]
    assert schema["required"] == ["enabled"]
    assert schema["properties"]["enabled"]["type"] == "boolean"
