"""Regressions from independent PostgreSQL review, using deterministic inputs."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_census_retail_timeseries import (  # noqa: F401
    AT,
    Wire,
    encode,
    no_network,
    payload,
    reader,
    response,
    target,
)
from test_retail_timeseries_backend import batch, service

from market_data_foundation.historical.census_retail_live import HttpResult
from market_data_foundation.historical.census_retail_timeseries_source import (
    RetailSourceError,
)
from market_data_foundation.historical.retail_timeseries_store import (
    InMemoryRetailStore,
)


def test_unconfirmed_revision_masks_stale_only_after_observation():
    store = InMemoryRetailStore(clock=lambda: AT + timedelta(days=1))
    store.save_batch("initial", batch())
    table = payload("2025-09", "2026-09")
    target(table, "2026-09", "441")[table[0].index("cell_value")] = "12"
    store.clock = lambda: AT + timedelta(days=3)
    store.save_batch("changed", batch(table, observed=AT + timedelta(days=2)))
    key = ("2026-09", "441", "SM")
    store.revisions[key][-1] = replace(store.revisions[key][-1], available_at=None)
    assert key not in store.read_range("2026-09", "2026-09", basis="available")
    assert key not in store.read_range(
        "2026-09", "2026-09", basis="available", as_of=AT + timedelta(days=4)
    )
    past = store.read_range(
        "2026-09", "2026-09", basis="available", as_of=AT + timedelta(days=1)
    )
    assert past[key].revision == 1
    assert store.read_range("2026-09", "2026-09", basis="observed")[key].revision == 2
    point = service(store).query_range(
        "2026-09", "2026-09", query_mode="history", as_of=AT + timedelta(days=4)
    )["points"][0]
    assert point["control_level"]["value"] is None
    assert point["headline_level"]["value"] is not None


def test_query_fetches_only_missing_month_and_never_persists():
    wire = Wire()
    svc = service(wire=wire)
    svc.store.save_batch("initial", batch())
    del svc.store.revisions["2026-06", "441", "SM"]
    before = svc.store.status()
    result = svc.query_range("2026-09", "2026-09")
    assert [call[1]["time"] for call in wire.calls] == ["from 2026-06 to 2026-06"]
    assert result["points"][0]["control_rates"][3]["value"] is not None
    assert result["persisted"] is False and result["provider_requests"] == 1
    assert (result["start_month"], result["end_month"]) == ("2026-09", "2026-09")
    assert svc.store.status() == before


def test_query_failure_retains_attempt_count_across_two_gap_spans():
    wire = Wire()
    svc = service(wire=wire)
    assert (
        svc.initialize("2006-10", "2026-10", collection_id="baseline")["status"]
        == "COMPLETED"
    )
    del svc.store.revisions["2005-10", "441", "SM"]
    del svc.store.revisions["2026-10", "441", "SM"]
    wire.calls.clear()
    wire.outcomes = [response(encode(payload("2005-10", "2005-10")))] + [
        HttpResult(503, b"", {})
    ] * 3
    before = svc.store.status()
    with pytest.raises(RetailSourceError) as failure:
        svc.query_range("2006-10", "2026-10")
    assert failure.value.attempts == 4
    assert [call[1]["time"] for call in wire.calls] == ["from 2005-10 to 2005-10"] + [
        "from 2026-10 to 2026-10"
    ] * 3
    assert svc.store.status() == before
