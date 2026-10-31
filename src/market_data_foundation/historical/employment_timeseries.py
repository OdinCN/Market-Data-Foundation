"""Continuous employment base series over the shared BLS/storage mechanisms.

No imports or construction perform requests, configuration writes or DDL.
Internal storage uses the shared numeric column; consumer values have units.
"""

from market_data_foundation.historical import cpi_timeseries as _core
from market_data_foundation.historical.bls_timeseries_profile import EMPLOYMENT_PROFILE
from market_data_foundation.historical.cpi_timeseries_source import (
    BlsRangeSource as _BlsRangeSource,
)
from market_data_foundation.historical.cpi_timeseries_source import (
    parse_response as _parse_response,
)

SERIES_IDS = EMPLOYMENT_PROFILE.series_ids
SERIES_METADATA = EMPLOYMENT_PROFILE.series_metadata
EmploymentTimeSeriesError = _core.CpiTimeSeriesError


class BlsEmploymentRangeSource(_BlsRangeSource):
    def __init__(self, **options):
        super().__init__(profile=EMPLOYMENT_PROFILE, **options)


def parse_response(raw, series_ids, start_month, end_month, **options):
    return _parse_response(
        raw, series_ids, start_month, end_month, profile=EMPLOYMENT_PROFILE, **options
    )


class EmploymentTimeSeriesService(_core.CpiTimeSeriesService):
    def __init__(self, store, source):
        super().__init__(store, source, profile=EMPLOYMENT_PROFILE)


def build_service(**options):
    return _core.build_service(profile=EMPLOYMENT_PROFILE, **options)


def latest_view_sql():
    """Public employment projection; shared `latest` remains an internal port."""
    schema = EMPLOYMENT_PROFILE.schema
    return f"""CREATE OR REPLACE VIEW {schema}.employment_latest AS
        SELECT series_id,month,revision AS local_revision,
            observed_at,available_at,version_status,batch_id,
            index_value AS value,
            CASE series_id WHEN 'CES0000000001' THEN 'thousand_jobs'
                WHEN 'LNS14000000' THEN 'percent'
                WHEN 'LNS11300000' THEN 'percent'
                WHEN 'CES0500000002' THEN 'hours'
                WHEN 'CES0500000003' THEN 'USD/hour' END AS value_unit,
            CASE WHEN series_id IN ('LNS14000000','LNS11300000')
                THEN 'CPS' ELSE 'CES' END AS survey,
            bls_pct_1m AS official_pct_1m,bls_pct_3m AS official_pct_3m,
            bls_pct_6m AS official_pct_6m,bls_pct_12m AS official_pct_12m,
            provider_extra->'net_changes' AS official_net_changes,
            CASE series_id WHEN 'CES0000000001' THEN 'thousand_jobs'
                WHEN 'LNS14000000' THEN 'percentage_points'
                WHEN 'LNS11300000' THEN 'percentage_points'
                WHEN 'CES0500000002' THEN 'hours'
                WHEN 'CES0500000003' THEN 'USD/hour' END AS official_net_change_unit,
            provider_extra,footnotes
        FROM {schema}.latest"""


def rates_view_sql():
    """Unit-specific nullable columns; no CPS inflation or CES person counts."""
    schema = EMPLOYMENT_PROFILE.schema
    return f"""CREATE OR REPLACE VIEW {schema}.employment_timeseries_rates AS
        SELECT c.series_id,c.month,c.revision,c.observed_at,c.available_at,
            c.version_status,c.batch_id,c.footnotes,c.provider_extra,
            c.bls_pct_1m AS official_pct_1m,c.bls_pct_3m AS official_pct_3m,
            c.bls_pct_6m AS official_pct_6m,c.bls_pct_12m AS official_pct_12m,
            CASE WHEN c.series_id='CES0000000001' THEN c.index_value END
                AS total_nonfarm_thousand_jobs,
            CASE WHEN c.series_id='LNS14000000' THEN c.index_value END
                AS unemployment_rate_percent,
            CASE WHEN c.series_id='CES0500000003' THEN c.index_value END
                AS average_hourly_earnings_usd_per_hour,
            CASE WHEN c.series_id='CES0000000001'
                THEN c.index_value-p1.index_value END AS net_change_1m_thousand_jobs,
            CASE WHEN c.series_id='LNS14000000'
                THEN c.index_value-p1.index_value END AS change_1m_percentage_points,
            CASE WHEN c.series_id='CES0500000003'
                THEN round(c.index_value/nullif(p1.index_value,0)-1,4) END
                AS ahe_change_1m_ratio,
            CASE WHEN c.series_id='CES0500000003'
                THEN round(c.index_value/nullif(p12.index_value,0)-1,4) END
                AS ahe_change_12m_ratio,
            CASE WHEN c.series_id='CES0500000003'
                THEN round((c.index_value/nullif(p1.index_value,0)-1)*100,2) END
                AS ahe_mom_percent,
            CASE WHEN c.series_id='CES0500000003'
                THEN round((c.index_value/nullif(p12.index_value,0)-1)*100,2) END
                AS ahe_yoy_percent,
            CASE WHEN c.series_id='LNS11300000' THEN c.index_value END
                AS labor_force_participation_rate_percent,
            CASE WHEN c.series_id='LNS11300000'
                THEN c.index_value-p1.index_value END
                AS participation_change_1m_percentage_points,
            CASE WHEN c.series_id='CES0500000002' THEN c.index_value END
                AS average_weekly_hours,
            CASE WHEN c.series_id='CES0500000002'
                THEN c.index_value-p1.index_value END AS change_1m_hours
        FROM {schema}.latest c
        LEFT JOIN {schema}.latest p1 ON p1.series_id=c.series_id
            AND p1.month=(c.month-interval '1 month')::date
            AND p1.version_status='active'
        LEFT JOIN {schema}.latest p12 ON p12.series_id=c.series_id
            AND p12.month=(c.month-interval '12 months')::date
            AND p12.version_status='active'
        WHERE c.version_status='active'"""


def five_series_migration_sql():
    """Owner-run, transaction-scoped extension of the existing three-series DB.

    Print/review only here; no connection, schema initialization, grants or commit.
    Existing view columns retain their order/type and CREATE OR REPLACE retains
    ownership and grants. Unknown constraint layouts fail closed for owner review.
    """
    schema = EMPLOYMENT_PROFILE.schema
    admitted = ",".join("'" + sid + "'" for sid in SERIES_IDS)
    return f"""-- Run inside an owner-managed transaction; rehearse with ROLLBACK first.
SET LOCAL lock_timeout = '5s';
SELECT pg_advisory_xact_lock({EMPLOYMENT_PROFILE.lock_id});
DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='{schema}.revisions'::regclass
          AND conname='revisions_series_id_check' AND contype='c'
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='{schema}.revisions'::regclass
          AND conname='revisions_check' AND contype='c'
    ) THEN
        RAISE EXCEPTION 'employment_constraint_layout_review_required';
    END IF;
END;
$migration$;
ALTER TABLE {schema}.revisions
    DROP CONSTRAINT revisions_series_id_check,
    ADD CONSTRAINT revisions_series_id_check CHECK(series_id IN ({admitted})),
    DROP CONSTRAINT revisions_check,
    ADD CONSTRAINT revisions_check CHECK(
        index_value >= 0
        AND (index_value > 0 OR series_id IN ('LNS14000000','LNS11300000'))
        AND index_value != 'NaN'::numeric
        AND index_value != 'Infinity'::numeric);
{latest_view_sql()};
{rates_view_sql()};
"""
