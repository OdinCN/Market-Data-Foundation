import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_data_foundation.web.cpi_timeseries_jobs import WebError
from market_data_foundation.web.worker_control import (
    ASSETS,
    DevWorkerControl,
    install_worker_control,
)

AT = datetime(2026, 11, 4, 17, tzinfo=UTC)


def setup(tmp_path):
    path = tmp_path / "window.json"
    path.write_text(
        json.dumps(
            {
                "started_at": AT.isoformat(),
                "expires_at": (AT + timedelta(days=45)).isoformat(),
                "snapshot": "/fixed/revision",
            }
        )
    )
    worker = {
        "state": "idle",
        "last_heartbeat_at": AT.isoformat(),
        "process": {"pid": None},
    }
    host = SimpleNamespace(
        auto_strategy=SimpleNamespace(get=lambda: {"worker": worker}),
        capabilities=lambda _: {"collect": True},
        source=lambda: {},
        journal=SimpleNamespace(list=lambda: []),
    )
    calls = []
    active = [False]

    def run(args, **kwargs):
        calls.append(args)
        if args[1] in {"start", "restart"}:
            active[0] = True
            worker["process"]["pid"] = 123
            worker["last_heartbeat_at"] = AT.isoformat()
        stdout = (
            "LoadState=loaded\n"
            f"ActiveState={'active' if active[0] else 'inactive'}\n"
            f"MainPID={'123' if active[0] else '0'}\n"
            "WorkingDirectory=/fixed/revision\n"
            f"ActiveEnterTimestamp={'observed' if active[0] else ''}\n"
        )
        return SimpleNamespace(returncode=0, stdout=stdout)

    control = DevWorkerControl(path, run=run, clock=lambda: AT)
    return control, host, calls, active, worker


def test_start_and_stale_recovery_require_actual_service_and_heartbeat(tmp_path):
    control, host, calls, active, worker = setup(tmp_path)
    assert not control.inspect("employment", host)["online"]
    assert control.act("employment", host, "start")["online"]
    assert calls[-2][1:] == ["start", "mdf-macro-dev-employment.service"]
    worker["last_heartbeat_at"] = (AT - timedelta(seconds=121)).isoformat()
    assert control.inspect("employment", host)["reason"] == "worker_heartbeat_stale"
    assert control.act("employment", host, "recover")["online"]
    assert any(call[1] == "restart" for call in calls)


@pytest.mark.parametrize(
    "asset,action", [("../../other", "start"), ("cpi", "stop"), ("cpi", ["start"])]
)
def test_client_cannot_select_commands_or_foreign_units(tmp_path, asset, action):
    control, host, calls, *_ = setup(tmp_path)
    with pytest.raises(WebError):
        control.act(asset, host, action)
    assert not calls


def test_expired_window_denies_before_systemctl(tmp_path):
    control, host, calls, *_ = setup(tmp_path)
    control.clock = lambda: AT + timedelta(days=45)
    with pytest.raises(WebError, match="dev_window_expired"):
        control.act("cpi", host, "start")
    assert not calls


def test_busy_jobs_and_unmanaged_processes_are_not_restarted(tmp_path):
    control, host, calls, active, worker = setup(tmp_path)
    host.journal.list = lambda: [{"state": "running"}]
    with pytest.raises(WebError, match="worker_active_collection_in_progress"):
        control.act("cpi", host, "recover")
    host.journal.list = lambda: []
    worker["process"]["pid"] = 999
    with pytest.raises(WebError, match="worker_unmanaged_process_present"):
        control.act("cpi", host, "recover")
    assert not any(c[1] in {"start", "restart"} for c in calls)


def test_private_route_and_public_read_only_boundary(tmp_path, monkeypatch):
    control, host, calls, active, worker = setup(tmp_path)
    worker["process"]["pid"] = None
    runtime = SimpleNamespace(
        **{
            (
                "timeseries"
                if a == "cpi"
                else "m3_query_host"
                if a == "m3"
                else a + "_timeseries"
            ): host
            for a in ASSETS
        }
    )
    app = FastAPI()
    install_worker_control(app, runtime, control)
    monkeypatch.setenv("MDF_DEV_READONLY_ORIGIN", "http://odin.pub:18761")
    with TestClient(
        app, base_url="http://192.168.10.20:18761", client=("192.168.10.25", 1234)
    ) as http:
        before = len(calls)
        response = http.post(
            "/api/r5/cpi/timeseries/worker",
            headers={"Host": "odin.pub:18761", "Origin": "http://odin.pub:18761"},
            json={"action": "start"},
        )
        assert response.status_code == 403 and len(calls) == before
        response = http.post(
            "/api/r5/cpi/timeseries/worker",
            headers={"Origin": "http://192.168.10.20:18761"},
            json={"action": "start", "unit": "foreign"},
        )
        assert response.status_code == 400 and len(calls) == before
        # Inspect with a registered but stopped unit succeeds without source calls.
        assert http.get("/api/r5/cpi/timeseries/worker").status_code == 200
        schema = app.openapi()["paths"]["/api/r5/{asset}/timeseries/worker"]["post"][
            "requestBody"
        ]["content"]["application/json"]["schema"]
        assert schema["properties"]["action"]["enum"] == ["start", "recover"]
