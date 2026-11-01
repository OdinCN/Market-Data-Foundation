-- Owner transaction required. Rehearse with ROLLBACK; no data rewrite.
SET LOCAL lock_timeout = '5s';
SELECT pg_advisory_xact_lock(78328741642028101);
ALTER TABLE mdf_cpi_timeseries_dev_r1.revisions
 DROP CONSTRAINT revisions_series_id_check,
 ADD CONSTRAINT revisions_series_id_check CHECK(series_id IN ('CUSR0000SA0','CUUR0000SA0','CUSR0000SA0L1E','CUUR0000SA0L1E','CUSR0000SAH1','CUUR0000SAH1','CUSR0000SAF1','CUUR0000SAF1','CUSR0000SA0E','CUUR0000SA0E','CUSR0000SACL1E','CUUR0000SACL1E','CUSR0000SASLE','CUUR0000SASLE'));
SELECT pg_advisory_xact_lock(78328741642028102);
ALTER TABLE mdf_ppi_timeseries_dev_r1.revisions
 DROP CONSTRAINT revisions_series_id_check,
 ADD CONSTRAINT revisions_series_id_check CHECK(series_id IN ('WPSFD4','WPUFD4','WPSFD49104','WPUFD49104','WPSFD49116','WPUFD49116'));
-- Run inside an owner-managed transaction; rehearse with ROLLBACK first.
SET LOCAL lock_timeout = '5s';
SELECT pg_advisory_xact_lock(78328741642028104);
DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='mdf_employment_timeseries_dev_r1.revisions'::regclass
          AND conname='revisions_series_id_check' AND contype='c'
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='mdf_employment_timeseries_dev_r1.revisions'::regclass
          AND conname='revisions_check' AND contype='c'
    ) THEN
        RAISE EXCEPTION 'employment_constraint_layout_review_required';
    END IF;
END;
$migration$;
ALTER TABLE mdf_employment_timeseries_dev_r1.revisions
    DROP CONSTRAINT revisions_series_id_check,
    ADD CONSTRAINT revisions_series_id_check CHECK(series_id IN ('CES0000000001','LNS14000000','CES0500000003','LNS11300000','CES0500000002','CES0500000001','LNS12300000','CES1000000001','CES2000000001','CES3000000001','CES4000000001','CES5000000001','CES5500000001','CES6000000001','CES6500000001','CES7000000001','CES8000000001','CES9000000001')),
    DROP CONSTRAINT revisions_check,
    ADD CONSTRAINT revisions_check CHECK(
        index_value >= 0
        AND (index_value > 0 OR series_id IN (
            'LNS14000000','LNS11300000','LNS12300000'))
        AND index_value != 'NaN'::numeric
        AND index_value != 'Infinity'::numeric);
CREATE OR REPLACE VIEW mdf_employment_timeseries_dev_r1.employment_latest AS
        SELECT series_id,month,revision AS local_revision,
            observed_at,available_at,version_status,batch_id,
            index_value AS value,
            CASE series_id WHEN 'CES0000000001' THEN 'thousand_jobs' WHEN 'LNS14000000' THEN 'percent' WHEN 'CES0500000003' THEN 'USD/hour' WHEN 'LNS11300000' THEN 'percent' WHEN 'CES0500000002' THEN 'hours' WHEN 'CES0500000001' THEN 'thousand_jobs' WHEN 'LNS12300000' THEN 'percent' WHEN 'CES1000000001' THEN 'thousand_jobs' WHEN 'CES2000000001' THEN 'thousand_jobs' WHEN 'CES3000000001' THEN 'thousand_jobs' WHEN 'CES4000000001' THEN 'thousand_jobs' WHEN 'CES5000000001' THEN 'thousand_jobs' WHEN 'CES5500000001' THEN 'thousand_jobs' WHEN 'CES6000000001' THEN 'thousand_jobs' WHEN 'CES6500000001' THEN 'thousand_jobs' WHEN 'CES7000000001' THEN 'thousand_jobs' WHEN 'CES8000000001' THEN 'thousand_jobs' WHEN 'CES9000000001' THEN 'thousand_jobs' END AS value_unit,
            CASE WHEN series_id IN ('LNS14000000','LNS11300000','LNS12300000')
                THEN 'CPS' ELSE 'CES' END AS survey,
            bls_pct_1m AS official_pct_1m,bls_pct_3m AS official_pct_3m,
            bls_pct_6m AS official_pct_6m,bls_pct_12m AS official_pct_12m,
            provider_extra->'net_changes' AS official_net_changes,
            CASE series_id WHEN 'CES0000000001' THEN 'thousand_jobs' WHEN 'LNS14000000' THEN 'percentage_points' WHEN 'CES0500000003' THEN 'USD/hour' WHEN 'LNS11300000' THEN 'percentage_points' WHEN 'CES0500000002' THEN 'hours' WHEN 'CES0500000001' THEN 'thousand_jobs' WHEN 'LNS12300000' THEN 'percentage_points' WHEN 'CES1000000001' THEN 'thousand_jobs' WHEN 'CES2000000001' THEN 'thousand_jobs' WHEN 'CES3000000001' THEN 'thousand_jobs' WHEN 'CES4000000001' THEN 'thousand_jobs' WHEN 'CES5000000001' THEN 'thousand_jobs' WHEN 'CES5500000001' THEN 'thousand_jobs' WHEN 'CES6000000001' THEN 'thousand_jobs' WHEN 'CES6500000001' THEN 'thousand_jobs' WHEN 'CES7000000001' THEN 'thousand_jobs' WHEN 'CES8000000001' THEN 'thousand_jobs' WHEN 'CES9000000001' THEN 'thousand_jobs' END AS official_net_change_unit,
            provider_extra,footnotes
        FROM mdf_employment_timeseries_dev_r1.latest;
CREATE OR REPLACE VIEW mdf_employment_timeseries_dev_r1.employment_timeseries_rates AS
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
            CASE WHEN c.series_id IN ('CES0000000001','CES0500000001','CES1000000001','CES2000000001','CES3000000001','CES4000000001','CES5000000001','CES5500000001','CES6000000001','CES6500000001','CES7000000001','CES8000000001','CES9000000001')
                THEN c.index_value-p1.index_value END AS net_change_1m_thousand_jobs,
            CASE WHEN c.series_id IN ('LNS14000000','LNS12300000')
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
                THEN c.index_value-p1.index_value END AS change_1m_hours,
            CASE WHEN c.series_id='CES0500000001' THEN c.index_value END
                AS total_private_thousand_jobs,
            CASE WHEN c.series_id='LNS12300000' THEN c.index_value END
                AS employment_population_ratio_percent,
            c.index_value AS value,
            CASE c.series_id WHEN 'CES0000000001' THEN 'thousand_jobs' WHEN 'LNS14000000' THEN 'percent' WHEN 'CES0500000003' THEN 'USD/hour' WHEN 'LNS11300000' THEN 'percent' WHEN 'CES0500000002' THEN 'hours' WHEN 'CES0500000001' THEN 'thousand_jobs' WHEN 'LNS12300000' THEN 'percent' WHEN 'CES1000000001' THEN 'thousand_jobs' WHEN 'CES2000000001' THEN 'thousand_jobs' WHEN 'CES3000000001' THEN 'thousand_jobs' WHEN 'CES4000000001' THEN 'thousand_jobs' WHEN 'CES5000000001' THEN 'thousand_jobs' WHEN 'CES5500000001' THEN 'thousand_jobs' WHEN 'CES6000000001' THEN 'thousand_jobs' WHEN 'CES6500000001' THEN 'thousand_jobs' WHEN 'CES7000000001' THEN 'thousand_jobs' WHEN 'CES8000000001' THEN 'thousand_jobs' WHEN 'CES9000000001' THEN 'thousand_jobs' END AS value_unit
        FROM mdf_employment_timeseries_dev_r1.latest c
        LEFT JOIN mdf_employment_timeseries_dev_r1.latest p1 ON p1.series_id=c.series_id
            AND p1.month=(c.month-interval '1 month')::date
            AND p1.version_status='active'
        LEFT JOIN mdf_employment_timeseries_dev_r1.latest p12 ON p12.series_id=c.series_id
            AND p12.month=(c.month-interval '12 months')::date
            AND p12.version_status='active'
        WHERE c.version_status='active';

CREATE OR REPLACE VIEW mdf_pce_timeseries_dev_r1.pce_timeseries_rates AS
        SELECT c.*,to_char(c.month,'YYYY-MM') AS month_label,round(c.index_value / nullif(p1.index_value,0) - 1,4) AS change_1m_ratio,round(c.index_value / nullif(p3.index_value,0) - 1,4) AS change_3m_ratio,round(c.index_value / nullif(p6.index_value,0) - 1,4) AS change_6m_ratio,round(c.index_value / nullif(p12.index_value,0) - 1,4) AS change_12m_ratio,round(power(c.index_value/nullif(p3.index_value,0),4)-1,4) AS annualized_3m_ratio,round(power(c.index_value/nullif(p6.index_value,0),2)-1,4) AS annualized_6m_ratio
        FROM mdf_pce_timeseries_dev_r1.latest c LEFT JOIN mdf_pce_timeseries_dev_r1.latest p1 ON p1.series_id=c.series_id AND p1.month=(c.month - interval '1 months')::date AND p1.version_status='active' LEFT JOIN mdf_pce_timeseries_dev_r1.latest p3 ON p3.series_id=c.series_id AND p3.month=(c.month - interval '3 months')::date AND p3.version_status='active' LEFT JOIN mdf_pce_timeseries_dev_r1.latest p6 ON p6.series_id=c.series_id AND p6.month=(c.month - interval '6 months')::date AND p6.version_status='active' LEFT JOIN mdf_pce_timeseries_dev_r1.latest p12 ON p12.series_id=c.series_id AND p12.month=(c.month - interval '12 months')::date AND p12.version_status='active' WHERE c.version_status='active';
CREATE OR REPLACE VIEW "mdf_retail_timeseries_dev_r1".retail_timeseries_rates AS
 WITH inputs AS (SELECT month,max(value) FILTER(WHERE category_code='44X72' AND data_type_code='SM') AS v0,max(value) FILTER(WHERE category_code='441' AND data_type_code='SM') AS v1,max(value) FILTER(WHERE category_code='444' AND data_type_code='SM') AS v2,max(value) FILTER(WHERE category_code='447' AND data_type_code='SM') AS v3,max(value) FILTER(WHERE category_code='722' AND data_type_code='SM') AS v4,
 max(value) FILTER(WHERE data_type_code='MPCSM') AS official_headline_mpcsm_percent
 FROM "mdf_retail_timeseries_dev_r1".latest GROUP BY month),
 levels AS (SELECT month,v0 AS headline,v0-v1-v2-v3-v4 AS control,
 official_headline_mpcsm_percent,v0-v1 AS ex_auto FROM inputs)
 SELECT c.month,c.headline,c.control,c.official_headline_mpcsm_percent,c.headline::numeric(64,32)/nullif(p1.headline,0)-1 AS headline_change_1m_ratio,c.headline::numeric(64,32)/nullif(p3.headline,0)-1 AS headline_change_3m_ratio,c.headline::numeric(64,32)/nullif(p6.headline,0)-1 AS headline_change_6m_ratio,c.headline::numeric(64,32)/nullif(p12.headline,0)-1 AS headline_change_12m_ratio,c.control::numeric(64,32)/nullif(p1.control,0)-1 AS control_change_1m_ratio,c.control::numeric(64,32)/nullif(p3.control,0)-1 AS control_change_3m_ratio,c.control::numeric(64,32)/nullif(p6.control,0)-1 AS control_change_6m_ratio,c.control::numeric(64,32)/nullif(p12.control,0)-1 AS control_change_12m_ratio,
 c.ex_auto,c.ex_auto::numeric(64,32)/nullif(p1.ex_auto,0)-1 AS ex_auto_change_1m_ratio,c.ex_auto::numeric(64,32)/nullif(p3.ex_auto,0)-1 AS ex_auto_change_3m_ratio,c.ex_auto::numeric(64,32)/nullif(p6.ex_auto,0)-1 AS ex_auto_change_6m_ratio,c.ex_auto::numeric(64,32)/nullif(p12.ex_auto,0)-1 AS ex_auto_change_12m_ratio,'mdf_retail_control_v1'::text AS control_derivation_identity,
 'SM44X72-SM441-SM444-SM447-SM722'::text AS control_formula,
 'mdf_retail_ex_auto_v1'::text AS ex_auto_derivation_identity,
 'SM44X72-SM441:v1'::text AS ex_auto_formula
 FROM levels c LEFT JOIN levels p1 ON p1.month=(c.month-interval '1 month')::date LEFT JOIN levels p3 ON p3.month=(c.month-interval '3 month')::date LEFT JOIN levels p6 ON p6.month=(c.month-interval '6 month')::date LEFT JOIN levels p12 ON p12.month=(c.month-interval '12 month')::date;
