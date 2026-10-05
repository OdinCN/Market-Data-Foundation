# P3 Canonical Kernel R1

`MDF-P3-CANONICAL-KERNEL-R1` implements the bounded kernel defined by the
accepted P3 canonical model and persistence architecture. It is not a P4/P5
collector or realtime data plane.

## Boundary

The kernel contains:

- immutable contract records for the accepted eight logical responsibilities;
- exact revision/reference validation and scope monotonicity;
- finite `DatasetVersion` content identity;
- immutable `Publication` decision history with occurrence-level idempotence,
  predecessor checks and historical-as-of rules;
- explicit `CONFIRMED_*` versus `OUTCOME_UNKNOWN` reconciliation;
- a protected-material state machine enforcing protected final verification
  through durable handoff;
- a deterministic in-memory ledger for ordinary hosted CI;
- a PostgreSQL ledger for the isolated development integration gate;
- one small synthetic canonical-content `bytea` path proving durable content
  readback without making PostgreSQL the universal future payload store.

The kernel does not contact providers, activate source bindings, implement
Garage/S3, QuestDB or Parquet, or consume production data.

## Completion levels

The implementation keeps these claims separate:

1. semantic/reference kernel: deterministic rules and reference ledger only;
2. durable metadata ledger: PostgreSQL identities/relations are durable but no
   durable `DatasetVersion` content proof has passed;
3. durable canonical-kernel candidate: PostgreSQL ledger plus bounded synthetic
   content, reconnection/restart readback, `OUTCOME_UNKNOWN` reconciliation and
   protected final-verification gates have passed.

Only the third level may be returned after the private PostgreSQL integration
gate succeeds.

## Deterministic CI

Ordinary CI excludes both live providers and private PostgreSQL:

```sh
uv run --locked pytest -m 'not live_provider and not postgres_integration'
```

Private LAN services are never a hosted-CI prerequisite.

## Opt-in development PostgreSQL gate

The integration test is explicit opt-in and reads the ignored local
`config/local/runtime.toml` and `config/local/secret.toml`. It never prints the
password.

The AO-authorized R1 namespace is transaction-specific and must be absent
before the test:

```sh
MDF_RUN_POSTGRES_INTEGRATION=1 \
MDF_PG_TEST_SCHEMA=mdf_p3_canonical_kernel_r1_run_001 \
uv run --locked pytest -q -m postgres_integration \
  tests/test_canonical_postgres_integration.py
```

The test:

- connects only to the configured development PostgreSQL target;
- creates only the supplied, validated `mdf_p3_canonical_kernel_r1_*` schema;
- writes synthetic data only;
- closes and reconnects before durable content/publication readback;
- verifies a committed publication can reconcile an `OUTCOME_UNKNOWN` caller
  state to the original canonical identity;
- explicitly rolls back a later publication and verifies absence;
- drops the transaction schema at the end.

A failed isolation/preflight gate must stop database mutation rather than
selecting another target.

## Publication time

The application does not accept a caller-provided authoritative
`decision_at`. The reference ledger uses its injected kernel clock; the
PostgreSQL ledger requires an idle connection, owns the Publication transaction,
serializes the decision chain, samples the database server clock inside that
bounded transaction, and commits before returning the Publication. A caller
cannot delay the commit after receiving a decision, and a later caller rollback
cannot erase the accepted Publication. Historical-as-of therefore never treats
an uncommitted Publication as consumer-visible history. Physical production
HA/timing semantics beyond this bounded decision transaction remain outside
this P3 kernel.

## Non-effects

This R1 does not authorize commit/push, release, deployment, provider
requests, source-binding activation, storage adoption, MDD migration/cutover,
or P4/P5 implementation.
