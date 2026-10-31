-- Read-only owner review before migration and after a rollback rehearsal.
SELECT c.conname, c.contype, pg_get_constraintdef(c.oid) AS definition
FROM pg_constraint c
WHERE c.conrelid = 'mdf_employment_timeseries_dev_r1.revisions'::regclass
ORDER BY c.conname;

SELECT table_name, ordinal_position, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'mdf_employment_timeseries_dev_r1'
ORDER BY table_name, ordinal_position;

SELECT c.relname, c.relkind, pg_get_userbyid(c.relowner) AS owner,
       has_schema_privilege('odin_dba', n.oid, 'USAGE') AS schema_usage,
       has_table_privilege('odin_dba', c.oid, 'SELECT') AS can_select,
       has_table_privilege('odin_dba', c.oid, 'INSERT') AS can_insert,
       has_table_privilege('odin_dba', c.oid, 'UPDATE') AS can_update,
       has_table_privilege('odin_dba', c.oid, 'DELETE') AS can_delete
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'mdf_employment_timeseries_dev_r1'
  AND c.relkind IN ('r', 'v')
ORDER BY c.relname;

SELECT series_id, count(*) AS revision_rows, count(index_value) AS numeric_rows,
       min(month) AS first_month, max(month) AS last_month,
       sum(index_value) AS value_sum
FROM mdf_employment_timeseries_dev_r1.revisions
GROUP BY series_id ORDER BY series_id;
SELECT count(*) AS batch_rows FROM mdf_employment_timeseries_dev_r1.batches;
SELECT count(*) AS latest_rows FROM mdf_employment_timeseries_dev_r1.latest;
