# BLS PPI final-demand local-first vertical

This T01 candidate is an explicitly operated, nonproduction vertical for the
September 2026 BLS Producer Price Index final-demand release. It is not a
scheduler, active SourceBinding, MBO admission, general macro lifecycle, or
Demand/Ensure platform.

## Source and semantics

The exact BLS API v2 request contains two series and one bounded year range:

- `WPSFD4`: Final demand, seasonally adjusted;
- `WPUFD4`: Final demand, not seasonally adjusted;
- `startyear=2025`, `endyear=2026`, `calculations=true`;
- target reference `2026-09`, with `2026-08` and `2025-09` comparison rows.

The admitted measurements are the seasonally adjusted index level and BLS
one-month percentage calculation, plus the not-seasonally-adjusted index level
and BLS 12-month percentage calculation. Units are `index_point` and `percent`;
the adapter does not assume or rewrite a base-period label. The official release
slot is `2026-10-15T12:30:00Z` (08:30 Eastern). Release schedule, reference,
obtained, frozen, decision, and usable times remain separate.

PPI observations can be revised during the three interim months and become
final with the fourth monthly release after initial publication. A row is called
`preliminary` only when the returned BLS footnote says so. Otherwise its state is
`not_declared_by_footnote`; a retrieval is never promoted to a provider vintage
or final observation by elapsed time. Source pointers and original footnotes are
retained with every projected measurement.

Primary references:

- <https://www.bls.gov/ppi/fd-id/ppi-final-demand-intermediate-demand-aggregation-indexes-by-title-and-series-id.htm>
- <https://www.bls.gov/schedule/2026/10_sched.htm>
- <https://www.bls.gov/ppi/notices/2022/ppi-to-publish-interim-index-values-to-3-decimal-places.htm>
- <https://www.bls.gov/opub/hom/ppi/calculation.htm>

## Explicit execution and local read

`bls_ppi_live.execute_live_candidate` is the sole source edge. It loads the BLS
registration key in process, makes exactly one bounded HTTPS request, refuses
redirects/retries/non-identity encoding, and stores raw source artifacts only
under ignored `artifacts/`. It never persists the registration key.

`ppi_materialization.materialize_persist_publish` verifies or creates the
content-addressed raw object in Garage before persisting the source, candidate
binding, acquisition, Version, content, and Publication in the dedicated
development PostgreSQL namespace `mdf_macro_bls_ppi_vertical_r1`. Publication is
last. Commit or Publication uncertainty retains writer protection and requires
exact authoritative readback; raw or Version presence never creates a missing
Publication. If the raw PUT outcome is unknown but exact GET readback succeeds,
the result and job journal record `unknown_reconciled_present`; they do not claim
that this attempt created the object.

`read_local_ppi` accepts only the exact PPI Definition/reference/coverage/grades
and reconstructs the complete expected source, binding, run, acquisition,
ORIGINAL/PROJECTED materials, Version, content, and Publication request from the
embedded raw bytes. Raw digest, size, and content-addressed locator must agree at
both materialization and read time. It creates no source, object-store, journal,
or PostgreSQL client. The PPI admission is private to this producer-local wrapper;
`mdf.consumer.read.v1` and its existing admitted Definition set are not expanded
by T01.

## Job and recovery boundary

`PpiLocalJob` accepts only `PpiJobJournal` and an exact `DirectPpiAcquirer`; the
shared CPI journal has no PPI resolution transition. The controller owns the
kernel, Garage store, writer protection, commit operation, and fresh-ledger
readback factory. For owner nonproduction execution it requires the dedicated
schema on `192.168.10.4`, the exact Garage adapter, same-connection PostgreSQL
advisory protection, and an idle writer before and after each boundary. A
submitted target has one attempt and one immutable range reservation. Budgets
are one provider request, one raw create, one ledger handoff, and one Publication.
The journal persists the complete encoded plan, raw identity, and PostgreSQL
writer backend identity, but never raw bytes, credentials, or exception messages.

Preflight rejection and cancellation before storage are terminal without storage
effects. A restarted running or outcome-unknown job cannot acquire or publish
again; `reconcile` only observes exact persisted identities and records
`confirmed_published`, `confirmed_not_published`, or `unresolved` while the job
state remains monotonically `outcome_unknown`. A fresh owner controller must use
a different PostgreSQL backend before reacquiring the exact raw lock; a
same-backend session cannot assert its predecessor is gone. The journal is a
qualified single-host local control, not PostgreSQL job state or a distributed
lease. A common multi-source lifecycle, repeated refresh, scheduler, and repair
actions remain T02 or later work and are not implemented here.
