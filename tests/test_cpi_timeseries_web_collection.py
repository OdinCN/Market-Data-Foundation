"""Long Web collections through the real BLS planner/parser, entirely offline."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from threading import Event

import pytest

from market_data_foundation.historical.bls_cpi_direct import SERIES_IDS
from market_data_foundation.historical.bls_cpi_direct_live import HttpResult
from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesService
from market_data_foundation.historical.cpi_timeseries_source import BlsRangeSource
from market_data_foundation.web.cpi_timeseries_api import PREFIX
from market_data_foundation.web.cpi_timeseries_host import LocalOnlySource
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from tests.test_cpi_timeseries_web import (  # noqa: F401
    NOW,
    client,
    command,
    fixture,
    no_network,
)


class ContinuousTransport:
    def __init__(self):
        self.requests = []
        self.offset = 0
        self.now = NOW
        self.missing = set()
        self.nulls = set()

    def index(self, series, year, month):
        # A continuous, distinct sequence for each series across every year.
        return Decimal(1000 + 100 * SERIES_IDS.index(series) + self.offset) + (
            Decimal((year - 1999) * 12 + month - 1) / 10
        )

    def post(self, endpoint, body, headers, *, timeout, max_response_bytes):
        request = json.loads(body)
        self.requests.append((request["startyear"], request["endyear"]))
        series = []
        for sid in request["seriesid"]:
            rows = []
            for year in range(int(request["startyear"]), int(request["endyear"]) + 1):
                for month in range(1, 13):
                    key = f"{year:04d}-{month:02d}"
                    if key in self.missing:
                        continue
                    rows.append(
                        {
                            "year": str(year),
                            "period": f"M{month:02d}",
                            "value": (
                                "-"
                                if key in self.nulls
                                else str(self.index(sid, year, month))
                            ),
                            "footnotes": [],
                        }
                    )
            series.append({"seriesID": sid, "data": rows})
        raw = json.dumps(
            {"status": "REQUEST_SUCCEEDED", "Results": {"series": series}}
        ).encode()
        return HttpResult(200, raw, {})


@pytest.fixture
def collection(fixture):  # noqa: F811 - shared pytest fixture
    host, store, _, _, _ = fixture
    transport = ContinuousTransport()
    store._clock = host.clock = lambda: transport.now
    host.packages.credential_loader = lambda *_: "a" * 32
    host.read_factory = lambda: CpiTimeSeriesService(store, LocalOnlySource())

    def write_service(*, credential_loader):
        return CpiTimeSeriesService(
            store,
            BlsRangeSource(
                credential_loader=credential_loader,
                transport=transport,
                clock=lambda: transport.now,
            ),
        )

    host.write_factory = write_service
    return host, store, transport


def assert_continuous_ratios(host, transport):
    result = host.query(
        {
            "start": "2000-01",
            "end": "2026-09",
            "basket": "all",
            "adjustment": "all",
            "vintage_mode": "available",
        }
    )
    assert result["provider_requests"] == result["provider_attempts"] == 0
    assert result["coverage_complete"] and not result["calculation_missing"]
    points = {(p["series_id"], p["month"]): p for p in result["points"]}
    assert len(points) == 4 * 321
    for sid in SERIES_IDS:
        for year in (2000, 2019):
            point = points[sid, f"{year}-01"]
            current = transport.index(sid, year, 1)
            assert Decimal(point["index_value"]) == current
            assert point["origin"] == "local"
            for lag in (1, 3, 6, 12):
                previous = transport.index(sid, year - 1, 13 - lag)
                expected = (current / previous - 1).quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                )
                metric = f"change_{lag}m_ratio"
                assert point[metric] == str(expected)
                assert (
                    point["calculation_inputs"][metric]["denominator_origin"] == "local"
                )


def test_long_collection_running_receipt_readback_replay_and_update(
    collection, monkeypatch
):
    host, store, transport = collection
    request = command(start="2000-01", end="2026-09")
    entered, release, finished = Event(), Event(), Event()
    original_submit = host.submit

    def submit(*args, **kwargs):
        try:
            return original_submit(*args, **kwargs)
        finally:
            if release.is_set():
                finished.set()

    monkeypatch.setattr(host, "submit", submit)
    original_post = transport.post

    def paused_post(*args, **kwargs):
        if len(transport.requests) == 1:
            entered.set()
            assert release.wait(10), "test_transport_not_released"
        return original_post(*args, **kwargs)

    monkeypatch.setattr(transport, "post", paused_post)
    headers = {"Origin": CANDIDATE_ORIGIN}

    def post_and_close_client():
        # The DEV HTTP bridge creates and closes a TestClient per request.
        # Closing it must not wait for the still-blocked collection worker.
        with client(host) as requester:
            return requester.post(PREFIX + "/jobs", json=request, headers=headers)

    with client(host) as http, ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(post_and_close_client)
        try:
            assert entered.wait(10), "second_backend_batch_not_started"
            response = pending.result(timeout=5)
            assert response.status_code == 202
            assert response.json()["state"] == "running"
            running = http.get(PREFIX + "/jobs").json()["jobs"]
            assert len(running) == 1
            assert running[0]["request_id"] == request["request_id"]
            assert running[0]["state"] == "running"
            replay = http.post(PREFIX + "/jobs", json=request, headers=headers)
            assert replay.status_code == 202 and replay.json() == running[0]
            busy = http.post(
                PREFIX + "/jobs",
                json=command(start="2000-01", end="2026-09"),
                headers=headers,
            )
            assert busy.status_code == 409
            assert busy.json()["classification"] == "cpi_jobs_busy"
            assert len(http.get(PREFIX + "/jobs").json()["jobs"]) == 1
            assert transport.requests == [("1999", "2018")]
        finally:
            release.set()
        assert finished.wait(10), "collection_not_finished"
        job = http.get(PREFIX + "/jobs").json()["jobs"][0]
        assert job["state"] == "complete"
        assert job["result"]["readback"]["verified"] is True
        assert job["result"]["readback"]["point_count"] == 4 * 321
        assert job["result"]["saved_points"] == 4 * (321 + 12)
        assert job["result"]["provider_requests"] == 2
        assert transport.requests == [("1999", "2018"), ("2019", "2026")]
        assert http.get(PREFIX + "/jobs").json()["jobs"] == [job]
        assert host.journal.read(request["request_id"]) == job
        assert_continuous_ratios(host, transport)
        assert http.post(PREFIX + "/jobs", json=request, headers=headers).json() == job
        repeated = host.submit(command(start="2000-01", end="2026-09"))
        assert repeated["state"] == "complete"
        assert repeated["result"]["provider_requests"] == 0
        assert repeated["result"]["readback"]["verified"] is True
        assert len(transport.requests) == 2

        transport.offset = 50
        transport.now += timedelta(days=1)
        updated = host.submit(command("update", start="2000-01", end="2026-09"))
        assert updated["state"] == "complete", updated
        assert updated["result"]["provider_requests"] == 2
        assert updated["result"]["readback"]["verified"] is True
        assert updated["result"]["saved_revisions"] == 4 * (321 + 12)
        assert transport.requests == [("1999", "2018"), ("2019", "2026")] * 2
        assert store.status()["revision_count"] == 4 * (321 + 12)
        assert_continuous_ratios(host, transport)


@pytest.mark.parametrize("kind", ["missing", "nulls"])
def test_real_denominator_gaps_remain_null_across_batches(collection, kind):
    host, _, transport = collection
    getattr(transport, kind).update({"1999-01", "2018-01"})
    job = host.submit(command(start="2000-01", end="2026-09"))
    assert job["state"] == ("failed" if kind == "missing" else "complete")
    assert transport.requests == [("1999", "2018"), ("2019", "2026")]
    for start in ("2000-01", "2019-01"):
        result = host.query(
            {"start": start, "end": start, "basket": "all", "adjustment": "all"}
        )
        assert len(result["points"]) == 4
        for point in result["points"]:
            assert point["change_12m_ratio"] is None
            assert all(point[f"change_{lag}m_ratio"] is not None for lag in (1, 3, 6))
        assert (
            sum(
                item["metric"] == "change_12m_ratio"
                for item in result["calculation_missing"]
            )
            == 4
        )
    assert len(transport.requests) == 2


def test_default_web_limit_still_enforces_core_request_budget(collection, monkeypatch):
    from market_data_foundation.historical import cpi_timeseries

    host, store, transport = collection
    monkeypatch.setattr(cpi_timeseries, "MAX_RANGE_REQUESTS", 1)
    job = host.submit(command(start="2000-01", end="2026-09"))
    assert job["state"] == "unknown"
    assert job["classification"] == "cpi_timeseries_request_budget_exceeded"
    assert job["result"]["provider_attempts"] == 0
    assert transport.requests == []
    assert store.status()["observation_count"] == 0
