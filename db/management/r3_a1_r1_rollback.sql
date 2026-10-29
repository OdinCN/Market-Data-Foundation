-- OFFLINE ONLY. Gate owner must verify schema ownership, export registrations,
-- stop admitted writers and approve loss of this management metadata first.
-- RESTRICT deliberately refuses to remove dependent objects; never CASCADE.
DROP TABLE IF EXISTS mdf_r3_a1.instance_registration RESTRICT;
DROP SCHEMA IF EXISTS mdf_r3_a1 RESTRICT;
