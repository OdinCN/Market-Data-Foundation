"""Immutable, allowlisted namespaces for the shared monthly index core."""

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
    provider: str = "BLS"
    explicit_series_metadata: tuple = ()

    @property
    def series_metadata(self):
        if self.explicit_series_metadata:
            return {
                series: dict(values) for series, values in self.explicit_series_metadata
            }
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
PCE_PROFILE = BlsTimeSeriesProfile(
    key="pce",
    label="PCE price indexes; core excludes food and energy; seasonally adjusted",
    series_ids=("DPCERG", "DPCCRG"),
    schema="mdf_pce_timeseries_dev_r1",
    raw_prefix="pce-timeseries/dev-r1/sha256/",
    earliest_month=None,
    lock_id=78328741642028103,
    rates_view="pce_timeseries_rates",
    provider="BEA",
    explicit_series_metadata=(
        (
            "DPCERG",
            (
                ("indicator", "headline"),
                ("seasonal_adjustment", "SA"),
                ("provider", "BEA"),
                ("table_name", "T20804"),
                ("line_number", "1"),
                (
                    "reference_index_name",
                    "Personal consumption expenditures price index",
                ),
            ),
        ),
        (
            "DPCCRG",
            (
                ("indicator", "core"),
                ("seasonal_adjustment", "SA"),
                ("provider", "BEA"),
                ("table_name", "T20804"),
                ("line_number", "25"),
                ("reference_index_name", "PCE excluding food and energy price index"),
            ),
        ),
    ),
)


EMPLOYMENT_PROFILE = BlsTimeSeriesProfile(
    key="employment",
    label="Employment Situation; seasonally adjusted CES and CPS base series",
    series_ids=("CES0000000001", "LNS14000000", "CES0500000003"),
    schema="mdf_employment_timeseries_dev_r1",
    raw_prefix="employment-timeseries/dev-r1/sha256/",
    earliest_month=None,
    lock_id=78328741642028104,
    rates_view="employment_timeseries_rates",
    explicit_series_metadata=(
        (
            "CES0000000001",
            (
                ("indicator", "total_nonfarm"),
                ("seasonal_adjustment", "SA"),
                ("survey", "CES"),
                ("value_unit", "thousand_jobs"),
            ),
        ),
        (
            "LNS14000000",
            (
                ("indicator", "unemployment_rate"),
                ("seasonal_adjustment", "SA"),
                ("survey", "CPS"),
                ("value_unit", "percent"),
            ),
        ),
        (
            "CES0500000003",
            (
                ("indicator", "average_hourly_earnings"),
                ("seasonal_adjustment", "SA"),
                ("survey", "CES"),
                ("value_unit", "USD/hour"),
            ),
        ),
    ),
)


def validate_profile(profile):
    """Only builtins may supply SQL identifiers, series and raw namespaces."""
    if all(
        profile is not builtin
        for builtin in (CPI_PROFILE, PPI_PROFILE, PCE_PROFILE, EMPLOYMENT_PROFILE)
    ):
        raise ValueError("bls_timeseries_profile_invalid")
    return profile
