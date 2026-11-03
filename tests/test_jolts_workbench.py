"""Shared preview admission and frontend wire fixtures; synthetic sources only."""

import json
import time
from pathlib import Path

import pytest

from market_data_foundation.web.cpi_timeseries_host import create_timeseries_preview
from market_data_foundation.web.jolts_runtime import JoltsOptions
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from tests import test_readonly_host as old
from tests.test_cpi_timeseries_web import no_network  # noqa: F401
from tests.test_jolts_timeseries import OPENINGS, OPENINGS_RATE
from tests.test_jolts_web import PREFIX, command, setup  # noqa: F401
from tests.test_ppi_timeseries_web import client


def workbench_wire(fixture):
    app, host, _, source, _ = fixture
    source.overrides[OPENINGS, "2026-07"] = "0"
    source.overrides[OPENINGS_RATE, "2026-07"] = "4.2"
    with client(app) as http:
        reply = http.post(
            PREFIX + "/jobs",
            json=dict(command(), request_id="11111111-2222-4333-8444-555555555555"),
            headers={"Origin": CANDIDATE_ORIGIN},
        )
        assert reply.status_code == 202
        for _ in range(100):
            jobs = http.get(PREFIX + "/jobs").json()
            if jobs["jobs"][0]["state"] != "running":
                break
            time.sleep(0.01)
        assert jobs["jobs"][0]["state"] == "complete"
        params = dict(
            start="2026-07", end="2026-08", series=f"{OPENINGS},{OPENINGS_RATE}"
        )
        query = http.get(PREFIX + "/query", params=params)
        projected = http.get(PREFIX + "/query", params=dict(params, columns="value"))
        history = http.get(
            PREFIX + "/query",
            params=dict(
                params,
                query_mode="history",
                local_only="true",
                as_of_basis="available",
                vintage_mode="available",
                as_of="2026-11-02T00:00:00Z",
            ),
        )
        assert query.status_code == projected.status_code == history.status_code == 200
        return dict(
            evidence="synthetic real Host and HTTP serializer; no DEV data",
            query_wire=query.text,
            projected_wire=projected.text,
            history_wire=history.text,
            **{
                op: http.get(PREFIX + "/" + op).json()
                for op in ("status", "strategy", "schedule", "executions", "releases")
            },
        )


def test_http_fixture_semantics(setup):  # noqa: F811
    doc = workbench_wire(setup)
    body = json.loads(doc["query_wire"])
    quantity = next(
        p
        for p in body["points"]
        if p["series_id"] == OPENINGS and p["month"] == "2026-07"
    )
    assert quantity["value"] == "0"
    rate = next(
        p
        for p in body["points"]
        if p["series_id"] == OPENINGS_RATE and p["month"] == "2026-08"
    )
    assert rate["value"] == "4.3" and rate["change_1m_percentage_points"] == "0.1"
    assert json.loads(doc["history_wire"])["points"] == []
    assert body["provider_requests"] == body["provider_attempts"] == 0
    assert doc["status"]["initialization"]["months"] == 240
    assert doc["status"]["initialization"]["available"] is True
    retained = json.loads(Path("tests/fixtures/jolts_ui_http.json").read_text())
    # UUID and elapsed wall-clock are execution identities, not numerical fixture data.
    assert retained["query_wire"] == doc["query_wire"]
    assert retained["projected_wire"] == doc["projected_wire"]
    assert retained["history_wire"] == doc["history_wire"]


def test_shared_preview_default_disabled_and_independent_options(tmp_path, monkeypatch):
    _, previous, *_ = old.setup(tmp_path, monkeypatch, enabled_caps=("jolts",))
    options = dict(
        enabled=True,
        connector=previous.connector,
        schedule_path=tmp_path / "cpi" / "plan.json",
        job_directory=tmp_path / "cpi" / "jobs",
    )
    app = create_timeseries_preview(previous.settings, **options)
    assert app.state.timeseries_runtime.jolts_timeseries is None
    with client(app) as http:
        assert http.get(PREFIX + "/status").status_code in {403, 503}
        assets = http.get("/api/r5/packages/macro").json()["assets"]
        asset = next(a for a in assets if a["identity"] == "mdf.macro.bls.jolts")
        assert asset["revisions"][0]["local_read"]["allowed"] is False
    with pytest.raises(ValueError, match="jolts_independent_paths_required"):
        create_timeseries_preview(
            previous.settings,
            **options,
            jolts_options=JoltsOptions(state_directory=tmp_path / "cpi"),
        )
    with pytest.raises(ValueError, match="jolts_explicit_permissions_required"):
        create_timeseries_preview(
            previous.settings,
            **options,
            jolts_options=JoltsOptions(
                state_directory=tmp_path / "jolts", allow_read=1
            ),
        )


def test_shared_enabled_zero_coverage_and_history_never_requests_source(
    tmp_path,
    monkeypatch,
    setup,  # noqa: F811
):
    _, previous, *_ = old.setup(tmp_path, monkeypatch, enabled_caps=("jolts",))
    _, standalone, _, source, _ = setup
    app = create_timeseries_preview(
        previous.settings,
        enabled=True,
        connector=previous.connector,
        clock=lambda: source.at,
        schedule_path=tmp_path / "cpi" / "plan.json",
        job_directory=tmp_path / "cpi" / "jobs",
        jolts_options=JoltsOptions(
            state_directory=tmp_path / "shared-jolts",
            allow_read=True,
            read_factory=standalone.read_factory,
        ),
    )
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    with client(app) as http:
        status = http.get(PREFIX + "/status")
        assert status.status_code == 200
        assert status.json()["observation_count"] == 0
        assert status.json()["source_qualified"] is False
        assert status.json()["initialization"]["months"] == 240
        result = http.get(PREFIX + "/query?start=2026-07&end=2026-08")
        assert result.status_code == 200
        assert result.json()["provider_requests"] == 0
        assert result.json()["points"] == []
        assert len(result.json()["missing"]) == 16
        historical = http.get(
            PREFIX + "/query?start=2026-07&end=2026-08&query_mode=history"
            "&local_only=false&as_of=2026-11-02T00:00:00Z"
        )
        assert historical.status_code == 200
        assert historical.json()["provider_requests"] == 0
        assert http.get(PREFIX + "/schedule").json()["calendar_status"] != "fresh"
        assert http.get(PREFIX + "/executions").json() == {"executions": []}
    assert source.calls == []
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before


def test_shared_jolts_http_job_and_strategy_admission(tmp_path, monkeypatch, setup):  # noqa: F811
    _, previous, *_ = old.setup(tmp_path, monkeypatch, enabled_caps=("jolts",))
    _, standalone, _, source, _ = setup
    app = create_timeseries_preview(
        previous.settings,
        enabled=True,
        connector=previous.connector,
        clock=lambda: source.at,
        schedule_path=tmp_path / "cpi" / "plan.json",
        job_directory=tmp_path / "cpi" / "jobs",
        jolts_options=JoltsOptions(
            state_directory=tmp_path / "shared-jolts",
            allow_read=True,
            allow_collect=True,
            read_factory=standalone.read_factory,
            write_factory=standalone.write_factory,
        ),
    )
    with client(app) as http:
        headers = {"Origin": CANDIDATE_ORIGIN}
        reply = http.post(PREFIX + "/jobs", json=command(), headers=headers)
        assert reply.status_code == 202
        for _ in range(100):
            jobs = http.get(PREFIX + "/jobs").json()["jobs"]
            if jobs[0]["state"] != "running":
                break
            time.sleep(0.01)
        assert jobs[0]["state"] == "complete"
        enabled = http.put(
            PREFIX + "/strategy", json={"enabled": True}, headers=headers
        )
        assert enabled.status_code == 200 and enabled.json()["enabled"] is True
        paused = http.put(
            PREFIX + "/strategy", json={"enabled": False}, headers=headers
        )
        assert paused.status_code == 200 and paused.json()["enabled"] is False
