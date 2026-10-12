# MDF deferred architecture roadmap

This planning record captures Owner's 2026-10-13 long-term request/cache/source
acquisition/WebUI direction and the MDF × MBO formal technical alignment:
`AGREE_WITH_CHANGES / BILATERAL_TECHNICAL_ROUTE_ALIGNED /
DEFER_GENERIC_PLATFORM_IMPLEMENTATION`. It is design direction, not a new
canonical approval, active AGENTS rule, phase controller, implementation
authorization, runtime fact, Source Admission or adoption decision.

The owner-local evidence is
`MDF-CONSUMER-DEMAND-AND-MATERIALIZATION-ROUTE-R1/AO_ROUTE_DISPOSITION.md` and
the received
`MBO_MDF_CONSUMER_DEMAND_MATERIALIZATION_ROUTE_R1_CONSULTATION_VERDICT_20261013.md`.
MDF accepted MBO's five changes independently. The original MDF unilateral
route candidate's waiting-for-feedback status has been superseded by that
technical coordination disposition; it is not an implementation contract.

## Current boundary and near-term candidate

At the reviewed MDF main baseline
`c23d9118f2f0d2380bf81cdfbb7ed88987109b35`, P6 is complete for its bounded
engineering scope; P5/P7 remain defined only, not authorized. Existing
`mdf.consumer.read.v1` supports side-effect-free exact-version/as-of reads for
the two accepted CPI headlines and raw daily bars Definitions. The bounded
PostgresLedger and named BLS CPI Garage/S3 material path do not constitute
universal storage adoption. QuestDB has no adopted product path. Operator
status/HTML/WebEntry/WSGI is an in-process read-only surface, without production
listener/authentication or materialization policy writes. QMT, ClickHouse and
Redis realtime infrastructure is not materialized.

The independently authorized near-term candidate is only the two-stock
[Go observation bridge](p5/go-stock-observation-bridge-r1.md): synthetic
decoded shape → declared observation clocks/batch identity → existing bounded
Current-State candidate. It adds no general demand platform, persistent stream
recording or real provider operation. It does not inherit rights, clock
provenance, SourceBinding or admission from structural replay compatibility.
External host QA, fresh independent review and AO disposition are distinct
successor gates; Git delivery and live qualification require separate authority.

MBO's consultation baseline described P3.12-S1 as defined, not authorized.
Its Evidence → factual Claim work can start, under its own future authority,
from existing specifically admitted historical Evidence with bounded scope,
provenance, quality and cutoff. It is **not blocked by MDF's universal
Demand/Materialization platform**. Existing proof freshness is not extended;
new recent30/29 material is not automatically admitted. Existing P3.11/P6
read, negative and lineage proofs need no duplicate project.

## Owner direction: qualified locality and explicit acquisition

The intended experience is that MBO expresses a semantic data need; MDF finds
eligible retained material and returns it, or records an unmet need. Separately
authorized MDF operations may acquire missing/stale material from a specifically
bound source. MDF Operator can eventually configure prefetch, refresh, retention
and archival policies through a controlled WebUI. Each part is deferred until
a concrete unmet need justifies a bounded contract and independent acceptance.

| Deferred responsibility | Design constraint |
| --- | --- |
| `Read / Resolve` | Pure lookup of eligible retained Version/Publication under exact/as-of, scope, source-revision, coverage, quality and time constraints; no side effects on miss |
| `Ensure / Materialize` | Explicit intent plus separate MDF execution authority; bounded source operation, budget, frequency, scope, rights and deadline; pending identity distinct from Publication |
| Coverage + quality + time-aware locality | Locate eligible material using semantic metadata and admitted storage location; physical cache hit alone establishes no qualification |
| PG | Proposed canonical metadata, Version/Publication, lineage and bounded relational responsibilities; existing bounded paths do not authorize a general expansion |
| QuestDB | Possible dense ordered market/time-series responsibilities, only after separate design, capability qualification and adoption |
| S3/Garage | Possible immutable raw/material/archive objects and integrity identities; named existing material capability does not authorize arbitrary archival |
| WebUI Operator policy | Future controlled view/edit of prefetch/refresh, cost quotas, retention/TTL, destinations and source permissions; policy existence is distinct from activation or execution |

PG, QuestDB and S3 have different proposed responsibilities. Resolve must use
coverage, quality, temporal eligibility and locality metadata rather than a
blind PG → QuestDB → S3 fallback. Semantic source selection stays explicit and
capability-specific; physical relocation does not confer new source rights.
No provider failure may silently switch SourceBinding or SourceRevision.

Data-class policies may differ: release-scheduled CPI prefetch/versioning,
bounded post-close daily-bar acquisition and evidenced repair, and separately
qualified sampled current-state retention. There is no universal TTL or implicit
always-refresh promise. No general scheduler, async job service, Demand API,
cache index, storage tier or WebUI write implementation is authorized by this
roadmap.

## MDF × MBO responsibility and time invariants

MBO owns business demand, accepted Source/Revision constraints, actual
receipt/derivation and evaluation cutoff, Evidence/Observation/Claim/H/Prior
admission and cognition. Its request expresses semantics, not physical database
addresses. Future request concepts could include DatasetDefinition@revision,
exact Version or as-of, finite members/reference periods, permitted
quality/completeness/freshness/partial coverage and source-revision constraints;
these are not additions to the existing v1 schema.

MDF owns authorized providers and SourceBindings, acquisition/materialization,
physical PG/QuestDB/S3 choices, DatasetDefinition/Version/Publication,
coverage/quality/time/lineage metadata and Operator execution policy. MBO can
express an Ensure intent without gaining provider execution or cost authority.
Physical cache hit is not qualified Publication; acquisition success is not
Publication; Publication is not MBO Source/Revision Admission. Pending,
timeout and cancellation cannot manufacture published Evidence.

Source event time remains UNKNOWN when unproved. Requested/obtained/frozen,
Publication decision/usable and MBO receipt/cutoff retain separate identities.
A later acquisition, restoration, parser change or SourceBindingRevision must
not replace an existing exact receipt or make later knowledge visible at an
earlier as-of. New Versions/Publications preserve lineage and require their
own visibility and admission decisions.

Miss, unavailable, ambiguous-as-of, not-admitted, pending, and stale/partial
material that violates the demand are unmet requirements. Explicit future MBO
policy might permit a limited historical/local factual Claim from specifically
admitted members, but that never asserts current freshness or global coverage
and does not force an H. Existing `DEGRADED / POST_CUTOFF` fail-closed behavior
remains unchanged.

## Deferred gates

`mdf.consumer.read.v1` stays unchanged and side-effect-free: no Provider calls,
fees, source switching, persistence, jobs or historical backfill from ordinary
Read. There is no authorized v2. A future Resolve/Ensure interface, if needed,
belongs to a separate bounded public-contract and execution review.

The next demand-platform gate requires a named consumer need that already
published Read cannot satisfy, or an evidenced minimum MDF source-operation
candidate. Only after new AO authority should an incremental proof establish:
unmet need → explicitly authorized Ensure → pending with bounded operation
identity → independent eligible Publication → exact-version Read → independent
MBO admission. It must reject unauthorized execution and earlier-as-of
backfilling. It should validate only genuinely new contract behavior rather
than repeat completed P3.11/P6 proof.

This roadmap creates no implementation successor, milestone commitment,
MDF/MBO phase authorization, SourceBinding activation, data-paid interface,
Runtime, release, deployment or adoption. Active AGENTS, PHASES and canonical
approval remain outside it. P5 bridge progress and MBO P3.12-S1 decisions follow
their respective separate authorities.
