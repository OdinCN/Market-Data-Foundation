"""Deterministic employment semantics; no live provider or PostgreSQL."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    EMPLOYMENT_PROFILE,
)
from market_data_foundation.historical.cpi_timeseries import month_number, month_range
from market_data_foundation.historical.cpi_timeseries_source import (
    SourceBatch,
    SourcePoint,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    InMemoryTimeSeriesStore,
    _provider_calculations,
    _rates_view_sql,
    _relational_content,
    _revision_ddl,
)
from market_data_foundation.historical.employment_timeseries import (
    SERIES_IDS,
    BlsEmploymentRangeSource,
    EmploymentTimeSeriesService,
    latest_view_sql,
    parse_response,
)
from market_data_foundation.historical.employment_timeseries_cli import main
from tests.test_cpi_timeseries_source import WindowTransport

NOW = datetime(2026, 10, 31, tzinfo=UTC)


class Source:
    profile = EMPLOYMENT_PROFILE

    def __init__(self):
        self.calls = []
        self.at = NOW
        self.overrides = {}

    def fetch(self, series, start, end):
        self.calls.append((series, start, end))
        points = []
        for sid in series:
            for month in month_range(start, end):
                n = Decimal(month_number(month) - month_number("2025-01"))
                value = {
                    SERIES_IDS[0]: Decimal(150000) + n * 10,
                    SERIES_IDS[1]: Decimal(4) + n / 10,
                    SERIES_IDS[2]: Decimal(30) + n / 10,
                }[sid]
                points.append(
                    SourcePoint(
                        sid,
                        month,
                        self.overrides.get((sid, month), value),
                        {"net_changes": {"1": "-2.0"}, "pct_changes": {"1": "0.1"}},
                        ({"code": "P", "text": "Preliminary"},),
                    )
                )
        return (
            SourceBatch(
                tuple(points),
                str(self.at).encode(),
                {"seriesid": list(series)},
                self.at,
                1,
            ),
        )


@pytest.fixture
def setup():
    source = Source()
    store = InMemoryTimeSeriesStore(profile=EMPLOYMENT_PROFILE, clock=lambda: source.at)
    return EmploymentTimeSeriesService(store, source), store, source


def test_three_series_have_distinct_units_and_same_series_comparisons(setup):
    service, store, source = setup
    result = service.init_range("2025-01", "2026-01")
    jobs, rate, ahe = [
        next(p for p in result["points"] if p["series_id"] == sid) for sid in SERIES_IDS
    ]
    assert jobs["value"] == "150000" and jobs["value_unit"] == "thousand_jobs"
    assert jobs["net_change_1m_thousand_jobs"] == "10"
    assert rate["value"] == "4" and rate["survey"] == "CPS"
    assert rate["change_1m_percentage_points"] == "0.1"
    assert "mom_pct" not in rate and "change_1m_ratio" not in rate
    assert "mom_pct" not in jobs and "change_1m_ratio" not in jobs
    assert ahe["value_unit"] == "USD/hour" and ahe["survey"] == "CES"
    assert ahe["mom_pct"] == "0.33" and ahe["change_1m_ratio"] == "0.0033"
    assert ahe["yoy_pct"] == "4.17" and ahe["change_12m_ratio"] == "0.0417"
    assert jobs["provider_calculations"]["net_changes"]["1"] == "-2.0"
    assert source.calls[0][1] == "2024-01"
    assert result["calculation_missing"] == []
    assert service.init_range("2025-01", "2026-01")["provider_requests"] == 0
    assert store.status()["revision_count"] == 0


def test_query_fills_only_gaps_without_saving_and_history_never_fetches(setup):
    service, store, source = setup
    service.init_range("2025-01", "2025-01", series_ids=(SERIES_IDS[0],))
    count = store.status()["observation_count"]
    result = service.query_range("2025-01", "2025-01")
    assert source.calls[-1][0] == SERIES_IDS[1:]
    assert result["complete"] and result["persisted"] is False
    assert store.status()["observation_count"] == count
    assert result["points"][1]["raw_locator"] is None
    calls = len(source.calls)
    result = service.query_range("2025-01", "2025-01", as_of=source.at)
    assert len(source.calls) == calls and len(result["missing"]) == 2


def test_changed_only_and_same_cutoff_comparison_inputs(setup):
    service, _, source = setup
    first = service.init_range("2025-01", "2025-02")
    source.at += timedelta(minutes=1)
    assert service.update_range("2025-01", "2025-02")["saved_revisions"] == 0
    source.at += timedelta(minutes=1)
    source.overrides[SERIES_IDS[0], "2025-01"] = Decimal(149990)
    changed = service.update_range("2025-01", "2025-02")
    assert changed["saved_revisions"] == 1
    assert changed["points"][1]["net_change_1m_thousand_jobs"] == "20"
    historical = service.query_range("2025-02", "2025-02", as_of=NOW)
    assert historical["points"][0]["net_change_1m_thousand_jobs"] == "10"
    assert first["points"][0]["revision"] == 1
    assert "not official first release" in first["calculation_method"]


def test_missing_comparison_is_not_zero_and_zero_unemployment_is_valid(setup):
    service, _, source = setup
    source.overrides[SERIES_IDS[0], "2024-12"] = None
    source.overrides[SERIES_IDS[1], "2025-01"] = Decimal(0)
    result = service.query_range("2025-01", "2025-01")
    assert result["points"][0]["net_change_1m_thousand_jobs"] is None
    assert result["points"][1]["change_1m_percentage_points"] == "-3.9"
    assert result["calculation_missing"][0]["required_month"] == "2024-12"


def test_bounded_bls_batches_and_cross_batch_first_month(setup):
    transport = WindowTransport()
    source = BlsEmploymentRangeSource(
        transport=transport, credential_loader=lambda: "a" * 32, clock=lambda: NOW
    )
    store = InMemoryTimeSeriesStore(profile=EMPLOYMENT_PROFILE, clock=lambda: NOW)
    result = EmploymentTimeSeriesService(store, source).init_range("2005-01", "2026-01")
    assert len(transport.requests) == 2
    assert all(
        r["seriesid"] == list(SERIES_IDS) and r["calculations"]
        for r in transport.requests
    )
    first = next(
        p
        for p in result["points"]
        if p["series_id"] == SERIES_IDS[2] and p["month"] == "2024-01"
    )
    assert first["calculation_inputs"]["mom_pct"]["denominator_month"] == "2023-12"
    assert first["mom_pct"] is not None


def test_parser_keeps_official_calculations_footnotes_decimal_and_null():
    calculations = {"net_changes": {"1": "-100"}, "pct_changes": {"12": "2.5"}}
    raw = json.dumps(
        {
            "status": "REQUEST_SUCCEEDED",
            "Results": {
                "series": [
                    {
                        "seriesID": sid,
                        "data": [
                            {
                                "year": "2026",
                                "period": "M09",
                                "value": value,
                                "calculations": calculations,
                                "footnotes": [{"code": "P", "text": "Preliminary"}],
                            }
                        ],
                    }
                    for sid, value in zip(
                        SERIES_IDS, ("150000.123456789", "0", "-"), strict=True
                    )
                ]
            },
        }
    ).encode()
    points = {
        p.series_id: p for p in parse_response(raw, SERIES_IDS, "2026-09", "2026-09")
    }
    assert points[SERIES_IDS[0]].index_value == Decimal("150000.123456789")
    assert points[SERIES_IDS[1]].index_value == 0
    assert points[SERIES_IDS[2]].index_value is None
    point = points[SERIES_IDS[0]]
    relational = _relational_content(point, profile=EMPLOYMENT_PROFILE)
    restored = _provider_calculations(
        relational[1:5], relational[5], profile=EMPLOYMENT_PROFILE
    )
    assert restored == calculations
    assert point.footnotes[0]["code"] == "P"
    assert point.provider_calculations == calculations


def test_employment_ddl_is_isolated_and_has_unit_specific_view():
    ddl = _revision_ddl(profile=EMPLOYMENT_PROFILE)
    rates = _rates_view_sql(profile=EMPLOYMENT_PROFILE)
    assert EMPLOYMENT_PROFILE.schema in ddl and "index_value >= 0" in ddl
    assert "unemployment_rate_percent" in rates and "ahe_mom_percent" in rates
    assert "net_change_1m_thousand_jobs" in rates
    assert "value_unit" in latest_view_sql()
    assert "local_revision" in latest_view_sql()
    assert CPI_PROFILE.schema not in ddl + rates
    assert "index_value > 0" in _revision_ddl()


def test_cli_defaults_to_240_months_and_update_requires_range(setup, capsys):
    service, _, source = setup

    def factory(**options):
        return service

    assert main(["init", "--end", "2026-09"], service_factory=factory) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["start_month"] == "2006-10" and output["end_month"] == "2026-09"
    count = len(source.calls)
    assert main(["update"], service_factory=factory) == 2
    assert len(source.calls) == count


def test_cli_source_credential_binding_requires_explicit_live_opt_in(capsys):
    def factory(**options):
        options["credential_loader"]()
        pytest.fail("credential loader must reject before any live operation")

    assert main(["init", "--end", "2026-09"], service_factory=factory) == 2
    assert json.loads(capsys.readouterr().err)["classification"] == (
        "employment_live_opt_in_required"
    )


def test_employment_cannot_accidentally_reuse_cpi_or_ppi_calendar(tmp_path):
    from market_data_foundation.historical.cpi_release_schedule import (
        CpiReleaseScheduler,
        CpiScheduleError,
        parse_official_history,
        parse_official_ics,
    )

    for parser in (parse_official_history, parse_official_ics):
        with pytest.raises(CpiScheduleError, match="employment_schedule_not_connected"):
            parser(b"", profile=EMPLOYMENT_PROFILE)
    with pytest.raises(CpiScheduleError, match="employment_schedule_not_connected"):
        CpiReleaseScheduler(
            None, tmp_path / "schedule.json", profile=EMPLOYMENT_PROFILE
        )
    assert list(tmp_path.iterdir()) == []
