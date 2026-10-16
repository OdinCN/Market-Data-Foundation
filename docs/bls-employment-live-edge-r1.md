# T03 Employment acquisition edge — Occam correction R1

`historical/bls_employment_live.py` is an explicit, default-closed entrypoint for
one BLS publicAPI v2 Employment acquisition. It does not register a binding,
connect to PostgreSQL/Garage, publish, or run on ordinary local Read. Existing
Employment parser, materialization, Publication and consumer time rules are
unchanged. Definition Source qualification remains `NOT_ESTABLISHED`.

The latest Owner Occam ruling treats MDF's existing BLS Source capability as
available: no additional grant channel, verifier, manifest approval or per-attempt
approval is required. This supersedes the historical grant blockers in the T03
CONTRACT/STATUS evidence, without changing Employment scope, qualification,
attempt/effect accounting or provenance. This correction turn is offline only;
no real Provider, Garage or PG operation is performed. Fresh independent review
of these changed bytes precedes the real bounded Source → Garage → PG → Read
qualification run. No further Source grant implementation is a prerequisite.
Product revision: `T03_EMPLOYMENT_SOURCE_OCCAM_R1`.

The frozen request is POST `https://api.bls.gov/publicAPI/v2/timeseries/data/`,
exactly `CES0000000001`, `LNS14000000`, `CES0500000003`, years 2025–2026,
`catalog=true`, `calculations=true`. The only additional wire field is the BLS
registration key, read in memory from owner-local
`/srv/market-data-foundation/config/local/secret.toml` (`bls.api_key`). No key
value, length, hash, secret-bearing body, URL parameter or raw exception is
reported. The request descriptor and request identity contain no credential.

`execute_live_candidate(enabled=True)` uses fixed `EmploymentSourceLimits`
by default. These limits bind source/request/parser/fixture/revision, one provider
request, 4096 request bytes, 200000 response bytes, 20 network seconds and 30
attempt seconds. Changed scope or limits fail before secret IO. The acquisition
entry has zero PG/Garage/Binding effects. `enabled is True` remains the explicit
live execution opt-in required by AGENTS; it is not a new Owner grant protocol.
No manifest path, externally granted digest or review receipt is needed to run.
`plan_sha256` preserves the historical correction-001 plan identity as provenance;
it is not an execution grant. Observations record the current revision and limits,
request identity, attempt identity, times, receipt and original-byte digest.

`bls_employment_source_control.execute_source_once(enabled=True)` delegates to
this same bounded runner. `inspect_committed_source()` separately verifies the
immutable historical evidence; it never gates current execution. A historical
commit mismatch therefore does not block use of the existing Source capability.
Source success still returns an unqualified retained candidate. Independently
check the original bytes and source-specific fixture before passing that candidate
to the existing `EmploymentLocalJob` with its exact raw digest. Its separate
Garage verification, PG canonical/Publication, independent readback, durable job
states and effect accounting remain unchanged. The retained job makes no Provider
requests. Ordinary Read never triggers either entry or creates a job.

The implementation is deliberately restricted to a single-threaded Linux process
on the qualified host. There is no platform/transport/provider fallback. An outer
process supervisor bounds the logical request from credential loading through
retention. Each wire attempt has a 20-second network bound; the shared MDF Source
request policy permits at most three transport attempts with 1s/2s backoff. At expiry it sends
SIGKILL, closing network descriptors. Parent-death SIGKILL also prevents an orphan
network child. Local processing cannot extend the network deadline. IPC uses a
bounded, deadline-checked local pipe. Only strict UTF-8 JSON primitive envelopes
cross it: exact version/attempt/ok/result keys, duplicate-key rejection, fixed
classification enum, integer transport attempts 0–3, HTTP status null or integer 100–599,
exact response/acquisition schemas, bounded timestamps/body/frame and no executable
object deserialization. The frame cap is 1300000 bytes, including JSON escaping;
original body remains capped at 200000. Both ends validate. Arbitrary callback
exceptions (including forged acquisition errors and their fields) become fixed
safe transport errors. Error messages, URLs, callback objects and exception
objects never cross the pipe. Parent reconstruction uses only validated primitives
and fixed source-specific constructors. Process reap uses
only the remaining deadline (up to 0.1 second); expiry adds no blocking reap wait.
OS scheduling is not a realtime guarantee.

There is one logical BLS POST request. The canonical MDF Source request policy
permits at most three transport attempts for transient network/timeout failures or
HTTP 408/429/500/502/503/504, with 1s/2s backoff. Redirects, authentication or
request failures, non-identity encoding, ambiguous/chunked framing, duplicate
headers, invalid/oversized Content-Length, body overflow, credential screening and
contract failures are terminal and never retried. There is no pagination or
Provider fallback. The HTTP reader asks for at most 200001 bytes; accepted
original bodies are at most 200000 bytes. A declared length must match the
delivered original body. The shared attempt counter records actual wire attempts
inside one parent-known nonsecret UUID logical attempt identity. Once a complete
response is obtained, parser/artifact/storage work cannot requery BLS.

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
overwrites an existing run. A write/cancel failure can leave safe partial files; local artifact/storage
outcome remains subject to existing readback/reconciliation and never triggers a
new Provider request. Before secret loading or transport, the supervised logical
request fsyncs an exclusive, read-only `attempt-<UUID>.pending.json` under the
ignored protected root. It contains only intent identity, relative run/release
names and the shared Source transport policy identity/budget. It is immutable
history, never proof of a successful request. On normal unwinding,
a separate exclusive `attempt-<UUID>.writer-closed.json` records artifact handle
closure and safe outcome/count; it explicitly leaves worker reap pending. The
parent's safe observation reports actual reap only when observed. Cancellation may leave only the pending receipt and safe partial files at the
already known run path. A release-write failure cannot become success and cannot
cause a new Provider request. These safe control receipts may remain on a
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

Offline coverage includes explicit opt-in and scope/limit rejection, slow drip/blocked workers,
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
invalid limit descriptors, partial response,
partial artifacts, cancellation and pending receipt overwrite. Positive cases
retain exact UTF-8 originals, round-trip safe primitive IPC, and preserve
CES/CPS/AHE semantics. Executor tests do not close the independent review findings.

Correction 002 adds negative cases across URL/JSON/HTML/Base64/Base64url/hex/
Unicode forms, prefixes, prose, attached text, spacing and nested encodings in
message values, nested values and nested names. Nonrepeating synthetic dummy
values and binary surroundings exercise alignment without using owner secrets.
Parser, digest, evidence, plan, candidate, observation and payload writer entry
are forbidden in each rejection test. Budget exhaustion and unresolved harmless
deep encoding also reject before these boundaries. Non-sensitive encoded
CES/CPS/NFP/AHE descriptions pass with sixteen synthetic attempt identities per
encoding and retain exact originals and identical measurement projections.
These are executor self-checks. Historical review results describe their frozen
bytes; the Occam correction requires fresh independent review.
