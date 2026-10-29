-- UNEXECUTED CANDIDATE. Separate authority and schema/role preflight required.
-- Exact target: 192.168.10.4:5432/market_data_foundation_dev.
-- No IF NOT EXISTS: an existing schema is a STOP, never migration/reuse.
-- No GRANT, role creation, runtime configuration, or automatic application hook.
BEGIN;
CREATE SCHEMA mdf_b3_cpi_05_dev_r1;
CREATE TABLE mdf_b3_cpi_05_dev_r1.records (
    kind text NOT NULL,
    identity text NOT NULL,
    revision text NOT NULL,
    payload text NOT NULL,
    PRIMARY KEY (kind, identity, revision)
);
CREATE TABLE mdf_b3_cpi_05_dev_r1.contents (
    version_identity text PRIMARY KEY,
    digest text NOT NULL,
    payload bytea NOT NULL
);
CREATE TABLE mdf_b3_cpi_05_dev_r1.publications (
    identity text PRIMARY KEY,
    occurrence text NOT NULL UNIQUE,
    version_identity text NOT NULL,
    decision_at timestamptz NOT NULL,
    payload text NOT NULL
);
CREATE TABLE mdf_b3_cpi_05_dev_r1.jobs (
    identity text PRIMARY KEY CHECK (identity ~ '^[a-z0-9][a-z0-9-]{0,63}$'),
    scope text NOT NULL UNIQUE CHECK (scope = 'mdf-b3-cpi-05-dev-r1'),
    document jsonb NOT NULL,
    event_count integer NOT NULL CHECK (event_count BETWEEN 1 AND 256),
    CHECK (octet_length(document::text) <= 1048576),
    CHECK (document->>'job_id' IS NOT DISTINCT FROM identity),
    CHECK ((document->>'state') IS NOT NULL AND document->>'state' IN
           ('pending','running','succeeded','failed','cancelled','outcome_unknown')),
    CHECK ((document->>'attempts') IS NOT NULL AND document->>'attempts' IN ('0','1')),
    CHECK ((document->>'attempts' = '0' AND document->'attempt_id' = 'null'::jsonb)
        OR (document->>'attempts' = '1' AND document->>'attempt_id' = identity || '-a1')),
    CHECK (document->>'state' NOT IN ('running','succeeded','outcome_unknown')
        OR document->>'attempts' = '1'),
    CHECK (document->'binding'->>'operator' IS NOT NULL
        AND document->'binding'->>'operator' ~ '^[a-z0-9][a-z0-9-]{0,63}$')
);
ALTER TABLE mdf_b3_cpi_05_dev_r1.jobs ADD CONSTRAINT exact_binding
CHECK (((document->'binding') - 'operator') IS NOT DISTINCT FROM
    '{"binding":"mdf.binding-candidate.bls-v2.cpi.release-2026-09","binding_revision":"p4-release-reference-2026-09-r1","definition":"mdf.macro.bls.cpi.headlines","definition_revision":"p3-r1","operation":"timeseries.data:CPI","provider_revision":"not_declared","reference":"2026-09","release_slot":"CPI/2026-09/2026-10-14T12:30:00Z","source":"mdf.source-candidate.bls-v2.cpi.release-2026-09","source_revision":"p4-release-reference-2026-09-r1","target":{"database":"market_data_foundation_dev","domain":"mdf-b3-cpi-05-dev-r1","host":"192.168.10.4","port":5432,"schema":"mdf_b3_cpi_05_dev_r1"}}'::jsonb);
CREATE TABLE mdf_b3_cpi_05_dev_r1.attempts (
    job_id text PRIMARY KEY REFERENCES mdf_b3_cpi_05_dev_r1.jobs(identity),
    attempt_id text NOT NULL UNIQUE CHECK (attempt_id = job_id || '-a1'),
    document jsonb NOT NULL,
    CHECK (document->>'job_id' IS NOT DISTINCT FROM job_id),
    CHECK (document->>'attempt_id' IS NOT DISTINCT FROM attempt_id),
    CHECK (document->>'attempts' IS NOT DISTINCT FROM '1'),
    CHECK (octet_length(document::text) <= 1048576)
);
CREATE TABLE mdf_b3_cpi_05_dev_r1.job_events (
    job_id text NOT NULL REFERENCES mdf_b3_cpi_05_dev_r1.jobs(identity),
    sequence integer NOT NULL CHECK (sequence BETWEEN 1 AND 256),
    event jsonb NOT NULL,
    PRIMARY KEY (job_id, sequence),
    CHECK (event->>'job_id' IS NOT DISTINCT FROM job_id),
    CHECK (octet_length(event::text) <= 1048576)
);
-- Defend immutable execution identity, single attempt and UNKNOWN at the store.
CREATE FUNCTION mdf_b3_cpi_05_dev_r1.protect_job() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'cpi_pg_delete_forbidden';
    END IF;
    IF NEW.identity <> OLD.identity OR NEW.scope <> OLD.scope
       OR NEW.document->'binding' IS DISTINCT FROM OLD.document->'binding'
       OR NEW.document->'spec' IS DISTINCT FROM OLD.document->'spec'
       OR NEW.document->'created_at' IS DISTINCT FROM OLD.document->'created_at'
       OR NEW.event_count <> OLD.event_count + 1 THEN
        RAISE EXCEPTION 'cpi_pg_immutable_identity';
    END IF;
    IF NOT (NEW.document->>'state' = OLD.document->>'state'
       OR (OLD.document->>'state' = 'pending' AND NEW.document->>'state' IN ('running','failed','cancelled'))
       OR (OLD.document->>'state' = 'running' AND NEW.document->>'state' IN ('succeeded','failed','cancelled','outcome_unknown'))) THEN
        RAISE EXCEPTION 'cpi_pg_state_regression';
    END IF;
    IF OLD.document->>'attempts' = '1' AND
       (NEW.document->'attempts' IS DISTINCT FROM OLD.document->'attempts'
        OR NEW.document->'attempt_id' IS DISTINCT FROM OLD.document->'attempt_id'
        OR NEW.document->'started_at' IS DISTINCT FROM OLD.document->'started_at'
        OR NEW.document->'worker_identity' IS DISTINCT FROM OLD.document->'worker_identity') THEN
        RAISE EXCEPTION 'cpi_pg_attempt_immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER protect_job BEFORE UPDATE OR DELETE ON mdf_b3_cpi_05_dev_r1.jobs
FOR EACH ROW EXECUTE FUNCTION mdf_b3_cpi_05_dev_r1.protect_job();
CREATE FUNCTION mdf_b3_cpi_05_dev_r1.protect_event() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION 'cpi_pg_event_append_only';
END;
$$;
CREATE TRIGGER protect_event BEFORE UPDATE OR DELETE ON mdf_b3_cpi_05_dev_r1.job_events
FOR EACH ROW EXECUTE FUNCTION mdf_b3_cpi_05_dev_r1.protect_event();
COMMIT;
