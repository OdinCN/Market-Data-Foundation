# P3 Domain Contracts R1

`MDF-P3-DOMAIN-CONTRACTS-R1` closes the named-domain semantic gaps identified by the first P3 exit review. It sits on top of the accepted canonical kernel; it is not a collector, runtime binding, provider qualification run, storage adoption, or P4/P5 data plane.

## Contract status

All contracts in this document are semantic/product contracts.

They do **not** imply:
- an ACTIVE source binding;
- a current KD/MDD runtime descriptor;
- new provider evidence;
- production data availability;
- database/S3/QuestDB adoption;
- migration/cutover.

Historical P2 qualification results are preserved exactly as evidence boundaries.

## Dense market contracts

### Security identity / universe

Product definition:

`mdf.security.universe.a-share@p3-r1`

Identity uses an explicit security namespace. Provider suffix identities such as `600000.SH`, `000001.SZ`, or `920000.BJ` remain provider-qualified; MIC identities such as `600000.XSHG`, `000001.XSHE`, or an evidenced `920000.XBSE` remain a separate namespace. P3 does not silently equate `BJ` with `XBSE` or invent a cross-namespace mapping. A finite observed/member set may be versioned when its scope and identity namespace are explicit.

Current P3 disposition for **complete A-share universe**:

**NOT_ADMITTED**

Reason:
- TDX R1 did not prove a complete cash universe across all markets or complete listing/status semantics.
- Fuyao R1 market-filter/pagination evidence did not prove complete SH/SZ/BJ coverage.
- sector/watchlist membership, representative securities, server counts, or successful pages do not repair that gap.

Later admission needs explicit market coverage/exhaustion evidence plus listing/status and historical-membership semantics. P3 does not reopen provider requests to manufacture this evidence.

### Raw daily bars

Product definition:

`mdf.market.bars.daily.raw@p3-r1`

Contract:
- key = exact security identity + source-qualified local trading date;
- the P3-R1 normalized daily-bar contract admits `CNY` price, `shares` volume and `CNY` amount only after the source-specific mapping has established that normalization;
- source-native lots, milli-CNY or other units remain source evidence until an explicit mapping converts them; generic `unknown` / `provider-native` / `mixed` is not a unit mapping;
- provider midnight encoding is not automatically an exchange event timestamp or a UTC trading date;
- observed rows remain observed rows: missing sessions are not inserted and gaps do not imply suspension;
- repeated acquisition/source revision remains distinct from logical content identity.

TDX/Fuyao bounded daily-bar qualification remains historical source evidence; this contract does not create a binding.

### Corporate actions

Product definition:

`mdf.market.corporate-actions@p3-r1`

P3-R1 admits a normalized event only when the category and unit mapping are explicit. The only normalized helper currently represented by delivered qualification code is a cash dividend mapped to `CNY_per_share`; other categories remain source-qualified/unknown until separately mapped.

Fuyao r17 remains unresolved and is not converted to an empty event set.

### Adjustment factors

Product definition:

`mdf.market.adjustment-factors@p3-r1`

Current general canonical factor-series disposition:

**NOT_ADMITTED**

A factor may not be inferred from:
- forward/backward/qfq/hfq labels;
- adjusted-vs-raw price differences;
- a local helper name;
- a provider path that mentions adjustment factors but returned no factor evidence.

A future admitted factor contract must pin identity, dimensionless unit, effective-date semantics, revision/anchor semantics and provenance.

### Adjusted bars

Provider-returned and MDF-derived views use different product identities:

- `mdf.market.bars.daily.adjusted.provider@p3-r1`
- `mdf.market.bars.daily.adjusted.mdf@p3-r1`

A provider-returned view keeps exact provider-qualified adjustment semantics in its source binding. It is not silently rebranded as a universal MDF qfq/hfq algorithm.

An MDF-derived view requires:
- exact raw-bar DatasetDefinition and DatasetVersion;
- an explicitly admitted adjustment-input DatasetDefinition and DatasetVersion;
- transform identity and revision;
- effective parameters;
- explicit anchor policy.

For P3-R1 the only adjustment input accepted by the helper contract is the bounded `mdf.market.corporate-actions@p3-r1` input, and the only admitted anchor policy is `latest_observed_raw_bar`. The general `mdf.market.adjustment-factors@p3-r1` series is still NOT_ADMITTED, so a provider label such as qfq/hfq or an adjusted-vs-raw price difference cannot be passed as a substitute factor identity.

Missing/unknown inputs fail closed. No hidden provider fetch is allowed.

## P2 -> P3 disposition

| Capability | P3-R1 disposition | Meaning |
| --- | --- | --- |
| `security.universe` | **NOT_ADMITTED** | Complete A-share universe is not proved. Bounded explicit memberships may be represented under their actual scope. |
| `corporate_actions` | **BOUNDED_CONTRACT** | Typed source-qualified events; normalized contract currently limited to explicit cash-dividend-per-share semantics. |
| `adjustment.factors` | **NOT_ADMITTED** | No provider-independent factor series is established by current P2 evidence. |
| `bars.adjusted` | **NOT_ADMITTED** as a provider-independent contract | Future provider-view or MDF-derived admission must pin exact adjustment semantics. |

These dispositions do not rewrite TDX/Fuyao qualification labels.

## Macro release / revision / evidence

Named product definition:

`mdf.macro.bls.cpi.headlines@p3-r1`

The source-operation name `bls.cpi.headlines` is a named **candidate semantic identity** for this P3 contract. MDF does not have fresh evidence in this transaction for a current MDD runtime descriptor, build identity, or active material-retention binding, so the source candidate is explicitly **not binding-ready**.

The semantic contract separates:
- reference/report period;
- provider release/vintage time;
- MDF `obtained_at`;
- MDF `recorded_at`;
- MDF `usable_at`;
- Publication decision time.

Rules:
- a historical vintage/release time cannot backdate MDF known/usable time;
- provider revision != acquisition identity != retained artifact/evidence identity != parser revision != MDF semantic revision;
- repeated acquisitions can share a semantic dedupe key without deleting acquisition history;
- a parser correction reuses the same acquisition/artifact evidence, changes parser revision, and does not become a provider revision;
- durable/replay evidence claims require a separately admitted retention contract.

No MDD migration or S3 adoption is implied.

## KD managed source — B043

Named MDF definition:

`mdf.kd.sector-analysis.current-rank@p3-r1`

Architecture-bounded candidate:
- operation concept: `sector-analysis.current_rank`;
- source basis: `socket17000:cmd3007:plateType7`;
- acquisition mode: `current_state`;
- completeness: `bounded_current_window`;
- source-qualified fields include `strength`, `mainNetAmount` and `institutionIncrease`;
- silent `RealRankingInfo` / direct Kaipanla fallback is prohibited.

MDF may describe periodic samples as:

`sampled_current_state_history`

It may not call them:
- full snapshot;
- full universe;
- provider event-complete history.

The candidate contains **no current build/registry/operation/projection tuple**. A real MDF binding requires a descriptor observed from the current KD runtime with build revision, registry revision, operation-contract revision, projection revision and matching source variant. Architecture-era or synthetic values are rejected as runtime evidence.

This P3 contract is inactive and does not call KD.

### B044

`market-stock-ranking.current_a_share` is:

**NOT_ADMITTED_SELF_BLOCKED**

until its own runtime evidence closes dynamic-column, pagination and freshness questions. This is not a global P3 blocker.

## Offline validation boundary

The deterministic tests for this contract intentionally reject:
- complete-universe claims from bounded membership/list evidence;
- unspecified/unknown unit normalization;
- inferred missing trading sessions;
- implicit/provider-label-only adjustment transforms;
- Macro time backfilling;
- parser corrections masquerading as provider revisions;
- KD full-snapshot/event-complete claims;
- architecture/synthetic KD descriptors presented as current runtime descriptors;
- principal-scope widening.

The tests do not contact providers, KD, MDD, PostgreSQL or object storage.

## Deferred

Still deferred by design:
- active source bindings;
- new provider qualification;
- production collection/backfill;
- P4/P5 data planes;
- current KD runtime discovery;
- MDD migration/cutover;
- S3/QuestDB/Parquet adoption;
- MBO access/WebUI;
- release/deployment/activation/adoption.
