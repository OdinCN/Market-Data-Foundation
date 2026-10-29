"""Immutable, allowlisted namespaces for the shared BLS time-series core."""

from dataclasses import dataclass


@dataclass(frozen=True)
class BlsTimeSeriesProfile:
    key: str
    label: str
    series_ids: tuple[str, ...]
    schema: str
    raw_prefix: str
    earliest_month: str | None
    lock_id: int
    rates_view: str

    @property
    def series_metadata(self):
        return {
            series: {
                "indicator": "headline" if position < 2 else "core",
                "seasonal_adjustment": "SA" if position % 2 == 0 else "NSA",
            }
            for position, series in enumerate(self.series_ids)
        }


CPI_PROFILE = BlsTimeSeriesProfile(
    key="cpi",
    label="CPI",
    series_ids=("CUSR0000SA0", "CUUR0000SA0", "CUSR0000SA0L1E", "CUUR0000SA0L1E"),
    schema="mdf_cpi_timeseries_dev_r1",
    raw_prefix="cpi-timeseries/dev-r1/sha256/",
    earliest_month=None,
    lock_id=78328741642028101,
    rates_view="cpi_timeseries_rates",
)
PPI_PROFILE = BlsTimeSeriesProfile(
    key="ppi",
    label="PPI final demand; core excludes foods and energy",
    series_ids=("WPSFD4", "WPUFD4", "WPSFD49104", "WPUFD49104"),
    schema="mdf_ppi_timeseries_dev_r1",
    raw_prefix="ppi-timeseries/dev-r1/sha256/",
    earliest_month=None,
    lock_id=78328741642028102,
    rates_view="ppi_timeseries_rates",
)


def validate_profile(profile):
    """Only builtins may supply SQL identifiers, series and raw namespaces."""
    if profile is not CPI_PROFILE and profile is not PPI_PROFILE:
        raise ValueError("bls_timeseries_profile_invalid")
    return profile
