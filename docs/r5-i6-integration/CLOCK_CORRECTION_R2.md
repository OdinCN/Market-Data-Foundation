# R5-I6 integration clock corrections R2

Base: `e7b64b382cbd8492b3e98785014270f2326ee2d3`.
Addresses the two clock findings in `integration/integrated_review2.return.md`
under the batch transaction. This is a central offline correction to the copied
candidate, without changing the lane interfaces or implementing the documented
D2-to-D3 and D3-to-D4 adapter gaps.

## Corrections

Python compares aware datetimes sharing a timezone by wall time, ignoring the
repeated-hour `fold`. Comparisons and ordering keys now use UTC instants:

- D3 checks assessment, freeze, acquisition, publication and recovery chronology
  in `market_material.py`, `market_publication.py` and `market_persistence.py`.
  Publication decision, usable and effective gates use the same instant semantics.
  Full history validation, rebuild and coverage reconciliation order decisions
  by UTC. Usable-time equality also distinguishes the two repeated-hour instants.
  The ledger clock is checked for an explicit timezone before UTC normalization.
- A-STD checks receipt and page chronology, snapshot known time and latest
  obtained time in its `adapter.py` and `universe.py`.
- A-EX-DIR checks receipt and capture chronology and selects the latest known
  time in its `adapter.py`. Missing evidence clocks remain unknown.

Only comparisons and aggregation keys change. Receipt and publication fields
retain their original timestamp evidence, including timezone and fold. Existing
canonical UTC serialization and identity rules remain in use. No economic units,
qualifications, budgets or publication transitions change. Existing domain clock
classifications are retained; naive ledger clocks fail with `naive_time`.

## Deterministic regressions

`tests/test_r5_i6_integration_clocks.py` reuses the reviewed synthetic fixtures.
It exercises New York's 2024-11-03 repeated hour and equivalent UTC timestamps:
future decision/usable/effective gates, material obtained after freezing,
requested/obtained order, known/assessment order, assessment/freeze order,
future freezing, publication chronology, complete history ordering and exact
offline recovery, reversed directory receipts, valid receipts across the fold,
page/capture ordering, timestamp aggregation and retained UNKNOWNs.

The final regression file, run against an isolated temporary copy of the
original implementations, records **21 failures and 19 passes**. Against the
corrections, all **40 tests pass**. Both logs are retained; earlier fixture and
assertion diagnostics are also retained.

The broader D1 plus six-lane deterministic run passes **967 tests in 165.61s**
(929 existing tests and the initial 38 clock cases). After adding explicit
ledger-clock validation and its two regressions, the final D3 plus integration
run passes **209 tests in 28.90s**, including all 40 final clock cases. Ruff check,
Ruff format check and repository hygiene pass. Python is uv-managed **3.14.8**,
with the existing locked dependencies; no environment or dependency substitution
is used. Whitespace checks cover all 48 candidate and integration files without
diagnostics.

## Validation boundaries

The earlier `VALIDATION.md` describes the first integration environment and QA
attempt. The subsequent external Host receipt `integration/qa2.json` records a
completed **6,798 passed, 25 skipped, 19 deselected** for the original candidate.
That successful historical run does not qualify these corrected bytes.

The current full deterministic attempt is **incomplete and unqualified**. Its
progress records 1,577 passes and one failure before stalling in the unchanged
`test_cpi_scheduling.py::test_http_acceptance_auth_strict_input_and_no_execution`.
The 30-second faulthandler dump shows the asyncio selector and AnyIO worker
waiting. An independent local socket-pair send fails with `PermissionError`,
errno 1, supporting the same sandbox wakeup limitation observed previously.
The stalled exec session was interrupted (tool exit 1); pytest did not emit a
completion summary or a recoverable pytest exit code. Progress counts are not a
completed suite result.

A separate deterministic reproduction of the unchanged
`test_bea_pce_live.py::test_total_attempt_deadline_during_local_sanitization`
fails (exit 1): its synthetic worker reaches the 0.5-second deadline with zero
observations, while the assertion requires two. No Provider is called. Neither
the deadline nor the test is changed or excluded. Full QA of the corrected
candidate requires a Host rerun with normal local asyncio wakeups and evaluation
of that existing deadline failure.

Current correction evidence is retained under ignored
`artifacts/r5-i6-integration/clock-correction-r2/`. Original transaction evidence
is read-only and is not overwritten.

Exactly six of the 45 copied candidate files change; the other 39 retain their
input hashes. No existing D1/kernel, SDK/pin, dependency manifest, lockfile or
`web/app.py` changes. HEAD remains the designated base. No live Provider, PG or
Garage access, migration, commit, push, deployment, activation or adoption occurs.

These tests establish offline clock behavior. They do not establish an
end-to-end pipeline, live qualifications, real storage durability or database
query-cost qualification. Independent acceptance of the corrected candidate and
full Host QA remain separate from this correction's focused evidence.
