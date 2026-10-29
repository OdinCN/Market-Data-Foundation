-- OFFLINE CANDIDATE ONLY. Separate one-schema DEV gate required before execution.
-- Apply in one caller-owned transaction. No roles, grants or credentials here.
CREATE SCHEMA IF NOT EXISTS mdf_r3_a1;
CREATE TABLE IF NOT EXISTS mdf_r3_a1.instance_registration (
    identity text PRIMARY KEY CHECK (
        (identity ~ '^[A-Za-z0-9][A-Za-z0-9_.:@-]*$')
        AND (char_length(identity) BETWEEN 1 AND 512)),
    catalog_identity text NOT NULL CHECK (
        (catalog_identity ~ '^[A-Za-z0-9][A-Za-z0-9_.:@-]*$')
        AND (char_length(catalog_identity) BETWEEN 1 AND 512)),
    ownership text NOT NULL CHECK (
        ownership IN ('MDF_MANAGED', 'OWNER_MANAGED', 'OBSERVE_ONLY')),
    endpoint_ref text CHECK (
        (endpoint_ref ~ '^[A-Za-z0-9][A-Za-z0-9_.:@-]*$')
        AND (char_length(endpoint_ref) BETWEEN 1 AND 512))
);
-- catalog_identity is the existing directory identity including bound revision.
-- endpoint_ref is a public symbolic identifier, never a URL/DSN or credential.
-- Ownership confers no command authority. Adapter supports immutable registration
-- and exact replay; changed registration requires a new identity.
-- No runtime health, raw material, Version or Publication is persisted here.
-- IF NOT EXISTS supports exact reapply, NOT upgrade of an incompatible schema.
