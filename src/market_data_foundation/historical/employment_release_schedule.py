"""Employment Situation official reference-month calendar adapter."""

from market_data_foundation.historical.bls_timeseries_profile import EMPLOYMENT_PROFILE
from market_data_foundation.historical.cpi_release_schedule import (
    ICS_URL,
    CpiReleaseScheduler,
    parse_official_calendar,
)
from market_data_foundation.historical.cpi_release_schedule import (
    parse_official_history as _parse_history,
)
from market_data_foundation.historical.cpi_release_schedule import (
    parse_official_ics as _parse_ics,
)

CALENDAR_URL = "https://www.bls.gov/schedule/news_release/empSit.htm"


def parse_official_ics(raw, *, known_uids=()):
    return _parse_ics(raw, profile=EMPLOYMENT_PROFILE, known_uids=known_uids)


def parse_official_history(raw):
    return _parse_history(raw, profile=EMPLOYMENT_PROFILE)


class EmploymentReleaseScheduler(CpiReleaseScheduler):
    def __init__(self, service, state_path, **options):
        super().__init__(service, state_path, profile=EMPLOYMENT_PROFILE, **options)


__all__ = [
    "CALENDAR_URL",
    "ICS_URL",
    "EmploymentReleaseScheduler",
    "parse_official_calendar",
    "parse_official_history",
    "parse_official_ics",
]
