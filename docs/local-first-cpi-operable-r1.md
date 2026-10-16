# BLS CPI local-first nonproduction manual job

This candidate composes the existing direct BLS v2 executor, protected Garage
materialization, canonical kernel/ledger, independent exact/as-of consumer reads
and `mdf.consumer.read.v1`. It supplies a restricted **single-host file journal**.
It does **not** implement PostgreSQL job state. Canonical Version, lineage,
content and Publication continue to use the existing ledger; the newly admitted
host namespace is exactly `mdf_local_first_cpi_operable_r1`.

The executable target is the existing September 2026 reference contract and its
prebound official slot `CPI/2026-09/2026-10-14T12:30:00Z`. August and September
remain the finite read catalog. There is no automatic calendar discovery,
scheduler, listener, SourceBinding activation or provider fallback. Manual
execution after that slot does not reopen the old automatic release runner's
window or claim an on-time capture.

The four series are `CUSR0000SA0`, `CUUR0000SA0`, `CUSR0000SA0L1E` and
`CUUR0000SA0L1E`. The four output measures are headline/core month-over-month and
year-over-year percentages from BLS provider calculations. Source-original raw
and projected semantic material have separate grades. Provider actual release
time, revision and vintage remain undeclared by this adapter. The prebound
calendar slot is evidence of an official schedule, not a claim about an actual
response's release timestamp. No later official slot is currently proved here.

## Manual entry and owner seams

The single action entry is `CpiLocalJob.run`. Register an immutable target first
with `submit`, which writes pending state and the budget before execution:

```python
from pathlib import Path
from market_data_foundation.historical.cpi_job_journal import FileJobJournal
from market_data_foundation.historical.cpi_local_job import (
    CONTRACT,
    QUALITY,
    CpiJobSpec,
    CpiLocalJob,
    DirectCpiAcquirer,
)
from market_data_foundation.historical.bls_cpi_direct import (
    MAX_RESPONSE_BYTES,
    SERIES_IDS,
)
from market_data_foundation.historical.bls_cpi_direct_live import TIMEOUT_SECONDS
from market_data_foundation.historical.bls_cpi_release_runner import SLOT_IDENTITY

spec = CpiJobSpec(
    definition="mdf.macro.bls.cpi.headlines",
    definition_revision="p3-r1",
    series=SERIES_IDS,
    reference="2026-09",
    source_identity=CONTRACT.source_identity,
    source_revision=CONTRACT.contract_revision,
    provider_revision="not_declared",
    completeness=CONTRACT.completeness,
    quality=QUALITY,
    release_slot=SLOT_IDENTITY,
    mode="acquire",
    max_provider_requests=1,
    max_response_bytes=MAX_RESPONSE_BYTES,
    timeout_seconds=TIMEOUT_SECONDS,
    effect_domain="owner_nonproduction",  # synthetic for deterministic fixtures
)
journal = FileJobJournal(Path("artifacts/cpi-local-first-r1/jobs"))
job = CpiLocalJob(
    journal,
    clock,
    kernel,
    material_store,
    writer_protection,
    independent_readback_context,
    authoritative_commit,
)
job.submit("owner-chosen-unique-id", spec)
acquirer = DirectCpiAcquirer(transport, secret_path, artifact_root, clock)
result = job.run(
    "owner-chosen-unique-id",
    enabled=True,
    preflight=owner_nonproduction_preflight,
    acquirer=acquirer,
)
```

Variables supplied by the owner in this example are deliberately not initialized
by the module. `clock` returns aware actual times. `preflight(spec)` must raise
on rejection and perform only authorized read-only checks; no provider execution
belongs inside it. It must check active authority, the actual nonproduction PG
capability/namespace, Garage `mdf-artifacts` data role, budget and selected slot.
It is not a caller-provided boolean used to qualify Publication. Ordinary kernel
qualification and exact independent readback are mandatory after it.

Host bindings use the existing `PostgresLedger`, `CanonicalKernel`,
`PostgresAdvisoryWriterProtection`, `GarageMaterialStore` and, only for a separately
opted-in source request, `ExactOneHttpsTransport`. The PG writer is checked against
the dedicated schema and `192.168.10.4`. Provisioning that schema is a separate
explicit host action using the existing ledger API. Supply the writer's actual
`connection.commit`; the existing ledger owns the Publication commit on an idle
writer connection. Do not substitute a no-op commit for a real PG writer.
`independent_readback_context()` must own/close another connection and provide a
read-only consistent ledger snapshot from that same namespace. It may not return
the writer ledger. The code checks distinct ledger/PG connection objects and
the admitted host/namespace; physical sessions, database, role and snapshot
isolation must also be checked by the host. No module
constructs PG, S3 or HTTPS clients implicitly.

The writer connection must be exclusively owned for the complete job attempt;
the `PostgresAdvisoryWriterProtection` must use that same connection. Before
calling preflight, after preflight, before acquisition, at the provider request
boundary and before materialization, the job checks the dedicated host/schema,
`owner_nonproduction` domain, open connection and actual psycopg
`TransactionStatus.IDLE`. An active, failed, unknown or uninspectable transaction
is rejected without commit, rollback, close, protection acquisition or store
calls by the job. With the required exclusive connection/call sequence, the job
cleans up only its own core transaction.

Ticket `assert_held`, `release` and `mark_outcome_unknown` first inspect the
bound material identity, job/ticket mapping, live per-connection owner claim,
ledger/protection/connection objects, libpq `pgconn` object and backend PID,
plus held/valid state. This inspection issues no SQL, commit, rollback or close.
It remains mandatory throughout materialization. Only a matching attached core
ticket may accept `INTRANS` or `INERROR` for that job's own transaction handling;
acquisition, detached tickets, readback and recovery still require `IDLE`.
`ACTIVE`, `UNKNOWN`, missing metadata or any detected ownership drift permanently
revoke the owner. Even restoring the old identities or reporting IDLE cannot
revive it. The job must not enter the inner protection after that rejection;
uncertain locks/sessions are left for owner-host isolation, never forced cleanup.

PostgreSQL transaction status does not identify the application caller that
started work. These checks cannot prevent an arbitrary in-process caller that
already holds the very same PG connection from writing concurrently while the
core is active. The host must keep the writer connection private and serialize
its complete call sequence, including injected callbacks; stores and readback
must not use that writer to start unrelated transactions. This is an owner
binding and calling convention, not a PostgreSQL isolation guarantee.

Even a read-only preflight SQL query can leave the writer `INTRANS`. The host
must start its probes only from the checked idle session, isolate its own
read-only transaction and explicitly finish that scope before returning, for
example with `with connection.transaction():` and `SET TRANSACTION READ ONLY`.
The host must not unconditionally roll back a connection supplied by a caller.
Leaving a probe transaction open produces `preflight_rejected` before any source
request or Garage operation. These checks do not authenticate a caller's domain
label or replace the host's authority and capability checks.

The `effect_domain` label is caller-declared, not host authentication or proof.
Only synthetic transports/stores/ledgers were exercised in the implementation
workspace. Host positive source/PG/Garage effects require separate verification.
Read real credentials in place only at the opted-in edge; do not copy or print
them. Put raw responses, journal, observations and private proof under ignored
`artifacts/` or the owner-local transaction directory. No runtime material belongs
in Git.

For retained input, register `mode="retained"`, `max_provider_requests=0` and
`expected_raw_sha256` (the exact retained raw digest). Pass a
`LiveExecutionCandidate` through `retained=...` without an acquirer. Construct it
from the retained original `DirectCpiLiveEvidence` and the existing typed plan
builder, preserving original acquisition identity/times/receipt and release
causation. Do not fabricate a fresh acquisition or relabel synthetic evidence as
live. Both paths reconstruct and compare the whole typed plan before storage.
Caller-supplied quality, durability or Publication metadata cannot promote raw.

## Journal and recovery contract

Use **one** owner-only trusted directory on durable local disk for this product
instance. SQLite FULL synchronous transactions persist intent and history;
nonblocking per-job `flock` excludes another local executor/reconciler. These
locks are not cross-host leases and cannot coordinate independent journal copies.
Do not delete, copy into a new active instance, or reset the journal to bypass
range reservations. The host must qualify filesystem durability and permissions.

Each immutable job has zero or one attempt. Job IDs are lowercase alphanumeric
with hyphens, at most 64 characters; attempt identity is `<job>-a1`. The exact
Definition/reference/series/source contract/provider-revision range is unique.
A same-ID identical submission or terminal run returns the recorded result;
a different job for that range is refused, even with a different budget/mode.
An unsupported source contract or provider vintage is rejected before IO. There
is no automatic retry or new revision invention; a failed range stays reserved.
This restricted first slice does not support repeated refresh of that range.

The journal records pending → running → succeeded/failed/cancelled/outcome_unknown,
stage, start/end, requested cancellation, effect intent/observations, raw identity,
recovery plan, classifications, append-only history and a separately retained
last-success observation. Upper bounds (`*_possible`) are explicitly distinguished
from returned operations/confirmed readback. The provider budget is one logical Source request. Its wire exchange follows
the canonical MDF Source request policy: at most three transport attempts for
transient network failures/408/429/5xx with 1s/2s backoff; redirects and semantic
failures are terminal. The raw-create API, canonical handoff and Publication are each bounded
to one call per attempt. The existing Garage adapter can issue preliminary GET,
one ordinary PUT and verification GET; successful job readback adds a GET. This
is MDF writer protection, not S3 Object Lock/versioning. Reconciliation has no
PUT, ledger mutation or provider callback.

`journal.cancel(job_id, at)` requests cooperative stopping. A pending job or a
job stopped before storage can finish cancelled. Cancellation at/after raw or
canonical intent conservatively leaves outcome_unknown for reconciliation. It
cannot undo an already committed Publication; a late cancellation flag may
coexist with a verified success. Running cancellation checks occur before source,
before storage and at raw/commit checkpoints.

On a running job after interruption, ordinary `run` refuses re-execution. After
the previous executor/session is confirmed gone, call explicitly:

```python
observed = job.reconcile("owner-chosen-unique-id", enabled=True)
```

The reconciler first obtains the local executor lock. Existing in-process core
unknown handles retain material protection until the core's exact reconciliation
releases it. Before changing a running journal entry or entering any protection
or core reconciliation, it checks the active spec and the same idle PG writer
requirements as `run`. Rejection leaves the caller's transaction and journal
history intact. An in-process PG handle additionally requires its original job,
ledger, protection object, connection, libpq session, backend PID and unreleased
ticket. Detached ticket queries and release repeat these checks, including after
readback callbacks. A detected ownership gap revokes that handle even if the
connection later reports IDLE. Successful execution and core reconciliation
also retain this session binding across the release/reacquire gap.
A per-connection claim prevents another engine from reentering a held or
uncertain ticket, including after the original engine has been collected.

A fresh process uses the injected protected writer to reacquire the
same raw identity; a still-live PG lock owner causes unresolved status. Do not
force-steal locks. Loss of a session is a host fact to establish, not a timeout
assumption in this code. A rejected old handle requires independent observation
with a new controlled writer/session after the host establishes the old owner's
loss; the job never cleans up or silently rebinds the old writer.

Recovery reads the persisted expected graph, exact raw bytes, lineage, version,
content and Publication occurrence, then uses canonical exact/as-of and wire
reads. Raw-only or version-only presence yields `confirmed_not_published`, with
raw/version observations, and performs no publication. No durable plan, read
failure, mismatched metadata, lost protection, unavailable raw or rejected
consumer read yields unresolved. A matching Publication and all independent
reads yield `confirmed_published`. The attempt's outcome_unknown remains
monotone; reconciliation is an additional observation. Later observation failure
cannot erase the recorded last-success/history. If the journal itself is
unavailable, fail closed and reconcile after restoring journal access; never
retry the external action based on an exception or raw object alone.

## Independent reads and operator status

Existing `read_exact_version`, `read_as_of` and `consumer_wire.handle_request`
remain unchanged, including both admitted Definitions and all wire v1 fields.
Use a local read-only PG ledger; these readers have no provider/journal hooks.
The optional `read_local_cpi(ledger, reference=..., as_of=...,
version_identity=...)` adds a named reference/four-measure/grade guard over those
same reads. It refuses stale/mismatched reference, partial coverage and unknown
references. This is separate from wire v1, which has no expected-reference field.
After canonical eligibility, it decodes the direct CPI content envelope with the
existing retained-handoff JSON decoder and passes `data` to the existing
`validate_cpi_projection`, with explicit `AUGUST_REFERENCE_SEMANTICS` or
`SEPTEMBER_REFERENCE_SEMANTICS` selected by the requested reference. Exactly four
measurements, reference/comparison periods, series, value/unit and availability
must satisfy that validator. Malformed envelopes or semantic disagreement fail
closed as `CpiReadRejected("cpi_payload_invalid_or_mismatched")`; underlying
messages and content values are not returned. Matching digests and claimed
coverage/grades alone cannot qualify a CPI read.

`local_cpi_status(journal, at=..., ledger=local_readonly_snapshot)` lists exact
eligible publications, coverage, quality, reference, known/decision/usable times,
last success and latest job/attempt/failure/raw locator observations. As-of
ambiguity is refused; there is no "latest wins" override. Two published versions
can therefore retain valid exact reads while as-of is ambiguous. No old
Publication or historical usable time is edited to resolve that ambiguity.
Each generic-readable exact publication retains `storage_state=canonical_readable`
and a separate `cpi_read_classification`, obtained through the same CPI wrapper
using that publication's single scope reference. Stored content with invalid CPI
semantics remains observable as saved canonical metadata, with its rejection
classification. The September as-of selection also uses the wrapper; rejection
leaves selected publication, selected coverage and selected quality empty.

The journal-only CLI creates no clients, files or job updates:

```sh
uv run --offline python -m market_data_foundation.historical.cpi_local_status \
  --journal artifacts/cpi-local-first-r1/jobs
```

It reports canonical availability `not_checked`. Supply a read-only local PG
ledger to the Python status function for authoritative read eligibility. Raw
locator state is a dated journal observation, not a fresh S3 probe. Freshness
uses reference, prebound official schedule evidence, actual acquisition-known
and Publication-usable times, latest successful job/retrieval, and a null next
proved slot. It never invents provider release/vintage or uses TTL as release
semantics.

## Verification boundaries

`tests/test_cpi_local_job.py` uses typed plans, synthetic BLS transport/key,
in-memory stores/ledgers and the real Garage adapter over a fake wire. It covers
the closed loop, range/identity/concurrent rejection, revision refusal,
cooperative cancellation, partial source rejection, raw/ledger failure,
acknowledgement loss, protection gaps, interruption/restart, independent exact
readback, invalid/missing metadata, future-as-of isolation, retained evidence,
ambiguity, last-success retention and status without write/provider effects.
A subprocess exits after a fake Publication, and another subprocess restores
isolated fake snapshots and reconciles through the persistent file journal.
This proves local journal/process behavior, not PG/S3 process durability.

P1/P2 correction regressions use an in-memory PG connection with the actual
psycopg transaction-status enum and the existing advisory protection over a fake
canonical ledger. They preserve existing ambient work with zero commit,
rollback, close, provider and Garage calls; reject unclean preflight probes and
transaction-state changes before requests/materialization; and exercise an idle
writer's synthetic closed loop with an isolated preflight. Separate canonical
graphs have matching content/projection digests and successful generic reads,
but empty measurements, incorrect reference/series/value/unit/availability or
malformed envelopes. CPI reads and status reject them; valid August and
September projections pass their own explicit reference checks. These are
deterministic regressions, not a professional review result or real-effect
acceptance.

The remaining reconciliation P1 was reproduced before correction with the
actual PG protection over a fake connection: INTRANS discarded caller input,
rolled back once, committed twice and returned `confirmed_not_published`;
INERROR also discarded input. Added regressions reject these writers before
journal transitions, SQL or store calls, preserve input, and record zero
commit/rollback/close, source and material-store effects. They also exercise
unchanged IDLE recovery, in-process unknown ticket release, session/owner/lock
loss, callback contamination and the release/reacquire gap. Independent fake
snapshots and a newly opened journal verify publication/absence observations
without another source request, PUT or Publication. These are control-flow
models, not real PostgreSQL isolation or durability evidence. The pre-correction
negative output is under ignored
`artifacts/cpi-local-first-r1-reconcile-correction/`.

The prior reconciliation correction used uv-managed Python 3.14.8 with locked,
offline dependencies: all 130 focused cases passed, including the original 45
and the prior R2 corrections. Ruff, format, repository hygiene (including the
seven candidate paths) and diff checks passed. The final full deterministic
suite returned **NONPASS**: 2722 passed, four failed, 17 deselected and three
warnings. All four failures were the existing `tests/test_tdx_bounds.py` ptrace
cases failing in subprocess `preexec_fn` under the Codex sandbox; an isolated
`PTRACE_TRACEME` diagnostic returned EPERM. Those security tests were unchanged.
The final full-suite output is retained under ignored
`artifacts/cpi-local-first-r1-reconcile-correction/full-pytest-final.log`.

The materialization correction first reproduced the new R3 P1 against the
unchanged R3 source, with a valid IDLE session and acquired job ticket. A retained
store callback swapped both `pgconn` and backend PID, left caller work in either
INTRANS or INERROR, then raised. In both states R3 discarded that work and entered
inner release once, with one rollback, one commit and one SQL statement after
the swap. The same regression after correction preserves the work, enters no
inner release and records zero SQL/commit/rollback/close after the swap. Both
versions return outcome_unknown without Publication; that state alone never
proved safe cleanup. The original source and before/after output are retained
under ignored `artifacts/cpi-local-first-r1-materialization-correction/`
(`r3-original/`, `r3-before.log`, `r4-after.log`).

Additional regressions cover all three ticket operations in attached and
detached phases, individual identity/claim/job/protection drift, permanent
revocation, ACTIVE/UNKNOWN rejection, returning and failing store/commit/
Publication callbacks, and inner protection failures. Same-session core writes
exercise IDLE → INTRANS → commit and INTRANS/INERROR → rollback cleanup. Commit
acknowledgement failures can mark the held ticket unknown without SQL during
core execution; if the transaction remains non-idle after return, detached
recovery refuses cleanup and leaves host isolation necessary. Publication
transaction rollback followed by observation-only recovery remains possible.
These are deterministic transaction-state models, not real PG transactions.
All prior R3 test functions/classes remain unchanged, retaining the run ambient,
payload semantics and reconcile regressions. The uv-managed Python 3.14.8 focused
suite passed all 259 cases (130 retained and 129 new), with output in
`artifacts/cpi-local-first-r1-materialization-correction/focused-final.log`.
Ruff, format and repository hygiene, including all seven candidate paths,
passed. This correction changes only the local job module, its tests and this
document relative to R3; the general P4 material store and P6 consumer are
unchanged. Self-check results do not replace independent professional review.
The full deterministic run returned **NONPASS**: 2851 passed, four failed,
17 deselected and three warnings. The four unchanged `tests/test_tdx_bounds.py`
cases failed in subprocess `preexec_fn`, matching the previously recorded
sandbox ptrace failures. The complete output is retained in
`artifacts/cpi-local-first-r1-materialization-correction/full-pytest.log`;
these failures were neither skipped nor converted to PASS. A separate local
`PTRACE_TRACEME` diagnostic returned `-1` / `EPERM` (errno 1), retained in that
directory's `ptrace-diagnostic.log`.

The owner host still needs to verify actual dedicated PG/Garage role/namespace,
server transactions/advisory sessions, independent connection raw/lineage/read
proof, retained/live accounting and a separately bounded real source request.
The implementation thread must not run these private/live effects. Professional
review, Git/CI and product acceptance remain independent gates. No production,
HA, continuous scheduling, full history, adoption or release claim follows.
