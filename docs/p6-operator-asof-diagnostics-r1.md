# Operator as-of metadata diagnostics R1 candidate

`canonical.operator_asof.inspect_as_of(ledger, definition, as_of=...)` is an
in-process read-only helper for already authorized operators. It accepts only
the existing CPI headlines and raw daily bars `p3-r1` definitions. It introduces
no service, authentication mechanism, public wire mode, or source admission.

The public `mdf.consumer.read.v1` errors deliberately carry only a classification.
This helper supplies a bounded metadata explanation of zero/multiple Publication
candidates: exact Version identities, frozen time, finite coverage, completeness,
binding record identity/limitations, and Publication identities, scopes and clocks.
Each Publication lists which decision/usable/effective clocks are still pending
at the requested instant. The last effective decision determines each candidate's
Publication eligibility using the existing consumer time rules. It never selects
a Version, combines coverage, or chooses a latest fallback.

The outcome is `as_of_no_version`, `as_of_ambiguous`, or
`one_publication_candidate`. The last outcome means only that exactly one
Publication candidate exists; the ordinary exact consumer read must still pass
content and lineage gates. The inspection fetches no content. It leaves content
integrity, binding activation, source health, current freshness, external
admission, durability and replayability explicitly `unobservable`. Finite scope
is recorded coverage, not a complete-market or missing-session claim. Freeze
and Publication clocks are not invented source event timestamps.

Candidate enumeration must be ordered, unique and consistent with exact records.
Definition, Version, binding/source, acquisition/material and complete Publication
history checks use the existing canonical/consumer gates; unpublished, future,
withdrawn and superseded candidates receive these checks too. Coverage shape is
validated before projection. Operator resolution requires each payload's complete
Key (kind, identity and revision) to match the requested Key and pins that record
for the candidate validation. Every referenced record is checked with the existing
canonical validator, including DataRun clocks, producer runs, inputs and assessment
bases. A finished run must have `finished_at >= started_at`; no missing clock is
invented. The closure traversal visits each record once, allowing assessment target
backreferences; existing lineage gates also run against these pinned records.
Any invalid candidate rejects the entire inspection
with a stable `ReadContractError` classification; no partial projection or raw
exception is returned. Raw content, receipts, controlled locators and Publication
reasons are omitted. Callers must provide a consistent read context; this helper
does not create a transactional snapshot or prove a service is available.
PostgresLedger callers can explicitly use the
[Operator snapshot candidate](p6-operator-consistent-snapshot-r1.md) to supply
that context; the default query entry remains unchanged.

The shared helper permits internal lineage validation without a Publication for
unpublished candidates and an internal resolver override for operator validation.
Public reads still pass a Publication and use their existing resolver and behavior.
Deterministic tests exercise both definitions,
zero/one/multiple candidates, all three time boundaries, transitions, corrupt
enumerations and lineage, redaction, read-only behavior, and public-wire isolation.
Independent professional review and any conditional Git delivery remain separate
gates owned by MDF Product Control.

The operator helper now admits work within fixed per-call application budgets:

| Work unit | Inclusive maximum |
| --- | ---: |
| Enumerated candidate Versions | 128 |
| Publication entries across all candidate histories | 512 |
| Newly resolved lineage records across candidate closures | 2,048 |
| Ledger `get` calls plus pinned resolver invocations, including cache hits | 32,768 |
| Metadata value visits across readback and final diagnostic projection | 1,048,576 |
| String characters across those visits | 4,194,304 |
| Characters in any one string | 16,384 |
| Entries in any one tuple or frozenset | 4,096 |
| Nested metadata depth (root is zero) | 32 |

Integer metadata also fits within 64 magnitude bits. Repeated readbacks, closure
records and metadata occurrences count again, including records shared between
candidates. String accounting uses the built-in `str.__len__` payload length,
bypassing custom subclass length and string conversion methods. This preserves
ordinary strings and canonical StrEnum values while enforcing both per-string and
aggregate character limits on readbacks, transitive DataRun metadata and the final
Version diagnostic projection. All counters reset for each inspection.
The limits are operator policy,
not DatasetDefinition coverage or consumer read limits. A legitimate larger query
is rejected rather than truncated; it needs a separately reviewed operator policy.
There is no caller override or pagination. Reference graphs remain iteratively
validated with pinned records; legitimate assessment backreferences terminate.
Deep or cyclic object shapes stop before recursive canonical validation. Exact
built-in tuple enumeration is required so unaccountable iterators are rejected.

Any exceeded budget raises `OperatorQueryBudgetError`, a `ReadContractError`
subclass carrying only the operator classification
`operator_query_budget_exceeded`. It is absent from the public consumer error
vocabulary and `mdf.consumer.read.v1`. Corruption retains classified fail-closed
behavior. Earlier candidates and intermediate projections never escape a failed
call, including failure during the final metadata accounting pass. These limits
also cover metadata omitted from the result because canonical lineage validation
must examine it. Existing private-locator redaction and unobservable states remain.

The producer-read admission candidate adds a required, operator-only
`operator_reads()` capability to the two concrete ledgers. Each inspection creates
a fresh read session and uses it for Definition, Version, Publication and every
lineage `get`. A ledger lacking that capability fails `storage_unavailable`; no
legacy unbounded read fallback occurs. The public Ledger/consumer APIs, codec,
wire contract and canonical storage schema remain unchanged. Tests that inject
already-decoded application faults now explicitly supply a narrow test capability.

| Producer admission unit | Inclusive maximum |
| --- | ---: |
| All serialized Version rows, including other definitions | 128 |
| Publication entries across requested candidate histories | 512 |
| One serialized payload in UTF-8 bytes | 1,048,576 (1 MiB) |
| Serialized payload bytes admitted across the session | 67,108,864 (64 MiB) |
| JSON lexical container nesting before parsing/decoding | 64 |
| Dictionary slots visited across memory enumeration scans | 65,536 |

These are fixed operator policy, with no caller override. Repeated exact reads
count their bytes again. Whole bounded batches pass size, cumulative bytes and
lexical depth checks before any `json.loads` or canonical `decode` in that batch.
The depth scan ignores quoted and escaped brackets. String character length is
checked before UTF-8 encoding to bound its temporary allocation; multibyte byte
length is then checked independently. Invalid encoding retains existing contextual
classifications. Any producer overflow becomes `operator_query_budget_exceeded`
and rejects the entire inspection, including earlier candidate projections.
Original application budgets still apply independently after decode.

InMemoryLedger visits dictionary entries incrementally, counting entries of every
kind before collecting at most 128 Version payload references. It rejects a 129th
Version before any batch decode or sorting, even if that row belongs to another
definition. Its first history read admits a complete global batch of at most 512
Publication payloads and decodes it once into the session cache; subsequent
histories filter that admitted cache and enforce the remaining shared application
allowance. A 513th global Publication stops before any history decode. This is
conservative: unrelated definitions, Publications and dictionary entries can
reject an otherwise small requested query. Cache creation is read-only and does
not create snapshot consistency; exact readbacks still check the enumerated data.

PostgresLedger has no separate definition column. The narrow Version query thus
limits the whole Version kind to 128 plus one overflow row, then filters admitted
records by definition in Python. Each Publication query uses the remaining shared
512 allowance plus one probe, including a one-row probe when no allowance remains.
Exact `get` predicates preserve kind/identity/revision in records, or Publication
identity in its existing table; consumer/operator checks still verify payload Keys.

One SQL statement materializes a limited batch, computes count, largest UTF-8
payload bytes and total UTF-8 payload bytes with `convert_to`, and gates every
returned payload with `CASE`. If any row-count, single-byte or remaining-byte
condition fails, the statement projects only NULL payloads and bounded scalar
markers. No overflow payload is exposed as a partial successful batch. The Python
side rechecks returned lengths and bytes, runs lexical preflight, and only then
calls the codec. `fetchall()` now consumes that limited projected result; it is
not a claim about the database's intermediate materialization. Complete admitted
Versions/histories are sorted in Python without trimming or selecting only eligible
candidates. These SQL statements perform reads only and introduce no migrations,
indexes, paging, service timeouts or transaction snapshot management.

**Capability limitation / B-END-TO-END-READ-CAPABILITY remains STOP:** offline SQL
doubles assert query structure and modeled return/decoding order. They do not
execute PostgreSQL and cannot prove real server or driver transfer behavior. The
SQL expresses bounded returned rows and payload projection; real dependency and
synthetic PostgreSQL qualification requires separate authority and evidence.
The statement requires PostgreSQL's `AS MATERIALIZED` CTE support (PostgreSQL 12
or later); an unsupported server fails classified without a legacy SQL fallback.
The offline environment uses the existing locked psycopg 3.3.6 and Python 3.14.8;
no database server version or execution plan was observed in this candidate.
LIMIT does not bound physical table/index scans, TOAST access/decompression,
`convert_to`/aggregate CPU, server intermediates, planner work, locks, elapsed time,
process memory or a production SLA. Serialized payload limits omit wire protocol
and scalar marker overhead; decoded allocation and validator revisits are not
instruction or heap bounds. The new admission narrows producer return/decode work;
it does not establish an end-to-end backend resource guarantee. Operator caller
eligibility and consistent read context remain external prerequisites. Independent
revision-bound professional review and any Git delivery remain separate gates.
