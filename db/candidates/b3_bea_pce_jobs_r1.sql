-- HOST ONLY: invoked transactionally by mdf_b3_pce_dev.py ddl.
-- Existing canonical schema is independently pinned and checked before append.
-- No table replacement, IF NOT EXISTS, role creation, or legacy CPI changes.
CREATE TABLE mdf_macro_bea_pce_vertical_r1.jobs (
    identity text PRIMARY KEY CHECK (identity ~ '^[a-z0-9][a-z0-9-]{0,63}$'),
    scope text NOT NULL UNIQUE CHECK (scope = 'mdf-b3-bea-pce-202607-r1'),
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
-- Exact binding constraint is supplied below.
CREATE TABLE mdf_macro_bea_pce_vertical_r1.attempts (
    job_id text PRIMARY KEY REFERENCES mdf_macro_bea_pce_vertical_r1.jobs(identity),
    attempt_id text NOT NULL UNIQUE CHECK (attempt_id = job_id || '-a1'),
    document jsonb NOT NULL,
    CHECK (document->>'job_id' IS NOT DISTINCT FROM job_id),
    CHECK (document->>'attempt_id' IS NOT DISTINCT FROM attempt_id),
    CHECK (document->>'attempts' IS NOT DISTINCT FROM '1'),
    CHECK (octet_length(document::text) <= 1048576)
);
CREATE TABLE mdf_macro_bea_pce_vertical_r1.job_events (
    job_id text NOT NULL REFERENCES mdf_macro_bea_pce_vertical_r1.jobs(identity),
    sequence integer NOT NULL CHECK (sequence BETWEEN 1 AND 256),
    event jsonb NOT NULL,
    PRIMARY KEY (job_id, sequence),
    CHECK (event->>'job_id' IS NOT DISTINCT FROM job_id),
    CHECK (octet_length(event::text) <= 1048576)
);
-- Defend immutable execution identity, single attempt and UNKNOWN at the store.
CREATE FUNCTION mdf_macro_bea_pce_vertical_r1.protect_job() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'pce_pg_delete_forbidden';
    END IF;
    IF NEW.identity <> OLD.identity OR NEW.scope <> OLD.scope
       OR NEW.document->'binding' IS DISTINCT FROM OLD.document->'binding'
       OR NEW.document->'spec' IS DISTINCT FROM OLD.document->'spec'
       OR NEW.document->'created_at' IS DISTINCT FROM OLD.document->'created_at'
       OR NEW.event_count <> OLD.event_count + 1 THEN
        RAISE EXCEPTION 'pce_pg_immutable_identity';
    END IF;
    IF NOT (NEW.document->>'state' = OLD.document->>'state'
       OR (OLD.document->>'state' = 'pending' AND NEW.document->>'state' IN ('running','failed','cancelled'))
       OR (OLD.document->>'state' = 'running' AND NEW.document->>'state' IN ('succeeded','failed','cancelled','outcome_unknown'))) THEN
        RAISE EXCEPTION 'pce_pg_state_regression';
    END IF;
    IF OLD.document->>'attempts' = '1' AND
       (NEW.document->'attempts' IS DISTINCT FROM OLD.document->'attempts'
        OR NEW.document->'attempt_id' IS DISTINCT FROM OLD.document->'attempt_id'
        OR NEW.document->'started_at' IS DISTINCT FROM OLD.document->'started_at'
        OR NEW.document->'worker_identity' IS DISTINCT FROM OLD.document->'worker_identity') THEN
        RAISE EXCEPTION 'pce_pg_attempt_immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER protect_job BEFORE UPDATE OR DELETE ON mdf_macro_bea_pce_vertical_r1.jobs
FOR EACH ROW EXECUTE FUNCTION mdf_macro_bea_pce_vertical_r1.protect_job();
CREATE FUNCTION mdf_macro_bea_pce_vertical_r1.protect_event() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION 'pce_pg_event_append_only';
END;
$$;
CREATE TRIGGER protect_event BEFORE UPDATE OR DELETE ON mdf_macro_bea_pce_vertical_r1.job_events
FOR EACH ROW EXECUTE FUNCTION mdf_macro_bea_pce_vertical_r1.protect_event();
ALTER TABLE mdf_macro_bea_pce_vertical_r1.jobs ADD CONSTRAINT exact_pce_binding
CHECK (document->'binding' IS NOT DISTINCT FROM
'{"binding": "mdf.binding-candidate.bea.nipa.pce", "binding_revision": "t04-retained-candidate-r1", "candidate_only": true, "definition": "mdf.macro.bea.pce.headline-core", "definition_revision": "t04-r1", "operation": "GetData:NIPA:T20807:T20804:M", "operator": "ao-dev-bea-pce", "reference": "2026-07", "source": "mdf.source-candidate.bea.nipa.pce", "source_qualified": false, "source_revision": "t04-retained-candidate-r1", "target": {"database": "market_data_foundation_dev", "domain": "mdf-b3-bea-pce-202607-r1", "host": "192.168.10.4", "port": 5432, "schema": "mdf_macro_bea_pce_vertical_r1"}}'::jsonb);
GRANT USAGE ON SCHEMA mdf_macro_bea_pce_vertical_r1 TO odin_dba;
GRANT SELECT ON TABLE mdf_macro_bea_pce_vertical_r1.jobs, mdf_macro_bea_pce_vertical_r1.attempts,
mdf_macro_bea_pce_vertical_r1.job_events, mdf_macro_bea_pce_vertical_r1.records, mdf_macro_bea_pce_vertical_r1.contents,
mdf_macro_bea_pce_vertical_r1.publications TO odin_dba;
