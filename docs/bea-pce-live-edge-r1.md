# T04 BEA PCE source-only acquisition candidate

This is the default-off PCE Source adapter. Its fixed request/receipt context uses
`SOURCE_EFFECT_PLAN_CORRECTION_002.json`, SHA256
`c5b7e35d0d6434b2d2d0d80223ea8ea083ac33dc77443bcce744bfab23965726`.
Owner has designated `/srv/market-data-foundation/config/local/secret.toml`
-> `bea.api_key` as the MDF standing Source capability within the authorized T04
scope. Execution needs no external grant/verifier, reviewer trust mechanism,
per-call Owner approval, B1/B2/B3 closure or whole-process <=30s hard proof.
The historical plan identity remains receipt context, not a runtime grant.
This correction performs no real requests or storage effects. The existing PCE
retained-only job and inactive source binding are unchanged.

`historical/bea_pce_live.py::acquire` returns `disabled_budget_zero` without
loading credentials or starting a worker by default (`enabled=False`, budget 0).
There is no CLI, import-time IO, read-side acquisition, storage or publication
edge. `acquire(identity, enabled=True, provider_budget=2)` uses the existing
`_HttpsTransport` and `_load_key` in the supervised worker. The budget must be
the exact integer 2; all other values and non-True enable flags fail before IO.
Identity validation still checks the fixed receipt context. Paired transport and
loader overrides remain synthetic test seams; unpaired callbacks are
`context_rejected`. Fake outputs and caller identity assertions cannot establish
Source qualification. The historical
[offline boundary helper](bea-pce-execution-boundary-r1.md) is not imported or
consulted by this runtime path.

The only real transport is stdlib HTTPS to `https://apps.bea.gov/api/data/`.
It sends GET `GetData`, dataset `NIPA`, frequency `M`, years `2025,2026`, JSON,
first table `T20807`, then `T20804`. These remain exactly two ordered logical
requests. Each table independently uses the canonical MDF Source request policy:
at most three transport attempts with 1s/2s backoff for transient network/timeout
or HTTP 408/429/500/502/503/504 failures. No discovery, redirect following,
pagination, concurrency or Provider fallback is implemented.
The loader reads the standing capability only inside the explicitly enabled
worker. Tests use fabricated responses and a fake loader, or exercise the real
loader/HTTPS code with a fabricated TOML file and mocked HTTPS connection inside
the spawned worker. No deterministic test reads the real file or opens a socket.

The parent records intent before permitting each logical table request and waits
on fixed, bounded-size IPC messages. Each wire attempt retains a 10-second
request cancellation threshold; the outer logical/network bounds are expanded
only enough to contain the shared three-attempt policy and its 1s/2s backoff.
These are cancellation thresholds, **not a proved full-lifecycle hard bound**. `process.start`, pipe operations, clocks, final `join`, persistence and
reclamation can overrun them. Offline counterexamples demonstrate this gap.
No complete host hard bound is claimed or required for admission. The existing
mechanism cancels blocked DNS/TLS/headers/body and local processing when the
supervising process can run.
The child never persists a first response. A first failure/unknown prohibits
request two; a second failure/unknown returns neither projection nor receipt.
Completed first-response observations remain available for accounting.

The admitted HTTP shape is deliberately strict: status 200, JSON content type,
single valid Content-Length, no Transfer-Encoding or Location, and identity or
gzip encoding. Chunked/ambiguous framing fails closed. Header parsing is bounded.
Body reads are at most 16,384 bytes, and both wire body and incrementally decoded
body must be <= 2,000,000 bytes. Gzip expansion, truncated/concatenated streams,
invalid framing and oversized advertised or actual bodies are rejected. Actual
BEA framing/metadata availability has not been established by these fixtures.
A changed live shape requires review, never silent relaxation or another call.

Only the exact expected RequestParam UserID slot is accepted in the in-memory
BEA response envelope; the full request echo must otherwise match the fixed
nonsecret GET parameters. Request is removed in memory. Results, metadata and
caller-controlled identity values are checked for credential remnants, encoded
variants (JSON/nested Unicode escapes, HTML entities, repeated URL escaping,
base64/URL-safe base64 and hex),
query/URL echoes and extra Request envelopes before any response digest is
computed or any candidate is emitted. Original response hashes are not computed.
Transport/worker exceptions become fixed classifications without their messages,
URLs, key values, key length or key hash. Unsanitized bodies never cross IPC.
The shared result buffer holds only credential-clean projection/receipt data and
is cleared on every exit. Sanitized output is returned in memory; any later
retention must use reviewed, ignored restricted artifacts and approved locators.

Correction 001 closes the independent review's two P1 findings. The response
wire schema admits UTF-8 JSON only; literal/escaped NUL and NUL-bearing decoded
blobs fail closed. The finite decoding set is UTF-8, UTF-16/32 with BOM and both
byte orders, base64/URL-safe base64, hex, URL and HTML decoding. Each scalar has
at most eight transformation levels, 128 distinct states and 4,000,000 scanned
characters; exhaustion rejects. No exception is stringified at either process
boundary. Only an exact built-in EdgeRejected with one exact string argument
matching the local classification map can select a constant frame. Subclasses,
mutated arguments and arbitrary exceptions cannot supply IPC bytes.

The shared `bea_pce.project_provider_results` replaces the synthetic whole-table
validator. Its shape evidence is the Owner-generated sanitized diagnostic pair
under ignored `artifacts/t04-shape-diagnostic/` (620 rows per table). Committed
fixtures encode representative fields and samples; CI needs no diagnostic files.
Provider Results require Statistic=`NIPA Table`, a production timestamp (including
the observed fractional seconds without a Z), the nine expected dimension names,
Data and Notes. Structural limits are 2,000 rows and 128 notes within the existing
2 MB body cap. Unrelated rows/notes are structurally checked and discarded.

Only lines 1/25 for the requested 2025/2026 monthly periods are projected (at most
48 rows per table). Retained fields include the actual provider NoteRef,
Dimensions, Statistic, production time, table note and extracted inline
LastRevised label. Both tables use METRIC_NAME=`Fisher Price Index`; T20807 uses
CL_UNIT=`Percent change`, T20804 uses `Level`. The T20807 monthly table note says
“preceding period” and omits explicit SA wording; the fixed series mapping and
paired T20804 SA indexes preserve the SA interpretation. T20804 must declare
2017=100 and seasonally adjusted. Required July target/comparison rows, duplicate
checks, series/description/scale/value checks and cross-table revision equality
remain enforced. Replay reapplies the same helper and requires the exact projected
shape, including consistency of the extracted revision with the original table
note. Unknown extra source fields are discarded, never copied into the projection.
Sanitizer/schema/parser revision v2 and source adapter revision
`t04-bea-pce-live-edge-real-shape-r2` distinguish these bytes from old synthetic
bundles. The authenticated Request and full response are never retained.

After both validated `ResponseCapture`s, `retain_sanitized_bundle` performs full
cross-table validation and returns a complete `source_projection` candidate of
at most 2 MB. Its class is `sanitized_results_bundle`,
`exact_original_retained=false`; these are transformed bytes, not BEA original
bytes. Existing normalization remains bounded at 32 MB. Monthly SA Headline/Core
MoM is provider-returned calculation (T20807 lines 1/25, DPCERGM/DPCCRGM).
YoY is product-derived from July 2026 and July 2025 SA 2017=100 indexes (T20804
lines 1/25, DPCERG/DPCCRG), using the existing Decimal ratio formula and lineage.
Release/vintage remain unavailable; matching LastRevised labels do not establish
an atomic provider snapshot. Existing obtained/frozen/decision/usable invariants
continue in the retained materialization layer, which this adapter never calls.

The receipt binds producer execution identity, code Git base and candidate
manifest SHA, plan SHA, adapter/attempt/observation identity, fixed nonsecret
endpoint and parameters, per-response UTC request/completion, sanitizer/schema,
and actual projection size/SHA. Completion acknowledges a local complete HTTP
response; failures retain sent/remote receipt uncertainty. Receipt identity
fields are caller assertions until independently verified against the controlled
owner-local observation. A nonempty string, mock, source-shaped JSON, key-present
boolean or self-created receipt cannot qualify Source. `inspect_candidate` checks
exact receipt shape/binding and independently read projection bytes/replay; it
always returns `source_qualified=False`. No API here issues a real qualification
verdict or converts a candidate into an accepted Source.

`tests/test_bea_pce_live.py` contains a separately constructed fabricated fixture:
expected request parameters, mappings, rows and toy operands do not use adapter
builders. It covers default-off admission, ordered failure/unknown accounting,
slow drip and blocked-stage cancellation, combined/attempt deadlines, streaming
size boundaries, encoded credential echoes, replay and forged receipts. Tests use short private supervision deadlines with the same cancellation
mechanism; synthetic retry tests replace backoff sleep while preserving attempt
accounting. Fake HTTPS construction
checks never open a socket. This is deterministic offline fixture qualification,
not independent professional review, a real BEA response or Source acceptance.
Correction tests additionally capture actual in-memory worker IPC for wide
encodings/NUL/nested encodings and typed exception subclass/argument mutations,
exercise the production spawn protocol with fake transport, and retain positive
two-response replay and ISO metadata examples. Failed captures cannot call the
retained-bundle builder or digest and leave the result buffer and sizes empty.

The correction is ready for fresh independent review of the current bytes.
After review, the existing explicit Source acquisition and retained
Garage/PG materialization APIs can be used in the authorized T04 scope, followed
by independent projection/receipt, publication and exact/as-of local Read
readback. No replacement approval or authority entity is introduced. The supplied
sanitized pair establishes the observed Results shape; actual acquisition
framing and real storage/readback remain unverified by deterministic QA; an
incompatible response is a terminal semantic failure and never triggers retry or
silent parser relaxation. This patch does not claim Source qualification or product completion.
