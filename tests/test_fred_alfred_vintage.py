"""Deterministic, IO-free tests of the bounded vintage semantic contract."""

import json
from decimal import Decimal

import pytest

from market_data_foundation.historical.fred_alfred_vintage import (
    ContractError,
    parse_discovery,
    parse_observations,
)
from market_data_foundation.historical.vintage_contract import METADATA, PROVIDER

VINTAGES = ("2026-09-30", "2026-08-28")


def encoded(value):
    return json.dumps(value).encode()


def rows():
    return [
        {
            "realtime_start": vintage,
            "realtime_end": "9999-12-31",
            "date": "2025-01-01",
            "value": value,
        }
        for vintage, value in zip(VINTAGES, ("123.4500", "."), strict=True)
    ]


def test_discovery_first_two_distinct_valid_dates():
    raw = encoded(
        {
            "vintage_dates": [
                VINTAGES[0],
                VINTAGES[0],
                VINTAGES[1],
                "2026-07-01",
            ]
        }
    )
    assert parse_discovery(raw) == VINTAGES


@pytest.mark.parametrize(
    "raw",
    [
        b"no json",
        b"[]",
        b"{}",
        b'{"vintage_dates":null}',
        encoded({"vintage_dates": [VINTAGES[0]]}),
    ],
)
def test_invalid_discovery(raw):
    with pytest.raises(ContractError):
        parse_discovery(raw)


def test_values_and_date_strings_preserved():
    result = parse_observations(encoded({"observations": rows()}), "GDP", VINTAGES)
    assert result[0].value == Decimal("123.4500")
    assert result[0].raw_value == "123.4500"
    assert result[0].vintage == VINTAGES[0]
    assert type(result[0].vintage) is str
    assert result[0].unavailable is False
    assert result[1].raw_value == "."
    assert result[1].value is None
    assert result[1].unavailable is True


@pytest.mark.parametrize("field", ["realtime_start", "realtime_end", "date", "value"])
def test_required_fields(field):
    data = rows()
    del data[0][field]
    with pytest.raises(ContractError):
        parse_observations(encoded({"observations": data}), "GDP", VINTAGES)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("realtime_start", "2026-07-01"),
        ("realtime_start", None),
        ("realtime_end", "bad"),
        ("realtime_end", "2020-01-01"),
        ("date", "2025-02-30"),
        ("date", "2024-12-31"),
        ("date", "2027-01-01"),
        ("value", 12),
        ("value", "invalid"),
        ("value", "NaN"),
        ("value", "Infinity"),
        ("value", ""),
    ],
)
def test_strict_fields(field, value):
    data = rows()
    data[0][field] = value
    with pytest.raises(ContractError):
        parse_observations(encoded({"observations": data}), "GDP", VINTAGES)


@pytest.mark.parametrize("data", [[], rows()[:1], rows() + rows()[:1], [None]])
def test_representation_and_duplicate_rejection(data):
    with pytest.raises(ContractError):
        parse_observations(encoded({"observations": data}), "GDP", VINTAGES)


def test_metadata():
    gdp, payems = METADATA["GDP"], METADATA["PAYEMS"]
    assert (
        gdp.frequency,
        gdp.units,
        gdp.seasonal_adjustment,
        gdp.underlying_source,
    ) == ("quarterly", "billions USD", "SAAR", "BEA via FRED")
    assert (
        payems.frequency,
        payems.units,
        payems.seasonal_adjustment,
        payems.underlying_source,
    ) == ("monthly", "thousands persons", "SA", "BLS via FRED")
    for metadata in METADATA.values():
        assert metadata.provider == PROVIDER
        assert metadata.first_party_release is False


@pytest.mark.parametrize(
    "values",
    [
        ["2026-02-30", *VINTAGES],
        ["2026-9-30", *VINTAGES],
        [None, *VINTAGES],
        [42, *VINTAGES],
        [*VINTAGES, "invalid"],
        [*VINTAGES, None],
        list(reversed(VINTAGES)),
        [*VINTAGES, "2026-10-01"],
        [*VINTAGES, VINTAGES[0]],
        [VINTAGES[0], VINTAGES[0]],
    ],
)
def test_discovery_fails_closed_for_invalid_or_unordered_dates(values):
    with pytest.raises(ContractError):
        parse_discovery(encoded({"vintage_dates": values}))
