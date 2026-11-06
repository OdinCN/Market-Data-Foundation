"""Documentation example against the real local route/projection; no private IO."""

import copy
import json
import socket
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_data_foundation.historical.financial_daily_service import (
    FinancialDailyService,
)
from market_data_foundation.web.financial_daily_api import (
    PREFIX,
    FinancialOptions,
    financial_router,
)
from tests.test_financial_daily import AT, payload, snapshot

QUERY = dict(
    start="2026-10-30",
    end="2026-10-31",
    series="BAMLC0A0CM",
    as_of=(AT + timedelta(seconds=3)).isoformat(),
    offset=0,
    limit=200,
)
EXAMPLE = Path("web/src/financial-query-example.json")


class Store:
    schema = "isolated_doc_contract"

    def __init__(self):
        self.data = snapshot(
            payload("BAMLC0A0CM", {"2026-10-30": "1.2500", "2026-10-31": "."})
        )
        self.connection = self
        self.closed = 0

    def read(self, selected, start, end, *, as_of, basis):
        data = copy.deepcopy(self.data)
        data["values"] = [
            r
            for r in data["values"]
            if r[0] in selected and start <= r[1].isoformat() <= end and r[7] <= as_of
        ]
        data["batches"] = [r for r in data["batches"] if r[4] <= as_of]
        data["ranges"] = [r for r in data["ranges"] if data["batches"]]
        data.update(cutoff=as_of, basis=basis)
        return data

    def read_snapshot(self):
        return nullcontext()

    def execute(self, sql, params):
        assert sql.lstrip().startswith("SELECT")
        start, end, cutoff = params
        rows = [
            r for r in self.data["values"] if start <= r[1] <= end and r[7] <= cutoff
        ]
        valid = [r for r in rows if r[3] is not None]
        return SimpleNamespace(
            fetchall=lambda: (
                [
                    (
                        "BAMLC0A0CM",
                        len(rows),
                        len(valid),
                        min(r[1] for r in valid) if valid else None,
                        max(r[1] for r in valid) if valid else None,
                        len(rows) - len(valid),
                    )
                ]
                if rows
                else []
            )
        )

    def close(self):
        self.closed += 1


def fixture(tmp_path):
    store = Store()
    calls = []

    def factory(**kwargs):
        assert not kwargs
        calls.append("local_read")
        return FinancialDailyService(store, clock=lambda: AT + timedelta(seconds=3))

    app = FastAPI()
    app.include_router(
        financial_router(
            FinancialOptions(tmp_path / "jobs", True, True), service_factory=factory
        )
    )
    return app, store, calls


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("documentation contract attempted external IO")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)


def test_success_example_equals_actual_router_and_projection_without_upstream(tmp_path):
    app, store, calls = fixture(tmp_path)
    with TestClient(app) as client:
        result = client.get(PREFIX + "/query", params=QUERY)
        assert result.status_code == 200, result.text
        assert result.json() == json.loads(EXAMPLE.read_text())
        assert result.json()["rows"][0]["basis_points"] == "125.0000"
        assert result.json()["rows"][1]["value"] is None
        assert result.json()["rows"][1]["state"] == "provider_missing"
        assert len(result.json()["coverage"]["series"]) == 10
        paged = client.get(
            PREFIX + "/query", params=QUERY | {"offset": 1, "limit": 1}
        ).json()
        assert len(paged["rows"]) == 1 and paged["rows"][0]["value"] is None
        assert paged["offset"] == 1 and paged["next_offset"] is None
    assert calls == ["local_read", "local_read"] and store.closed == 2


@pytest.mark.parametrize(
    "params,classification",
    [
        (QUERY | {"basket": "all"}, "financial_query_invalid"),
        (QUERY | {"limit": 1001}, "financial_query_invalid"),
        (QUERY | {"offset": -1}, "financial_query_invalid"),
        (QUERY | {"series": "DGS10,DGS10"}, "financial_series_invalid"),
        (QUERY | {"series": "NOT_REGISTERED"}, "financial_series_invalid"),
        (QUERY | {"start": "2026-10-32"}, "financial_date_invalid"),
        (QUERY | {"start": "2026-11-01"}, "financial_query_invalid"),
        (QUERY | {"as_of": "2026-11-05T08:00:03"}, "financial_instant_invalid"),
    ],
)
def test_documented_failures_are_actual_sanitized_route_results(
    tmp_path, params, classification
):
    app, _, _ = fixture(tmp_path)
    with TestClient(app) as client:
        result = client.get(PREFIX + "/query", params=params)
    assert result.status_code == 400
    assert result.json() == {"status": "UNKNOWN", "classification": classification}


def test_early_cutoff_and_unknown_rows_do_not_borrow_later_values(tmp_path):
    app, _, _ = fixture(tmp_path)
    with TestClient(app) as client:
        result = client.get(
            PREFIX + "/query",
            params=QUERY | {"as_of": (AT - timedelta(days=1)).isoformat()},
        ).json()
    assert result["provider_requests"] == 0
    assert all(
        r["value"] is None and r["state"] == "not_known_at_cutoff"
        for r in result["rows"]
    )


def test_query_unconfigured_classification_differs_from_other_read_routes():
    app = FastAPI()
    app.include_router(financial_router(None))
    with TestClient(app) as client:
        query = client.get(PREFIX + "/query", params=QUERY)
        assert query.status_code == 400
        assert query.json() == {
            "status": "UNKNOWN",
            "classification": "financial_query_invalid",
        }
        status = client.get(PREFIX + "/status")
        assert status.status_code == 403
        assert status.json()["classification"] == "financial_capability_not_enabled"
