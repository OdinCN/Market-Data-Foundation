# Bounded direct BLS CPI admission candidate

This slice implements a parser and pre-provider canonical admission boundary for
the exact official BLS v2 endpoint
`https://api.bls.gov/publicAPI/v2/timeseries/data/`. It contains no HTTP client,
credential manager, scheduler, retry loop, release runner, or provider fallback.

`BlsCpiRequest` fixes POST, the four admitted CPI series, 2025–2026,
`catalog=true`, and `calculations=true`. Its persisted representation has no
registration-key field. Response parsing is size bounded, rejects credential
echoes and duplicate JSON keys, validates the exact catalog identities and the
2026-08/2026-07/2025-08 source pointers, and takes decimal percentage text only
from provider-returned `pct_changes`. Index levels are never calculation inputs.

The direct parser calls the retained-handoff path's shared
`project_provider_calculations` function. This keeps measure identity,
comparison period, unit, seasonality, preliminary/footnote treatment, and
availability semantics in one mapping. Provider messages and original response
bytes are preserved. Provider revision is explicitly `not_declared`; release
time and vintage remain null. Response timing is not part of the four-measure
semantic projection, and health/freshness is not admitted into dataset semantics.

`build_offline_replay_plan` parses only supplied bytes and creates a `REPROCESS`
run with no binding, mapping, or `AcquisitionOccurrence`. Raw-response SHA-256
records content identity only; it does not prove whether or when an acquisition
occurred. A later real acquisition may therefore return byte-identical content.

`build_future_live_plan` accepts exactly `DirectCpiLiveEvidence`, never a
`DirectCpiReplayPlan` or another object. That evidence explicitly carries the
direct-execution mode, exact request identity, actual request/obtain times, one
provider-data attempt, a fresh acquisition identity distinct from the prior MDD
identity, and a controlled external receipt. These are facts a future authorized
runner must supply; constructing the Python value is not cryptographic or network
attestation. The source and binding objects remain candidates, and there is no
live persistence/activation function in this slice.

The ignored retained bundle at
`artifacts/macro-bls-cpi-retained-handoff-r1/capture-001` is used read-only for
exact offline equivalence. No recapture, MDD access, provider request, or
credential access occurs.

The PostgreSQL integration proof requires explicit opt-in and mutates only
`mdf_p4_macro_bls_cpi_direct_admission_r1`. It persists offline retained replay
evidence with acquisition count zero, reconnects for durable readback, then
drops the namespace and performs a read-only absent postflight:

```sh
MDF_RUN_POSTGRES_INTEGRATION=1 \
MDF_PG_TEST_SCHEMA=mdf_p4_macro_bls_cpi_direct_admission_r1 \
MDF_CPI_DIRECT_OFFLINE_EVIDENCE_DIR=artifacts/macro-bls-cpi-retained-handoff-r1/capture-001 \
uv run --frozen pytest -q -m postgres_integration \
  tests/test_p4_bls_cpi_direct_admission_postgres_integration.py
```

The safe proof observation is written under ignored
`artifacts/macro-bls-cpi-direct-admission-r1/pg-proof.json`. No source binding is
active and no authority for a provider request follows from this candidate.
