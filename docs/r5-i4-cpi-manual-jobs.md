# R5-I4 CPI manual initialization candidate

Bounded engineering candidate on branch `mdf/r5-i4-cpi-manual-jobs-20261020`,
base `fccd00e5024247bee85b09d5cf9d3cb61b707436`. No deployment, activation,
real provider, PostgreSQL or Garage execution is included or authorized.
Host QA and independent professional review are **pending**, not PASS.

## 05 operator flow

1. In 04, open the named CPI definition and choose `新建 CPI 一次性采集`.
   The link carries the definition identity and revision into 05. A missing or
   mismatched identity disables submission; no holding is needed to initialize.
2. 05 preserves the peer tabs `新建采集 / 周期任务 / 执行记录`. Only manual,
   one-time acquisition of `2026-09`, release slot
   `CPI/2026-09/2026-10-14T12:30:00Z`, is supported. Confirm the explicit intent,
   then submit. Storage inherits the trusted 03/04 host binding, with no browser
   storage selector. Arbitrary historical dates, backfill, multiple sources,
   retained repair and Cron/schedule activation are not exposed here.
3. Submission returns HTTP 202 only after SQLite acceptance. It does not execute
   acquisition. The stable, host-derived intent identity survives browser close
   and response loss. Refresh execution records first after an HTTP failure;
   an explicit resubmission uses that same identity. Scope conflicts remain
   visible denials, not a second collection.
4. A separately invoked, explicitly opted-in Host worker processes the accepted
   identity. Execution records show the persisted state, one Attempt, worker
   identity, clocks, error classification, cancellation request and append-only
   history. `running` is a journal state, not proof of a live worker. Cancellation
   requests cannot undo existing effects. A pending cancellation is finalized
   by the worker and can have zero attempts.
5. `04 版本与读取资格` returns to existing canonical/consumer reads. A planned
   Version is labelled a candidate; its presence does not assert publication or
   current reader eligibility. Publication occurrence appears only from recorded
   successful independent verification. `01 关联异常` queries the same journal
   for failed/unknown Jobs and links back to that exact Job in 05. This is a
   durable snapshot, separate from host/process health evidence.

## Explicit host and worker boundary

`create_app(jobs_host=None)` remains inert: GET is UNKNOWN and POST is denied.
`CpiJobsHost` requires an existing writable `FileJobJournal`, an exact acquire
`CpiJobSpec`, server clock, evidence mode and an authorization callback. The
trusted host must authenticate every request, map its server-side identity to
an `Operator` and check write eligibility, including CSRF/origin admission for
cookie sessions. Browser roles, JSON claims and arbitrary headers have no
default authority. The synthetic fixture returns a server-owned test identity;
it is not a production authentication implementation.

GET `/api/r5/cpi/jobs` (optional `job_identity`) uses a read-only handle and a
single bounded SQLite read transaction for jobs plus history. It never creates
directories, journals, locks, jobs or attempts. Missing/unreadable storage or an
invalid record returns UNKNOWN with no partial jobs. Budgets are 100 jobs and
256 history events per job; exceeding either returns UNKNOWN. No paging or
unlimited history capability is claimed. Reading does not qualify a remote
storage endpoint or create a source request.

POST `/api/r5/cpi/jobs` accepts only the strict `Intent` JSON contract; POST
`/api/r5/cpi/jobs/{job_identity}/cancel` accepts an empty JSON object. New write
contracts reject extra fields and return bounded classifications, never raw
validation details or exception text. Same identity/spec is idempotent; changed
identity/spec and reserved scope conflicts are rejected by the existing journal.

`CpiManualWorker.process(identity, enabled=True)` is a library composition seam,
not a new CLI, daemon, job store or deployed runtime. The host supplies the real
`CpiLocalJob`, exact spec, `DirectCpiAcquirer`, preflight and worker identity.
The engine's journal must be the same owner-only, durable local directory used
for acceptance. The executor's flock and existing budgets remain authoritative.
The original worker identity is recorded with the Attempt before source effects;
older records without it show UNKNOWN. There is no SQL schema migration.
The host owns filesystem protection, target/source qualification and actual
worker invocation. No such real host composition is installed in this slice.

Interrupted `running` or `outcome_unknown` goes only to existing `reconcile`.
It can observe exact Raw/Version/Publication and append a reconciliation result,
but cannot reacquire source or republish. The original unknown Attempt remains
unknown even when reconciliation confirms an outcome. No HTTP retry/run/repair
endpoint is introduced. Canonical authority remains the existing ledger, never
SQLite job status.

## Offline qualification prepared for AO Host

`tests/r5_jobs_fixture.py` composes a synthetic Operator, temporary SQLite,
fake BLS transport, in-memory material store/protection and ledger, using the
existing `CpiLocalJob` and existing CPI reader. Worker identity is
`synthetic-cpi-worker`; Attempt identity is `<job>-a1`; the unchanged journal
filename is `cpi-jobs.sqlite3`. `tests/test_r5_cpi_jobs.py` prepares acceptance,
separate-run and independent-reader checks plus negative cases for authorization,
scope/identity conflicts, unsupported target/mode/budget/storage, cancellation,
preflight failure, unknown commit, opt-in, locks, restart, read/query budgets and
exception redaction. Existing executor tests retain deeper storage/cancellation,
credential and commit/ownership ambiguity coverage.

`tests/r5_jobs_ui_exchange.py` is a Host-only test bridge, with no TCP listener.
Its worker control is a test protocol, not a product endpoint. New Vitest cases
use actual ASGI/Pydantic/journal/domain responses, including a lost acceptance
response, browser remount, separate synthetic worker run, existing reader and
01 failure round trip. Socket connection/bind and psycopg connection attempts
are rejected in these deterministic fixtures.

Codex must not execute pytest, TestClient or Vitest in this sandbox. AO should
run on the Host, in this worktree, against the precise candidate:

```bash
uv run --offline --locked pytest tests/test_r5_cpi_jobs.py tests/test_cpi_local_job.py tests/test_r5_cpi_api.py
uv run --offline --locked pytest -m 'not live_provider and not postgres_integration'
```

In `web/`, run `npm test`, `npm run check`, `npm run contract:check`,
`npm run format:check`, and `npm run build`. No new dependencies or lock changes
are required. Python remains uv-managed 3.14.8. Static checks can run offline:

```bash
uv run --offline --locked python scripts/check_repository_hygiene.py --include-untracked
uv run --offline --locked ruff check .
uv run --offline --locked ruff format --check .
```

No Host QA result, independent review, CI result or live qualification is
claimed by this document. Offline success would still not prove real provider,
PG/Garage availability or authorization. AO must complete actual Host QA and
independent review before its separately controlled Git delivery. Runtime
effects, deployment, release, activation and adoption remain separate gates.

## Candidate inventory and sandbox static evidence

Changed files (18, all unstaged at handoff):

- Historical foundation: `cpi_job_journal.py` (bounded consistent snapshot),
  `cpi_local_job.py` (optional Attempt worker identity), `macro_job_journal.py`
  (retain that identity in existing history).
- Python web: `web/cpi_api.py`, new `web/cpi_jobs.py` under
  `src/market_data_foundation/`.
- Frontend/contracts: `web/openapi.json`, `web/src/api.generated.ts`,
  `web/src/api.ts`, `web/src/App.tsx`, `web/src/App.test.tsx`,
  `web/src/Observability.tsx`, `web/src/Observability.test.tsx`,
  new `web/src/CpiJobs.tsx`, new `web/src/CpiJobs.test.tsx`.
- Host-only tests: new `tests/r5_jobs_fixture.py`,
  `tests/r5_jobs_ui_exchange.py`, `tests/test_r5_cpi_jobs.py`.
- This document. `uv.lock`, `pyproject.toml`, `web/package.json` and
  `web/package-lock.json` are unchanged; no dependency or baseline change.

Sandbox static evidence: repository hygiene including untracked files passed
(300 files); `ruff check .` passed; `ruff format --check .` passed (266 files);
OpenAPI export and TypeScript regeneration completed; `npm run check`,
`npm run contract:check`, `npm run format:check`, `npm run build` and
`git diff --check` passed. Build outputs stay ignored under `web/dist/`.
These are static results only. No pytest/TestClient/Vitest, synthetic worker
execution, independent review or CI run was performed by Codex.
