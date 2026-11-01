-- Root read-only review, after explicit schema preparation and initialization.
BEGIN READ ONLY;
SELECT table_name, ordinal_position, column_name, data_type
FROM information_schema.columns
WHERE table_schema='mdf_consumption_timeseries_dev_r1'
ORDER BY table_name, ordinal_position;
SELECT c.relname, pg_get_userbyid(c.relowner) AS owner,
       has_schema_privilege('odin_dba',n.oid,'USAGE') AS schema_usage,
       has_table_privilege('odin_dba',c.oid,'SELECT') AS can_select,
       has_table_privilege('odin_dba',c.oid,
           'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS any_write
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='mdf_consumption_timeseries_dev_r1' AND c.relkind IN ('r','v')
ORDER BY c.relname;
SELECT pg_get_userbyid(d.defaclrole) AS future_object_owner,
       x.grantee::regrole AS grantee, x.privilege_type
FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace,
     LATERAL aclexplode(d.defaclacl) x
WHERE (n.nspname='mdf_consumption_timeseries_dev_r1' OR d.defaclnamespace=0)
ORDER BY future_object_owner,grantee,privilege_type;
SELECT series_id,count(*) AS versions,count(value) AS numeric_versions,
       min(month) AS first_local_month,max(month) AS last_local_month
FROM mdf_consumption_timeseries_dev_r1.revisions GROUP BY series_id ORDER BY series_id;
SELECT series_id,month,value,value_unit,annual_rate,chained_base,
       change_1m_ratio,mom_pct,change_1m_percentage_points
FROM mdf_consumption_timeseries_dev_r1.consumption_timeseries_rates
WHERE month='2026-09-01' ORDER BY series_id;
SELECT event_id,reference_month,reference_months
FROM mdf_consumption_timeseries_dev_r1.release_events ORDER BY event_id;
SELECT evidence_id,event_id,raw_sha256,obtained_at,available_at,
       document->>'parser_revision' AS parser_revision
FROM mdf_consumption_timeseries_dev_r1.release_evidence ORDER BY obtained_at;
ROLLBACK;
