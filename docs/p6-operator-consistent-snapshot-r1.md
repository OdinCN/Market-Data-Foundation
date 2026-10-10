# P6 Operator consistent PostgreSQL snapshot candidate

`PostgresLedger.operator_snapshot()` supplies an explicit, synchronous, in-process
read boundary for the existing two Operator inspections:

```python
with ledger.operator_snapshot() as snapshot:
    asof = inspect_as_of(snapshot, definition, as_of=instant)
    bindings = inspect_binding_candidates(snapshot, definition)
# Use results only after successful context exit.
```

The caller must already be an authorized operator and exclusively hold the
connection for the whole scope, including setup and cleanup. No other thread,
task or caller may use the original connection, start driver contexts, change
transaction settings, or issue BEGIN/COMMIT/ROLLBACK during that scope. This is
not a connection pool or a concurrency coordinator. Nested snapshot contexts
are rejected without touching the outer transaction.

Entry requires a confirmed open, IDLE connection and no psycopg transaction,
pipeline or two-phase context. Unknown/missing bookkeeping fails closed. The
implementation relies on the existing pinned psycopg 3.3.6 internal bookkeeping
(`_num_transactions`, `_pipeline`, `_tpc`); a dependency change requires renewed
qualification. Ambient INTRANS/INERROR/ACTIVE/UNKNOWN states are rejected before
SQL, autocommit mutation, rollback or close. Even an IDLE connection within a
driver transaction context is rejected. Caller work is never adopted.
The original autocommit value must be read successfully as a boolean before
admission. Because psycopg 3.3.6 `close()` can return a connection to its pool,
entry also requires no pool or explicitly `pool.close_returns is False`.
Returning or unknown pool policies are rejected with the same zero side effects;
the pool policy must remain unchanged throughout the exclusively held scope.

The boundary temporarily enables connection-local autocommit to prevent an
implicit BEGIN, then sends exactly
`BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY`. It confirms INTRANS and reads
back `transaction_isolation = repeatable read` and `transaction_read_only = on`
before exposing the read source. A default READ COMMITTED connection is therefore
not silently reused at its default isolation. Transaction characteristics are
specified for this transaction only; session isolation/read-only defaults are
not modified. No SERIALIZABLE or unverified fallback is accepted.

READ COMMITTED permits each statement to obtain a new view even inside one
transaction. The requested PostgreSQL REPEATABLE READ transaction is intended to
hold the view established by the first SELECT for subsequent enumeration, exact
record readbacks, histories and lineage reads. Both inspections can share that
transaction; each invocation still creates a fresh producer session and retains
its existing producer/application budgets. The inspection implementations,
successful content, ordering, error vocabulary, accepted Definitions and
SourceBinding `unobservable` states are unchanged. The legacy `operator_reads()`
entry still requires caller-supplied consistency; it is not upgraded implicitly.

The yielded source exposes the Operator capability rather than the ledger's
publication, content or mutation APIs. Each producer execution checks the scope
is active and the connection remains INTRANS/unmanaged. Escaped sources and
sessions fail `storage_unavailable` after exit, including inside a later scope.
Exclusive use remains a prerequisite: this guard cannot detect a caller secretly
ending and replacing the transaction on the original connection while leaving
its status INTRANS. The source is not a security boundary for arbitrary SQL.

Every exit invalidates the source. An owned INTRANS/INERROR transaction is rolled
back on both success and failure; no COMMIT is issued. Confirmed IDLE is required
before restoring the original autocommit value. A caught SQL failure leaving
INERROR also prevents successful context exit. Original inspection/caller errors
survive successful cleanup. Setup, uncertain transaction state, rollback or
restore failure returns only `storage_unavailable`, with no chained raw driver
or caller exception. An unconfirmed cleanup causes a close attempt only after
reconfirming the non-returning pool policy. If that policy becomes returning or
unknown, close is not called. If close is withheld or fails, physical cleanup is
unknown; the caller must discard the unusable handle and must not return it to a
pool.
There is no retry or weaker read path.

Offline tests use synthetic canonical fixtures and transaction/SQL doubles.
They exercise both Definitions and both inspections, fresh budgets, shared
modeled view identity across statements/queries, a modeled writer between reads,
new views on a later scope, a READ COMMITTED-style negative control, ambient and
unknown connection rejection, stale capabilities, original budget/lineage errors,
body exceptions/interrupts, failed setup and cleanup, and failed close. They
also inject first-read autocommit errors and returning/unknown pool policies,
asserting rejection without SQL, setting changes, rollback, close or pool return.
The installed driver `Connection.close()` is exercised on in-memory handles to
reproduce the pool-return counterexample and verify non-returning cleanup. They
neither execute PostgreSQL nor prove real isolation, server read-only enforcement,
driver transport behavior or concurrent writer behavior. Those remain **unknown**.

No schema, Publication decision, acquisition, provider, SourceBinding activation,
public `mdf.consumer.read.v1`, authentication, service or deployment is added.
This candidate does not establish CPU, TOAST, I/O, heap, lock-wait, latency or
production SLA bounds. **B-END-TO-END-READ-CAPABILITY remains STOP / NOT PROVEN.**
MDF Product Control owns the subsequent fresh independent review, separately
authorized isolated synthetic devPG proof, Git delivery and exact-head CI gates.
