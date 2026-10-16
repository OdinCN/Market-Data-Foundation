# T04 BEA PCE source-only acquisition candidate

This is a default-off implementation candidate for plan
`SOURCE_EFFECT_PLAN_CORRECTION_002.json`, SHA256
`c5b7e35d0d6434b2d2d0d80223ea8ea083ac33dc77443bcce744bfab23965726`.
The plan's read-only PASS permits development and offline QA. Current Provider,
PG and Garage budgets remain zero. No real Source opt-in has been granted.
The existing PCE retained-only job and inactive source binding are unchanged.

`historical/bea_pce_live.py::acquire` returns `disabled_budget_zero` without
loading credentials or starting a worker by default (`enabled=False`, budget 0).
There is no CLI, import-time IO, read-side acquisition, storage or publication
edge. A future caller must obtain a separate exact-code/manifest/plan-bound
Owner opt-in before enabling one source-only attempt with budget exactly 2.
Repeated calls are separate attempts; this API supplies no retry/resume path.
An external execution owner must enforce the single approved attempt and retain
its nonsecret observation under controlled, append-only execution accounting.

The only real transport is stdlib HTTPS to `https://apps.bea.gov/api/data/`.
It sends GET `GetData`, dataset `NIPA`, frequency `M`, years `2025,2026`, JSON,
first table `T20807`, then `T20804`. Both request two years once. No discovery,
redirect, pagination, concurrency, retry or provider fallback is implemented.
One process loads `/srv/market-data-foundation/config/local/secret.toml` ->
`bea.api_key` in memory; it does not read environment or legacy MDD text.
Transport and credential-loader doubles must be supplied together for offline
fixtures, and their receipts are labeled synthetic. No tests load that file.

The parent records intent before permitting each exchange and waits on fixed,
bounded IPC messages. It kills and reaps the worker on an absolute monotonic
10-second exchange deadline, at most 20 seconds combined network time, or the
30-second entire attempt deadline (including startup, credential loading and
local sanitation). This cancellation boundary covers blocked DNS, TLS, headers,
body and local processing; it does not rely on a socket idle timeout. As with
any process timeout, actual return/reaping latency depends on OS scheduling.
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

Echo scanning is supplemented by a finite Results schema, also applied during
receipt replay before hashing. Only the nine named Data columns, lines 1/25,
monthly 2025/2026 periods, fixed series/unit/description values and bounded
decimal operands are admitted (at most 48 rows). Notes contain exactly one
table description and one LastRevised label. The two table descriptions are
the exact fixture sentences with or without the explicit FABRICATED prefix.
Production metadata admits the fixture sentinel or a valid YYYY-MM-DDTHH:MM:SSZ;
LastRevised admits the fixture sentinel or a valid YYYY-MM-DD. Extra columns,
rows for other lines, unknown prose, opaque encodings and unsupported dates are
rejected before request two. The fixture sentinels remain explicitly synthetic.
These conservative shapes have no real BEA response qualification: actual BEA
tables/notes may fail admission, requiring evidence, revised schema and fresh
review before any further attempt. Unknown metadata is never silently copied,
discarded or accepted on the strength of an echo scanner alone.

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
size boundaries, encoded credential echoes, replay and forged receipts. Tests
use short private supervision deadlines with the same cancellation mechanism;
the public entry retains fixed 10/20/30 second bounds. Fake HTTPS construction
checks never open a socket. This is deterministic offline fixture qualification,
not independent professional review, a real BEA response or Source acceptance.
Correction tests additionally capture actual in-memory worker IPC for wide
encodings/NUL/nested encodings and typed exception subclass/argument mutations,
exercise the production spawn protocol with fake transport, and retain positive
two-response replay and ISO metadata examples. Failed captures cannot call the
retained-bundle builder or digest and leave the result buffer and sizes empty.

Next gate:
`FRESH_INDEPENDENT_PROFESSIONAL_REVIEW_THEN_EXCLUSIVE_HOST_QA` /
`EXACT_BOUND_SOURCE_OPT_IN_NOT_GRANTED`. After those gates, a separately approved
bounded source-only attempt must produce both responses and controlled receipt;
an independent new-process source readback must establish the actual identity,
size/SHA and lineage. Garage/PG counters, storage opt-in and independent
publication/local-read proof remain separate unfinished work. The historical
Git/CI/Postflight checkpoint cannot qualify this new uncommitted candidate.
