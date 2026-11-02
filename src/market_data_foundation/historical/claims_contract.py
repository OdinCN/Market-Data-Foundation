"""Weekly national UI claims: DOL-ETA origin, explicit FRED transmission."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from market_data_foundation.historical.cpi_timeseries_source import CpiSourceError

ClaimsError = CpiSourceError
ASSET_ID = "mdf.macro.dol.claims"
ORIGINAL_AGENCY = "DOL-ETA"
PROVIDER = "FRED"


@dataclass(frozen=True)
class ClaimsMetric:
    series_id: str
    indicator: str
    seasonal_adjustment: str
    statistic: str = "weekly_level"
    unit: str = "Number"
    frequency: str = "weekly_ending_saturday"
    geography: str = "US"
    program: str = "regular_state_unemployment_insurance"


METRICS = (
    ClaimsMetric("ICSA", "initial_claims", "SA"),
    ClaimsMetric("ICNSA", "initial_claims", "NSA"),
    ClaimsMetric("CCSA", "continued_claims", "SA"),
    ClaimsMetric("CCNSA", "continued_claims", "NSA"),
    ClaimsMetric("IC4WSA", "initial_claims", "SA", "official_four_week_average"),
    ClaimsMetric("CC4WSA", "continued_claims", "SA", "official_four_week_average"),
)
SERIES_IDS = tuple(m.series_id for m in METRICS)
BY_ID = {m.series_id: m for m in METRICS}


def day(value):
    try:
        if type(value) is not str or len(value) != 10:
            raise ValueError
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError
        return result
    except ValueError, TypeError:
        raise ClaimsError("claims_date_invalid") from None


def instant(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ClaimsError("claims_instant_invalid")
    return value


def validate_series(series_ids):
    if (
        type(series_ids) is not tuple
        or not series_ids
        or any(type(s) is not str or s not in BY_ID for s in series_ids)
        or len(set(series_ids)) != len(series_ids)
    ):
        raise ClaimsError("claims_series_invalid")
    return series_ids


def weeks(start, end):
    """Inclusive calendar-date bounds, projected onto actual Saturday weeks."""
    first, last = day(start), day(end)
    if first > last or (last - first).days > 366 * 100:
        raise ClaimsError("claims_range_invalid")
    first += timedelta(days=(5 - first.weekday()) % 7)
    return tuple(
        (first + timedelta(weeks=i)).isoformat()
        for i in range(max(0, (last - first).days // 7 + 1))
    )


def buffer_start(start):
    return (day(start) - timedelta(weeks=4)).isoformat()


def twenty_year_range(end):
    """Calendar years, including leap-year clamping; never 240 weekly rows."""
    last = day(end)
    try:
        first = last.replace(year=last.year - 20)
    except ValueError:
        first = last.replace(year=last.year - 20, day=28)
    return first.isoformat(), last.isoformat()
