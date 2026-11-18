CREATE TABLE mdf_tdx_daily.roster_name_changes (
    market smallint NOT NULL CHECK (market IN (0, 1, 2)),
    instrument text NOT NULL CHECK (instrument ~ '^[0-9]{6}$'),
    effective_date date NOT NULL,
    old_name text NOT NULL,
    new_name text NOT NULL,
    source_url text NOT NULL,
    raw_sha256 text NOT NULL,
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (market, instrument, effective_date, old_name, new_name)
);
