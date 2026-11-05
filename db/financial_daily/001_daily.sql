CREATE TABLE {s}.schema_metadata (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    schema_revision text NOT NULL, dataset_id text NOT NULL, definition_revision text NOT NULL
);
CREATE TABLE {s}.batches (
    batch_id text PRIMARY KEY,
    series_id text NOT NULL CHECK(series_id IN ('DFEDTARL','DFEDTARU','EFFR','SOFR','DGS2','DGS10','DFII10','T10YIE','BAMLC0A0CM','BAMLH0A0HYM2')),
    start_date date NOT NULL, end_date date NOT NULL CHECK(end_date>=start_date),
    raw_sha256 text NOT NULL, raw_locator text NOT NULL, raw_bytes integer NOT NULL CHECK(raw_bytes>0),
    metadata_sha256 text NOT NULL, metadata_locator text NOT NULL,
    metadata_bytes integer NOT NULL CHECK(metadata_bytes>0), metadata jsonb NOT NULL,
    metadata_observed_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL CHECK(observed_at>=metadata_observed_at),
    available_at timestamptz CHECK(available_at>=observed_at),
    request jsonb NOT NULL, origin text NOT NULL CHECK(origin IN ('fred','controlled_fixture')),
    provider_attempts integer NOT NULL CHECK(provider_attempts BETWEEN 1 AND 3)
);
CREATE TABLE {s}.revisions (
    series_id text NOT NULL,
    observation_date date NOT NULL, revision integer NOT NULL CHECK(revision>0),
    value numeric, provider_extra jsonb NOT NULL,
    batch_id text NOT NULL REFERENCES {s}.batches(batch_id),
    observed_at timestamptz NOT NULL,
    available_at timestamptz CHECK(available_at>=observed_at),
    PRIMARY KEY(series_id,observation_date,revision)
);
CREATE INDEX revisions_cutoff ON {s}.revisions(series_id,observation_date,available_at);
CREATE TABLE {s}.attempts (
    attempt_id uuid PRIMARY KEY,
    series_id text NOT NULL, start_date date NOT NULL, end_date date NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    state text NOT NULL CHECK(state IN ('complete','failed','unknown')),
    classification text, batch_id text REFERENCES {s}.batches(batch_id)
);
