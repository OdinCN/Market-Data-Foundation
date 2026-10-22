# R5-I6 central integration validation

Base: `e7b64b382cbd8492b3e98785014270f2326ee2d3`.

## QA1 diagnosis and environment correction

The complete `integration/qa1.full_pytest.log` records six collection errors:
`httpx` is missing in `test_cpi_scheduling.py`, and `fastapi` is missing in
the five CPI/observability/source-catalog test modules. Pytest exits 2 before
running the suite. These packages are already declared in `pyproject.toml` and
`uv.lock`; no dependency or product-source change is required for this failure.

The external `CENTRAL_INTEGRATION.py` selects
`/srv/market-data-foundation/.venv/bin/pytest`, even when its working directory
is the integration worktree. That interpreter lacks the declared web packages.
Changing the working directory or setting `PYTHONPATH=src` does not install them.

The correction performed here is a worktree-local environment sync with uv
0.12.22, using the existing lock and uv-managed CPython 3.14.8:

```sh
UV_CACHE_DIR=/tmp/mdf-r5-uv-cache uv sync --locked --offline --group dev
```

The default cache lacks some exact locked packages and network DNS is unavailable
in this sandbox. The existing `/tmp/mdf-r5-uv-cache` contains the required exact
versions; the offline sync succeeded with 29 installed packages. In particular:
FastAPI 0.119.1, HTTPX 0.28.1, Pydantic 2.12.3, pydantic-core 2.41.4,
Starlette 0.48.0, pytest 8.4.2 and Ruff 0.16.10. No version substitution was used.

Subsequent central QA must use the synced worktree environment, for example
`uv run --locked --offline pytest -p no:cacheprovider -q` from this directory
with the same cache configuration, or this worktree's `.venv/bin/python -m
pytest` after sync. The controller and canonical checkout's environment are
outside this correction's writable scope and were not modified. Reusing its
original absolute pytest path would reproduce the collection failure.

## Scope and evidence

Read the active AGENTS, batch/shared R1 contracts, formal dataset design R2,
upstream field-semantic gap audit, complete QA1 logs and final six lane review
returns. D2 and D3's applicable reviews are `ao_directed_review1.return.md`;
their earlier correction-required reports describe older candidates.

All 45 copied candidate files match the byte lengths and SHA-256 values in
`INTEGRATION_INPUT.json`. HEAD matches the designated base and tracked files
are unchanged. This integration note is the only additional product file.
No implementation, test exclusion, budget or UNKNOWN semantic was changed.

Ruff check passed; Ruff format check passed for 328 files; repository hygiene
passed for 365 tracked and untracked files. `uv pip check` found all 29 installed
packages compatible. Tracked whitespace checks and this note's untracked
whitespace check produced no diagnostics.

After fixing collection, a focused reproduction of the existing
`tests/test_bea_pce_live.py::test_total_attempt_deadline_during_local_sanitization`
failed: its 0.5-second attempt deadline returned `deadline_unknown` with zero
observations, whereas the test requires two complete observations. The test
uses a synthetic multiprocessing worker and performs no actual network exchange.
This test and its BEA PCE implementation are unchanged baseline files outside
the authorized correction paths. No deadline was enlarged, test skipped or
failure converted into a pass.

The focused D1 plus six-lane deterministic run passed **929 tests in 180.90s**.
It used uv to invoke the existing uv-managed Python 3.14.8/pytest 8.4.2 with this
worktree's test configuration and source tree. The exact test selection was:

```text
tests/test_r5_i6_market_contracts.py
tests/test_r5_i6_d2_runner.py
tests/test_r5_i6_d3_material_projection.py
tests/test_r5_i6_d4_market.py
tests/test_r5_i6_a_std.py
tests/test_r5_i6_a_cal.py
tests/test_r5_i6_a_ex_dir.py
```

The full run used the newly synced worktree environment, the repository's
unchanged deterministic marker selection, and the existing offline Go
environment from central QA. Collection succeeded: **6,823 selected, 19
deselected**. Progress recorded 1,577 passes and the BEA failure before the run
stalled in the unchanged
`test_cpi_scheduling.py::test_http_acceptance_auth_strict_input_and_no_execution`.
After over two minutes without progress, the full invocation was interrupted
(exit 130); these are progress counts, not a completed pytest summary.

A separate reproduction of that one CPI test, with an eight-second
faulthandler dump and a 20-second timeout plus five-second kill grace, also
stalled (exit 137 after forced termination). The stack shows the main thread
waiting in the asyncio selector and the AnyIO worker waiting on its queue.
A product-independent `asyncio.run()` of
`await anyio.to_thread.run_sync(lambda: 42)` reproduced the same wait and was
also terminated by its timeout. A local `socket.socketpair()` send failed with
`PermissionError`, errno 1. Together these observations support a sandbox
restriction on the event loop's thread-wakeup socket; no MDF route, Provider
or database is needed to reproduce it. No workaround was injected into the
application, tests, dependency code or event loop.

**Full Host QA remains unqualified.** A host permitting normal local asyncio
wakeups must rerun the complete suite using a synced locked environment, and
resolve or re-evaluate the unchanged BEA deadline regression. Neither the
successful collection nor the focused lane result establishes full QA PASS.

Diagnostic logs and a machine-readable validation receipt are retained under
ignored `artifacts/r5-i6-integration/qa-correction1/`. Original transaction QA
logs and review receipts were not overwritten.

This is offline validation only. Existing documented D2-to-D3 receipt and
D3-to-D4 port integration gaps remain; passing separate lane suites does not
qualify an end-to-end production path, storage durability or database costs.
No live Provider/PG/Garage, migration, Git commit/push, deployment, activation
or adoption was performed.
