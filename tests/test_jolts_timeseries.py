"""Synthetic JOLTS foundation regressions; no live service or private database."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    EMPLOYMENT_PROFILE,
    JOLTS_PROFILE,
    validate_profile,
)
from market_data_foundation.historical.cpi_request_control import CpiRequestControl
from market_data_foundation.historical.cpi_timeseries import month_range
from market_data_foundation.historical.cpi_timeseries_source import CpiSourceError
from market_data_foundation.historical.cpi_timeseries_store import (
    InMemoryTimeSeriesStore,
    _provider_calculations,
    _rates_view_sql,
    _relational_content,
    _revision_ddl,
    raw_locator,
)
from market_data_foundation.historical.jolts_contract import (
    JOLTS_DEFINITION,
    METRICS,
)
from market_data_foundation.historical.jolts_timeseries import (
    SERIES_IDS,
    BlsJoltsRangeSource,
    JoltsTimeSeriesError,
    JoltsTimeSeriesService,
    parse_native_metadata,
    parse_response,
)
from tests.test_cpi_timeseries_source import WindowTransport

AT = datetime(2026, 11, 3, tzinfo=UTC)
OPENINGS, OPENINGS_RATE, HIRES, HIRES_RATE, QUITS, QUITS_RATE, LAYOFFS, LAYOFFS_RATE = (
    SERIES_IDS
)


def response(series=SERIES_IDS, months=("2026-08",), overrides=None):
    overrides = overrides or {}
    return json.dumps(
        {
            "status": "REQUEST_SUCCEEDED",
            "message": ["Unable to get Catalog Data"],
            "Results": {
                "series": [
                    {
                        "seriesID": sid,
                        "data": [
                            {
                                "year": month[:4],
                                "period": "M" + month[-2:],
                                "value": overrides.get(
                                    (sid, month), "4.3" if sid.endswith("R") else "100"
                                ),
                                "footnotes": [{"code": "P", "text": "preliminary"}],
                                "calculations": {
                                    "net_changes": {"1": "-0.1"},
                                    "pct_changes": {"1": "-2.3"},
                                },
                            }
                            for month in months
                        ],
                    }
                    for sid in series
                ]
            },
        }
    ).encode()


class Source:
    profile = JOLTS_PROFILE

    def __init__(self):
        self.calls = []
        self.at = AT
        self.overrides = {}

    def fetch(self, series, start, end):
        from market_data_foundation.historical.cpi_timeseries_source import SourceBatch

        self.calls.append((series, start, end))
        raw = response(series, month_range(start, end), self.overrides)
        return (
            SourceBatch(
                parse_response(raw, series, start, end),
                raw,
                {"seriesid": list(series)},
                self.at,
                1,
            ),
        )


@pytest.fixture
def setup():
    source = Source()
    store = InMemoryTimeSeriesStore(profile=JOLTS_PROFILE, clock=lambda: source.at)
    return JoltsTimeSeriesService(store, source), store, source


def test_eight_exact_identities_and_candidate_metadata_are_isolated():
    assert SERIES_IDS == tuple(
        "JTS000000000000000" + suffix
        for suffix in ("JOL", "JOR", "HIL", "HIR", "QUL", "QUR", "LDL", "LDR")
    )
    assert not JOLTS_DEFINITION.source_qualified
    assert all(m.start_month is None for m in METRICS)
    metadata = JOLTS_PROFILE.series_metadata
    assert metadata[OPENINGS]["reference_basis"] == "last_business_day_stock"
    assert metadata[HIRES]["reference_basis"] == "during_month_flow"
    assert metadata[OPENINGS_RATE]["rate_denominator"] == (
        "employment_plus_job_openings"
    )
    assert metadata[HIRES_RATE]["rate_denominator"] == "employment"
    assert metadata[OPENINGS]["value_unit"] == "thousand_jobs"
    assert metadata[HIRES]["value_unit"] == "thousand_persons"
    assert JOLTS_PROFILE.schema != EMPLOYMENT_PROFILE.schema
    assert "jolts-timeseries/" in raw_locator("0" * 64, profile=JOLTS_PROFILE)
    with pytest.raises(ValueError, match="profile_invalid"):
        validate_profile(replace(JOLTS_PROFILE))


def test_official_rates_are_independent_of_levels_and_growth(setup):
    service, _, source = setup
    source.overrides.update(
        {
            (OPENINGS, "2026-08"): "120.123456789",
            (OPENINGS_RATE, "2026-07"): "4.2",
            (HIRES, "2026-08"): "0",
        }
    )
    result = service.query_range("2026-08", "2026-08", allow_upstream=True)
    points = {p["series_id"]: p for p in result["points"]}
    assert points[OPENINGS]["value"] == "120.123456789"
    assert points[OPENINGS]["change_1m_level"] == "20.123456789"
    assert points[OPENINGS]["change_1m_ratio"] == "0.2012"
    assert points[OPENINGS_RATE]["value"] == "4.3"
    assert points[OPENINGS_RATE]["change_1m_percentage_points"] == "0.1"
    assert "change_1m_ratio" not in points[OPENINGS_RATE]
    assert points[HIRES]["change_1m_level"] == "-100"
    assert points[HIRES]["change_1m_ratio"] == "-1.0000"
    assert points[OPENINGS]["provider_status"]["preliminary"] is True
    assert result["source_qualified"] is False
    assert result["publication_event"] is None and result["source_version"] is None
    assert result["surprise"] is None and not result["official_vintage_supported"]


def test_query_defaults_local_and_temporary_fill_does_not_persist(setup):
    service, store, source = setup
    assert not service.query_range("2026-08", "2026-08")["complete"]
    assert source.calls == []
    filled = service.query_range("2026-08", "2026-08", allow_upstream=True)
    assert filled["complete"] and filled["persisted"] is False
    assert filled["provider_requests"] == filled["provider_attempts"] == 1
    assert store.status()["observation_count"] == 0
    assert all(p["available_at"] is None for p in filled["points"])
    assert source.calls[0] == (SERIES_IDS, "2025-08", "2026-08")


def test_readback_changed_only_and_same_available_cutoff(setup):
    service, store, source = setup
    first = service.init_range("2026-07", "2026-08")
    cutoff = source.at
    source.at += timedelta(minutes=1)
    unchanged = service.update_range("2026-07", "2026-08")
    assert unchanged["saved_revisions"] == 0
    source.at += timedelta(minutes=1)
    source.overrides[OPENINGS_RATE, "2026-07"] = "4.1"
    source.overrides[OPENINGS, "2026-07"] = "80"
    changed = service.update_range("2026-07", "2026-08")
    assert changed["saved_revisions"] == 2
    calls = len(source.calls)
    historical = service.query_range(
        "2026-08", "2026-08", as_of=cutoff, allow_upstream=True
    )
    current = service.query_range("2026-08", "2026-08")
    assert len(source.calls) == calls
    old = {p["series_id"]: p for p in historical["points"]}
    new = {p["series_id"]: p for p in current["points"]}
    assert old[OPENINGS]["change_1m_level"] == "0"
    assert new[OPENINGS]["change_1m_level"] == "20"
    assert old[OPENINGS_RATE]["change_1m_percentage_points"] == "0.0"
    assert new[OPENINGS_RATE]["change_1m_percentage_points"] == "0.2"
    assert historical["as_of_basis"] == "available"
    assert store.status()["revision_count"] == 2
    assert first["points"][0]["revision"] == 1
    assert "not official first release" in first["calculation_method"]


def test_null_gaps_zero_denominator_and_unknown_provider_status(setup):
    service, _, source = setup
    source.overrides[OPENINGS, "2026-07"] = "0"
    source.overrides[HIRES, "2026-08"] = "-"
    result = service.query_range("2026-08", "2026-08", allow_upstream=True)
    points = {p["series_id"]: p for p in result["points"]}
    assert points[OPENINGS]["change_1m_level"] == "100"
    assert points[OPENINGS]["change_1m_ratio"] is None
    assert any(
        item["reason"] == "zero_denominator" and item["metric"] == "change_1m_ratio"
        for item in result["calculation_missing"]
    )
    assert not result["complete"] and result["coverage_complete"]
    assert result["source_unavailable"][0]["value"] is None
    raw = json.loads(response())
    raw["Results"]["series"][0]["data"][0]["footnotes"] = [{}]
    point = parse_response(json.dumps(raw).encode(), SERIES_IDS, "2026-08", "2026-08")
    assert point[0].footnotes
    from market_data_foundation.historical.cpi_timeseries import _provider_status

    status = _provider_status([{}])
    assert status["preliminary"] is None and status["revised"] is None


@pytest.mark.parametrize("value", ["0", "0.000", "-"])
def test_zero_and_missing_are_preserved_for_all_eight_series(value):
    raw = response(overrides={(sid, "2026-08"): value for sid in SERIES_IDS})
    parsed = parse_response(raw, SERIES_IDS, "2026-08", "2026-08")
    assert len(parsed) == 8
    assert all(p.index_value == (None if value == "-" else Decimal(0)) for p in parsed)
    for point in parsed:
        values = _relational_content(point, profile=JOLTS_PROFILE)
        assert _provider_calculations(
            values[1:5], values[5], profile=JOLTS_PROFILE
        ) == (point.provider_calculations)


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity"])
def test_invalid_values_rejected(value):
    with pytest.raises(CpiSourceError, match="index_invalid"):
        parse_response(
            response(overrides={(OPENINGS, "2026-08"): value}),
            SERIES_IDS,
            "2026-08",
            "2026-08",
        )


def test_provider_identity_and_official_vintage_fail_closed(setup):
    service, _, source = setup
    with pytest.raises(CpiSourceError, match="series_invalid"):
        parse_response(response(), ("JTSJOL",), "2026-08", "2026-08")
    with pytest.raises(JoltsTimeSeriesError, match="official_vintage_unsupported"):
        service.query_range("2026-08", "2026-08", as_of_basis="official")
    with pytest.raises(JoltsTimeSeriesError, match="profile_mismatch"):
        JoltsTimeSeriesService(InMemoryTimeSeriesStore(), source)


def test_existing_bls_windows_merge_eight_series_and_compare_across_boundary(tmp_path):
    transport = WindowTransport()
    source = BlsJoltsRangeSource(
        transport=transport,
        allow_live=True,
        credential_loader=lambda: "a" * 32,
        clock=lambda: AT,
        request_control=CpiRequestControl(
            tmp_path / "bls-control.json", clock=lambda: AT, min_interval_seconds=0
        ),
    )
    store = InMemoryTimeSeriesStore(profile=JOLTS_PROFILE, clock=lambda: AT)
    result = JoltsTimeSeriesService(store, source).query_range(
        "2006-01", "2026-08", allow_upstream=True
    )
    assert len(transport.requests) == 2
    assert all(r["seriesid"] == list(SERIES_IDS) for r in transport.requests)
    point = next(
        p
        for p in result["points"]
        if p["series_id"] == OPENINGS and p["month"] == "2025-01"
    )
    assert point["calculation_inputs"]["change_1m_ratio"]["denominator_month"] == (
        "2024-12"
    )
    assert point["change_1m_ratio"] is not None


def test_live_opt_in_precedes_credentials():
    def forbidden():
        pytest.fail("must reject before reading credentials")

    source = BlsJoltsRangeSource(credential_loader=forbidden)
    with pytest.raises(JoltsTimeSeriesError, match="live_opt_in_required"):
        source.fetch(SERIES_IDS, "2026-08", "2026-08")


def test_schema_plan_preserves_zero_and_unit_specific_projection_without_execution():
    ddl = _revision_ddl(profile=JOLTS_PROFILE)
    sql = _rates_view_sql(profile=JOLTS_PROFILE)
    assert "index_value >= 0" in ddl
    assert "change_1m_level" in sql and "change_12m_percentage_points" in sql
    assert "AS value_unit" in sql and "AS value" in sql
    assert "mom_pct" not in sql and "CES0000000001" not in sql
    assert CPI_PROFILE.schema not in ddl + sql
    assert "index_value > 0" in _revision_ddl()


def catalog():
    """Synthetic catalog structure, with deliberately distinct values from live."""
    rows = (
        ("Series Id:", OPENINGS),
        ("Industry:", "Total nonfarm"),
        ("State/Region:", "Total US"),
        ("Area:", "All areas"),
        ("Data Element:", "Job openings"),
        ("Size Class:", "All size classes"),
        ("Rate/Level:", "Level - In Thousands"),
    )
    return (
        '<table id="catalog0"><tr><td colspan="2">Seasonally adjusted</td></tr>'
        + "".join(f"<tr><th>{key}</th><td>{value}</td></tr>" for key, value in rows)
        + "</table>"
    ).encode()


def test_native_metadata_reuses_material_parser_and_keeps_proof_scope():
    result = parse_native_metadata(catalog(), (OPENINGS,))
    assert result[OPENINGS]["Industry:"] == "Total nonfarm"
    assert result[OPENINGS]["Rate/Level:"] == "Level - In Thousands"
    assert "start_month" not in result[OPENINGS]
    assert "rate_denominator" not in result[OPENINGS]


@pytest.mark.parametrize(
    "old,new",
    [
        (b"Total nonfarm", b"Total private"),
        (b"Total US", b"California"),
        (b"Seasonally adjusted", b"Not seasonally adjusted"),
        (b"Job openings", b"Hires"),
        (b"Level - In Thousands", b"Rate"),
        (OPENINGS.encode(), b"JTSJOL"),
    ],
)
def test_native_metadata_rejects_scope_or_unit_mismatch(old, new):
    with pytest.raises(JoltsTimeSeriesError, match="jolts_metadata_"):
        parse_native_metadata(catalog().replace(old, new), (OPENINGS,))


def test_native_metadata_rejects_partial_and_duplicate_catalogs():
    with pytest.raises(JoltsTimeSeriesError, match="series_missing"):
        parse_native_metadata(catalog())
    with pytest.raises(JoltsTimeSeriesError, match="duplicate_field"):
        parse_native_metadata(catalog() + catalog(), (OPENINGS,))
    with pytest.raises(JoltsTimeSeriesError, match="identity_invalid"):
        parse_native_metadata(
            catalog() + catalog().replace(b"catalog0", b"catalog1"), (OPENINGS,)
        )
