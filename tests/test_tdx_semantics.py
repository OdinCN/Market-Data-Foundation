"""Synthetic semantic counterexamples; no SDK installation or network required."""

from datetime import date

import pytest

from market_data_foundation.qualification.tdx_semantics import (
    cash_dividend_per_share,
    daily_rows,
)


def sample(day=date(2024, 1, 2)):
    return dict(
        year=day.year,
        month=day.month,
        day=day.day,
        open=10,
        high=11,
        low=9,
        close=10.5,
        vol=100,
        amount=105000,
    )


def test_equivalent_stock_units_keep_quantization_visible():
    lots = sample()
    shares = dict(lots, vol=10000)
    milli = dict(
        time="2024-01-02T15:00:00",
        open=10000,
        high=11000,
        low=9000,
        close=10500,
        volume=100,
        amount=105000000,
    )
    expected = daily_rows("pytdxdata", [shares], kind="stock")
    assert daily_rows("mootdx-plus", [lots], kind="stock") == expected
    assert daily_rows("tdx-python", [milli], kind="stock") == expected
    assert daily_rows("tdx-python", [milli], kind="index")[0]["volume"] == 100


@pytest.mark.parametrize(
    "corruption", [dict(high=10), dict(vol=-1), dict(amount=float("nan"))]
)
def test_impossible_daily_evidence_is_rejected(corruption):
    with pytest.raises(ValueError):
        daily_rows("pytdxdata", [dict(sample(), **corruption)], kind="stock")


def test_page_overlap_and_reversed_order_are_not_silently_repaired():
    a, b = sample(), sample(date(2024, 1, 3))
    with pytest.raises(ValueError, match="duplicate"):
        daily_rows("pytdxdata", [a, a], kind="stock")
    with pytest.raises(ValueError, match="ascending"):
        daily_rows("pytdxdata", [b, a], kind="stock")
    with pytest.raises(ValueError, match="explicit"):
        daily_rows("unknown", [a], kind="stock")


def test_dividend_scaling_does_not_treat_capital_change_as_cash():
    assert cash_dividend_per_share(
        "tdx-python", dict(category=1, c1=4.2)
    ) == pytest.approx(0.42)
    assert cash_dividend_per_share(
        "mootdx-plus", dict(category=1, fenhong=4.2)
    ) == pytest.approx(0.42)
    assert cash_dividend_per_share(
        "pytdxdata", dict(category=1, fenhong=0.42)
    ) == pytest.approx(0.42)
    with pytest.raises(ValueError, match="category 1"):
        cash_dividend_per_share("tdx-python", dict(category=5, c1=320000000))
    with pytest.raises(ValueError, match="unknown"):
        cash_dividend_per_share("unknown", dict(category=1, c1=4.2))
