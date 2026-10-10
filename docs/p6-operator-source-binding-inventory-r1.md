# P6 stored SourceBinding candidate inventory

`canonical.operator_bindings.inspect_binding_candidates(ledger, definition)` is
an operator-only, in-process metadata read. The caller supplies authorization
and a consistent storage snapshot. The exact Definition must be the canonical
`mdf.macro.bls.cpi.headlines@p3-r1` or `mdf.market.bars.daily.raw@p3-r1`.
PostgresLedger callers can explicitly supply the transaction context with the
[Operator snapshot candidate](p6-operator-consistent-snapshot-r1.md).

The result contains the unchanged Definition and stored binding revisions whose
mappings reference that exact Definition, including bindings with no Version or
Publication. Each candidate reports its exact binding Key/revision, validated
SourceOperationRef with its original contract claims, matching OutputMappings,
compatibility policy, limitations and silent-fallback flag. Candidates sort by
binding identity/revision; matching mappings sort by identity. Other mappings
are validated but never reported as accepted mapping scope. No latest-revision
selection or provider fallback occurs.

Record presence proves only stored metadata presence. Activation, current
executability, source health, current freshness, external admission, finite
coverage, observed quality, durability and replayability are explicitly
`unobservable`. Mapping evidence requirements, including durable/replayable
requirements, remain requirements rather than evidence of fulfillment. Source
temporal/completeness/pagination/freshness contracts and binding limitations are
preserved verbatim; no acquisition, material, content or runtime state is fetched.

The required `operator_reads().binding_candidates(maximum)` producer admits the
complete global binding kind, including unrelated candidates, before decoding.
The fixed inclusive row cap is 128. It reuses serialized admission limits of
1 MiB per payload, 64 MiB cumulatively per session (including exact readbacks),
JSON nesting 64 and 65,536 visited dictionary slots for memory enumeration.
The existing operator application metadata and resolution budgets also apply.
Unrelated rows can conservatively reject a query; no batch is truncated.

Every enumerated candidate receives an exact-key readback and canonical
validation of its Source and all mapping Definitions, with decoded field-shape
checks. Accepted Definition payloads must equal the canonical admitted records.
Missing, conflicting, corrupt or duplicate metadata rejects the entire
inventory. A missing producer capability fails `storage_unavailable` without a
legacy fallback. Producer/metadata overflow raises the existing operator-only
`operator_query_budget_exceeded`; invalid lineage raises
`lineage_missing_or_mismatched`. Storage/decoding errors and missing/unadmitted
Definitions retain contextual classifications. Exceptions expose only a
classification, never a raw storage exception or partial successful inventory.

The Postgres producer reuses the existing global-kind SQL admission query and
schema. Offline SQL doubles check query shape and modeled admission order; they
do not execute PostgreSQL or qualify server/driver behavior. Exact readbacks and
duplicate rejection check candidate identity under the caller's snapshot
prerequisite. This candidate establishes no physical backend scan, TOAST,
transfer, memory, CPU or wall-time bound. **B-END-TO-END-READ-CAPABILITY remains
STOP / NOT PROVEN.**

The public `mdf.consumer.read.v1`, whole-Version/as-of semantics, P3 models,
schemas and accepted Definitions are unchanged. This helper adds no endpoint,
authentication, activation, mutation, deployment or cross-product admission.
Offline proofs use synthetic in-memory fixtures and SQL doubles only. Host QA,
fresh independent revision-bound review, Git delivery and exact-head CI are
separate later gates.
