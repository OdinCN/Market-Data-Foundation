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

VINTAGES = ("2026-09-30", "2026-08-26")


def encoded(value):
    return json.dumps(value).encode()


COLUMNS = ("GDP_20260930", "GDP_20260826")


def rows():
    return [{"date": "2025-01-01", COLUMNS[0]: "123.4500", COLUMNS[1]: "."}]


def document(data=None):
    return {
        "realtime_start": VINTAGES[1],
        "realtime_end": VINTAGES[0],
        "observation_start": "2025-01-01",
        "observation_end": "2026-12-31",
        "units": "lin",
        "output_type": 2,
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": "asc",
        "observations": rows() if data is None else data,
    }


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
    result = parse_observations(encoded(document()), "GDP", VINTAGES)
    assert len(result) == 2
    assert result[0].value == Decimal("123.4500")
    assert result[0].raw_value == "123.4500"
    assert tuple(item.vintage for item in result) == VINTAGES
    assert all(type(item.vintage) is str for item in result)
    assert all(item.realtime_end == VINTAGES[0] for item in result)
    assert result[0].unavailable is False
    assert result[1].raw_value == "."
    assert result[1].value is None
    assert result[1].unavailable is True


@pytest.mark.parametrize("column", COLUMNS)
def test_missing_marker_in_each_vintage(column):
    data = rows()
    data[0].update(dict.fromkeys(COLUMNS, "2.5000"))
    data[0][column] = "."
    result = parse_observations(encoded(document(data)), "GDP", VINTAGES)
    for item, key in zip(result, COLUMNS, strict=True):
        assert item.unavailable is (key == column)
        assert item.raw_value == ("." if key == column else "2.5000")
        assert item.value == (None if key == column else Decimal("2.5000"))


@pytest.mark.parametrize("field", ["date", *COLUMNS])
def test_required_fields(field):
    data = rows()
    del data[0][field]
    with pytest.raises(ContractError):
        parse_observations(encoded(document(data)), "GDP", VINTAGES)


@pytest.mark.parametrize("field", list(document())[:-1])
@pytest.mark.parametrize("mode", ["missing", "mismatch"])
def test_top_metadata_required_and_exact(field, mode):
    doc = document()
    if mode == "missing":
        del doc[field]
    else:
        doc[field] = "wrong"
    with pytest.raises(ContractError):
        parse_observations(encoded(doc), "GDP", VINTAGES)


@pytest.mark.parametrize("value", [True, 2.0, "2", None])
def test_output_type_requires_integer(value):
    doc = document()
    doc["output_type"] = value
    with pytest.raises(ContractError):
        parse_observations(encoded(doc), "GDP", VINTAGES)


@pytest.mark.parametrize(
    "field,value",
    [
        ("count", 0),
        ("count", 2),
        ("count", True),
        ("count", 1.0),
        ("count", "1"),
        ("count", None),
        ("offset", 1),
        ("offset", False),
        ("offset", 0.0),
        ("offset", "0"),
        ("offset", None),
        ("limit", 0),
        ("limit", -1),
        ("limit", True),
        ("limit", 1.0),
        ("limit", "1"),
        ("limit", None),
    ],
)
def test_invalid_optional_pagination(field, value):
    doc = document()
    doc[field] = value
    with pytest.raises(ContractError):
        parse_observations(encoded(doc), "GDP", VINTAGES)


def test_valid_optional_pagination_and_multiple_rows():
    data = rows() + [{**rows()[0], "date": "2026-12-31"}]
    doc = {**document(data), "count": 2, "offset": 0, "limit": 100000}
    result = parse_observations(encoded(doc), "GDP", VINTAGES)
    assert len(result) == 4
    assert len({(item.series_id, item.vintage, item.date) for item in result}) == 4


@pytest.mark.parametrize("column", COLUMNS)
@pytest.mark.parametrize(
    "value", [True, 12, 1.2, None, "invalid", "NaN", "Infinity", "-Infinity", "", " "]
)
def test_strict_cell_values(column, value):
    data = rows()
    data[0][column] = value
    with pytest.raises(ContractError):
        parse_observations(encoded(document(data)), "GDP", VINTAGES)


@pytest.mark.parametrize(
    "day", ["2025-02-30", "2025-1-01", "2024-12-31", "2027-01-01", None]
)
def test_strict_dates(day):
    data = rows()
    data[0]["date"] = day
    with pytest.raises(ContractError):
        parse_observations(encoded(document(data)), "GDP", VINTAGES)


@pytest.mark.parametrize("wrong", ["GDP_20260828", "PAYEMS_20260826", "GDP_2026-08-26"])
def test_wrong_column_suffix_or_series(wrong):
    data = rows()
    data[0][wrong] = data[0].pop(COLUMNS[1])
    with pytest.raises(ContractError):
        parse_observations(encoded(document(data)), "GDP", VINTAGES)


def test_extra_column_rejected():
    data = rows()
    data[0]["value"] = "1"
    with pytest.raises(ContractError):
        parse_observations(encoded(document(data)), "GDP", VINTAGES)


@pytest.mark.parametrize(
    "data",
    [
        [],
        None,
        {},
        [None],
        rows() + rows(),
        [{**rows()[0], "date": "2025-04-01"}, *rows()],
        [
            {
                "date": "2025-01-01",
                "realtime_start": VINTAGES[0],
                "realtime_end": VINTAGES[0],
                "value": "1",
            }
        ],
    ],
)
def test_invalid_rows_or_date_order(data):
    doc = document()
    doc["observations"] = data
    with pytest.raises(ContractError):
        parse_observations(encoded(doc), "GDP", VINTAGES)


@pytest.mark.parametrize(
    "vintages", [VINTAGES[::-1], (VINTAGES[0],) * 2, ("bad", VINTAGES[1])]
)
def test_invalid_selection(vintages):
    with pytest.raises(ContractError):
        parse_observations(encoded(document()), "GDP", vintages)


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
