CREATE SCHEMA IF NOT EXISTS mdf_consumption_timeseries_dev_r1;
CREATE TABLE IF NOT EXISTS mdf_consumption_timeseries_dev_r1.batches (batch_id text PRIMARY KEY, raw_sha256 text NOT NULL, observed_at timestamptz NOT NULL, request jsonb NOT NULL, provider_attempts integer NOT NULL CHECK(provider_attempts >= 0));
CREATE TABLE IF NOT EXISTS mdf_consumption_timeseries_dev_r1.revisions (
        series_id text NOT NULL CHECK(series_id IN ('DPCERC','DPCERX','DGDSRX','DSERRX','A067RC','A067RX','A072RC')),
        month date NOT NULL CHECK(EXTRACT(DAY FROM month)=1),
        revision integer NOT NULL CHECK(revision > 0),
        observed_at timestamptz NOT NULL,
        batch_id text NOT NULL REFERENCES mdf_consumption_timeseries_dev_r1.batches(batch_id),
        value numeric CHECK((series_id='A072RC' OR value > 0)
            AND value NOT IN
                ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)),

        provider_extra jsonb,
        footnotes jsonb NOT NULL CHECK(jsonb_typeof(footnotes)='array'),
        available_at timestamptz,
        version_status text NOT NULL DEFAULT 'active'
            CHECK(version_status IN ('active','withdrawn')),
        evidence_url text,
        CHECK(version_status != 'withdrawn' OR
            (value IS NULL AND evidence_url IS NOT NULL)),
        PRIMARY KEY(series_id,month,revision)
    );
CREATE OR REPLACE VIEW mdf_consumption_timeseries_dev_r1.latest AS
        SELECT DISTINCT ON (series_id,month) series_id,month,revision,observed_at,batch_id,value,provider_extra,footnotes,available_at,version_status,evidence_url,CASE series_id WHEN 'DPCERC' THEN 'million_USD' WHEN 'DPCERX' THEN 'million_chained_2017_USD' WHEN 'DGDSRX' THEN 'million_chained_2017_USD' WHEN 'DSERRX' THEN 'million_chained_2017_USD' WHEN 'A067RC' THEN 'million_USD' WHEN 'A067RX' THEN 'million_chained_2017_USD' WHEN 'A072RC' THEN 'percent' END AS value_unit,'SA'::text AS seasonal_adjustment,series_id!='A072RC' AS annual_rate,CASE WHEN series_id IN ('DPCERX','DGDSRX','DSERRX','A067RX') THEN '2017'::text END AS chained_base
        FROM mdf_consumption_timeseries_dev_r1.revisions
        ORDER BY series_id,month,revision DESC;
CREATE OR REPLACE VIEW mdf_consumption_timeseries_dev_r1.consumption_timeseries_rates AS SELECT c.*,to_char(c.month,'YYYY-MM') AS month_label,CASE WHEN c.series_id!='A072RC' THEN round(c.value/nullif(p1.value,0)-1,4) END AS change_1m_ratio,CASE WHEN c.series_id!='A072RC' THEN round(c.value/nullif(p3.value,0)-1,4) END AS change_3m_ratio,CASE WHEN c.series_id!='A072RC' THEN round(c.value/nullif(p6.value,0)-1,4) END AS change_6m_ratio,CASE WHEN c.series_id!='A072RC' THEN round(c.value/nullif(p12.value,0)-1,4) END AS change_12m_ratio,CASE WHEN c.series_id!='A072RC' THEN round((c.value/nullif(p1.value,0)-1)*100,2) END AS mom_pct,CASE WHEN c.series_id!='A072RC' THEN round((c.value/nullif(p12.value,0)-1)*100,2) END AS yoy_pct,CASE WHEN c.series_id='A072RC' THEN c.value-p1.value END AS change_1m_percentage_points,CASE WHEN c.series_id='A072RC' THEN c.value-p12.value END AS change_12m_percentage_points FROM mdf_consumption_timeseries_dev_r1.latest c LEFT JOIN mdf_consumption_timeseries_dev_r1.latest p1 ON p1.series_id=c.series_id AND p1.month=(c.month-interval '1 months')::date AND p1.version_status='active' LEFT JOIN mdf_consumption_timeseries_dev_r1.latest p3 ON p3.series_id=c.series_id AND p3.month=(c.month-interval '3 months')::date AND p3.version_status='active' LEFT JOIN mdf_consumption_timeseries_dev_r1.latest p6 ON p6.series_id=c.series_id AND p6.month=(c.month-interval '6 months')::date AND p6.version_status='active' LEFT JOIN mdf_consumption_timeseries_dev_r1.latest p12 ON p12.series_id=c.series_id AND p12.month=(c.month-interval '12 months')::date AND p12.version_status='active' WHERE c.version_status='active';
-- Existing schema/tables required. No continuous values change.
SET LOCAL lock_timeout = '5s';
SELECT pg_advisory_xact_lock(78328741642028106);
CREATE TABLE IF NOT EXISTS mdf_consumption_timeseries_dev_r1.release_events (
    event_id text PRIMARY KEY,
    family text NOT NULL,
    reference_month text NOT NULL
);
ALTER TABLE mdf_consumption_timeseries_dev_r1.release_events ADD COLUMN IF NOT EXISTS reference_months jsonb;
UPDATE mdf_consumption_timeseries_dev_r1.release_events SET reference_months=jsonb_build_array(reference_month)
    WHERE reference_months IS NULL;
CREATE TABLE IF NOT EXISTS mdf_consumption_timeseries_dev_r1.release_evidence (
    evidence_id text PRIMARY KEY,
    event_id text NOT NULL REFERENCES mdf_consumption_timeseries_dev_r1.release_events(event_id),
    raw_sha256 text NOT NULL,
    raw_locator text NOT NULL,
    obtained_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    available_at timestamptz,
    document jsonb NOT NULL,
    CHECK (available_at IS NULL OR available_at >= obtained_at)
);
CREATE TABLE IF NOT EXISTS mdf_consumption_timeseries_dev_r1.release_annotations (
    record_id text PRIMARY KEY,
    event_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('consensus','association')),
    obtained_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    available_at timestamptz,
    document jsonb NOT NULL,
    CHECK (available_at IS NULL OR available_at >= obtained_at)
);
CREATE INDEX IF NOT EXISTS release_annotations_event_available
    ON mdf_consumption_timeseries_dev_r1.release_annotations(event_id, available_at);
CREATE INDEX IF NOT EXISTS release_evidence_event_available
    ON mdf_consumption_timeseries_dev_r1.release_evidence(event_id, available_at);

REVOKE ALL ON SCHEMA mdf_consumption_timeseries_dev_r1 FROM PUBLIC;
REVOKE ALL ON SCHEMA mdf_consumption_timeseries_dev_r1 FROM odin_dba;
GRANT USAGE ON SCHEMA mdf_consumption_timeseries_dev_r1 TO odin_dba;
REVOKE ALL ON ALL TABLES IN SCHEMA mdf_consumption_timeseries_dev_r1 FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA mdf_consumption_timeseries_dev_r1 FROM odin_dba;
GRANT SELECT ON ALL TABLES IN SCHEMA mdf_consumption_timeseries_dev_r1 TO odin_dba;
ALTER DEFAULT PRIVILEGES IN SCHEMA mdf_consumption_timeseries_dev_r1 REVOKE ALL ON TABLES FROM odin_dba;
ALTER DEFAULT PRIVILEGES IN SCHEMA mdf_consumption_timeseries_dev_r1 REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA mdf_consumption_timeseries_dev_r1 GRANT SELECT ON TABLES TO odin_dba;
