# Bounded CPI retained handoff candidate

This adapter consumes only the transaction's already captured MDD v1 CPI record
and opaque original bytes. It has no transport, provider client or active binding.
The fixed identities and canonical semantic-record digest bind the single retained
revision; it neither calculates CPI values nor reparses the old BLS artifact.

`CpiHandoffReceipt` carries immutable current/retained/raw/lineage bytes.
`build_handoff_plan` admits their identities, current-vs-retained agreement and the
exact four measurements, then builds one `REPROCESS` run and one
`mdf.macro.bls.cpi.headlines@p3-r1` version. `persist_handoff` and
`publish_handoff` use the accepted canonical kernel, rejecting modified plans.
Exact raw and retained semantic members survive byte-for-byte in canonical content;
`readback_materials` extracts and revalidates them.

The raw material is an EXACT copy, represented by the existing `Grade.ORIGINAL`
(`provider_original`) byte class and `exact_mdd_retained_raw_byte_copy` named class.
Its producer is the MDF reprocess run, with no `AcquisitionOccurrence`. The semantic
material uses `Grade.PROJECTED` (`source_projection`) for the MDD consumer record.
Each material's digest identifies its own member, not the whole content envelope.
The slice's `mdf-cpi-content://` references identify those members inside that
version's canonical content; the adapter verifies their stored carrier before
publication. This is a bounded member convention, not a new shared resolver or
Garage/S3 retention contract. No standalone material replayability is claimed.

MDD acquisition, artifact, parser and observation-change identities, obtained_at,
usable_at, null release/vintage and not-declared provider revision survive in
transform parameters and content. Exact `current.json`, full safe `lineage.json`
and the manifest remain in the ignored intake bundle only. Their health,
freshness, as-of and capture observations gate admission where applicable but do
not enter canonical content, semantic material identity or transform identity.
Completeness is limited to four measurements of 2026-08; no current BLS freshness
is asserted.

Callers sample current MDF processing times for run/freeze. Publication uses the
kernel's processing clock and, on PostgreSQL, its accepted server-timed publication
commit. Historical MDD times never become MDF decision/usable times. The adapter
requires a clock at or after the frozen version. For PostgreSQL, use an autocommit
connection (so prepublication reads leave it idle), with an explicit transaction
around namespace creation and ordinary persistence. Publication owns its commit.

Default deterministic tests use synthetic semantic records and opaque bytes with
fixture-scoped digest/size substitution; they require neither retained runtime
data nor a private service. The separate `postgres_integration` test validates the
real manifest and pinned production identities, performs a fresh read-only gate,
mutates only `mdf_p4_macro_bls_cpi_retained_handoff_r1`, reconnects to prove durable
readback and historical as-of behavior, then explicitly drops that schema and
performs read-only postflight. It requires all three opt-ins:

```sh
MDF_RUN_POSTGRES_INTEGRATION=1 \
MDF_PG_TEST_SCHEMA=mdf_p4_macro_bls_cpi_retained_handoff_r1 \
MDF_CPI_HANDOFF_EVIDENCE_DIR=artifacts/macro-bls-cpi-retained-handoff-r1/capture-001 \
uv run --frozen pytest -q -m postgres_integration \
  tests/test_p4_cpi_retained_handoff_postgres_integration.py
```

Safe proof observations are written under ignored
`artifacts/macro-bls-cpi-retained-handoff-r1/pg-proof.json`. No dependency, baseline,
CI workflow, MDD runtime or other product slice is changed.
