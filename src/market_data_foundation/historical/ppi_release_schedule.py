"""PPI final-demand calendar binding over the shared release scheduler.

Monthly collection reconciles configured FD-ID history after release capture.
There is no PPI errata discovery contract; explicit signal imports are rejected.
"""

from market_data_foundation.historical.bls_timeseries_profile import PPI_PROFILE
from market_data_foundation.historical.cpi_release_schedule import (
    ICS_URL,
    CpiReleaseScheduler,
    CpiScheduleError,
    _fetch_calendar,
    parse_official_calendar,
)
from market_data_foundation.historical.cpi_release_schedule import (
    parse_official_ics as _parse_official_ics,
)

CALENDAR_URL = "https://www.bls.gov/schedule/news_release/ppi.htm"
PpiScheduleError = CpiScheduleError


def parse_official_ics(raw, *, known_uids=()):
    """Select only Producer Price Index / Producer Price Indexes events."""
    return _parse_official_ics(raw, profile=PPI_PROFILE, known_uids=known_uids)


def fetch_official_calendar():
    return _fetch_calendar(ICS_URL, "text/calendar", title="PPI")


def fetch_reference_calendar():
    return _fetch_calendar(CALENDAR_URL, "text/html", title="PPI")


class PpiReleaseScheduler(CpiReleaseScheduler):
    def __init__(self, service, state_path, **options):
        if "calendar_fetcher" not in options:
            options.setdefault("fallback_fetcher", fetch_reference_calendar)
        options.setdefault("calendar_fetcher", fetch_official_calendar)
        options.setdefault("reference_fetcher", fetch_reference_calendar)
        super().__init__(service, state_path, profile=PPI_PROFILE, **options)


__all__ = [
    "CALENDAR_URL",
    "ICS_URL",
    "PpiReleaseScheduler",
    "PpiScheduleError",
    "fetch_official_calendar",
    "fetch_reference_calendar",
    "parse_official_calendar",
    "parse_official_ics",
]
