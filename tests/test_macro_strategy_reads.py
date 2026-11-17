"""Actual read routers/hosts: policy identity never depends on holdings scans."""

import json
import socket

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from tests import test_pce_timeseries_web as pce_web
from tests.test_claims_web import setup as claims
from tests.test_gdp_web import setup as gdp
from tests.test_m3_segment4 import host as m3
from tests.test_retail_timeseries_web import retail


@pytest.fixture
def pce(tmp_path):
    return pce_web.fixture.__wrapped__(tmp_path)


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*_, **__):
        pytest.fail("external_io_forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)


@pytest.mark.parametrize("protocol", ["monthly", "period", "claims", "retail", "m3"])
def test_policy_preference_retains_real_policy_without_aggregate_reads(
    protocol, pce, tmp_path, monkeypatch
):
    for name in ("gdp", "claims", "retail", "m3"):
        (tmp_path / name).mkdir()
    if protocol == "monthly":
        app, host, *_ = pce
    elif protocol == "period":
        app, host, *_ = gdp(tmp_path / "gdp", monkeypatch)
    elif protocol == "claims":
        app, host, *_ = claims(tmp_path / "claims", monkeypatch)
    elif protocol == "retail":
        app, runtime, *_ = retail(tmp_path / "retail", monkeypatch)
        host = runtime.retail_timeseries
    else:
        from market_data_foundation.web.m3_query_api import timeseries_router

        host, *_ = m3(tmp_path / "m3")
        app = FastAPI()
        app.include_router(timeseries_router(host))
    path = ("/api/r5/m3/timeseries" if protocol == "m3" else host.prefix) + "/strategy"
    normal = json.loads(json.dumps(host.strategy()))
    calls = []

    def aggregate(_host):
        calls.append("aggregate")
        return {"collection_facts_classification": "fixture_aggregate_unavailable"}

    monkeypatch.setattr(
        "market_data_foundation.web.macro_runtime_facts.collection_facts", aggregate
    )
    with TestClient(
        app, base_url=CANDIDATE_ORIGIN, client=("192.168.10.25", 1234)
    ) as http:
        response = http.get(path, headers={"Prefer": "return=minimal"})
        assert response.status_code == 200, response.text
        policy = response.json()
        for key in ("exists", "enabled", "history_start", "worker", "policy"):
            assert policy[key] == normal[key]
        assert policy["collection_facts_status"] == "not_requested"
        assert "latest_data_period" not in policy
        assert calls == []
        assert http.get(path + "?unexpected=1").status_code == 400
        # Existing clients still receive the full read protocol and true failure.
        assert http.get(path).json()["collection_facts_classification"] == (
            "fixture_aggregate_unavailable"
        )
        assert calls == ["aggregate"]
