# PCE official monthly percent persistence

PCE revisions store `bea_pct_1m NUMERIC`, the published BEA T20807 monthly
percent change. `"0.3"` means **0.3 percent**, not a ratio. Missing official values
remain SQL NULL and serialize as `bea_pct_1m: null`; the provider calculations
omit the key. `bea_pct_1m` NULL means BEA did not supply the value or historical
official data has not yet been collected; it never means zero.
`coverage_complete` describes index coverage only, not official percent coverage.
PCE no longer has the four empty `bls_pct_*` columns or BLS unit
claims. CPI and PPI retain their existing columns and interfaces.

The API keeps derived `mom_pct`, `yoy_pct` and `change_*m_ratio` unchanged.
`units.bea_pct_1m` is `percent`, separately from derived percent and ratio fields.
Official values and notes participate in normal immutable revision comparison;
an official correction can produce a revision when the index is unchanged.
Decimal scale alone does not create a revision.

An unchanged index whose refresh omits a previously known official value fails
with `pce_timeseries_official_percent_missing_unchanged_index`. The entire save
transaction rolls back. This avoids silently erasing evidence or attributing
an old official value to a new response. A changed index may have NULL official
percent; prior official values are never copied across changed indices.

Each PCE five-year source window makes two HTTP requests. Shared preflight
budgets count both across all gaps, `provider_requests` sums each batch's
`request_count` (default one for CPI/PPI), and attempts retain actual retry counts.

# Operator-controlled migration

`migrate_pce_percent_schema` is also exported by
`scripts/pce_timeseries_percent_migrate.py`. It never connects, reads credentials,
commits, creates roles, or runs during service startup. The parent/operator owns
backup, stopping writers, the connection, transaction and real database checks.
Use an account that can preserve existing view owners and grant permissions.

The function requires an already open psycopg transaction. It checks the exact
legacy revision/view shapes, validated PCE series constraint and identities.
Any non-NULL legacy BLS percent, including zero, is rejected. Preexisting official
JSON attributes are rejected rather than backfilled or exposed inconsistently.
Already migrated schemas are rejected by the legacy-shape guard.

All old revisions receive NULL official percent. No index, month, revision,
observation time, availability time, version status, evidence or raw batch link
is rewritten. Metadata compaction deletes only these six keys inside
`provider_extra.bea`: `SeriesCode`, `TableName`, `LineNumber`, `CL_UNIT`,
`UNIT_MULT`, `METRIC_NAME`, and only on exact verified values reconstructible
from the PCE contract and row. `TimePeriod`, `NoteRef`, `LineDescription`, nested
official notes, unknown keys, unknown or mismatched values and other
representations are preserved; uncertain cleanup is deferred.

Removing view columns requires dropping the rates view, then latest, and
recreating both. Every DROP uses RESTRICT. External dependents cause failure;
the caller must roll back on **any** error. Captured view privileges (including
column privileges on retained columns, PUBLIC and grant options), owners,
view options and comments are restored. Grants on removed columns cause rejection.
New default view ACLs are cleared before replay so access does not broaden.
Table grants and schema USAGE, including existing `odin_dba` access, remain
in place. No role is created. The returned dictionary reports preserved revision
counts, grant counts, metadata compaction and zero backfills; it does not claim
that the caller committed.

These examples use an existing parent-owned `conn`; they do not open connections.
The `with` block must be the caller's outer transaction (start from IDLE).

```python
import psycopg
from market_data_foundation.historical.pce_timeseries_migration import (
    migrate_pce_percent_schema,
)

# Rehearse against the real target and deliberately roll back every change.
with conn.transaction():
    summary = migrate_pce_percent_schema(conn)
    # Parent inspects summary and performs read-only checks here.
    raise psycopg.Rollback()

# Apply: only successful exit from the caller's outer context commits.
with conn.transaction():
    summary = migrate_pce_percent_schema(conn)
    # Parent verifies schema, preserved history, views and odin_dba privileges.
```

An explicitly authorized clone rehearsal is supported only under the fixed
prefix below, with a lowercase suffix of at most 20 characters, and the same
PCE shape, validated constraint and content checks. CPI, PPI, arbitrary schema
names and clone names without explicit opt-in are rejected before any SQL.

```python
with conn.transaction():
    summary = migrate_pce_percent_schema(
        conn,
        schema="mdf_pce_timeseries_dev_r1_rehearsal_parent",
        allow_rehearsal=True,
    )
    raise psycopg.Rollback()
```

Validation in this change uses fake PostgreSQL connections and synthetic batches
only. Actual DDL, ACL restoration and rollback require the parent's real apply
and rehearsal; no live database or provider execution is part of these tests.
