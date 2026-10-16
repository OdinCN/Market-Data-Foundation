# BEA PCE retained candidate R1

This T04 candidate is bounded to BEA NIPA monthly T20807/T20804, lines 1 and 25,
reference July 2026 and its July 2025 index comparison. The explicit
[Source adapter](bea-pce-live-edge-r1.md) uses the standing BEA capability; real
retained source proof is still unqualified. The historical MDD text sent
by WebChat is NON-AUTHORITATIVE / LEGACY; MDF-DEV has not read MDD source code,
original API responses or its retained artifacts. It grants no migration or
cutover authority. The fixed reference is a candidate boundary, not a freshness
or historical-coverage claim.

| Measurement | Table / line | Series | Semantics |
| --- | --- | --- | --- |
| Headline MoM SA | T20807 / 1 | DPCERGM | Provider-returned calculation |
| Core MoM SA | T20807 / 25 | DPCCRGM | Provider-returned calculation |
| Headline YoY SA | T20804 / 1 | DPCERG | Product-derived index ratio |
| Core YoY SA | T20804 / 25 | DPCCRG | Product-derived index ratio |

The bounded mapping now follows the Owner-generated sanitized real Provider pair
under ignored `artifacts/t04-shape-diagnostic/`. Both Results have
Statistic=`NIPA Table`, Dimensions, 620 rows, Notes and fractional
UTCProductionTime metadata.
The shared projection retains only lines 1/25 for 2025/2026 monthly periods,
actual provider row fields including NoteRef, Dimensions, production time, the
table note and its extracted inline LastRevised. Unrelated rows/notes receive
bounded structural checks and are discarded. The committed fixtures encode the
representative shape without depending on ignored diagnostic files.

Both tables use METRIC_NAME=`Fisher Price Index`; CL_UNIT is `Percent change`
for T20807 and `Level` for T20804. T20807 declares changes from the preceding
period in a monthly context. Its real table note omits explicit SA wording;
the fixed DPCERGM/DPCCRGM mapping and paired SA level table preserve that
interpretation. T20804 must declare 2017=100 and seasonally adjusted. Required
July 2026/2025 rows, exact table/line/series/description, zero scale, values,
duplicate checks and matching LastRevised labels remain enforced. Replay
requires the exact bounded projection and consistent extracted revision.
Missing metadata, unavailable operands and incompatible units fail closed.
This correction's deterministic QA has zero real Provider effects.

YoY is `(current_index / index_12_months_earlier - 1) * 100`, formula revision
`pce-index-ratio-12m-v1`. It uses a fresh Decimal context with precision 28 and
ROUND_HALF_EVEN, then quantizes to six decimal places. Those places are a
reproducible representation, not an accuracy claim for rounded source indexes.
MoM preserves the original returned numeric string and is never recomputed from
indexes. Each YoY carries both input periods, values, table/line/series, revision
label, JSON pointers and retained SHA256. `input_version_identity` is explicitly
a content address of a retained row, **not a separate canonical DatasetVersion**.
The canonical Version binds the full retained bundle through its acquisition and
materials; the indexes are not independently published datasets.

`retain_sanitized_bundle` accepts two already received in-memory ResponseCapture
objects and the caller's credential for echo checking. It validates the returned
non-secret request parameters, removes the entire BEA Request envelope in memory,
and rejects credential echoes outside it. It performs no network or filesystem
IO. The returned bundle keeps non-secret request parameters, per-response start
and completion times and the bounded Results projection. It has
`exact_original_retained=false` and sanitizer revision `bea-pce-bounded-results-v2`. Neither the original bytes
nor an original-response digest are produced or persisted. Callers must keep
original input out of logging and store sanitized bytes only in restricted,
ignored `artifacts/` or an independently approved material store.

The retained material has grade `source_projection` and named class
`bea_sanitized_results_bundle_not_original`. Its normalized content carrier has
grade `mdf_normalized`. Publication claims only those grades, never
`provider_original`. Synthetic and `retained_unqualified` provenance remain in
the immutable content. Neither provenance qualifies Source evidence. The
candidate SourceBinding is explicitly inactive. Definition `source_qualified`
uses the kernel's source-qualified dataset semantics; it does not assert live
capability qualification or an active binding.

`build_plan` freezes acquisition identity and completion times from the retained
bundle. The reprocessing run uses supplied local processing time; it does not
rewrite the original acquisition as a new Provider request. Provider attempt
count is unknown (`None`), rather than inferring actual network effects from two
retained Results objects. UTCProductionTime and LastRevised stay metadata, with
no asserted release instant, official vintage or atomic two-table snapshot.
MDF Publication decision time is local known time, and usability must follow
that decision and the Version freeze. Actual source completion, processing and
known/usable times remain distinct.

Correction 001 enforces `obtained_at <= frozen_at <= decision_at <= usable_at`
at canonical Publication validation for the exact PCE definition/revision. This
also rejects malformed historical occurrence reuse. PG samples its own actual
clock under the publication lock and revalidates the final sample before INSERT;
clock rollback fails closed. Source/freeze times and the server decision are
never backdated or advanced to pass the rule. Ordinary Read still rejects legacy
inverted graphs. A materialization publication failure retains the existing
protected outcome-unknown/reconciliation behavior and does not auto-retry.

The sanitized retained bundle, parser, job spec and `PceMaterialIdentity` admit
1 through 2,000,000 bytes inclusive. PCE explicitly uses this named identity for
materialization, reconciliation and local Read. The normalized content embeds
those exact bytes as base64 and has a separate bounded 32,000,000-byte construction
and decoding ceiling. This PCE-only ceiling covers repeated revision-label lineage
and UTF-8-to-JSON escaping; valid dense 2 MB bundles can exceed 4 MB normalized.
The existing verified in-memory/Garage store transport and advisory
protection accept the named identity without changing CPI's `MaterialIdentity`
100,000-byte constructor limit or any CPI/PPI source limits. Digest/bucket/key
allowlists, byte-size/hash checks, no-overwrite and verified readback remain in
force. Old small PCE carriers retain the same content address; byte-different
JSON with equal values has different retained identity and lineage and cannot
reuse or overwrite the old object as equivalent evidence. These are sanitized
projection identities, with `exact_original_retained=false` throughout.

`PceLocalJob` uses `MacroJobJournal` and the T02 plan codec, exact-material
observer and RetainedRepair. To execute a candidate, explicitly submit a
`PceJobSpec(expected_raw_sha256=...)`, then `run(..., enabled=True,
preflight=..., retained=PceRetainedCandidate(...))`. Only retained mode and zero
Provider budget are admitted. Scope includes the exact retained digest so a
changed revision can have a new job while equivalent raw cannot silently create
another same-scope job. Every execution has one attempt, at most one raw create,
one ledger handoff and one Publication intent; journal intent precedes effects.
These are candidate limits, not approval to use any real infrastructure.

Raw create/readback precedes canonical Version; Publication is last, followed by
an independent local ledger snapshot and exact/as-of readback. A raw or Version
alone is unavailable to consumers. Job success is a verified **candidate
publication**, not source qualification or product completion. Ordinary
`read_local_pce` uses only the supplied ledger: no provider, journal, material
store, implicit job or write. As-of selects the latest available MDF decision
for the bounded reference; equal latest decision times are ambiguous. It retains
old exact revisions and validates each available candidate's entire graph by
reconstructing the plan and projection from embedded sanitized bytes. It does
not claim historical as-published coverage. Equivalent new acquisitions are not
semantically deduplicated across different raw bundles in this revision.

Preflight failure and cancellation before storage have no storage effects.
Partial canonical handoff and lost acknowledgements become outcome_unknown.
Interrupted running jobs require explicit reconcile; they never auto-retry.
Reconcile uses protected raw and an independent snapshot, preserves the unknown
terminal, and records confirmation separately. A fresh process cannot assume
an interrupted synthetic lock was released. Failed or cancelled jobs with a
saved plan before canonical effects, or unknown jobs separately confirmed not
published with verified raw, can register one explicit retained repair successor.
Repair must use the exact predecessor plan/digest, preserves the old attempt and
never calls BEA. Unresolved unknown and already published jobs cannot repair.
These paths have deterministic proof; no real failure/repair effects were induced.

Synthetic jobs require in-memory ledger, material store and protection. The
nonproduction adapter retains the existing owned PG/Garage protections: exact
`192.168.10.4`, exact namespace `mdf_macro_bea_pce_vertical_r1`, idle owned writer,
same-connection advisory protection and fresh independent readback. This child
adds that exact namespace to the canonical allowlist and the named temporal
validation above; no connection, schema, row or Garage mutation has been executed.
Shared publication paths also revalidate an existing occurrence and the final
PG decision with the existing canonical rules; CPI/PPI time policies are unchanged.
The BEA key is the standing Source capability in the authorized T04 scope;
explicit Source opt-in and exact budget 2 select the fixed request pair. External
grant/verifier, per-call Owner approval, B1/B2/B3 and a whole-process hard deadline
are retired requirements. Fresh independent review of this correction precedes
real Source -> Garage -> PG -> local Read execution through the existing explicit
APIs. The retained job still has zero Provider budget and never acquires from BEA.
Real Source evidence and operational storage/readback remain unproved by fixtures.

Python stays uv-managed 3.14.8 with unchanged dependencies and uv.lock. CI tests
are synthetic and reject external IO; private LAN and live-provider tests are
excluded by the existing markers. Focused QA does not substitute for the
coordinator's exclusive full Host QA, independent review, serial rebase/integration,
exact-head CI, Postflight or Owner disposition. T03 also changes the same PG
allowlist, so that hunk requires fresh conflict review during serial integration.
No main integration, commit or push is part of this candidate handoff.
