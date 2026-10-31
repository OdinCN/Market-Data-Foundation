"""Offline PCE calculations, query effects, history cutoffs and coverage."""

from datetime import timedelta
from decimal import Decimal

import pytest
from test_pce_timeseries_source import AT, SyntheticTransport, source

from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    PCE_PROFILE,
)
from market_data_foundation.historical.cpi_timeseries_source import (
    SourceBatch,
    SourcePoint,
)
from market_data_foundation.historical.pce_timeseries import (
    PceTimeSeriesError,
    PceTimeSeriesService,
)
from market_data_foundation.historical.pce_timeseries_store import (
    InMemoryTimeSeriesStore,
)


def test_twenty_year_init_with_buffer_and_idempotence():
    wire, store = SyntheticTransport(), InMemoryTimeSeriesStore(clock=lambda: AT)
    service = PceTimeSeriesService(store, source(wire))
    result = service.init_range("2006-10", "2026-09")
    assert result["complete"] and result["saved_points"] == 504
    assert len(result["points"]) == 480 and len(wire.calls) == 10
    assert result["provider_requests"] == result["provider_attempts"] == 10
    assert not result["calculation_missing"]
    assert store.status()["schema"] == "mdf_pce_timeseries_dev_r1"
    assert set(store.status()["latest_months"]) == {"DPCERG", "DPCCRG"}
    repeated = service.init_range("2006-10", "2026-09")
    assert repeated["saved_points"] == 0 and len(wire.calls) == 10
    assert {p["indicator"] for p in result["points"]} == {"headline", "core"}
    assert all(
        p["seasonal_adjustment"] == "SA" and p["provider"] == "BEA"
        for p in result["points"]
    )


def test_query_has_zero_store_writes_even_with_upstream_buffer():
    store = InMemoryTimeSeriesStore(clock=lambda: AT)
    service = PceTimeSeriesService(store, source())
    before = store.status()
    result = service.query_range("2026-09", "2026-09")
    assert result["complete"] and not result["persisted"]
    assert store.status() == before and not store._batches
    assert all(p["raw_locator"] is None for p in result["points"])


def test_same_cutoff_endpoints_and_ratio_unit():
    store = InMemoryTimeSeriesStore(clock=lambda: AT)

    def batch(at, previous, current):
        points = tuple(
            SourcePoint("DPCERG", month, Decimal(value), {}, ())
            for month, value in (("2025-09", previous), ("2026-09", current))
        )
        return SourceBatch(points, b"synthetic", {"seriesid": ["DPCERG"]}, at, 1)

    store.save_batches((batch(AT, "100", "103.12"),))
    store._clock = lambda: AT + timedelta(days=1)
    store.save_batches((batch(AT + timedelta(days=1), "200", "300"),))
    no_source = type(
        "LocalSource",
        (),
        {
            "profile": PCE_PROFILE,
            "fetch": lambda *args: pytest.fail("history called upstream"),
        },
    )()
    service = PceTimeSeriesService(store, no_source)
    result = service.query_range(
        "2026-09", "2026-09", series_ids=("DPCERG",), as_of=AT + timedelta(hours=1)
    )
    point = result["points"][0]
    assert point["change_12m_ratio"] == "0.0312" and point["yoy_pct"] == "3.12"
    assert (
        point["calculation_inputs"]["change_12m_ratio"]["denominator_index_value"]
        == "100"
    )
    assert result["source_request_count"] == 0
    assert point["available_at"] == AT.isoformat()
    assert (
        service.query_range("2026-09", "2026-09", as_of=AT - timedelta(seconds=1))[
            "points"
        ]
        == []
    )


def test_source_shortfall_is_reported_without_fake_coverage():
    from test_pce_timeseries_source import synthetic_response

    wire = SyntheticTransport(synthetic_response())
    result = PceTimeSeriesService(InMemoryTimeSeriesStore(), source(wire)).init_range(
        "2026-08", "2026-09"
    )
    assert not result["complete"] and len(result["missing"]) == 2
    assert result["calculation_missing"]
    assert all(p["change_12m_ratio"] is None for p in result["points"])


def test_cross_profile_source_and_series_are_rejected_before_fetch():
    other = type("WrongSource", (), {"profile": CPI_PROFILE})()
    with pytest.raises(PceTimeSeriesError, match="profile_mismatch"):
        PceTimeSeriesService(InMemoryTimeSeriesStore(), other)
    service = PceTimeSeriesService(InMemoryTimeSeriesStore(), source())
    with pytest.raises(PceTimeSeriesError, match="series_invalid"):
        service.query_range("2026-09", "2026-09", series_ids=CPI_PROFILE.series_ids)


def test_local_only_never_loads_provider_key():
    from market_data_foundation.historical.pce_timeseries import build_source

    no_credentials = build_source(
        credential_loader=lambda: pytest.fail("local read loaded BEA key")
    )
    service = PceTimeSeriesService(InMemoryTimeSeriesStore(), no_credentials)
    assert not service.query_range("2026-09", "2026-09", allow_upstream=False)[
        "complete"
    ]
    assert service.status()["observation_count"] == 0
