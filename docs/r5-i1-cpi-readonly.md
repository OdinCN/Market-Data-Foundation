# R5-I1 CPI read-only candidate

This slice implements the CPI 03 接口注册 → 04 数据资产 → 06 数据服务
vertical with React/TypeScript/Vite and an inert FastAPI ASGI factory. It is a
local engineering candidate, awaiting professional review, Host QA and CI.
There is no live storage binding or production data claim. Other navigation
entries remain visibly unimplemented. No T10 candidate was reused or changed;
T10 R2 has no independent professional PASS/Host QA established here, and is not
claimed COMPLETE.

The design inputs are the Owner freeze, DESIGN.md, FLOW.md and frozen interactive
HTML dated 2026-10-20. Before implementation the external reference was freshly
verified as 140583 bytes, SHA-256
`d6f4f0ebb8138e46409423f15b333b571c2eec8e9a62536bde5b3d9d439c397a`.
The shared blue data-hub PNG was extracted from that reference for the brand and
favicon. Seven entries, collapsible sidebar, light gray/white and black/gray
colors, compact tables/details and stable cross-page identity are retained.
Visual fidelity still requires Owner/Host browser review.

## Operation and semantics

`market_data_foundation.web.cpi_api.create_app()` creates an ASGI application
without starting a listener. It exposes:

- `GET /api/r5/cpi?reference=2026-08&as_of=2026-10-06T00:00:10Z`:
  canonical CPI contract, bounded P6 version/publication metadata, and qualified
  asset/service associations for that explicit reference and time.
- `GET /api/r5/cpi/read?reference=2026-08&as_of=2026-10-06T00:00:10Z&mode=as_of`:
  whole-version local CPI reading through existing `read_local_cpi` / `read_as_of`.
- The same read with `mode=exact_version&version_identity=<exact identity>`:
  existing exact-version semantics, including Publication and as-of guards.
- `GET /openapi.json`: strict Pydantic v2 DTO contract. Validation errors return
  only `status` and `classification`, without input echoes or raw exceptions.

All operational routes are GET-only, reject unknown/duplicate query fields and
send `Cache-Control: no-store`. Query times require a timezone. Exact mode
requires a nonempty bounded identity; as-of mode rejects that field. No implicit
latest/reference selection, fallback provider, write route, job creation,
scheduler, source client, database migration, or runtime probe is installed.
The original consumer wire and reader code remain unchanged.

The factory defaults to `UNCONFIGURED`: the admitted canonical contract can be
shown, but asset/service evidence is absent and state is `UNKNOWN` /
`capability_not_configured`. A stored contract is not a registered live public
Source. Source health and binding activation remain UNKNOWN even for successful
local reads. Catalog service eligibility requires both bounded P6 inspection and
an actual CPI reader success in the same caller-owned consistent scope. A single
Publication candidate alone does not prove current holding. Failed scope exit
invalidates the whole result. No partial metadata survives budget/storage errors.

The host may explicitly supply a `ScopeFactory` context manager that yields the
read-only `ReadScope` port. Its methods exclude all mutations. The host must
establish authorization, consistency and successful scope exit; this slice does
not install or qualify a PG/live adapter. Future live capability binding is a
separate gated task. `evidence_mode="OFFLINE_FIXTURE"` is mandatory for synthetic
fixture bindings. `ui_directory=Path("web/dist")` optionally mounts already-built
static UI at `/` on the same inert application, after API routes. No reverse
proxy, daemon or listener is needed by the checks below.

In the UI, apply report month/time, open CPI in 接口注册, choose 查看关联资产,
then 查看数据服务. The same asset identity is selected in each detail panel and
URL hash. A missing association explains why and disables the link. Data assets
show version/binding revision, publication decision/usable/effective times and
pending clocks. Data assets and qualified services offer as-of/exact local reads
of four CPI measurements, preserving reference/comparison period, series, unit,
seasonality and availability. Ambiguity never fabricates a service; an exact
version can still be read from the asset page. Refresh and optional 30-second
polling only repeat GETs; they clear prior read results. Changed conditions,
mode, identity or page discard delayed responses. Draft form changes do not
silently change the applied read conditions.

## Rebuild and deterministic checks

Observed environment: uv 0.12.22, uv CPython 3.14.8, Node 24.21.0, npm 11.19.0.
System Python and the product Python baseline were unchanged. New direct Python
dependencies: FastAPI 0.119.1, Pydantic 2.12.3; test HTTPX 0.28.1. Frontend direct
dependencies and tooling are exactly pinned in `web/package.json`; transitive
versions/integrity are locked in `uv.lock` and `web/package-lock.json`.

Package acquisition used only `https://pypi.org/simple`, its wheel/sdist host
`https://files.pythonhosted.org`, and `https://registry.npmjs.org`. npm install
scripts, audit and funding requests were disabled. Funding URLs in package-lock
are metadata, not downloaded dependencies. Both locked environments were also
restored offline from the acquired caches. No other business network capability
was accessed. Runtime/build caches and outputs are ignored.

From the repository root (after approved dependency preparation):

```sh
uv sync --locked --group dev
uv build
uv run --locked python scripts/check_repository_hygiene.py --include-untracked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -m 'not live_provider and not postgres_integration'
npm --prefix web ci --ignore-scripts --no-audit --no-fund
npm --prefix web run contract:check
npm --prefix web run format:check
npm --prefix web run build
npm --prefix web test
```

Rebuild DTO contracts after an intentional API change:

```sh
uv run --locked python scripts/export_r5_openapi.py
npm --prefix web run contract
```

Python pytest compares the exported OpenAPI with the actual factory. The frontend
contract check regenerates TypeScript in memory and compares the committed file;
request paths, query fields and response types are derived from it. No HTTP
listener or external schema fetch is used.

`tests/r5_fixture.py` reconstructs the existing admitted P6 synthetic CPI lineage
and Publication entirely in memory. Requests use a detached read-only snapshot.
`tests/r5_ui_exchange.py` connects UI tests to actual FastAPI TestClient through
JSON-lines pipes, with TCP connect/bind/listen disabled. These are actual
UI → API → domain-reader tests, not hard-coded frontend response mocks. Fixtures
cannot be interpreted as current provider observations or current data holding.

Coverage includes both readers; stable identity; no capability; no version;
content missing/corrupt; withdrawn publication; as-of clock boundaries;
ambiguous as-of with successful exact lookup; partial scope; unsupported/stale
reference; exact miss; strict/redacted invalid requests; duplicate keys;
P6 budget overflow; storage and scope-exit failures; stale UI response disposal;
unknown cross-page identity; disabled unqualified service; theme/sidebar and
unimplemented entries. Canonical write methods are disabled during read checks,
and the original ledger snapshot is compared before/after repeated GETs.

Current data availability remains UNKNOWN. This candidate authorizes no
Provider/PG/Garage/KD/MBO call, listener, daemon, worker, publication, deployment,
activation, push or merge. Professional review, browser/visual Host QA,
GitHub-hosted CI and Owner acceptance remain outstanding. The next bounded task
should review this candidate and run authorized Host QA/CI; actual data capability
qualification and deployment need their own explicit gate.

## Local Git delivery limitation

The host mounts this clone's `.git` read-only. The attempt to stage the candidate
failed creating `.git/index.lock` with `Read-only file system`. No alternate index,
Git directory or permission bypass was used; no candidate commit was created.
All changes remain as modified/untracked files in this clone. Hygiene can include
those files explicitly via `--include-untracked`, without writing Git metadata.
The next delivery step requires an Owner/Host-provided writable Git capability.

## Observed local validation

| Check | Actual result |
| --- | --- |
| Repository hygiene including untracked candidate files | PASS, 284 files |
| Ruff check / format check | PASS, 255 Python files formatted |
| Full deterministic pytest invocation | 5590 passed, 25 skipped, 17 deselected, 4 warnings; 476.73 seconds |
| Final targeted API + hygiene tests after bounded corrections | 29 passed, 1 warning (25 API, 4 hygiene) |
| UI → TestClient → canonical domain tests | 11 passed |
| TypeScript / Vite production build | PASS |
| OpenAPI export freshness + generated TypeScript comparison | PASS |
| Frontend Prettier check | PASS |
| uv source distribution + wheel build, offline | PASS |
| Fresh ignored uv environment + npm ci, both locked/offline | PASS |
| Built UI resources + same-origin API + reader via TestClient | PASS; network/listen forbidden, ledger unchanged |
| Local Git staging / commit | STOP: host read-only `.git`; no candidate commit |

The full suite was launched before the last two API edge tests were added; the
final targeted run includes those tests and the final API/hygiene corrections.
The full-suite warnings were the Starlette/AnyIO deprecated BlockingPortal alias
and three existing WSGI validation warnings for intentionally unusual methods.
No warning was suppressed. Skips/deselections are not evidence of live capability
qualification. No CI, professional review or browser Host QA was executed.
