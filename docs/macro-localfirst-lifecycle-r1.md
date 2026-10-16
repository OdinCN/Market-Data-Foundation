# CPI/PPI local-first lifecycle R1

This bounded Macro layer covers only the delivered BLS CPI headlines and PPI
final-demand contracts. It does not admit additional sources or references.

`MacroJobJournal` owns the single-host durable job/attempt history, cancellation,
scope reservations and monotone terminal states. The legacy `FileJobJournal`
import and `cpi-jobs.sqlite3` filename remain compatible, including existing PPI
journals. New explicit users may choose `MacroJobJournal` and its
`macro-jobs.sqlite3` filename. Read-only journal construction never creates files.

Both existing engines use shared request metering, typed plan serialization,
protected exact raw/Version/Publication observation and retained repair. Source
adapters still validate their own typed plans, release/reference/measurement,
revision, time and lineage. They retain their existing materialization and PG
session protection rules; source admission and the generic consumer wire remain
unchanged. This is not a provider-selection or Demand/Ensure framework.

Execution remains explicit: submit a family-specific spec, then call `run` with
`enabled=True`, an explicit preflight and either the exact family acquirer or a
typed retained candidate. CPI and PPI both support `mode="retained"`,
`max_provider_requests=0` and a required `expected_raw_sha256`. Retained mode
preserves the original acquisition identity and timestamps, never presenting a
local replay as a new Provider request. Provider budgets remain one for acquire
and zero for retained. Raw, ledger handoff and publication budgets remain one.

Raw is verified before canonical handoff; Publication is last. A raw or Version
alone is not a successful publication. Failure after a possible canonical effect
becomes `outcome_unknown`. Interrupted `running` jobs require explicit reconcile;
they are never silently retried. Reconcile holds material protection, opens a new
independent local ledger snapshot and checks the source-specific graph. It does
not acquire from a Provider, PUT raw, or retry canonical writes. A confirmed
observation is recorded separately: the unknown attempt is never rewritten to
`succeeded`. PG advisory-lock housekeeping remains part of writer protection;
reconcile is not a zero-SQL operation.

For an explicit repair, call `engine.repair(old_id, new_id, enabled=True)`. This
only registers a new pending job; execution still requires an explicit retained
`run`. The predecessor must contain a validated raw identity and plan and be:

- failed/cancelled before any possible canonical effect; or
- outcome-unknown with a confirmed-not-published, raw-verified reconciliation.

Repair preserves the predecessor and reserves at most one successor for it.
The replacement has its own job ID, attempt ID and history, zero Provider budget,
the original target, raw digest and exact acquisition plan. It must run with the
same typed retained candidate. A mismatched plan, unresolved unknown outcome,
already published predecessor or branched repair is rejected before storage.
There is no automatic retry, refresh, release scheduling or repair loop. Failed
preflight/acquisition without a saved valid plan cannot use this retained repair.

`read_local_cpi` and `read_local_ppi` remain local exact/as-of reads with no client
construction, Provider, journal or storage-write dependency. CPI keeps its four
headline/core measurements and admitted reference guards. PPI keeps its SA/NSA
final-demand series, distinct provider-returned and derived measurements,
embedded original bytes and complete lineage validation. Local processing time
does not replace release, acquisition, known, usable or revision semantics.

The journal still requires a trusted owner-only local filesystem and explicit
single-host execution. Existing dedicated nonproduction PG schema restrictions
remain in force. This does not establish distributed job state, leases, HA,
production readiness, MBO admission, a Scheduler or an Operator WebUI.
