-- Explicit DEV migration; compatible with the B1 native fact/receipt tables.
ALTER TABLE mdf_tdx_daily.bars_daily_current DROP CONSTRAINT bars_daily_current_market_check;
ALTER TABLE mdf_tdx_daily.bars_daily_current ADD CHECK (market IN (0,1,2));
ALTER TABLE mdf_tdx_daily.source_refresh_batches DROP CONSTRAINT source_refresh_batches_market_check;
ALTER TABLE mdf_tdx_daily.source_refresh_batches ADD CHECK (market IN (0,1,2));
CREATE INDEX bars_daily_day_market_idx ON mdf_tdx_daily.bars_daily_current
    (trading_day,market,instrument) INCLUDE (value_changed_at);
CREATE INDEX bars_daily_corrections_scope_idx ON mdf_tdx_daily.bars_daily_corrections
    (market,instrument,trading_day,old_value_changed_at,changed_at);
CREATE TABLE mdf_tdx_daily.p0_universes (
    snapshot_id text PRIMARY KEY, observed_at timestamptz NOT NULL,
    available_at timestamptz NOT NULL, details jsonb NOT NULL,
    CHECK (available_at >= observed_at)
);
CREATE TABLE mdf_tdx_daily.p0_members (
    snapshot_id text NOT NULL REFERENCES mdf_tdx_daily.p0_universes,
    market smallint NOT NULL CHECK (market IN (0,1,2)),
    instrument text NOT NULL CHECK (instrument ~ '^[0-9]{6}$'),
    name text NOT NULL, evidence jsonb NOT NULL,
    PRIMARY KEY (snapshot_id,market,instrument)
);
CREATE TABLE mdf_tdx_daily.p0_schema_revision (revision integer PRIMARY KEY CHECK (revision=1));
INSERT INTO mdf_tdx_daily.p0_schema_revision VALUES (1);
REVOKE ALL ON SCHEMA mdf_tdx_daily FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA mdf_tdx_daily FROM PUBLIC;
