from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from market_data_foundation.historical.cpi_timeseries_source import (
    SourceBatch,
    SourcePoint,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    SERIES_IDS,
    InMemoryTimeSeriesStore,
    PostgresTimeSeriesStore,
    _relational_content,
    _row_document,
    digest,
    raw_locator,
)

NOW = datetime(2026, 10, 28, tzinfo=UTC)


def point(**kwargs):
    original = SourcePoint(
        SERIES_IDS[0],
        "2026-09",
        Decimal("336.298"),
        {
            "net_changes": {},
            "pct_changes": {"1": "0.6", "3": "1.1", "6": "1.8", "12": "3.7"},
        },
        ({},),
    )
    return replace(original, **kwargs)


def batch(p, at=NOW):
    return SourceBatch((p,), b"original", {"seriesid": [p.series_id]}, at, 1)


def test_relational_roundtrip_preserves_source_values_without_document():
    p = point()
    values = _relational_content(p)
    assert values == (
        Decimal("336.298"),
        Decimal("0.6"),
        Decimal("1.1"),
        Decimal("1.8"),
        Decimal("3.7"),
        None,
        [{}],
    )
    doc = _row_document(
        (p.series_id, date(2026, 9, 1), 1, NOW, "batch", *values, digest(b"original"))
    )
    assert doc["index_value"] == "336.298"
    assert doc["provider_calculations"] == p.provider_calculations
    assert doc["raw_locator"] == raw_locator(digest(b"original"))
    assert doc["month"] == "2026-09"
    assert doc["availability"] == "available"


@pytest.mark.parametrize(
    "unknown", [{"24": "7.8"}, {"1": None}, {"1": "-"}, {"1": {"flag": "unknown"}}]
)
def test_optional_provider_fields_are_preserved_not_silently_dropped(unknown):
    calculations = {
        "pct_changes": unknown,
        "net_changes": {"1": "1.20"},
        "extra": ["source-note"],
    }
    p = point(provider_calculations=calculations)
    values = _relational_content(p)
    doc = _row_document(
        (p.series_id, date(2026, 9, 1), 1, NOW, "batch", *values, "sha")
    )
    assert doc["provider_calculations"] == calculations
    assert p.provider_calculations == calculations


def test_numeric_scale_only_change_does_not_create_revision():
    store = InMemoryTimeSeriesStore()
    p = point(index_value=Decimal("100.0"))
    assert store.save_batches((batch(p),)) == {"saved_points": 1, "saved_revisions": 0}
    equivalent = point(
        index_value=Decimal("100.00"),
        provider_calculations={
            "pct_changes": {"1": "0.60", "3": "1.10", "6": "1.80", "12": "3.70"}
        },
    )
    assert store.save_batches((batch(equivalent, NOW + timedelta(minutes=1)),)) == {
        "saved_points": 0,
        "saved_revisions": 0,
    }


def test_footnote_change_is_preserved_and_missing_is_not_zero():
    store = InMemoryTimeSeriesStore()
    p = point(index_value=None, footnotes=({"code": "X", "text": "Unavailable"},))
    store.save_batches((batch(p),))
    changed = replace(p, footnotes=({"code": "X", "text": "Explanation updated"},))
    result = store.save_batches((batch(changed, NOW + timedelta(minutes=1)),))
    assert result == {"saved_points": 1, "saved_revisions": 1}
    old = store.read_range(SERIES_IDS, "2026-09", "2026-09", as_of=NOW)[0]
    assert old["index_value"] is None
    assert old["footnotes"] == [{"code": "X", "text": "Unavailable"}]


def test_postgres_reconstructs_api_from_typed_columns():
    p = point()
    row = (
        p.series_id,
        date(2026, 9, 1),
        1,
        NOW,
        "batch",
        *_relational_content(p),
        "sha",
    )

    class Connection:
        autocommit = True

        def execute(self, sql, params):
            assert "document" not in sql and "content_sha256" not in sql
            assert "JOIN mdf_cpi_timeseries_dev_r1.batches" in sql
            assert params == ([p.series_id], date(2026, 9, 1), date(2026, 9, 1))
            return self

        def fetchall(self):
            return [row]

    result = PostgresTimeSeriesStore(Connection(), None).read_range(
        [p.series_id], "2026-09", "2026-09"
    )
    assert result[0]["index_value"] == "336.298"
    assert result[0]["provider_calculations"]["pct_changes"]["12"] == "3.7"


def test_missing_observation_keeps_absent_provider_calculations_empty():
    p = point(index_value=None, provider_calculations={}, footnotes=({"code": "X"},))
    row = (
        p.series_id,
        date(2025, 10, 1),
        1,
        NOW,
        "batch",
        *_relational_content(p),
        "sha",
    )
    doc = _row_document(row)
    assert doc["index_value"] is None
    assert doc["availability"] == "source_unavailable"
    assert doc["provider_calculations"] == {}
    assert doc["footnotes"] == [{"code": "X"}]
