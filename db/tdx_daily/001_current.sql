-- Candidate only: never applied by the offline CLI. All writers must use the lock.
CREATE SCHEMA mdf_tdx_daily;
CREATE TABLE mdf_tdx_daily.bars_daily_current (
    market smallint NOT NULL CHECK (market IN (0, 1, 2)),
    instrument text NOT NULL CHECK (instrument ~ '^[0-9]{6}$'),
    period text NOT NULL CHECK (period = '1d'),
    trading_day date NOT NULL CHECK (trading_day BETWEEN DATE '1990-01-01' AND DATE '2100-12-31'),
    basis text NOT NULL CHECK (basis = 'native_projection'),
    open bigint NOT NULL, high bigint NOT NULL, low bigint NOT NULL,
    close bigint NOT NULL, volume bigint NOT NULL CHECK (volume >= 0),
    amount bigint NOT NULL CHECK (amount >= 0),
    units text NOT NULL CHECK (units = 'go-tdx-v1.0.0-native-int-v1'),
    value_digest text NOT NULL CHECK (value_digest ~ '^[a-f0-9]{64}$'),
    value_source text NOT NULL CHECK (length(value_source) BETWEEN 1 AND 80),
    source_priority smallint NOT NULL CHECK (source_priority BETWEEN 1 AND 100),
    source_observed_at timestamptz NOT NULL,
    value_changed_at timestamptz NOT NULL,
    batch_id text NOT NULL CHECK (length(batch_id) BETWEEN 1 AND 80),
    PRIMARY KEY (market, instrument, period, trading_day, basis),
    CHECK (value_changed_at >= source_observed_at),
    CHECK (low <= LEAST(open, close) AND GREATEST(open, close) <= high)
);
CREATE TABLE mdf_tdx_daily.source_refresh_batches (
    source text NOT NULL, batch_id text NOT NULL,
    market smallint NOT NULL CHECK (market IN (0, 1, 2)),
    instrument text NOT NULL CHECK (instrument ~ '^[0-9]{6}$'),
    period text NOT NULL CHECK (period = '1d'),
    basis text NOT NULL CHECK (basis = 'native_projection'),
    batch_digest text NOT NULL CHECK (batch_digest ~ '^[a-f0-9]{64}$'),
    source_priority smallint NOT NULL CHECK (source_priority BETWEEN 1 AND 100),
    last_confirmed_at timestamptz NOT NULL,
    admitted_at timestamptz NOT NULL,
    confirmed_days date[] NOT NULL CHECK (cardinality(confirmed_days) BETWEEN 0 AND 800),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'array'
        AND jsonb_array_length(evidence) BETWEEN 1 AND 64),
    result text NOT NULL CHECK (result = 'complete_bounded_quality_checked'),
    new_count integer NOT NULL CHECK (new_count >= 0),
    unchanged_count integer NOT NULL CHECK (unchanged_count >= 0),
    changed_count integer NOT NULL CHECK (changed_count >= 0),
    blocked_count integer NOT NULL CHECK (blocked_count >= 0),
    CHECK (admitted_at >= last_confirmed_at),
    PRIMARY KEY (source, batch_id, market, instrument, period, basis)
);
CREATE TABLE mdf_tdx_daily.bars_daily_corrections (
    market smallint NOT NULL, instrument text NOT NULL,
    period text NOT NULL, trading_day date NOT NULL, basis text NOT NULL,
    source text NOT NULL, batch_id text NOT NULL,
    old_values bigint[] NOT NULL CHECK (cardinality(old_values) = 6),
    new_values bigint[] NOT NULL CHECK (cardinality(new_values) = 6),
    old_digest text NOT NULL, new_digest text NOT NULL,
    old_source text NOT NULL, old_batch_id text NOT NULL,
    old_source_observed_at timestamptz NOT NULL,
    old_value_changed_at timestamptz NOT NULL,
    source_observed_at timestamptz NOT NULL, changed_at timestamptz NOT NULL,
    reason text NOT NULL CHECK (reason = 'vendor_correction_or_unknown'),
    PRIMARY KEY (source, batch_id, market, instrument, period, trading_day, basis),
    FOREIGN KEY (market, instrument, period, trading_day, basis)
        REFERENCES mdf_tdx_daily.bars_daily_current,
    CHECK (changed_at >= source_observed_at),
    CHECK (old_values <> new_values)
);
-- No factor table is materialized without a named trusted factor source.
-- No Last, last_seen, wire frame, canonical Version or Publication per bar.

-- Scope lookup plus date-overlap filtering, independent of source/batch PK order.
CREATE INDEX source_refresh_scope_idx
    ON mdf_tdx_daily.source_refresh_batches
    (market, instrument, period, basis);
CREATE INDEX source_refresh_days_idx
    ON mdf_tdx_daily.source_refresh_batches USING gin (confirmed_days);
