-- Publish once per source batch, after its fact transaction has committed.
CREATE TABLE mdf_tdx_daily.p0_batch_publications (
    source text NOT NULL,
    batch_id text NOT NULL,
    available_at timestamptz NOT NULL,
    PRIMARY KEY (source, batch_id)
);

-- During migration, the database can only attest that these batches already exist.
INSERT INTO mdf_tdx_daily.p0_batch_publications
    SELECT DISTINCT source, batch_id, statement_timestamp()
    FROM mdf_tdx_daily.source_refresh_batches;
