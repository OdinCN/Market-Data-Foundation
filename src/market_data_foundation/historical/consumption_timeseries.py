"""Consumption/income service using the common version, cutoff and job ports."""

from market_data_foundation.historical.bls_timeseries_profile import CONSUMPTION_PROFILE
from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesService
from market_data_foundation.historical.cpi_timeseries import (
    build_service as shared_build,
)


class ConsumptionTimeSeriesService(CpiTimeSeriesService):
    def __init__(self, store, source):
        super().__init__(store, source, profile=CONSUMPTION_PROFILE)


def build_service(**options):
    options.setdefault("profile", CONSUMPTION_PROFILE)
    return shared_build(**options)


def rates_view_sql():
    p = CONSUMPTION_PROFILE
    rates = ",".join(
        f"CASE WHEN c.series_id!='A072RC' THEN "
        f"round(c.value/nullif(p{k}.value,0)-1,4) END AS change_{k}m_ratio"
        for k in (1, 3, 6, 12)
    )
    percents = ",".join(
        f"CASE WHEN c.series_id!='A072RC' THEN "
        f"round((c.value/nullif(p{k}.value,0)-1)*100,2) END AS {name}_pct"
        for k, name in ((1, "mom"), (12, "yoy"))
    )
    differences = ",".join(
        f"CASE WHEN c.series_id='A072RC' THEN c.value-p{k}.value "
        f"END AS change_{k}m_percentage_points"
        for k in (1, 12)
    )
    joins = " ".join(
        f"LEFT JOIN {p.schema}.latest p{k} ON p{k}.series_id=c.series_id "
        f"AND p{k}.month=(c.month-interval '{k} months')::date "
        f"AND p{k}.version_status='active'"
        for k in (1, 3, 6, 12)
    )
    return (
        f"CREATE OR REPLACE VIEW {p.schema}.{p.rates_view} AS SELECT c.*,"
        f"to_char(c.month,'YYYY-MM') AS month_label,{rates},{percents},{differences} "
        f"FROM {p.schema}.latest c {joins} WHERE c.version_status='active'"
    )
