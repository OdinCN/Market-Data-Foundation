# T03 Employment acquisition edge — Correction 002 candidate

`historical/bls_employment_live.py` is an explicit, default-closed entrypoint for
one future BLS publicAPI v2 Employment acquisition. It does not register a binding,
connect to PostgreSQL/Garage, publish, or run on ordinary local Read. Existing
Employment parser, materialization, Publication and consumer time rules are
unchanged. Definition Source qualification remains `NOT_ESTABLISHED`.

Current authorization is **offline implementation/qualification only**. The
provider/PG/Garage/activation budgets are all zero. No invocation example granting
live authority is supplied. The next gate is
`FRESH_INDEPENDENT_PROFESSIONAL_REVIEW_REQUIRED / NO_REAL_OPT_IN`.
Product revision: `T03_EMPLOYMENT_LIVE_EDGE_CORRECTION_002`.

Post-T04 authority binding correction R1 fixes only the admitted base to
`27437d6b34d43295a32f66a3a786e8e65faae452`, on
`mdf/t03-employment-r1-20261016`. It retains the correction-002 product revision;
the new external manifest SHA distinguishes these changed source/test/doc bytes.
The manifest cannot authorize another Git revision or branch by declaring one.
Both its base and actual Git HEAD must equal this fixed baseline. The caller's
independently granted manifest SHA must match before any manifest claims are used.
This admits only the exact three uncommitted candidate paths, with matching bytes
and digests and no other tracked changes or nonignored untracked files. Symlinks
in the manifest or product paths remain rejected. Ignored runtime artifacts keep
their existing contract and do not grant authority.

After a commit, HEAD changes and those paths cease to be the exact dirty set;
runtime admission therefore fails closed even if the branch name is unchanged.
A rebase, branch change or committed delivery needs a new explicit binding design,
fresh manifest, independent review, applicable Host QA and separately granted
exact Source opt-in. This correction supplies none of those future grants and
does not enable arbitrary Git revisions or committed operation.

The frozen request is POST `https://api.bls.gov/publicAPI/v2/timeseries/data/`,
exactly `CES0000000001`, `LNS14000000`, `CES0500000003`, years 2025–2026,
`catalog=true`, `calculations=true`. The only additional wire field is the BLS
registration key, read in memory from owner-local
`/srv/market-data-foundation/config/local/secret.toml` (`bls.api_key`). No key
value, length, hash, secret-bearing body, URL parameter or raw exception is
reported. The request descriptor and request identity contain no credential.

`execute_live_candidate` rejects unless `enabled is True` and an exact
`EmploymentAuthority` descriptor binds the reviewed candidate manifest, review
receipt digest, correction-001 plan, source/request/parser/fixture/revision and
fixed numeric limits. Before loading credentials it checks the actual manifest
SHA, current base and branch, the exact three changed product paths, and each
product file's bytes and SHA (including this adapter and its tests). The caller
must supply an absolute immutable manifest path and its externally granted SHA;
there is no historical manifest default. Missing identity fails before secret IO.
The manifest's `product_revision` names this code revision; its review-label
`revision` may differ. The code does not embed the manifest's digest or depend on
future control-file hashes. A descriptor's construction does not establish Owner permission or an
independent review. Future Owner opt-in must explicitly bind the reviewed code,
manifest, review and request; present credentials do not establish authority.

The implementation is deliberately restricted to a single-threaded Linux process
on the qualified host. There is no platform/transport/provider fallback. An outer
process supervisor bounds the attempt from credential loading through retention
at 30 seconds. A nested supervisor bounds the entire network exchange (including
DNS/connect/TLS/headers/slow body) at 20 monotonic seconds. At expiry it sends
SIGKILL, closing network descriptors. Parent-death SIGKILL also prevents an orphan
network child. Local processing cannot extend the network deadline. IPC uses a
bounded, deadline-checked local pipe. Only strict UTF-8 JSON primitive envelopes
cross it: exact version/attempt/ok/result keys, duplicate-key rejection, fixed
classification enum, integer attempts 0–1, HTTP status null or integer 100–599,
exact response/acquisition schemas, bounded timestamps/body/frame and no executable
object deserialization. The frame cap is 1300000 bytes, including JSON escaping;
original body remains capped at 200000. Both ends validate. Arbitrary callback
exceptions (including forged acquisition errors and their fields) become fixed
safe transport errors. Error messages, URLs, callback objects and exception
objects never cross the pipe. Parent reconstruction uses only validated primitives
and fixed source-specific constructors. Process reap uses
only the remaining deadline (up to 0.1 second); expiry adds no blocking reap wait.
OS scheduling is not a realtime guarantee.

There is one POST, no retries, redirects, pagination or fallback. HTTP errors,
non-identity encoding, ambiguous/chunked framing, duplicate headers, invalid or
oversized Content-Length and actual size overflow fail closed. The HTTP reader
asks for at most 200001 bytes; accepted original bodies are at most 200000 bytes.
A declared length must match the delivered original body. Transport failures and
lost/partial responses remain unknown and never cause automatic reacquisition.
The shared attempt counter changes only immediately before transport entry;
network outcome is still unknown on cancellation even if transport began. Before
forking any effect worker, the parent generates a nonsecret UUID attempt identity.
It travels through both IPC envelopes, safe observations, acquisition/run identity,
artifact directory and pending/release receipt names. Unknown with transport entry
continues to count one attempt. There is no identity guessing or automatic retry.

Echo screening runs in memory before parsing/digest/reporting/persistence. Original
BLS JSON must be strict UTF-8 with no BOM or raw NUL; UTF-16/32 and unknown byte
encodings fail closed. Screening inspects literal, nested JSON/URL escapes, HTML
entities and nested Base64/Base64url/hex text, including encoded UTF-16/32 and
whitespace-separated encodings. Hex runs are scanned independently of Base64;
`0x`, `reply=`, repeated `=` separators and ordinary sentence text cannot swallow
the encoded value. Two hex and four Base64 alignments inspect attached prefixes
and suffixes without enumerating every substring. Unicode compatibility forms
are normalized in rejection probes. Binary prefix/suffix noise is ignored only
in rejection probes; accepted original JSON still requires strict UTF-8. Decoded
probes never replace retained bytes. Lossy binary probes retain their provenance:
they still check credentials, sensitive names and nested encodings, while unknown
escape syntax is enforced on the original and strictly decoded text views. This
prevents ordinary digest bytes from asserting text syntax. Invalid UTF-8 URL
escapes and unsupported Unicode escape syntax in strict text fail closed.
Every candidate view, including duplicates,
and every encoded-run scan consumes a cumulative limit of 20,800,000 character
work units. There are at most 4096 distinct views, eight decode levels and
1,300,000 characters per view. Unknown escape syntax, unresolved deep encoding
or exhausted screening limits fail closed. It checks the actual credential and sensitive names
in all locations, including messages, then screens all candidate files/envelope
again. Rejected originals are never redacted, hashed into a receipt, embedded into
a source envelope or saved. Offline rejection fixtures create zero artifacts.
Safe failure observations contain only fixed source identity, validated bounded
classification/count/status, parent-known attempt/relative receipt names and
writer-reap state. They contain no rejected bytes or exception text.

A future accepted response creates an **unqualified** retained candidate, its
source envelope and a receipt marked
`RETAINED_UNQUALIFIED_PENDING_INDEPENDENT_READBACK`. The writer verifies the fixed
worktree `artifacts/t03-source-qualification-r1/` is Git ignored, rejects symlinks,
uses a new exclusive run directory/files, fsyncs exact originals, and makes files
and the completed directory read-only. It writes the observation last and never
overwrites an existing run. A write/cancel failure can leave safe partial files;
the receipt outcome is unknown, with no automatic cleanup, retry or repair.
Before secret loading or transport, the supervised attempt fsyncs an exclusive,
read-only `attempt-<UUID>.pending.json` under the ignored protected root. This
contains only intent identity, relative run/release names and `retry_allowed=false`.
It is immutable history, never proof of a successful request. On normal unwinding,
a separate exclusive `attempt-<UUID>.writer-closed.json` records artifact handle
closure and safe outcome/count; it explicitly leaves worker reap pending. The
parent's safe observation reports actual reap only when observed. Cancellation
may leave only the pending receipt and safe partial files at the already known
run path. Missing release evidence remains unknown and blocks retry. A release
write failure cannot become success. These safe control receipts may remain on a
future rejected live attempt; no rejected provider payload is written.
Independent original-byte readback and qualification are still required. This is
logical immutability in this API, not filesystem immutable flags or durable S3
proof. No storage handoff is performed.

`requested_at <= obtained_at <= finished_at <= frozen_at` is validated by the
existing Employment evidence/plan contract. `obtained_at` is sampled inside the
network worker when the response is delivered to the adapter, before echo/parser
work or IPC. Source known time is obtained time. Publication known and consumer
usable times remain null: no Publication exists. The existing Publication and
local Read enforce decision/usable/as-of bounds separately.

The source-specific pre-request fixture is
`t03-bls-employment-sa-2026-09-prior-two-months-qualification-r1`, capability
`timeseries.data:CES-CPS-employment`. `qualify_offline` requires a transport marked
`effect_domain="offline"`, dummy material and optional injected monotonic clock.
The fixture in `tests/test_bls_employment_live.py` uses only fake transport and the
synthetic CES/CPS rows/catalogs shared with the existing T03 regression. It reports
zero real provider requests, one simulated attempt, complete/missing-field state
and `NOT_ESTABLISHED`. It cannot issue an external receipt or qualification
verdict. The existing generic qualification capability enum covers other domains;
this source-specific contract does not expand or mislabel that shared enum.

Offline coverage includes default gates and drift, slow drip/blocked workers,
redirect/timeout/status failures, exact byte boundary and false Content-Length,
credential traps before digest/IO, required series/fields/catalog/SA/surveys/units/
populations/operand lineage, CES level differences, preserved provider AHE
percentages, time reversal, ignored/exclusive/read-only retention and partial
write failure. All full-pipeline receipts created in tests are simulations under
temporary directories, with fake credentials and denied real socket/PG access.
They are not real source evidence.

Still unknown: actual BLS endpoint/key entitlement, original response/catalog and
calculation shape, real receipt identity, independently qualified original raw,
PG/Garage capabilities and real lost-connection/reconciliation/repair behavior.
Parser acceptance and synthetic fixture PASS do not establish these facts.

Correction tests additionally reject encoded originals before parser/digest/writer,
poisoned error fields, executable pickle frames, malformed/oversize JSON IPC,
missing caller manifest identity, stale source/test bytes, partial response,
partial artifacts, cancellation and pending receipt overwrite. Positive cases
retain exact UTF-8 originals, accept a differing manifest review label with the
correct product revision, round-trip safe primitive IPC, and preserve CES/CPS/AHE
semantics. Executor tests do not close the independent review findings.

Correction 002 adds negative cases across URL/JSON/HTML/Base64/Base64url/hex/
Unicode forms, prefixes, prose, attached text, spacing and nested encodings in
message values, nested values and nested names. Nonrepeating synthetic dummy
values and binary surroundings exercise alignment without using owner secrets.
Parser, digest, evidence, plan, candidate, observation and payload writer entry
are forbidden in each rejection test. Budget exhaustion and unresolved harmless
deep encoding also reject before these boundaries. Non-sensitive encoded
CES/CPS/NFP/AHE descriptions pass with sixteen synthetic attempt identities per
encoding and retain exact originals and identical measurement projections.
These are executor self-checks; the prior independent
P1 remains pending fresh professional review of the newly frozen candidate.
