# T09 release-aware Scheduler/Operator R3 bounded offline correction

This candidate is an explicit Python API, with two concrete retained handlers.
It does not start a service, activate schedules, construct a Source client or
connect to Garage/PostgreSQL. It is not T09 product completion or permission for
nonproduction live effects. All mutation methods default to `enabled=False` and
require the schedule's named owner. Owner names are assertions inside a trusted
local process, not authentication for a public API.

## Capability intake from the current code

The incoming worktree is based on `1bedf067ff7647c93b457ac1c082d7368520fc7a`.
T03–T08 transaction statuses were fresh-read as completed; that historical
acceptance does not qualify these new scheduler bytes or activate any Source.

| Existing capability | Actual boundary found | T09 scheduler support |
| --- | --- | --- |
| T02 CPI/PPI | `MacroJobJournal`, `RetainedRepair`, typed plans, request metering, exact observation, terminal unknown, local exact/as-of Read | Reused journal mechanics; CPI/PPI adapters unsupported here |
| T03 Employment | `EmploymentLocalJob`, retained-only `EmploymentJobSpec`; direct Source admission is a separate controlled boundary | Unsupported scheduler adapter |
| T04 PCE | `PceLocalJob`, retained-only spec and separate `bea_pce_boundary` consumed acquisition intent | Unsupported scheduler adapter |
| T05 Retail | `RetailLocalJob`, `DirectRetailAcquirer`, one logical request, original/derived control-group graph | Unsupported scheduler adapter |
| T06 FRED/ALFRED | `VintageLocalJob`, four original reads, multi-raw materialization, exact/as-of Read, gated repair/reconcile | `VintageReleaseHandler`, retained-only, fixed GDP+PAYEMS / 2025–2026 window |
| T07 FOMC | `StatementLocalJob`, two original reads, parser-bound official release, exact/as-of Read, gated repair/reconcile | `StatementReleaseHandler`, retained-only, `FOMC:STATEMENT:2026-09-16` |
| T08 ECB/Eurostat | `Acquisition` persistent seven logical slots; separate provider `Handoff`, three/four raw materials, irreversible consumed claims | Unsupported; needs a dedicated adapter preserving both claim protocols |
| Other runtime | `PeriodicWatchlistSession` and `runtime/tdx_watchlist.py` are bounded stock/watchlist execution | Not a Macro scheduler or a fallback |

No existing generic Macro scheduler was found. Supported means the named
**offline retained handler**, not live acquisition, future references, general
FOMC calendars, arbitrary series, or global SDMX. `capabilities()` distinguishes
unbound handlers from unsupported families; unknown family/reference is rejected.
No common Demand/Ensure layer or placeholder multi-source handler is introduced.

## Evidence and time

`ReleaseSchedule` is an immutable, owner-supplied input with an identity,
`evidence_id`, admitted family/reference, acquisition execution window, optional
source release instant, slot/attempt limits and freshness bound. It is not a
cron expression or an inferred release calendar. No default calendar is guessed.
Unknown window inputs may be registered for visibility, but cannot trigger.
Schedule changes need a new identity. Registration/resume never executes work.

For FOMC, the existing source parser rebuilds the calendar discovery and actual
statement HTML and validates the official date / release-time metadata. The
schedule's `source_release_at` must match that parsed instant; it must not be
later than the schedule window's start. Merely setting a timestamp on a
candidate does not pass the original parser/plan validation. Unknown or
mismatched official release blocks execution.

For FRED/ALFRED, the schedule is explicitly an **aggregator acquisition window**.
There is no official-release instant. `source_release_at` must remain `None`;
selected vintage dates stay date strings, never midnight release instants.
Both GDP and PAYEMS retain their provider metadata and observation references.
Sparse absent cells and explicit `.` cells remain distinct. An unknown official
release is displayed, but it cannot authorize official-release scheduling; only
the identified acquisition window authorizes this handler.

Both handlers reconstruct their actual source-specific plan before registration
and execution. Requested/obtained instants and raw digests come from retained
acquisition evidence. The exact readback supplies `known_at` from Publication
`decision_at` and `usable_at` from Publication `usable_at`. Schedule time, raw
presence, job finish time and official-release time never replace these clocks.
Backfilling old evidence publishes with the current decision/usable clocks; it
does not invent historical MDF availability.

## Durable operator protocol

`ReleaseJournal` reuses `MacroJobJournal`'s local SQLite synchronous durability
and nonblocking `flock`. Scheduler schedules/slots/audit are in
`macro-release-operator.sqlite3`; source-specific jobs/attempts/plans/raw
identities/history live in the original journal implementation beneath each
handler root and slot identity. Per-slot journals preserve the existing source
spec's fixed scope and same-target repair constraints without changing those
interfaces. A persistent family binding rejects handler-root drift.

Keep the entire directory tree on one trusted, owner-only, durable local
filesystem, e.g. `artifacts/t09-runtime/`. Preserve both operator and handler
journals across restart. Never move/reset them to gain another attempt. SQLite,
lock files, private evidence and raw carriers belong only in ignored runtime
paths. The existing ignore rules cover `artifacts/` and SQLite files.

1. Inject explicit offline kernel/store/protection/readback/commit/clock bindings
   into `StatementReleaseHandler` and/or `VintageReleaseHandler`. The kernel must
   use `InMemoryLedger`; actual Garage and PG protection bindings are rejected.
   Readback must return a separate offline ledger matching the current synchronous
   writer state. T09 checks base `InMemoryLedger` serialization and uses a fresh
   base-ledger copy for reads; reader methods cannot assert absence. Arbitrary Python hooks are a
   trusted capability boundary, not a network sandbox.
2. Construct `ReleaseOperator(ReleaseJournal(root), handlers, clock)` and register
   a `ReleaseSchedule` with `actor=spec.owner, enabled=True`. Handler roots must
   be distinct, stable namespaces. Source-specific finite references remain fixed.
3. `trigger(schedule_id, slot_id, retained_candidate, actor=..., enabled=True)`
   persists a pending slot. `action` is `manual`, `scheduled`, `refresh` or
   `backfill`. Even `scheduled` is an explicit call; there is no timer or loop.
4. `run(slot_id, retained_candidate, actor=..., enabled=True)` consumes the intent
   before entering the source engine and performs at most one attempt. The
   candidate fingerprint must match the registered evidence. This executes
   existing typed retained materialization, commit/publication and independent
   exact/as-of verification, using only injected fixture effects in this R1.
5. `pause` blocks new triggers/runs, and `resume` only removes that block.
   `cancel` prevents a pending execution and cooperatively flags the source job
   during a running execution. Cancellation cannot undo an already started
   materialization or claim that a possible publication did not happen.
6. `reconcile` explicitly observes the original job. It never invokes Source,
   PUT, graph persistence or publication. A consumed Scheduler claim whose
   source job is missing/pending remains unknown; it is never requeued.
7. `repair` registers at most one retained successor after the source engine has
   confirmed **not published** and verified **all original raw**. The exact
   predecessor plan/raw, zero Provider budget and source-specific journal rules
   still apply. Run the registered repair separately. Original terminal attempts
   remain unchanged. A crash during repair registration also consumes its claim.

Slot IDs, acquired evidence fingerprints and equal family acquisition-completion
instants cannot be reused across schedules, including cancelled/failed slots.
Moving raw artifact paths does not change acquisition identity. Repeated trigger
or run returns the existing slot; it performs no Provider, material or canonical
operation. Family-wide unresolved/active claims also block already-pending slots
at execution time. A live executor's slot lock cannot be stolen by reconcile.

`refresh` requires an existing verified success and a strictly newer acquisition.
`backfill` explicitly permits executing retained evidence after its identified
window has expired; original acquisition instants must still lie in that window.
It supports only the two fixed admitted source targets. The original engines
require exact == latest-as-of, so an acquisition older than an already newer
successful family publication is **unsupported and blocked before effects**.
This is not a historical-series backfill engine. Repair execution retains the
original action and schedule window; an expired ordinary window is not silently extended.

`deadline` is an inclusive **effect-start admission deadline**, not just a claim
cutoff. T09 checks the window before claim, at source Job preflight, and again
after candidate validation immediately before materialization intent. The new
optional `effect_preflight` hook leaves existing source callers compatible. A
manual/scheduled/refresh attempt reaching that final gate after deadline is
terminally rejected with no raw-create or Publication; consumed claims remain
consumed. Once admitted, that bounded materialization may finish after deadline;
this is not a per-write deadline or cancellation of an in-flight publication.
Only an explicitly registered `backfill` action bypasses the execution deadline,
including its retained repair. Its original acquisition still must be inside the
identified window; backfill cannot change official/known/usable clocks.

Automatic retry is zero. Per-schedule slots are bounded to 1–16 and per-slot
attempts to 1–2 (initial plus one explicit retained repair). Provider budget is
always zero. Per-attempt raw-create bounds are two for FOMC and four for vintage;
ledger handoff/publication bounds are one each, as enforced by the original
engines. Cancel/pause/restart never refill limits. Failures before a valid saved
plan or without verified raw cannot use retained repair.

## Read-only observations and recovery limits

`status(schedule_id, at=...)` uses only the Scheduler journal, including when
constructed with `ReleaseJournal(path, read_only=True)` and no handlers. It does
not initialize files, invoke handlers, refresh, reconcile or acquire a lock.
The original `read_local_statement` / `read_local_vintage` remain unchanged and
have no Scheduler dependency.

Status exposes immutable input, pause/cancel state, attempt job identities and
observations, budgets, last verified success, exact finite coverage,
source-release unknown, unresolved slots and classified alerts. Acquisition age
is computed from the original obtained instant; it is **not** a claim of global
release freshness. Vintage success additionally records selected vintages and
explicit unavailable members. Coverage is the last successful exact version's
finite scope, not a claim that all dates or future releases exist. Alerts are
local projections (`outcome_unknown`, unknown schedule time, stale acquisition,
missing coverage after deadline, operator attention), not outbound messages.

A `claimed` slot on restart is visibly unknown. It cannot run again even if no
source job was registered or the source job is still pending. If its original
engine already succeeded, explicit reconcile recovers its exact success evidence.
An engine terminal `outcome_unknown` remains unknown after confirmed publication;
last-success evidence and unknown-attempt history are separate. A synthetic
restart lacking original ticket-release proof stays unresolved, following the
source implementation. Fixture ledgers are in-memory; these tests qualify durable
control restart, not survival of a live canonical store or host power loss.

## Bounded recovery and family admission

P1-A: a separate reader is not itself freshness proof. For these offline handlers
the admitted authoritative store is the synchronous `InMemoryLedger` writer.
Every readback compares the reader's complete base serialization with that
writer, serves a new base-ledger copy, and verifies that the writer remains
unchanged through reader context exit. Stale or changing state stays unresolved,
including before original writer tickets are released. No external reader's
method or outcome string authorizes repair. Resolution also validates the exact
job/attempt, source plan, candidate fingerprint, expected/raw digests and sizes.
The saved Scheduler evidence binds job/attempt/plan/raw and authoritative state
digests. A negative outcome must prove both occurrence and version publication
absence. Reconcile invalidates its previous certificate before acquiring any
readback protection, so a concurrent family run cannot use an older resolution.
Repair invalidates the previous certificate before a fresh protected
source reconciliation, checks fresh authority before reservation, and rechecks
absence at source preflight and immediately before its first materialization.
Original plan/raw equality remains enforced by the source repair engine.

Retained R2 protection fix: both source LocalJobs durably record `readback_protection_pending`
**before** acquiring readback tickets. All release attempts preserve the exact
failed tickets and successful release progress, allowing only unreleased tickets
to be retried. Reconciliation cannot certify publication/absence while that
marker is pending. A restarted process missing handles stays quarantined, even
when publication is present. An original process may release its retained
handles, durably clear the marker, and repeat exact verification. Losing the
process does not manufacture synthetic lock-release proof. Legacy journals that
entered independent readback without a marker are conservatively quarantined;
there is no reset/bypass operation in this API.

Source Job terminal state and Scheduler resolution are separate: an original
`outcome_unknown` Job remains terminally unknown after verified reconciliation.
The Scheduler may record last-success and clear its unknown slot only after
publication **and protection resolution** have been verified. With protection
pending it remains unknown and blocks repair, new family triggers and pending
family executions. Existing publication evidence cannot erase a pending ticket.
A crash after actual release but before durable proof can also leave quarantine;
safety takes precedence over automatic recovery.

R3 moves recovery authority into both standalone LocalJobs, using the bounded
`local_job_recovery` helper. The original job executor proves its synchronous
attempt has returned. A process-local weak object registry binds the recorded
synthetic writer session to the original writer object: a copied/reconstructed
ledger, missing provenance, changed attempt or process restart cannot manufacture
that authority. The proof hashes job/attempt/plan/raw/writer-session plus the base
writer snapshot. Both reader entry and normal context exit must match that
snapshot before original materialization or failed-readback tickets are released.
Queries use a new base ledger, so overridden reader methods cannot invent absence.
The source validates the materialization handle against the original durable
plan. Source repair checks the bound fresh absence at reservation and again
immediately before materialization. An old R2 source certificate lacks this proof
and cannot authorize repair; an explicit fresh source reconcile is required.

PG recovery has **no admitted fresh committed operator capability in this
revision**. Neither a different writer PID, distinct read-only connection nor an
old Repeatable Read transaction establishes writer quiescence and fresh authority.
Both LocalJobs stop unresolved before entering that reader or releasing retained
protection. There is no PG fallback or mock-derived live qualification. This is
a conservative recovery STOP, not expanded T06/T07 execution authority; their
source parsing, materialization, formal FOMC release and vintage acquisition
semantics, finite coverage and Publication/Read contracts remain unchanged.

R3 P1-B coordinates recovery and source effects with a family-specific
nonblocking journal flock, shared by every schedule/operator using that journal.
Run consumes the claim and registers the source intent first, then takes family
admission, rechecks other family slots and holds admission through source run and
result observation. Both existing preflight callbacks recheck the family guard;
the final callback still checks the inclusive effect-start deadline. Reconcile
and repair take the same family lock before invalidating a certificate or
acquiring recovery protection. The lock order is slot, family, release-control
(if needed), source executor; the initial claim's release-control is released
before requesting family admission. No path waits on a busy executor.

If A reconciles after B's claim but before B takes admission, B sees A's unresolved
protection and becomes terminally unknown with zero new effects. If B already
holds admission, A is refused as busy **before** reconciliation begins, without
acquiring tickets or changing its earlier observation. A later explicit call may
reconcile; this is not an automatic retry. A failed B admission never returns its
claim to pending. Ordinary and exceptional exits release family admission, while
unresolved protection stays in the durable source/slot quarantine. On repeated
reconcile, an unresolved result clears the Scheduler last-success projection;
historical source success is not reused as a current protection certificate.

Offline source-engine restarts can recover only with the same admitted writer
object and durable release proof. Process restart loses that authority and
requires STOP. This protocol makes no persistence claim for the synthetic writer
or its protection tickets. Local flock tests prove ordinary exclusion/release on
the test filesystem, not crash consistency, distributed exclusion or real PG
advisory-lock recovery.

Deterministic negative/positive cases cover both stale snapshot timings, lying
reader methods, writer changes on reader exit, attempt/plan/raw mismatch, fresh
repair registration and execution checks, release failure during successful run
and during reconcile, repeated failure/reconcile, restart without handles,
legacy quarantine and eventual original-handle recovery. Both families test
claim-time-valid execution at deadline+1s and time advancing after candidate
validation, with explicit backfill positives.

The authority proof relies on this single-host trusted offline writer model.
It does not prove PostgreSQL committed snapshots, replicas, writer-session loss,
persistent/distributed protection, arbitrary out-of-band writers, process death
while holding real locks or host power-loss recovery. If live bindings cannot
provide fresh committed evidence and recoverable protection, that future
qualification must stop; the offline snapshot protocol cannot be substituted.
No dependency/baseline, Source/data-product expansion or service activation is
part of R3. Self-verification is not independent PASS or T09 COMPLETE.

## Deterministic validation and next gates

The tests inject synthetic source bodies and in-memory storage/ledgers, and
explicitly forbid sockets, psycopg connections and actual Garage methods.
No dependency or Python-baseline change is required. Hosted CI needs no private
service or credential. Using the repository's uv-managed Python 3.14.8 environment:

```bash
uv run --locked pytest -q tests/test_release_scheduler_r3.py
uv run --locked pytest -q tests/test_release_scheduler_r3.py tests/test_release_scheduler.py tests/test_macro_lifecycle.py tests/test_fomc_statement_local.py tests/test_fred_alfred_vintage_local.py
uv run --locked python scripts/check_repository_hygiene.py
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -m 'not live_provider and not postgres_integration'
```

Full result evidence is reported in the EO return; ignored logs are under
`artifacts/t09-offline-qa/`. This is engineering self-verification, not an
independent professional verdict. Next: revision-bound independent professional
review and Host full QA, then a separately admitted bounded nonproduction
qualification plan before implementing/enabling live Scheduler bindings. Actual
Provider/Garage/devPG execution, writer-session loss, live freshness/operability,
service lifecycle and activation remain unqualified here. Git integration,
exact-head Hosted CI and independent final Postflight remain subsequent gates.
No T10/T11, production, deployment, activation, adoption or trading is authorized.

## R3 offline replay evidence

The red replay reconstructs the R2 source files in `/tmp`, with all five relevant
source files verified against the R2 exact7 manifest (including their bytes and
SHA-256). Ten initial regression cases fail on that source revision. A separate
count replay actually runs the stale-readback repair on those bytes: FOMC
raw-create 2→4, Vintage 4→8, Publication 1→2 for both. On R3 the same replay stays
unresolved, keeps 2/4 original tickets held, permits no repair, and records
raw-create 2→2 / 4→4 and Publication 1→1. The R2 Host 5529 PASS is old evidence.

Current engineering checks use `UV_PROJECT_ENVIRONMENT=/srv/market-data-foundation/.venv`
and `UV_CACHE_DIR=/tmp/mdf-t09-r3-uv`, with `uv run --offline --locked --no-sync`:

- R3 regression file: **24 passed**. Covers standalone source calls, two stale
  snapshot timings, stale registered repair, writer provenance/attempt loss,
  missing PG recovery capability, both claim-before-reconcile interleavings,
  independent Operator/thread exclusion during actual raw-create, repeated
  protection failure and stale last-success removal.
- Focused command above (R3, scheduler, Macro lifecycle, both LocalJobs):
  **334 passed**.
- Adjacent `test_fomc_statement.py`, `test_fred_alfred_vintage.py`, both source
  `*_live.py` protocol fixture tests, `test_canonical_kernel.py`: **339 passed**.
- Additional ignored offline source-boundary exception replay: **2 passed**;
  an unexpected source exception releases family admission to another Journal
  instance, leaves the slot unknown, and cannot replay its consumed claim.
- `ruff check .`, `ruff format --check .` (**248 files**), `git diff --check`,
  repository hygiene (**257 tracked files**) and all-candidate hygiene
  (**11 paths**) pass. The ten candidate Python files are hash-checked unchanged
  across QA. Three of the eleven paths are inherited R2 bytes without R3 edits:
  `release_handlers.py`, `vintage_materialization.py`, `test_release_scheduler.py`.

Logs, replay inputs and source identities are ignored under
`artifacts/t09-offline-qa/r3/`. Initial test-development failures are retained
there separately from the final passing checks. No dependency, baseline or
control-state change, commit or push is part of this correction. Provider/network,
Garage, devPG and service activation effects are all zero. Real PG committed
readback/transaction freshness, persistent protection, process/host crash recovery
and live operability remain unproved; recovery must STOP where those capabilities
are missing. This report does not provide professional PASS or T09 COMPLETE.

Full current deterministic run (`uv run --offline --locked --no-sync pytest -q`):
**5549 passed, 4 failed, 25 skipped, 17 deselected, 3 warnings**, 483.33 seconds.
The four failures are the three parameterizations of
`test_child_cannot_execute_denied_connection_or_overrun` and
`test_trace_follows_threads_without_provider_access` in `tests/test_tdx_bounds.py`.
They stop in child `preexec_fn`; a fresh capability probe returns
`PTRACE_TRACEME=-1, errno=1 (EPERM)`. These tests were neither excluded nor changed.
This sandbox full-suite result is **NONPASS** and needs fresh Host QA on the final
candidate; the old R2 Host result cannot satisfy that gate.

The eight R3-changed paths relative to R2 are:

- `src/market_data_foundation/historical/local_job_recovery.py`
- `src/market_data_foundation/historical/fomc_statement_local_job.py`
- `src/market_data_foundation/historical/vintage_local_job.py`
- `src/market_data_foundation/historical/release_scheduler.py`
- `tests/test_release_scheduler_r3.py`
- `tests/test_fomc_statement_local.py`
- `tests/test_fred_alfred_vintage_local.py`
- `docs/macro-release-scheduler-operator-r1.md`
