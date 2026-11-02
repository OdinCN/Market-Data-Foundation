"""BEA GDP quarterly identities; official statistics never inferred from levels."""

import re
from dataclasses import dataclass
from datetime import date

from market_data_foundation.historical.cpi_timeseries_source import CpiSourceError

GdpSourceError = CpiSourceError
ASSET_ID = "mdf.macro.bea.gdp-growth"
ASSET_REVISION = "r1"


@dataclass(frozen=True)
class GdpMetric:
    series_id: str
    indicator: str
    name: str
    table: str
    line: str
    metric_name: str
    cl_unit: str
    unit_mult: str
    value_unit: str
    statistic: str
    chained_base: str | None = None
    frequency: str = "Q"
    seasonal_adjustment: str = "SA"
    origin: str = "official"
    native_start: str | None = None


METRICS = (
    GdpMetric(
        "A191RX",
        "real_gdp",
        "实际GDP",
        "T10106",
        "1",
        "Chained Dollars",
        "Level",
        "6",
        "million_chained_2017_USD",
        "SAAR_level",
        "2017",
    ),
    GdpMetric(
        "LB000003",
        "real_private_final_demand",
        "实际私人国内最终需求",
        "T10406",
        "8",
        "Chained Dollars",
        "Level",
        "6",
        "million_chained_2017_USD",
        "SAAR_level",
        "2017",
    ),
    GdpMetric(
        "A191RL",
        "gdp_qoq_annualized",
        "实际GDP官方年化季环比",
        "T10101",
        "1",
        "Fisher Quantity Index",
        "Percent change, annual rate",
        "0",
        "percent",
        "official_annualized_qoq",
    ),
    GdpMetric(
        "DPCERY",
        "consumption_contribution",
        "消费对GDP增长的贡献",
        "T10102",
        "2",
        "Quantity Contributions",
        "Level",
        "0",
        "percentage_points",
        "contribution_to_annualized_gdp_growth",
    ),
    GdpMetric(
        "A007RY",
        "fixed_investment_contribution",
        "固定投资对GDP增长的贡献",
        "T10102",
        "8",
        "Quantity Contributions",
        "Level",
        "0",
        "percentage_points",
        "contribution_to_annualized_gdp_growth",
    ),
    GdpMetric(
        "A014RY",
        "inventories_contribution",
        "库存对GDP增长的贡献",
        "T10102",
        "14",
        "Quantity Contributions",
        "Level",
        "0",
        "percentage_points",
        "contribution_to_annualized_gdp_growth",
    ),
    GdpMetric(
        "A822RY",
        "government_contribution",
        "政府对GDP增长的贡献",
        "T10102",
        "22",
        "Quantity Contributions",
        "Level",
        "0",
        "percentage_points",
        "contribution_to_annualized_gdp_growth",
    ),
    GdpMetric(
        "A019RY",
        "net_exports_contribution",
        "净出口对GDP增长的贡献",
        "T10102",
        "15",
        "Quantity Contributions",
        "Level",
        "0",
        "percentage_points",
        "contribution_to_annualized_gdp_growth",
    ),
)
SERIES_IDS = tuple(m.series_id for m in METRICS)
TABLES = ("T10106", "T10406", "T10101", "T10102")
_BY_ID = {m.series_id: m for m in METRICS}
_QUARTER = re.compile(r"(?:19[0-9]{2}|[2-9][0-9]{3})-Q[1-4]")


def validate_quarter(value: str) -> str:
    if type(value) is not str or not _QUARTER.fullmatch(value):
        raise GdpSourceError("gdp_timeseries_quarter_invalid")
    return value


def source_quarter(value: str) -> str:
    if type(value) is not str or not re.fullmatch(r"[0-9]{4}Q[1-4]", value):
        raise GdpSourceError("gdp_timeseries_source_quarter_invalid")
    return validate_quarter(value[:4] + "-" + value[4:])


def quarter_start(value: str) -> date:
    validate_quarter(value)
    return date(int(value[:4]), (int(value[-1]) - 1) * 3 + 1, 1)


def shift_quarter(value: str, offset: int) -> str:
    validate_quarter(value)
    if type(offset) is not int:
        raise GdpSourceError("gdp_timeseries_quarter_offset_invalid")
    year, quarter = divmod(int(value[:4]) * 4 + int(value[-1]) - 1 + offset, 4)
    return validate_quarter(f"{year:04d}-Q{quarter + 1}")


def validate_range(start: str, end: str) -> None:
    validate_quarter(start)
    validate_quarter(end)
    if start > end:
        raise GdpSourceError("gdp_timeseries_range_invalid")


def validate_series(series_ids: tuple[str, ...]) -> tuple[str, ...]:
    if (
        type(series_ids) is not tuple
        or not series_ids
        or any(type(s) is not str or s not in _BY_ID for s in series_ids)
        or len(set(series_ids)) != len(series_ids)
    ):
        raise GdpSourceError("gdp_timeseries_series_invalid")
    return series_ids


def requested_tables(series_ids: tuple[str, ...]) -> tuple[str, ...]:
    validate_series(series_ids)
    return tuple(t for t in TABLES if any(_BY_ID[s].table == t for s in series_ids))


def year_windows(start: str, end: str, *, table_count: int, max_requests: int = 48):
    """Five-year windows; budget counts each table, before bounded wire retries."""
    validate_range(start, end)
    if (
        type(table_count) is not int
        or not 1 <= table_count <= len(TABLES)
        or type(max_requests) is not int
        or not 1 <= max_requests <= 48
    ):
        raise GdpSourceError("gdp_timeseries_request_budget_invalid")
    first, last = int(start[:4]), int(end[:4])
    windows = tuple((y, min(y + 4, last)) for y in range(first, last + 1, 5))
    if table_count * len(windows) > max_requests:
        raise GdpSourceError("gdp_timeseries_request_budget_exceeded")
    return windows
