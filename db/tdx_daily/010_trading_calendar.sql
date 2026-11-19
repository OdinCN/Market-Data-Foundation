CREATE TABLE mdf_tdx_daily.p0_trading_calendar (
    year integer NOT NULL CHECK (year BETWEEN 1990 AND 2100),
    available_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    holidays date[] NOT NULL,
    evidence jsonb NOT NULL,
    PRIMARY KEY (year, available_at)
);
REVOKE ALL ON mdf_tdx_daily.p0_trading_calendar FROM PUBLIC;
