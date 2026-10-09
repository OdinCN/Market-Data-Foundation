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

**Capability limitation:** `Ledger.versions_for_definition` first materializes the
complete tuple. InMemoryLedger scans/decodes/sorts records; PostgreSQL currently
`fetchall()`s Version records and decodes them before definition filtering.
Publication histories likewise materialize before application checks, and each
record read has backend cost. The checks only bound admitted work after each
readback, before application sorting, validation or projection of that readback.
They do not bound backend IO, pre-materialization memory, CPU, wall-clock time,
wire bytes or a production SLA. Shared canonical validators may revisit bounded
metadata; these counters are explicit application units, not instruction counts.
A backend limit, paginated capability or service timeout requires independent
capability evidence and authority; this candidate does not alter Ledger or SQL.
Operator caller eligibility and snapshot consistency remain external prerequisites.
