# Integrated clock identity correction R1

Binding: MDF-R5-I6-INTEGRATED-CLOCK-IDENTITY-CORRECTION-R1, Owner-local,
MDF-DEV, session #15; canonical base
`e7b64b382cbd8492b3e98785014270f2326ee2d3`.
Addresses only P1/P2 in the external transaction's exact
`integration/integrated_review3.return.md`. Historical review, QA3 and
`CLOCK_CORRECTION_R2.md` remain unchanged.

## Changes

- `market_persistence.py`: Version validation compares the complete canonical
  bytes of each assessment receipt with the retained canonical receipt. The same
  gate protects PUBLISH/RESTORE and recovery/rebuild. A missing receipt still
  fails with `canonical_material_missing`.
- `market_projection.py`: lineage uses the same complete canonical comparison;
  content read-back goes through lineage and fails with `INDEX_INCONSISTENT` on
  a mismatch. `select()` compares caller as-of with captured view-now in UTC;
  future requests retain `INDEX_INCONSISTENT`, naive input retains `naive_time`.
- `tests/test_r5_i6_integration_clocks.py`: 58 additional deterministic cases.
  No other production modules, shared D1, SDK/Go, dependencies or configuration
  change. Chinese market local dates retain `Asia/Shanghai`.

## Before/after evidence

New York's repeated 2024-11-03 hour gives the same wall time to different UTC
instants. A retained acquisition at 06:05 UTC must not match an assessment at
05:05 UTC for a Version frozen at 05:20 UTC. Negative tests independently reach
commit, PUBLISH, RESTORE, lineage, content, rebuild and backup restoration. They
cover requested/obtained/received/known timestamps, check failed writes leave
the checkpoint unchanged, and exercise recovery with individually consistent
commit receipts so that the cross-record identity gate is required.

Equivalent NY/UTC receipt representations must pass all those gates. As-of tests
cross both representations of caller and captured view time, including past,
equal, future, and naive inputs. In particular, view-now 05:50 UTC rejects as-of
06:10 UTC even when both use New York time, while view-now 06:10 UTC accepts
the earlier 05:50 UTC instant.

The final 58 added cases run against an isolated pre-correction source copy:
**28 failed, 30 passed** (22 swapped-receipt negatives, two equivalent-receipt
positives and four as-of cases fail). This excludes earlier fixture diagnostics.
Against corrected sources, the complete clock file is **98 passed**; combined
D3/D4/clock regression is **381 passed**. Python is uv-managed **3.14.8**, uv
**0.12.22**, using existing locked dependencies offline without synchronization.
Ruff check, format check, tracked/untracked repository hygiene and whitespace
checks pass. A byte comparison confirms every file outside this four-file
correction retains its input bytes; HEAD and local main remain the base.

## Delivery gate

**STOP: full deterministic QA is not complete or qualified.** The unchanged
`test_bea_pce_live.py::test_total_attempt_deadline_during_local_sanitization`
fails: its synthetic worker returns zero observations within the 0.5-second
deadline where two are expected. An isolated rerun also fails (1 failed, exit 1).
The full run subsequently stalls in the unchanged
`test_cpi_scheduling.py::test_http_acceptance_auth_strict_input_and_no_execution`.
A 30-second faulthandler dump shows asyncio selector and AnyIO worker waits;
an independent local socketpair send gets `PermissionError`, errno 1. This
supports a sandbox wakeup limitation, not a completed Host QA result.

The full log records 1,576 passes and one failure before interruption (tool exit
130); pytest emits no completion summary. Those are progress counts only.
No tests, deadlines, markers or sandbox settings were changed or bypassed.
Logs are retained under ignored
`artifacts/r5-i6-integration/clock-identity-correction-r1/`.

At this implementation handoff the new independent read-only review is next;
its separate return governs professional acceptance of these frozen files.
Host must independently record final source bytes/SHA and rerun full QA in its
normal environment. Historical QA3 does not qualify these corrected bytes.
No commit/push or merge has occurred. Any subsequent Git/CI delivery remains
gated on final QA, independent review, same-commit hosted CI and unchanged main.
There was no Provider/ExHq access, PG/Garage business IO, deployment, activation
or adoption.
