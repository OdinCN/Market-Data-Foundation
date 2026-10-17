# T08 Source/local-first compatibility correction

This replaces the eight-slot synthetic constraint request plan. It is an offline
engineering correction, not a professional PASS, Source qualification, Host QA
or T08 COMPLETE. Provider/network=0, Garage operations=0, PostgreSQL
connections/mutations=0. No control root changes or commit/push occurred.
Python remains the existing uv-managed 3.14.8 with uv 0.12.22; dependencies and
uv.lock are unchanged. Historical MDD success is non-authority input; current
official response evidence has not been acquired.

The exact-two parser/canonical contract is described in
[the offline candidate](sdmx-ecb-eurostat-r1.md). The prior R4 PASS binds the old
five-file identities; fresh independent review and Host QA must bind this
correction, including the Source/Storage code and its new tests.

## Source-seven R2 bounded offline correction

R1 professional review required correction (P0=0/P1=2/P2=1). R2 changes only
the following three boundaries and their mechanical fixtures; the exact-two,
seven-logical-read plan, three-attempt ceiling and no-fallback rule remain.
The R1 verification record below describes the preceding candidate. R2 evidence
is retained separately under ignored `artifacts/t08-source-seven-r2/`; the
original control manifest and professional review report remain unchanged.

Reconcile requires a different independent primary PG backend, exclusively held
by the caller. The existing guarded snapshot scope rejects ambient transactions
(including INTRANS/old REPEATABLE READ snapshots), driver transaction/TPC/pipeline
scopes and unknown session state without touching caller work. It starts and
verifies a new read-only REPEATABLE READ transaction from IDLE and reconstructs
Publication, partial/full lineage and local Read within that fresh committed
view. A retained writer must be IDLE with no pending driver scope; after restart,
the recorded writer PID must be absent before tickets can be reacquired. Only
successful snapshot cleanup and restored session state permit ticket release.
Any unavailable/untrusted read view leaves the handoff unresolved and protection
held; no rollback/commit of caller or writer work is used to manufacture absence.
These offline models do not prove actual PG commit visibility or concurrency.

Every Structure and GenericData message now requires exactly one Header and one
explicit, plain SDMX Test leaf with XML boolean `false` or `0` (surrounding
whitespace allowed). Missing, test/unknown values, duplicate, nested, alternate
namespace or attribute-bearing markers fail closed. Source metadata routing,
retained decode, build/validate, publication and restarted exact/as-of Read share
this gate. Synthetic positive fixtures remain labeled synthetic and use explicit
false markers; they do not establish authentic official origin or qualification.
Complete HTTP200 bytes are still retained before parsing, including rejected
test messages, and their slots cannot be queried again.

HTTPS connection-close OSError is cleanup only: it cannot revoke a completed
response or mask an earlier transport failure. Complete successful responses
reach retention with one request per logical slot and no backoff; incomplete
read/OSError and protocol failures retain their original shared-policy
classification and budgets. Garage/PG write boundaries are unchanged.

This correction performs no network/provider operation, Garage/PG write or live
qualification and does not authorize integration or declare professional PASS,
Host QA PASS or T08 COMPLETE. Current official wire/DSD/UNIT/header evidence,
real storage/session behavior and fresh independent review remain open.

## Seven-request plan for a later explicit live gate

Fixed proposed window: **2026-07-01 through 2026-09-30**, 92 inclusive days.
`B_E=https://data-api.ecb.europa.eu/service` and
`B_S=https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1`.
All requests are GET, official HTTPS only, without credentials.

| Slot | Exact identity or retained-evidence derivation |
| --- | --- |
| ECB.flow | `B_E/dataflow/ECB/EXR/latest?references=none` |
| ECB.dsd | `B_E/datastructure/<agency>/<id>/<version>?references=none`; exactly the sole concrete DSD Ref in ECB.flow, agency ECB; response identity must match |
| ECB.data | `B_E/data/EXR/D.USD.EUR.SP00.A?startPeriod=2026-07-01&endPeriod=2026-09-30` |
| ESTAT.flow | `B_S/dataflow/ESTAT/PRC_HICP_MINR/latest?references=none` |
| ESTAT.dsd | `B_S/datastructure/<agency>/<id>/<version>?references=none`; exactly the sole concrete DSD Ref in ESTAT.flow, agency ESTAT; response identity must match |
| ESTAT.unit | `B_S/codelist/ESTAT/UNIT/<version>?references=none`; exactly the unit dimension's concrete Codelist Ref in ESTAT.dsd, identity and I25 meaning must match |
| ESTAT.data | `B_S/data/PRC_HICP_MINR/M.I25.TOTAL.DE?startPeriod=2026-07-01&endPeriod=2026-09-30` |

Metadata Accept: `application/vnd.sdmx.structure+xml;version=2.1`.
Data Accept: `application/vnd.sdmx.genericdata+xml;version=2.1`.
All requests send `Accept-Encoding: identity`.

DSD/UNIT versions are evidence-bound derivations, not historical constants.
Historical MDD used `latest` for DSD and UNIT; this candidate uses the concrete
version read from each preceding Ref. The supported scoped Ref must contain that
version. Missing/unversioned/ambiguous/unsupported references STOP; no extra
request may discover an id/version. There is no ECB or Eurostat constraint slot.

Before ECB.data, flow/DSD identity, exact dimensions/order and representations
must validate. The returned exact series must then carry UNIT=USD/UNIT_MULT=0.
Before ESTAT.data, current retained DSD must match the historical candidate
`freq,unit,coicop18,geo`, point to the exact UNIT version, and the separately
retained codelist must define I25 as `Index, 2025=100`. Returned series must
confirm all four exact codes and the same DSD, directly or through the exact
retained Dataflow/StructureUsage chain. If current DSD instead says
`coicop` or anything else, STOP. No series, aliases or unit bases are substituted.
Non-unit code descriptions absent from references=none responses remain
unasserted; actual series codes are checked exactly.

The historical-wire parser revision and host-only replay command are described
in [the offline candidate](sdmx-ecb-eurostat-r1.md#historical-real-wire-replay-offline-host-only-opt-in).
Those seven immutable historical originals are not current Source evidence.
Current Source routing still pins DSD/UNIT versions and retains successful
HTTP200 bytes before parsing; no request budget or storage boundary is changed.

This is a mechanically bounded request plan, not evidence that today's official
API supports these URLs, media types, Ref serialization, dimensions or limits.
The real layer remains STOP pending fresh review/host gates and explicit live
opt-in. Any incompatibility fails closed on retained bytes; parser fixes never
authorize requery. Discovery catalogs, broader keys/windows, fallback, a hidden
codelist or an eighth/ninth automatic read are not implemented. The original
transaction ceiling remains eight logical requests, while this candidate only
admits seven, at most 21 attempts/14 retries, and 14 MB complete raw entities.

## Persistent Source accounting and retention

`SourcePlan(start,end)` requires no guessed constraint or DSD identities.
`Acquisition.acquire(..., enabled=True, exchange=...)` requires explicit opt-in
and an injected one-attempt exchange. `ExactHttpsSource` fixes hosts, paths,
headers, 10-second socket timeout and response bound, bypasses proxy/netrc and
provides no redirects, pagination, credentials, discovery or automatic fallback.
The socket timeout is not a hard process deadline against a trickling peer.

One trusted owner-only local artifacts directory belongs to the entire T08
transaction across providers, processes and corrections. Never switch roots or
delete a journal to reset allowance. Nonblocking flock protects budget.json,
bound to the exact date plan and only the seven admitted roles. Attempt intent
is fsynced before transport. Shared SOURCE_REQUEST_POLICY allows at most three
attempts per slot with transient-only 1s/2s backoff; TLS identity errors,
redirects and parser/content failures are terminal. Observations retain fixed
exception classifications/HTTP status, never exception messages or secrets.
Logical reads, attempts, retries and zero fallbacks are separate counters.

Complete HTTP200 entity bytes up to 2 MB are written unchanged and fsynced to
`<provider>.<role>.bin`, then an atomic fsynced JSON receipt is committed before
header/format/parser checks. Empty, malformed, incorrectly typed or compressed
entities are retained before rejection. Oversize entities cannot be accepted as
complete originals. Receipt includes URL/Accept/encoding, status, safe protocol
headers, requested/obtained times, attempts, digest and size. No decompression,
normalization or regeneration occurs. Local journal failures never retry Source.

Receipts are verified/reused on restart. Consumed slots without valid complete
receipts, including orphan raw files, cannot be queried again. Parser and
storage failures never reopen a slot. Older incompatible eight-slot journals
are left intact and rejected before transport; they do not receive a fresh
seven-slot allowance. Such a journal needs an explicit separate owner
resolution, never an automatic reset. This correction has no real consumed
Source slots; synthetic test roots do not represent a live transaction.
Filesystem ownership/fsync/rename are host prerequisites; hashes detect
corruption, not coordinated rewriting of all evidence.

## Storage and recovery at actual raw cardinality

The existing `sdmx_materialization` implementation iterates validated entities,
raw identities and tickets, so it now receives exactly three ECB/four ESTAT
originals. No padding raw or synthetic CC is constructed. Each provider has one
non-reusable durable handoff intent, one Version and Publication. At most seven
raw creates are possible when all seven content identities are absent; matching
objects are read/reused. T08 objects remain isolated at
`mdf-s3://mdf-artifacts/t08-sdmx/materials/sha256/<SHA256>/raw-response.bin`.
Each material size is bounded by 2 MB, using existing digest/bucket validation.
Other products' objects and material bounds are unchanged.

All actual raw tickets cover readback, graph persistence, authoritative commit
and Publication. Publication runs last with PostgreSQL's server decision clock;
graph checks occur inside its publication transaction. A PUT with unknown
outcome continues only after exact readback proves bytes and preserves unknown
disposition; no second PUT occurs. Raw-only success does not imply a Version.
External capabilities must use existing GarageMaterialStore and writer-bound
PostgresAdvisoryWriterProtection, host 192.168.10.4 and exact isolated schema
`mdf_macro_sdmx_t08_r1`. No namespace/bucket/role/service is provisioned. The
prior candidate's sole shared-kernel schema allowlist addition is unchanged by
this correction. Namespace availability/isolation must be checked at the later
explicit gate.

Any partial raw/graph/commit/publication/release failure consumes intent and
leaves outcome_unknown with protection retained when available. Reconcile uses
an independent ledger/PG backend on the same database/port/host/schema and checks
all three/four originals and partial/full graph. Present Publication requires
exact canonical local Read. Absent Publication yields confirmed_not_published
with per-raw presence, not a claim of atomic absence. Reconcile performs zero
Source reads, PUTs, graph writes or publications and never authorizes a retry.
Corruption/unproven protection remains unresolved. Release progress prevents
releasing the same successful ticket again after a later failure.

After restart, the old PG backend must be observed absent in pg_stat_activity
in a separate read-only probe transaction. Its rollback and session restoration
must succeed before the authoritative repeatable-read snapshot starts; a writer
that commits and exits after the probe snapshot starts is then visible in the
new readback snapshot. Caller transactions/old snapshots are rejected untouched.
An active or unproven old writer, probe/readback cleanup failure, or unverified
read-only/primary session leaves UNKNOWN without releasing tickets. Protection
is reacquired only after the disappearance gate, and confirmed outcomes may
release it only after successful readback cleanup. Live/reused PID blocks
recovery. Tests simulate process death by releasing synthetic tickets; a journal
alone cannot establish session disappearance. Further repair requires a separate
explicit owner gate.
Canonical no-network exact/as-of Read reconstructs all actual embedded originals
and graph, with exact durable locators. It cannot prove Garage objects still
exist; independent storage readback is separate.

## Offline verification and open layers

Verification results and exact path identities are recorded in ignored
`artifacts/t08-source-compatibility-qa/`; final results are reported with this
correction. Tests are synthetic and deny socket/DNS/psycopg connections,
including fresh-process local Reads. R1–R4 parsing/restart regressions were
migrated to actual cardinality; obsolete CC attachment cases now exercise
flow/DSD reference branches and explicit extra-source rejection. The suite also
covers DSD without embedded lists, dynamic concrete versions, I25 code/name/
identity failures, coicop18 mismatch, each role's retention, seven-slot retry
bounds, consumed/orphan budgets, mixed/raw lineage, and both families' partial
storage/restart/reconcile paths. It does not import historical success as a test
oracle for current official behavior.

Still unproven: current official endpoint/content negotiation/wire formats,
flow/DSD/UNIT identities and versions, coicop18 and I25, bounded real response
sizes, real Garage durability and unknown PUT behavior, devPG commit visibility/
server time/session loss/release, fresh external exact/as-of readback, canonical
integration, exact-head Hosted CI and independent postflight. Fresh independent
professional review and full Host QA are required for these revised bytes.

Current focused/adjacent run: **818 passed**. The command used the three
`test_sdmx_*.py` modules plus `test_source_request_policy.py`,
`test_canonical_kernel.py`, `test_fomc_statement_local.py`,
`test_fred_alfred_vintage_local.py` and `test_census_retail_vertical.py`.
Ruff check, format check (240 Python files), tracked hygiene (244 files), all
11 untracked candidate files' hygiene/whitespace, and git diff --check passed.
The new compatibility module has 39 synthetic cases, including five internally
consistent bad-UNIT graphs rejected by fresh-process exact/as-of Read.

Exact correction paths relative to the incoming 748-test candidate:

| Path | Correction |
| --- | --- |
| `src/market_data_foundation/historical/sdmx_contract.py` | Provider roles, coicop18 historical candidate, revised contract/parser identities |
| `src/market_data_foundation/historical/sdmx_official.py` | Actual raw cardinality, DSD/UNIT/series validation, no CC, explicit ECB unit evidence |
| `src/market_data_foundation/historical/sdmx_source.py` | Seven persistent slots, references=none and Ref-derived URLs, orphan-raw pretransport STOP |
| `src/market_data_foundation/historical/sdmx_wire.py` | Strict UNIT routing and shared validation seam |
| `src/market_data_foundation/historical/sdmx_local_read.py` | Actual raw count for durable whole-graph reconstruction |
| `tests/test_sdmx_official.py` | R1–R4 defense migration, both families' carrier/XML checks |
| `tests/test_sdmx_source_storage.py` | Seven-slot bounds, each role retention, both families' partial/restart/reconcile |
| `tests/test_sdmx_source_compatibility.py` | New lean-DSD, dynamic-ref, unit/dimension/lineage and consumed-budget QA |
| `docs/sdmx-ecb-eurostat-r1.md` | Corrected parser/canonical evidence contract |
| `docs/sdmx-source-local-first-r1.md` | This request plan, storage boundaries and correction report |

`sdmx_materialization.py` and `canonical/postgres.py` match the incoming
candidate SHA-256 manifest exactly; they are included in regression and the
new artifact manifest, not additional changes in this correction. No other
product source path changed.

Final sandbox deterministic run: **5119 passed, 4 failed, 1 skipped,
17 deselected, 3 warnings** in 255.01 seconds. All four failures are the existing
`tests/test_tdx_bounds.py` child pre-exec/trace guards (boundary, attempt, wall,
thread trace). Direct prerequisite readback returned PTRACE_TRACEME=-1, errno=1
(EPERM). No test/guard was weakened or skipped to hide this restriction.
This is not full Host QA PASS. The log is
`artifacts/t08-source-compatibility-qa/deterministic-pytest.txt`;
focused results, candidate SHA-256 and effect summary are stored beside it.

## Source-realwire R2 representation gate

The revision-bound [representation correction](sdmx-ecb-eurostat-r1.md#source-realwire-r2-representation-correction-offline)
closes the R1 DSD/data consistency P1 on the shared schema/parser path. Current
Acquisition stops on unsupported component formats using already retained bytes;
actual time/measure/declared attribute lexical checks also run on retained replay,
Publication/materialization and fresh-process local Read. Its six-per-provider
reviewer mutations and seven historical ORIGINAL identities are tested offline.
The seven logical slots, 21 transport attempts, retention-before-parse, Test
header, TLS/close handling, R3 writer-quiescence-before-authoritative-snapshot and
outcome_unknown ticket discipline are unchanged. This correction adds no Source
request, storage operation, provider fallback or live execution authority.
