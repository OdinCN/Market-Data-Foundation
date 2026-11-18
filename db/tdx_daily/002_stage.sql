CREATE TEMP TABLE tdx_daily_stage (
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
    request_index smallint NOT NULL CHECK (request_index BETWEEN 0 AND 63),
    received_at timestamptz NOT NULL,
    CHECK (low <= LEAST(open, close) AND GREATEST(open, close) <= high)
) ON COMMIT DROP;
