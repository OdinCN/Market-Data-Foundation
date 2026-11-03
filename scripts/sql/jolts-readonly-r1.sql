-- Explicit owner-run DEV ACL, after JOLTS prepare-schema, in a transaction.
-- No password or global role changes. Future grants apply to this object owner.
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '5s';
REVOKE ALL ON SCHEMA mdf_jolts_timeseries_dev_r1 FROM PUBLIC;
REVOKE CREATE ON SCHEMA mdf_jolts_timeseries_dev_r1 FROM odin_dba;
GRANT USAGE ON SCHEMA mdf_jolts_timeseries_dev_r1 TO odin_dba;
REVOKE ALL ON ALL TABLES IN SCHEMA mdf_jolts_timeseries_dev_r1 FROM odin_dba;
GRANT SELECT ON ALL TABLES IN SCHEMA mdf_jolts_timeseries_dev_r1 TO odin_dba;
ALTER DEFAULT PRIVILEGES IN SCHEMA mdf_jolts_timeseries_dev_r1
    GRANT SELECT ON TABLES TO odin_dba;
