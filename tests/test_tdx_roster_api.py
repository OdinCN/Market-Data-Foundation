"""Independent base API, published lineage, exact gates and one shared executor."""

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_data_foundation.canonical.model import ContractError
from market_data_foundation.historical import tdx_roster
from market_data_foundation.web.tdx_daily_api import (
    DailyRuntime,
    TdxOptions,
    tdx_router,
)
from market_data_foundation.web.tdx_roster_api import roster_router
from tests.test_tdx_p0 import Store
from tests.test_unified_dev_operations import handler_for, request


def published_store():
    store = Store()
    store.snapshots = [
        dict(
            snapshot_id="official-20261112",
            observed_at="2026-11-12T08:00:00+00:00",
            available_at="2026-11-12T09:00:00+00:00",
            members=[
                dict(
                    market=2,
                    instrument="920002",
                    name="示例证券",
                    evidence=dict(
                        listing_date="2020-07-27",
                        valid_through="2026-11-12",
                        old_code="430002",
                        sha256="a" * 64,
                    ),
                )
            ],
            details=dict(
                valid_through="2026-11-12",
                historical_membership="KNOWN_INTERVALS_ONLY_CODE_LINEAGE_UNVERIFIED",
                markets=[dict(market=m, state="COMPLETE") for m in (0, 1, 2)],
            ),
        )
    ]
    return store


def test_local_query_filters_and_reports_stale_missing_and_history_without_source():
    store = published_store()
    value = tdx_roster.query(store, date(2026, 11, 12), market=2, search="920002")
    assert value["rows"][0]["membership_state"] == "ACTIVE"
    assert value["rows"][0]["evidence"]["old_code"] == "430002"
    assert value["snapshot_id"] == "official-20261112"
    assert value["provider_requests"] == 0
    assert value["state"] == "DATED"
    assert (
        tdx_roster.query(store, date(2026, 11, 13))["classification"]
        == "tdx_roster_stale"
    )
    with pytest.raises(ContractError, match="tdx_roster_stale"):
        tdx_roster.published(store, date(2026, 11, 13), require_current=True)
    empty = SimpleNamespace(universe=lambda **_: None)
    assert tdx_roster.query(empty, date(2026, 11, 12))["state"] == "MISSING"
    with pytest.raises(ContractError, match="tdx_universe_required"):
        tdx_roster.published(empty, date(2026, 11, 12), require_current=True)


def test_actual_router_reuses_existing_jobs_and_preserves_daily_alias(
    tmp_path, monkeypatch
):
    store = published_store()
    runtime = DailyRuntime(
        TdxOptions(
            tmp_path / "state", tmp_path / "driver", "unused:1", allow_collect=True
        ),
        store=store,
        source_factory=lambda _: pytest.fail("GET must not contact a source"),
    )
    monkeypatch.setattr(runtime.jobs, "launch", lambda _: None)
    app = FastAPI()
    app.include_router(tdx_router(runtime))
    app.include_router(roster_router(runtime))
    app.state.mdf_dev_read_origin = "http://192.168.10.20:18766"
    handler = handler_for(monkeypatch, app)
    with TestClient(
        app, base_url="http://192.168.10.20:18766", client=("192.168.10.25", 1234)
    ) as client:
        value = client.get("/api/r5/tdx/roster/query?day=2026-11-12&market=2").json()
        assert value["rows"][0]["instrument"] == "920002"
        assert (
            client.get("/api/r5/tdx/daily/universe").json()["snapshot_id"]
            == value["snapshot_id"]
        )
        for query in [
            "unknown=1",
            "market=0&market=2",
            "as_of=2026-11-12T10:00:00",
            "limit=0",
        ]:
            assert client.get("/api/r5/tdx/roster/query?" + query).status_code == 400
        identity = str(uuid4())
        path = "/api/r5/tdx/roster/jobs"
        assert handler.request_admitted(request(path))
        assert not handler.request_admitted(
            request(path, extra=((b"origin", b"http://evil.invalid"),))
        )
        assert not handler.request_admitted(request(path, peer="203.0.113.1"))
        submitted = client.post(
            path,
            json={"request_id": identity},
            headers={"origin": "http://192.168.10.20:18766"},
        )
        assert submitted.status_code == 202, submitted.text
        assert runtime.jobs.read(identity)["command"]["action"] == "universe"
        assert (
            client.get(path + "/" + identity).json()
            == client.get("/api/r5/tdx/daily/jobs/" + identity).json()
        )
        assert client.get(path).json()["jobs"][0]["request_id"] == identity
        assert (
            client.post(
                path,
                json={"request_id": str(uuid4()), "action": "init"},
                headers={"origin": "http://192.168.10.20:18766"},
            ).status_code
            == 400
        )


def test_daily_stale_roster_fails_before_source_and_retains_exact_publication(tmp_path):
    from tests.test_tdx_p0 import jobs

    store = published_store()
    store.snapshots[0]["details"]["valid_through"] = "2026-11-11"
    executor, _, sources = jobs(tmp_path, store=store)
    identity = str(uuid4())
    executor.submit(
        dict(
            request_id=identity,
            action="update",
            end="2026-11-12",
            markets=[2],
            instruments=["2:920002"],
        ),
        launch=False,
    )
    executor.run(identity)
    receipt = executor.read(identity)
    assert receipt["state"] == "PARTIAL"
    assert receipt["classification"] == "tdx_roster_stale"
    assert receipt["snapshot_id"] == "official-20261112"
    assert receipt["roster"]["state"] == "STALE"
    assert receipt["provider_requests"] == 0
    assert all(s.requests == 0 for s in sources)


def test_same_day_directory_allows_collection_with_dated_exchange_evidence():
    store = published_store()
    store.snapshots[0]["details"]["directory_refreshed_at"] = (
        "2026-11-13T07:10:01+00:00"
    )
    _, status = tdx_roster.published(store, date(2026, 11, 13), require_current=True)
    assert status["state"] == "STALE"
    assert status["valid_through"] == "2026-11-12"
    assert status["same_day_directory"]
    with pytest.raises(ContractError, match="tdx_roster_stale"):
        tdx_roster.published(store, date(2026, 11, 14), require_current=True)
