-- Apply after 001 inside a fresh isolated schema, never alters an old ledger.
-- Frozen original payload is intentionally retained for process restart recovery.
CREATE TABLE {s}.ingest_pending (
    identity text PRIMARY KEY,
    attempt_identity text NOT NULL UNIQUE,
    intent text NOT NULL,
    intent_digest text NOT NULL,
    raw bytea NOT NULL CHECK (length(raw) > 0 AND length(raw) <= 8388608),
    state text NOT NULL CHECK (state IN ('pending', 'complete'))
);
