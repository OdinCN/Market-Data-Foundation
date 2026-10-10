# P6 Operator read-only status projection candidate

`canonical.operator_status.inspect_operator_status(scope, definition, as_of=...)`
adds an exact association gate to the existing as-of diagnostic and stored binding
inventory. Already authorized operators supply a consistent read-only scope:

```python
with ledger.operator_snapshot() as scope:
    candidate = inspect_operator_status(scope, definition, as_of=instant)
# Use candidate only after successful context exit.
```

This entry creates no transaction, endpoint, authentication or service. PostgreSQL
callers must explicitly supply the existing snapshot source; passing a concrete
`PostgresLedger` directly is rejected `storage_unavailable` without SQL or ambient
transaction adoption. There is no implicit READ COMMITTED fallback. A stable
synthetic InMemoryLedger can supply the scope
for offline inspection. The helper cannot certify caller authorization or storage
consistency; conflict detection does not replace the snapshot prerequisite.
Trusted custom sources own that same prerequisite. The concrete-ledger guard is
not a security boundary against proxies disguising an inconsistent source.

The immutable result preserves the exact admitted Definition and complete
`AsOfDiagnostic`. Each stored binding includes its original `BindingCandidate`,
`record_state = stored`, all `referencing_versions`, and the subset
`publication_candidates` eligible at the requested instant. Both lists contain
exact Version Keys in the existing ascending Version order. Stored bindings retain
inventory order by identity/revision. Matching mappings retain their original
claims and order. Versions with no recorded binding are separately listed as
`versions_without_binding_record`; no binding is invented for offline replay.

The join uses complete binding Keys, including revision. Different revisions of
one identity stay separate. A referenced binding must appear in the complete
inventory with a mapping to the exact Definition, otherwise the whole call fails
`lineage_missing_or_mismatched`. Complete exact readbacks are compared across both
inspections, including Source and Definition records. A changed readback fails
with the same classification. Neither check can detect every inconsistent view
or prove that an unreferenced binding/Version was omitted by a faulty producer.

This supplies independently falsifiable association information: operators can
distinguish stored-only bindings, bindings referenced by unpublished/future/
withdrawn/superseded Versions, and bindings referenced by one or multiple eligible
Publication candidates. Standalone inspections can succeed with an omitted
inventory entry; the combined association gate rejects that counterexample.
No latest revision, Version or provider is selected; no coverage is combined.

Publication eligibility remains the existing three-clock and terminal-decision
semantics. One candidate never proves an exact content read or current availability.
Binding activation, executability, source health, freshness, external admission,
observed finite coverage, quality, content integrity, durability, replayability
and current availability are explicitly `unobservable`. Recorded finite scope,
completeness, evidence requirements and Source contracts remain metadata claims.
No content, receipt, controlled reference or Publication reason is projected.

One fresh producer session and one application budget cover BOTH inspections,
repeated exact readbacks and final association metadata accounting. Existing fixed
row, serialized payload/aggregate bytes, JSON depth, memory-scan slots, resolution,
lineage, metadata/text/collection/depth limits apply without overrides. Repeated
reads count again; child projections and the final projection count cumulatively.
Some individually admissible queries therefore fail when combined. All work and
final results fail closed, without truncation or partial success. Existing stable
errors are preserved, including operator-only `operator_query_budget_exceeded`.
The public `mdf.consumer.read.v1`, its parser/input semantics, Canonical models,
storage schema and two admitted Definitions are unchanged.

Offline tests use synthetic fixtures and SQL/transaction doubles only. They cover
exact revision joins and reverse references, stored-only and unbound cases,
Publication states/time boundaries, order, redaction, unknown states, omitted
inventory and changing readbacks, corruption, shared budgets and failed snapshot
exit. They do not qualify real server isolation, transport or physical resources.
**B-END-TO-END-READ-CAPABILITY remains STOP / NOT PROVEN.** Host QA and fresh
independent revision-bound review are the next gates; conditional Git/CI delivery
requires separate Product Control authority. This is an uncommitted candidate,
not deployment, Source admission/activation, MBO adoption or whole-P6 completion.
