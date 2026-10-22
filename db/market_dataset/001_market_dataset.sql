-- market.dataset.physical.r1. Executed inside one transaction by the I-3 launcher.
-- {s} is substituted ONLY with the fixed, validated DEV schema identifier.
CREATE TABLE {s}.schema_metadata (
    singleton integer PRIMARY KEY CHECK (singleton = 1),
    schema_revision text NOT NULL,
    ddl_digest text NOT NULL,
    ledger_binding text NOT NULL,
    owner_run text NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE TABLE {s}.materials (
    id text PRIMARY KEY,
    payload text NOT NULL,
    payload_digest text NOT NULL,
    job_id text NOT NULL,
    shard_id text NOT NULL,
    attempt_id text NOT NULL,
    request_id text NOT NULL,
    source_id text NOT NULL,
    binding_id text NOT NULL,
    mapping_revision text NOT NULL,
    material_level text NOT NULL CHECK (material_level IN ('raw', 'source-projection', 'status', 'metadata')),
    locator text NOT NULL,
    byte_digest text NOT NULL,
    byte_length bigint NOT NULL CHECK (byte_length > 0 AND byte_length <= 8388608),
    requested_at timestamptz,
    obtained_at timestamptz,
    received_at timestamptz,
    known_at timestamptz
);
CREATE INDEX materials_attempt ON {s}.materials (job_id, shard_id, attempt_id, request_id);
CREATE INDEX materials_content ON {s}.materials (byte_digest, byte_length);
CREATE TABLE {s}.versions (
    id text PRIMARY KEY,
    payload text NOT NULL,
    payload_digest text NOT NULL,
    frozen_at timestamptz NOT NULL,
    content_digest text NOT NULL,
    content_length integer NOT NULL CHECK (content_length >= 0 AND content_length <= 65536)
);
CREATE TABLE {s}.version_materials (
    version_id text NOT NULL REFERENCES {s}.versions(id),
    material_id text NOT NULL REFERENCES {s}.materials(id),
    PRIMARY KEY (version_id, material_id)
);
CREATE INDEX material_references ON {s}.version_materials (material_id, version_id);
CREATE TABLE {s}.publication_events (
    id text PRIMARY KEY,
    occurrence text NOT NULL UNIQUE,
    version_id text NOT NULL REFERENCES {s}.versions(id),
    decision_at timestamptz NOT NULL,
    usable_at timestamptz NOT NULL,
    effective_at timestamptz,
    payload text NOT NULL,
    payload_digest text NOT NULL,
    UNIQUE (version_id, decision_at),
    CHECK (usable_at >= decision_at)
);
CREATE INDEX publication_history ON {s}.publication_events (version_id, decision_at, id);
CREATE TABLE {s}.publication_heads (
    version_id text PRIMARY KEY REFERENCES {s}.versions(id),
    event_count bigint NOT NULL CHECK (event_count >= 0),
    head_id text REFERENCES {s}.publication_events(id),
    history_digest text NOT NULL,
    CHECK ((event_count = 0 AND head_id IS NULL) OR (event_count > 0 AND head_id IS NOT NULL))
);
CREATE TABLE {s}.commit_receipts (
    operation text NOT NULL,
    identity text NOT NULL,
    payload_digest text NOT NULL,
    result_digest text NOT NULL,
    PRIMARY KEY (operation, identity)
);
CREATE TABLE {s}.admission_guards (
    subject text PRIMARY KEY,
    payload text NOT NULL
);
CREATE TABLE {s}.projection_generations (
    generation text PRIMARY KEY,
    payload text NOT NULL,
    active boolean NOT NULL
);
CREATE UNIQUE INDEX one_active_generation ON {s}.projection_generations (active) WHERE active;
CREATE TABLE {s}.projection_versions (
    version_id text PRIMARY KEY REFERENCES {s}.versions(id),
    payload text NOT NULL
);
CREATE TABLE {s}.projection_months (
    definition_id text NOT NULL,
    instrument_namespace text NOT NULL,
    instrument_id text NOT NULL,
    year integer NOT NULL,
    month integer NOT NULL CHECK (month BETWEEN 1 AND 12),
    payload text NOT NULL,
    PRIMARY KEY (definition_id, instrument_namespace, instrument_id, year, month)
);
CREATE TABLE {s}.projection_slots (
    definition_id text NOT NULL,
    instrument_namespace text NOT NULL,
    instrument_id text NOT NULL,
    local_date date NOT NULL,
    candidate_count bigint NOT NULL CHECK (candidate_count >= 0),
    candidate_digest text,
    overflow boolean NOT NULL,
    PRIMARY KEY (definition_id, instrument_namespace, instrument_id, local_date),
    CHECK ((candidate_count > 64 AND overflow AND candidate_digest IS NULL)
       OR (candidate_count <= 64 AND NOT overflow AND candidate_digest IS NOT NULL))
);
CREATE TABLE {s}.projection_rows (
    definition_id text NOT NULL,
    instrument_namespace text NOT NULL,
    instrument_id text NOT NULL,
    local_date date NOT NULL,
    version_id text NOT NULL REFERENCES {s}.versions(id),
    payload text NOT NULL,
    PRIMARY KEY (definition_id, instrument_namespace, instrument_id, local_date, version_id)
);
CREATE INDEX projection_reverse ON {s}.projection_rows (version_id, local_date);
CREATE TABLE {s}.projection_publications (
    event_id text PRIMARY KEY REFERENCES {s}.publication_events(id),
    version_id text NOT NULL REFERENCES {s}.versions(id),
    decision_at timestamptz NOT NULL,
    payload text NOT NULL
);
CREATE INDEX projected_history ON {s}.projection_publications (version_id, decision_at, event_id);
CREATE TABLE {s}.projection_guards (
    version_id text PRIMARY KEY REFERENCES {s}.versions(id),
    payload text NOT NULL
);
