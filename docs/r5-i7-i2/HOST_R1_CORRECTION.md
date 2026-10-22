# I2 independent Host R1 correction return

Scope: the existing I2 worktree, responding to transaction
`MDF-I1-I4-FOUR-STREAM-20261022-R1/outputs/I2_INDEPENDENT_CODE_REVIEW_R1.md`,
`BATCH_DIRECTIVE.md`, and `SHARED_PORT_R1.md`. This is an implementation return,
not independent Host PASS or permission to execute live.

## Exact implementation files

- `scripts/mdf_i2_build_source.py`
- `scripts/mdf_i2_source.go`
- `scripts/mdf_i2_source_test.go`
- `src/market_data_foundation/qualification/tdx_market_collect.py`
- `src/market_data_foundation/qualification/tdx_market_plan.py`
- `src/market_data_foundation/qualification/tdx_market_retained.py`
- `tests/test_i2_market_source.py`
- `tests/test_i2_market_corrections.py` (new)
- `docs/r5-i7-i2/HOST_R1_CORRECTION.md` (this new return)

All are I2-owned paths. Existing shared core, Driver pin, SDK checkout,
`pyproject.toml`, `uv.lock`, old receipts, frozen plans and their budgets were
left untouched. No additional worktree, Git mutation, Provider/PG/Garage access,
timer installation, deployment, or activation was performed. Read-only Git
queries/export are used by the required existing pin validator, offline build
and hygiene checks; no Git staging, commits, branch changes, or pushes.

## Findings and regression evidence

| Host finding | Correction | Deterministic regression |
| --- | --- | --- |
| P1: mutable SDK build input | Export the exact pinned commit into a private temporary directory; derive hosts, module and sums there; both test/build replace only that export. Record archive and endpoint SHA. | Synthetic mutable checkout contains an untracked Go `init()` and differing tracked-looking files; inspect both compiler invocations to prove only exported pinned inputs enter the build. Actual Go race/build uses the pinned export. |
| P2: response budget checked after send | Check remaining header capacity before `Write`, including the handshake and every later method. | Every remainder from 0 through 15 produces zero writes; a completed handshake exhausting the budget permits no next request. |
| P2: cancellation discards stdout | Persist/fsync each received stdout chunk, write UNKNOWN intent before launch, retain exact partial bytes with SHA/readback, kill/reap the child, finalize a classified ledger, then propagate interruption. SIGTERM gets a scoped catchable handler. | Inject KeyboardInterrupt, SystemExit and SIGTERM after two chunks; verify each prefix before the next receive, partial SHA, UNKNOWN effect, no quote launch, one spawn, one kill, and no retry. |
| P2: excess returned items | Check directory count against 1,000 and requested count; check Kline count against requested count before SDK decoding. | Well-formed oversized directory/Kline frames stop after their response, retain the frame and send no subsequent request. Exactly-at-limit cases still decode. |
| P2: after-close crosses date | Require the complete frozen source+quote budget, including the existing six-second allowance, to fit strictly before local midnight. Recheck after candidate sealing and before quotes. Go dial/socket deadlines and parent quote IPC use an absolute cutoff. | Full-budget, equality, one-second, microsecond and quote-only cases; expired Go deadlines yield no dial; dial/transport deadlines are capped; late quote configuration and ACK writes are refused. |
| P2: expiry shorter than session | Require full frozen budget within both approval expiry and the named market close; carry the earlier absolute deadline to the quote process. | AM/PM expiry and close boundaries, including equality and 35/36 seconds for the unchanged 36-second budget. Repaired lunch/late/closed/partial negatives use session plans and assert the intended error code. |
| P2: new quotes cannot be retained | Write canonical `config.json`/`report.json` alongside batches and receipt. Successful collection emits exact `quote-manifest.json`. Intake copies exact bytes and manifest, then reconstructs both receipt and completed-report readbacks from the copies. | A real offline session parser drives `collect`, then direct intake of that output succeeds without renaming or rewriting source files. Every manifest member rejects tampering before replay; original run identity and UNKNOWN source time are preserved. |

The source Go config now requires an absolute `deadline`; this is runtime gate
metadata and does not rewrite the frozen Plan schema or budget. Old source
binaries must not be paired with the updated runner. The qualified Python
baseline remains uv-managed 3.14.8; no dependencies were installed or changed.

## Evidence and candidate

Final command logs, exit codes and hashes are under the ignored directory
`artifacts/i2-host-r1-correction-qa-20261022/`. `checks.json` records exact commands;
`tested-files.json` pins the eight code/test files. The builder includes
`go test -mod=readonly -race -count=1 -timeout=60s`, with network dependency
resolution disabled and only in-memory test transports. It also builds the
independent source binary without executing it live.

| Check | Actual result |
| --- | --- |
| Focused I2 deterministic pytest | 51 passed in 1.04s |
| Watchlist runtime/readback, directory and D2 compatibility pytest | 490 passed in 139.52s |
| Go race/build | Passed, `mdf.local/i2-discovery`, 1.036s |
| Repository-wide Ruff check | Passed |
| Repository-wide Ruff format check | Passed, 342 files |
| Hygiene including untracked files | Passed; repeated after this return was added |
| gofmt check | Passed, no filenames emitted |
| uv-managed Python | 3.14.8 |

Final candidate: `artifacts/i2-host-r1-correction-qa-20261022/source`.
SHA256: `bdee7324d12e3a7263740a6f5b5712729ff6e96652889a59f7741bb2c270b770`.
Build provenance: adjacent `source.build.json`. Final readback and return-document
identity: `final-readback.json` in the same QA directory. The earlier trial
candidate `artifacts/i2-host-r1-correction-build/source` is preserved and is not
the candidate named by this return. No live run ID was allocated or used.

All these results are implementation-side offline evidence. Independent Host
review and full-tree deterministic integration tests were not performed in this
correction; the 490-test compatibility run is explicitly scoped, not a full run.

The first local `.venv` invocation could not import pytest/Ruff. All reported
qualification runs use the existing qualified `/srv/market-data-foundation/.venv`
through `UV_PROJECT_ENVIRONMENT`, `uv run --offline --no-sync`, and the private
`UV_CACHE_DIR=/tmp/mdf-i2-uv-cache`. No environment/dependency repair was needed.
Initial synthetic fixtures exposed an extra mock probe and a short Kline record;
those fixtures were corrected before the final runs.

## New quote retained-intake handoff

For a future independently approved new run, supply its exact collected layout:

```bash
uv run --offline --no-sync python scripts/mdf_i2_collect.py intake-quotes \
  --source-directory /ABSOLUTE/PATH/TO/artifacts/NEW-RUN \
  --manifest artifacts/NEW-RUN/quote-manifest.json \
  --output artifacts/NEW-INSPECTION
```

The caller/Host must freeze and review that named manifest. Output copies keep
original bytes, source run ID, source clocks and ACK evidence; intake adds an
inspection identity and current-age metadata. The manifest proves byte identity,
not Provider authenticity or market-wide qualification. No legacy receipt is
renamed or modified. Incomplete collection fails closed and is not a completed
retained snapshot.

## Effects, UNKNOWN, blockers and next gate

Actual effects of this correction: local source/test/document edits, ignored
build/QA artifacts, and deterministic local tests only. New Provider requests,
PG writes, Garage writes, timers, deployment and activation: zero. No retry or
live attempt was made; synthetic interruption UNKNOWN is a regression result,
not a newly observed Provider effect.

Remaining UNKNOWN: live Provider behavior/freshness, exact interrupted Provider
effects beyond retained evidence, source event time/units, calendar authority,
market-wide directory qualification, and quote transport response byte count
(the unchanged Driver exposes no read counter). Quote IPC bounds are not socket
response byte bounds. SIGKILL or power loss cannot guarantee a final ledger;
prewritten intent and fsynced prefixes must be inspected with effects UNKNOWN.

Independent Host re-review remains required. Any live execution requires a new
run ID and a newly frozen plan bound to the new binary SHA, plus the relevant
calendar approval for a session. Never patch an old plan, reuse an old run ID,
raise its budget, or adopt an old PASS for these bytes. No new live plan was
created here. Same-byte integration/full deterministic QA and independent Host
adjudication remain separate gates. A test or build exit code of zero is not
independent PASS, adoption, or activation authority.
