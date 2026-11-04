"""Candidate read API uses the existing host boundary and never writes."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_data_foundation.web.m3_query_api import (
    PREFIX,
    M3QueryHost,
    timeseries_router,
)
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from tests.test_m3_capital_goods import no_network  # noqa: F401
from tests.test_m3_segment3 import service


def client(host):
    app = FastAPI()
    app.include_router(timeseries_router(host))
    return TestClient(app, base_url=CANDIDATE_ORIGIN, client=("192.168.10.25", 1234))


def test_query_current_supplements_history_is_local_and_api_help_matches():
    s, wire = service()
    host = M3QueryHost(
        read_factory=lambda: s,
        query_factory=lambda: s,
        allow_read=True,
        allow_source=True,
        upstream_access=lambda: True,
    )
    with client(host) as http:
        help_data = http.get(PREFIX + "/api-help").json()
        assert (
            help_data["jobs"] == "not yet connected"
            and help_data["unit"] == "million_current_usd"
        )
        url = PREFIX + "/query?start=2025-01&end=2025-02"
        local = http.get(url).json()
        assert local["local_only"] and not wire.calls
        current = http.get(url + "&local_only=false").json()
        assert (
            current["coverage"]["available"] == 6
            and not current["persisted"]
            and len(wire.calls) == 3
        )
        historical = http.get(
            url + "&query_mode=history&as_of=2026-11-03T01:00:00Z&local_only=false"
        ).json()
        assert (
            historical["local_only"]
            and historical["provider_requests"] == 0
            and len(wire.calls) == 3
        )
        series = help_data["series"][0]
        selected = http.get(url + "&local_only=false&series=" + series).json()
        assert (
            selected["coverage"]["expected"] == 2 and selected["provider_requests"] == 1
        )
    assert not s.store.rows


def test_unbound_duplicate_and_unauthorized_are_explicit():
    with client(None) as http:
        assert http.get(PREFIX + "/query?start=2025-01&end=2025-02").status_code == 503
    s, wire = service()
    host = M3QueryHost(read_factory=lambda: s, allow_read=True)
    with client(host) as http:
        assert (
            http.get(
                PREFIX + "/query?start=2025-01&start=2025-01&end=2025-02"
            ).status_code
            == 400
        )
        assert (
            http.get(
                PREFIX + "/query?start=2025-01&end=2025-02&local_only=false"
            ).status_code
            == 403
        )
        assert (
            http.get(
                PREFIX + "/query?start=2025-01&end=2025-02&query_mode=history"
            ).status_code
            == 400
        )
    assert not wire.calls


def test_api_204_keeps_local_rows_and_reports_gap():
    s, wire = service()
    s.collect_range("2025-01", "2025-02", confirm_write=True)
    s.store.rows.pop()
    wire.fail_at = len(wire.calls) + 1
    host = M3QueryHost(
        read_factory=lambda: s,
        query_factory=lambda: s,
        allow_read=True,
        allow_source=True,
        upstream_access=lambda: True,
    )
    with client(host) as http:
        response = http.get(
            PREFIX + "/query?start=2025-01&end=2025-02&local_only=false"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["coverage"] == {"expected": 6, "available": 5}
        assert (
            data["source_failure"]["http_status"] == 204 and len(data["missing"]) == 1
        )
        assert not data["persisted"] and len(s.store.rows) == 5
        spec = http.get("/openapi.json").json()
        names = {
            p["name"] for p in spec["paths"][PREFIX + "/query"]["get"]["parameters"]
        }
        assert names == {
            "start",
            "end",
            "dataset",
            "series",
            "local_only",
            "query_mode",
            "as_of",
            "as_of_basis",
            "include_mom",
        }


def test_invalid_inputs_fail_before_service_factory():
    calls = []

    def factory():
        calls.append(True)
        raise AssertionError("must not be called")

    host = M3QueryHost(read_factory=factory, allow_read=True)
    with client(host) as http:
        for query in (
            "start=2025-01&end=2025-13",
            "start=2025-01&end=2025-02&series=bogus",
            "start=2025-01&end=2025-02&query_mode=history&as_of=2025-01-01T00:00:00",
        ):
            assert http.get(PREFIX + "/query?" + query).status_code == 400
    assert not calls


def test_router_is_mounted_in_existing_preview_when_injected(tmp_path, monkeypatch):
    from market_data_foundation.web.cpi_timeseries_host import create_timeseries_preview
    from tests import test_readonly_host as old

    _, previous, _, _, _, _ = old.setup(tmp_path, monkeypatch)
    s, wire = service()
    host = M3QueryHost(read_factory=lambda: s, allow_read=True)
    app = create_timeseries_preview(
        previous.settings,
        enabled=True,
        connector=previous.connector,
        schedule_path=tmp_path / "cpi-schedule.json",
        job_directory=tmp_path / "cpi-jobs",
        m3_query_host=host,
    )
    with TestClient(
        app, base_url=CANDIDATE_ORIGIN, client=("192.168.10.25", 1234)
    ) as http:
        response = http.get(PREFIX + "/query?start=2025-01&end=2025-02")
        assert response.status_code == 200 and response.json()["local_only"]
    assert not wire.calls
