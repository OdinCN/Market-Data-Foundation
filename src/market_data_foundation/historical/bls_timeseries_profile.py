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
        raise ValueError("series_metadata_required")

    @property
    def baskets(self):
        return tuple(
            dict.fromkeys(m["indicator"] for m in self.series_metadata.values())
        )

    @property
    def default_query_series_ids(self):
        # Preserve the original unfiltered employment query; collection uses all.
        return (
            EMPLOYMENT_DEFAULT_SERIES if self.key == "employment" else self.series_ids
        )


def _price_metadata(sa, nsa, indicator, *, qualified=False):
    common = {
        "indicator": indicator,
        "value_unit": "index",
        "index_base": None,
        "start_month": None,
        "coverage_basis": "unknown",
    }
    if qualified:
        common.update(
            evidence_start_month="2026-01",
            evidence_end_month="2026-09",
            coverage_basis="retained_bls_2026_qualification_not_series_start",
        )
    return tuple(
        (sid, tuple({**common, "seasonal_adjustment": adjustment}.items()))
        for sid, adjustment in ((sa, "SA"), (nsa, "NSA"))
    )


_CPI_METADATA = (
    *_price_metadata("CUSR0000SA0", "CUUR0000SA0", "headline"),
    *_price_metadata("CUSR0000SA0L1E", "CUUR0000SA0L1E", "core"),
    *_price_metadata("CUSR0000SAH1", "CUUR0000SAH1", "shelter", qualified=True),
    *_price_metadata("CUSR0000SAF1", "CUUR0000SAF1", "food", qualified=True),
    *_price_metadata("CUSR0000SA0E", "CUUR0000SA0E", "energy", qualified=True),
    *_price_metadata("CUSR0000SACL1E", "CUUR0000SACL1E", "core_goods", qualified=True),
    *_price_metadata(
        "CUSR0000SASLE", "CUUR0000SASLE", "services_less_energy", qualified=True
    ),
)
# Table 1 of the retained 2026-10-14 CPI release explicitly states this
# base for the selected NSA rows, without alternative-base row footnotes.
_CPI_METADATA = tuple(
    (
        sid,
        tuple(
            {
                **dict(values),
                **(
                    {
                        "index_base": "1982-84=100",
                        "index_base_basis": (
                            "BLS CPI 2026-10-14 Table 1 header and "
                            "selected row footnotes"
                        ),
                        "index_base_source_url": (
                            "https://www.bls.gov/news.release/archives/cpi_10142026.htm"
                        ),
                        "index_base_table": "cpipress1",
                        "index_base_material_sha256": (
                            "09944c5cfc801e2c7868d0a98760b3334b82b9d099550aa318468be6d1e89c4f"
                        ),
                    }
                    if sid.startswith("CUUR")
                    else {}
                ),
            }.items()
        ),
    )
    for sid, values in _CPI_METADATA
)
_PPI_METADATA = (
    *_price_metadata("WPSFD4", "WPUFD4", "headline"),
    *_price_metadata("WPSFD49104", "WPUFD49104", "core"),
    *_price_metadata("WPSFD49116", "WPUFD49116", "core_ex_trade", qualified=True),
)

EMPLOYMENT_DEFAULT_SERIES = (
    "CES0000000001",
    "LNS14000000",
    "CES0500000003",
    "LNS11300000",
    "CES0500000002",
)

EMPLOYMENT_RESEARCH_SERIES = (
    ("CES0500000001", "total_private", "aggregate"),
    ("LNS12300000", "employment_population_ratio", "rate"),
    ("CES1000000001", "mining_logging", "major_sector"),
    ("CES2000000001", "construction", "major_sector"),
    ("CES3000000001", "manufacturing", "major_sector"),
    ("CES4000000001", "trade_transportation_utilities", "major_sector"),
    ("CES5000000001", "information", "major_sector"),
    ("CES5500000001", "financial_activities", "major_sector"),
    ("CES6000000001", "professional_business_services", "major_sector"),
    ("CES6500000001", "private_education_health", "major_sector"),
    ("CES7000000001", "leisure_hospitality", "major_sector"),
    ("CES8000000001", "other_services", "major_sector"),
    ("CES9000000001", "government", "major_sector"),
)


CPI_PROFILE = BlsTimeSeriesProfile(
    key="cpi",
    label="CPI",
    series_ids=tuple(sid for sid, _ in _CPI_METADATA),
    explicit_series_metadata=_CPI_METADATA,
    schema="mdf_cpi_timeseries_dev_r1",
    raw_prefix="cpi-timeseries/dev-r1/sha256/",
    earliest_month=None,
    lock_id=78328741642028101,
    rates_view="cpi_timeseries_rates",
)
PPI_PROFILE = BlsTimeSeriesProfile(
    key="ppi",
    label="PPI final demand; core excludes foods and energy",
    series_ids=tuple(sid for sid, _ in _PPI_METADATA),
    explicit_series_metadata=_PPI_METADATA,
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
    series_ids=(
        "CES0000000001",
        "LNS14000000",
        "CES0500000003",
        "LNS11300000",
        "CES0500000002",
        *(sid for sid, _, _ in EMPLOYMENT_RESEARCH_SERIES),
    ),
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
        (
            "LNS11300000",
            (
                ("indicator", "labor_force_participation_rate"),
                ("seasonal_adjustment", "SA"),
                ("survey", "CPS"),
                ("value_unit", "percent"),
                ("fred_alias", "CIVPART"),
            ),
        ),
        (
            "CES0500000002",
            (
                ("indicator", "average_weekly_hours"),
                ("seasonal_adjustment", "SA"),
                ("survey", "CES"),
                ("value_unit", "hours"),
                ("fred_alias", "AWHAETP"),
            ),
        ),
        *(
            (
                sid,
                tuple(
                    {
                        "indicator": indicator,
                        "seasonal_adjustment": "SA",
                        "survey": "CPS" if sid == "LNS12300000" else "CES",
                        "value_unit": "percent"
                        if sid == "LNS12300000"
                        else "thousand_jobs",
                        "aggregation_role": role,
                        "start_month": None,
                        "coverage_basis": (
                            "retained_bls_2026_qualification_not_series_start"
                        ),
                        "evidence_start_month": "2026-01",
                        "evidence_end_month": "2026-09",
                    }.items()
                ),
            )
            for sid, indicator, role in EMPLOYMENT_RESEARCH_SERIES
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
