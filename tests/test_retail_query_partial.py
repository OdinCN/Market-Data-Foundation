"""Offline regression for retained retail data and incomplete reference months."""

from datetime import UTC, datetime

import pytest
from test_census_retail_timeseries import AT, Wire, no_network  # noqa: F401
from test_retail_timeseries_backend import batch, service

from market_data_foundation.historical.census_retail_live import HttpResult


def test_screenshot_range_retains_local_points_after_source_500():
    wire = Wire([HttpResult(500, b"", {})] * 3)
    svc = service(wire=wire)
    svc.clock = lambda: datetime(2026, 11, 1, tzinfo=UTC)
    svc.store.save_batch("existing", batch(start="2024-10"))
    before = svc.query_range("2025-10", "2026-12", local_only=True)
    result = svc.query_range("2025-10", "2026-12")
    assert result["status"] == "PARTIAL"
    assert result["points"] == before["points"]
    assert len(result["points"]) == 15
    assert sum(p["headline_level"]["value"] is not None for p in result["points"]) == 12
    assert result["source_failure"]["classification"] == "census_retail_http_failure"
    assert result["source_failure"]["http_status"] == 500
    assert result["provider_requests"] == 1
    assert result["provider_attempts"] == 3
    assert all(c[1]["time"] == "from 2026-10 to 2026-10" for c in wire.calls)


@pytest.mark.parametrize(
    "kind,attempts,status", [("timeout", 3, None), ("429", 1, 429), ("500", 3, 500)]
)
def test_safe_failure_facts_http_200_and_no_writes(kind, attempts, status):
    from test_retail_timeseries_backend import client

    outcomes = (
        [TimeoutError("must not leak")] * 3
        if kind == "timeout"
        else [HttpResult(status, b"", {"Retry-After": "600"} if kind == "429" else {})]
        * attempts
    )
    wire = Wire(outcomes)
    svc = service(wire=wire)
    svc.store.save_batch("existing", batch(start="2024-10"))
    before = svc.store.status()
    result = client(svc, source=True).get(
        "/api/r5/retail/timeseries/query?start=2025-10&end=2026-12"
    )
    assert result.status_code == 200
    data = result.json()
    assert data["status"] == "PARTIAL"
    assert data["provider_attempts"] == attempts
    assert data["source_failure"]["http_status"] == status
    assert data["missing_input_count"] == 18
    assert data["source_failure"]["retry_at"] == (
        "2026-10-31T00:10:00+00:00" if kind == "429" else None
    )
    assert "must not leak" not in result.text
    assert svc.store.status() == before


@pytest.mark.parametrize(
    "outcome",
    [
        HttpResult(403, b"", {}),
        HttpResult(200, b"{}", {"content-type": "application/json"}),
    ],
)
def test_permission_and_contract_failure_not_masked(outcome):
    from market_data_foundation.historical.census_retail_timeseries_source import (
        RetailSourceError,
    )

    svc = service(wire=Wire([outcome]))
    svc.store.save_batch("existing", batch())
    with pytest.raises(RetailSourceError):
        svc.query_range("2026-09", "2026-10")


@pytest.mark.parametrize("local_only,mode", [(True, "current"), (False, "history")])
def test_local_and_history_never_request(local_only, mode):
    wire = Wire()
    svc = service(wire=wire)
    svc.store.save_batch("existing", batch())
    result = svc.query_range(
        "2025-10", "2026-12", local_only=local_only, query_mode=mode
    )
    assert len(result["points"]) == 15
    assert result["source_failure"] is None
    assert not wire.calls


def test_future_months_preserved_and_only_ended_month_filled():
    wire = Wire()
    svc = service(wire=wire)
    svc.clock = lambda: datetime(2026, 11, 1, tzinfo=UTC)
    svc.store.save_batch("existing", batch(start="2024-10"))
    before = svc.store.status()
    result = svc.query_range("2025-10", "2026-12")
    assert len(result["points"]) == 15
    assert result["status"] == "OK" and result["source_failure"] is None
    assert result["last_completed_month"] == "2026-10"
    assert result["source_skipped_months"] == ["2026-11", "2026-12"]
    assert result["missing_input_count"] == 12
    assert [c[1]["time"] for c in wire.calls] == ["from 2026-10 to 2026-10"]
    assert all(p["headline_level"]["value"] is None for p in result["points"][:2])
    assert (
        result["points"][2]["input_versions"]["2026-10/44X72/SM"]["origin"]
        == "upstream"
    )
    assert svc.store.status() == before
    wire.calls.clear()
    result = svc.query_range("2025-10", "2026-09")
    assert result["status"] == "OK" and not wire.calls


def test_all_future_inputs_do_not_bind_or_call_source():
    svc = service()
    svc.clock = lambda: datetime(2026, 11, 1, tzinfo=UTC)
    svc.source = None
    result = svc.query_range("2028-01", "2028-03")
    assert len(result["points"]) == 3
    assert result["provider_attempts"] == 0
    assert result["missing_input_count"] == 90


def test_without_local_target_data_failure_still_errors():
    from market_data_foundation.historical.census_retail_timeseries_source import (
        RetailSourceError,
    )

    svc = service(wire=Wire([HttpResult(500, b"", {})] * 3))
    svc.store.save_batch("buffer_only", batch(end="2025-09"))
    with pytest.raises(RetailSourceError):
        svc.query_range("2025-10", "2026-12")


def test_transport_permission_error_is_not_transient():
    from market_data_foundation.historical.census_retail_timeseries_source import (
        RetailSourceError,
    )

    wire = Wire([PermissionError("never expose")])
    svc = service(wire=wire)
    svc.store.save_batch("existing", batch())
    with pytest.raises(RetailSourceError, match="census_retail_permission_denied"):
        svc.query_range("2026-09", "2026-10")
    assert len(wire.calls) == 1


def test_partial_retains_prior_success_and_total_counts_across_windows():
    from test_census_retail_timeseries import encode, payload, response

    wire = Wire()
    svc = service(wire=wire)
    svc.store.save_batch("existing", batch(start="2005-10", end="2026-09"))
    del svc.store.revisions["2005-10", "441", "SM"]
    wire.calls.clear()
    wire.outcomes = [response(encode(payload("2005-10", "2005-10")))] + [
        HttpResult(503, b"", {})
    ] * 3
    before = svc.store.status()
    result = svc.query_range("2006-10", "2026-10")
    assert result["status"] == "PARTIAL"
    assert result["provider_attempts"] == 4
    assert result["provider_requests"] == 2
    assert (
        result["points"][-1]["input_versions"]["2005-10/441/SM"]["origin"] == "upstream"
    )
    assert svc.store.status() == before


def test_reference_boundary_uses_utc_not_clock_offset_or_latest_local():
    from datetime import timedelta, timezone

    wire = Wire()
    svc = service(wire=wire)
    # November in Beijing is still October UTC. September remains eligible.
    svc.clock = lambda: datetime(
        2026, 11, 1, 0, 30, tzinfo=timezone(timedelta(hours=8))
    )
    svc.store.save_batch("existing", batch(start="2024-10"))
    del svc.store.revisions["2026-09", "441", "SM"]
    result = svc.query_range("2025-10", "2026-12")
    assert result["last_completed_month"] == "2026-09"
    assert [c[1]["time"] for c in wire.calls] == ["from 2026-09 to 2026-09"]
    assert len(result["points"]) == 15


@pytest.mark.parametrize("error", [PermissionError("private"), RuntimeError("private")])
def test_local_exception_propagates_without_source_io(error):
    wire = Wire()
    svc = service(wire=wire)
    svc.store.save_batch("existing", batch())

    def fail(*args, **kwargs):
        raise error

    svc.store.read_range = fail
    with pytest.raises(type(error)):
        svc.query_range("2026-09", "2026-12")
    assert not wire.calls


@pytest.mark.parametrize("existing", [True, False])
def test_204_empty_response_is_no_records_not_failure_or_official_null(existing):
    from test_retail_timeseries_backend import client

    wire = Wire([HttpResult(204, b"", {})])
    svc = service(wire=wire)
    svc.clock = lambda: datetime(2026, 11, 1, tzinfo=UTC)
    if existing:
        svc.store.save_batch("existing", batch(start="2024-10"))
    start = "2025-10" if existing else "2026-10"
    before = svc.query_range(start, "2026-12", local_only=True)
    stored = svc.store.status()
    http = client(svc, source=True)
    local = http.get(
        f"/api/r5/retail/timeseries/query?start={start}&end=2026-12&local_only=true"
    ).json()
    result = http.get(f"/api/r5/retail/timeseries/query?start={start}&end=2026-12")
    assert result.status_code == 200
    data = result.json()
    assert data["status"] == "PARTIAL"
    assert data["source_failure"] is None
    assert data["source_no_content"] == [
        {
            "classification": "census_retail_no_content",
            "http_status": 204,
            "response_bytes": 0,
            "start_month": "2026-10" if existing else "2025-10",
            "end_month": "2026-10",
        }
    ]
    assert data["points"] == local["points"]
    assert data["provider_requests"] == data["provider_attempts"] == 1
    assert len(data["points"]) == (15 if existing else 3)
    assert (
        svc.query_range(start, "2026-12", local_only=True)["points"] == before["points"]
    )
    assert all(
        c["status"] == "not_provided" and c["raw_sha256"] == ""
        for p in data["points"][:3]
        for c in p["source_cells"]
    )
    assert [c[1]["time"] for c in wire.calls] == [
        "from 2026-10 to 2026-10" if existing else "from 2025-10 to 2026-10"
    ]
    assert svc.store.status() == stored


def test_nonempty_204_is_a_contract_error_even_with_local_data():
    from market_data_foundation.historical.census_retail_timeseries_source import (
        RetailSourceError,
    )

    svc = service(wire=Wire([HttpResult(204, b"unexpected", {})]))
    svc.store.save_batch("existing", batch())
    with pytest.raises(RetailSourceError, match="no_content_body_invalid"):
        svc.query_range("2026-09", "2026-10")


def test_collection_204_keeps_waiting_behavior_and_creates_no_batch():
    wire = Wire([HttpResult(204, b"", {})])
    svc = service(wire=wire)
    before = svc.store.status()
    result = svc.initialize("2026-10", "2026-10", collection_id="empty-response")
    assert result["status"] == "ERROR"
    assert result["classification"] == "census_retail_http_failure"
    assert result["provider_attempts"] == 1
    assert result["saved_points"] == result["completed_batches"] == 0
    assert svc.store.status() == before


@pytest.mark.parametrize("start,end", [("2026-11", "2026-12"), ("2026-12", "2027-01")])
def test_unended_target_months_do_not_fetch_past_comparison_buffer(start, end):
    svc = service()
    svc.clock = lambda: datetime(2026, 11, 1, tzinfo=UTC)
    svc.source = None
    result = svc.query_range(start, end)
    assert len(result["points"]) == 2
    assert result["provider_requests"] == result["provider_attempts"] == 0
    assert all(point["headline_level"]["value"] is None for point in result["points"])
