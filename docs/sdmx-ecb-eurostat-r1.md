# T08 exact-two official SDMX offline candidate

The current engineering candidate adds bounded historical real-wire compatibility
to the Source-seven R3 candidate. It is offline only, not
professional PASS, Source qualification or T08 COMPLETE. Provider/network,
Garage operations and PostgreSQL connections/mutations in this correction are
zero. No controls, dependencies, uv.lock, commits or pushes are changed.

The 2026-09-17 MDD seven-read success is historical non-authority engineering
input, observed read-only on 2026-10-17 at clean MDD main
`6222de9a4cf54861ca87da57ec0c67d72a584d2a`. Its Eurostat dimension identifier
was `coicop18`. These facts inform a candidate, not a current API assertion.
The task input is
`transactions/MDF-MACRO-SDMX-ECB-EUROSTAT-VERTICAL-R1/T08_HISTORICAL_SOURCE_COMPATIBILITY_INPUT.md`
in the control workspace. No MDD code, runtime or dependency is imported into
the product; historical raw evidence stays external under ignored artifacts.

The only admitted surfaces remain ECB `EXR / D.USD.EUR.SP00.A` and Eurostat
`PRC_HICP_MINR / M.I25.TOTAL.DE`. Eurostat dimensions are the historical candidate
`(freq, unit, coicop18, geo)`. A different current identifier/order, including
`coicop`, fails closed. There is no alias, alternate series or provider fallback.

`sdmx_contract.roles_for` requires exactly these ordered independent originals:

| Family | Raw/acquisition roles | Version materials |
| --- | --- | --- |
| ECB | flow, dsd, data | 3 ORIGINAL + 1 PROJECTED |
| ESTAT | flow, dsd, unit, data | 4 ORIGINAL + 1 PROJECTED |

No contentconstraint read, identity, version, synthetic carrier or hidden source
is admitted. Each original retains exact bytes, SHA-256, size, role, URL,
requested/obtained UTC instants and recorded attempts. Each entity is at most
2 MB; the family totals are therefore at most 6 MB and 8 MB. The canonical
carrier embeds every original and receipt, bounded by the existing 32 MB content
limit. XML remains UTF-8 SDMX 2.1 Structure/GenericData, at most 30,000 nodes per
entity; DTD/entity, alternate encodings and unsupported formats fail closed.
StructureSpecificData, JSON, ZIP and arbitrary URN/reference variants remain
unsupported. No schema URL, annotation URL or external codelist is fetched.

The flow must identify the exact agency/flow and one concrete versioned DSD
Ref. DSD response identity must match it exactly. No DSD id/version is frozen
from historical notes or invented by the planner. Both metadata URLs use
`references=none`; concrete versions are used after reading the Ref to avoid
changing `latest` between reads. DSD dimensions, positions, TIME_PERIOD,
OBS_VALUE and each dimension's single enumeration reference are checked.

A references=none DSD need not embed codelists. If it does, duplicate identities,
missing/wrong target codes or meanings and malformed Name leaves still reject.
Absent descriptions remain `meaning=null`; fixed candidate descriptions are
never presented as observed provider meanings. The exact returned SeriesKey
mechanically confirms all target dimension codes. ECB additionally requires
explicit series attributes UNIT=USD and UNIT_MULT=0, and rejects conflicting
units/multipliers at any data level.

Eurostat's unit dimension must reference exactly agency ESTAT/id UNIT with a
concrete version. That exact UNIT codelist is the fourth-family metadata role,
not an implicit DSD attachment or a discovery call. Its identity must equal the
DSD reference; I25 must exist and its English meaning must match
`Index, 2025=100` (case/formatting whitespace normalization only). Conflicting
embedded/external UNIT evidence rejects. DSD + UNIT metadata validation gates
the data request. The response must then reference the same DSD directly, or
reference the exact retained Dataflow through StructureUsage and its DSD, and return
exactly `freq=M, unit=I25, coicop18=TOTAL, geo=DE`. UNIT attributes, if returned,
must also agree. Only the fully validated response becomes an accepted candidate.
There is no claim of a provider availability constraint or that preflight can
predict whether the provider will actually return a series.

R1–R4 defenses continue through build, validate, publication and fresh local
Read: exact cardinality at every required parent; single scoped Ref branches;
strict ObsValue, ObsDimension, Name, Attributes/Value and SeriesKey leaves;
no nested conflicting values, substantive mixed text, extra/namespaced value
attributes, duplicate/missing/wrong dimensions or alternate reference targets.
Constraint-specific obsolete fixtures have been replaced with corresponding
flow/DSD extra-reference rejection tests and explicit extra-constraint raw
rejection. Legal missing/formatting/reordered-key cases remain covered.

Absent, empty and colon observation values remain unavailable, never zero.
Unknown data attributes are preserved with no invented meaning. Returned cells
only define finite scope; omitted periods remain absent. Eurostat is the latest
retrieved snapshot: provider_vintage is null, revision state/freshness/release
currency are not asserted and overwritten vintages cannot be recovered.
Prepared/LAST_UPDATE/validFromDate remain provider metadata, separate from MDF
requested/obtained/finished/frozen/Publication decision/usable time.

`build_plan` creates provider-specific Source/Binding/Definition families,
acquisitions and originals at the actual cardinality. Parser and contract
identities are now `t08-sdmx21-generic-historical-wire-r2` and
`t08-historical-wire-r2`; prior approval does not transfer to these bytes.
Previously synthetic constraint-based graphs are not auto-migrated or accepted.
`validate_plan` reconstructs the complete plan and compares it exactly.
`publish_offline` still admits only the exact InMemoryLedger reference adapter.
Its budget is zero because retained replay performs no new Source calls.

`read_local_sdmx` reconstructs every actual original, projection and complete
Source/Binding/Definition/run/acquisition/material/Version/Publication graph.
Durable detection uses the actual raw count and cannot mistake the PROJECTED
carrier for an extra original. Mixed embedded/durable originals and arbitrary
locators reject. Exact Read preserves provider/window identity. As-of chooses
the usable matching-window version with latest acquisition obtained time;
equal-time candidates reject as ambiguous. Publication time only gates
availability. WITHDRAW/RESTORE/SUPERSEDE semantics remain intact. Read has no
Source, journal, Garage or write dependency.

See [Source/local-first plan](sdmx-source-local-first-r1.md) for the seven
request identities, consumed-budget rules, protected materialization/reconcile,
verification and remaining gates. Current official API behavior, external
persistence and real exact/as-of reconstruction remain unproven.

## Historical real-wire replay (offline, host-only opt-in)

The seven retained 2026-09-17 MDD originals are inputs, never current MDF Source
qualification. They cover 2026-07-01 through 2026-09-16. Tests accept only an
explicit `--sdmx-historical-evidence` path under ignored `artifacts/`, verify
the seven independent raw SHA-256/byte sizes and each evidence.json digest,
and preserve every original byte, receipt URL and historical acquisition time.
No local path is built into product code. Missing opt-in skips historical tests;
an explicitly supplied missing/corrupt directory fails, without network fallback.
There are no checked-in historical XML bytes. CI grammar fixtures remain small
and explicitly synthetic. Historical receipts do not record transport attempts;
replay retains that count as null, not an invented successful attempt count.

Run using the existing uv-managed environment, without syncing or downloads:

```sh
uv run --offline --no-sync --locked pytest -q tests/test_sdmx_historical_wire.py \
  --sdmx-historical-evidence=/path/to/ignored/artifacts/evidence
```

`sdmx_schema` implements a bounded metadata grammar, not a general SDMX XSD
validator. It checks namespace URIs (prefix spelling is immaterial), unique
containers, known fields/children, plain annotation/name leaves and scoped Ref
shapes. Maintainable DSD/Codelist Refs admit only their class/package pairs;
ConceptIdentity admits only the exact concept/agency/scheme and a concrete
maintainableParentVersion. Local group and attribute relationship references
admit only existing dimensions or OBS_VALUE. Extra attachment targets, URNs in
these scopes, hidden children/text, alternative Ref namespaces, unknown fields,
conflicting URN identities and mixed representations reject. The ECB
EnumerationFormat sibling is checked, including lengths of requested key codes.
External concepts and absent codelist meanings are not materialized or inferred.

ECB GenericData admits one header-only unqualified URN exactly naming the
validated DSD; it cannot appear beside another attachment. Eurostat's classless
StructureUsage Ref identifies Dataflow ESTAT/PRC_HICP_MINR/1.0, whose retained
Structure points to DSD ESTAT/PRC_HICP_MINR/23.0. These are different object
identities, not interchangeable versions. Both complete chains are validated
on build, validation and restarted Read using the same parser. Historical
metadata URLs ending in latest are preserved and admitted only with matching
concrete response/reference identities. Current Acquisition still requests the
concrete DSD and UNIT versions extracted from preceding retained references.

The historical UNIT original is 489788 UTF-8 bytes; its I25 English Name is
exactly `Index, 2025=100`, and its ESTAT/UNIT/82.0 identity matches the DSD unit
reference. `coicop18` remains at position 3 and returned series codes must match
all four dimensions, even when the wire SeriesKey uses reverse order. Tests
cover wrong base, code, version, dimension position, scope and hidden content,
including adversarial in-memory copies of historical bytes; original evidence
files are never overwritten. ECB has 56 returned daily cells; Eurostat has only
July/August cells (103.20/103.36), with no fabricated September cell. Historical
replay has no explicit missing cells; synthetic regressions retain all supported
missing forms and omitted-period semantics.

The replay creates only synthetic InMemory Publication at MDF processing time,
then reconstructs original carriers and lineage through no-network exact/as-of
Read, snapshot restore and a fresh Python process. Header Prepared/Extracted,
validFromDate and annotations remain provider metadata; they never establish
MDF availability or an overwritten historical vintage. Historical parser
compatibility proves no current wire/media/header/DSD/key/unit compatibility.
Fresh revision-bound independent review and full Host QA remain mandatory;
current Source and Garage/devPG qualification, integration/Hosted CI and final
postflight remain separate gates. The R3 two-snapshot reconciliation and closed
Header/Test/HTTP200 retention boundaries are preserved.

## Source-realwire R2 representation correction (offline)

The single R1 professional P1 was acceptance of data contradicting its DSD
representation. R2 validates representation by provider and component role at
the existing strict schema seam, then checks original lexical values through
`project`. Acquisition admission, build, plan validation, Publication,
materialization and retained exact/as-of Read all reach that same validation.
No alternate decoder or caller-supplied schema can bypass reconstruction.

| Role | Bounded supported representation |
| --- | --- |
| Code dimension | Codelist Enumeration, optional String EnumerationFormat; exact admitted series codes and available inline code membership remain checked |
| TIME_PERIOD | Explicit TextFormat ObservationalTimePeriod, then ECB daily date or Eurostat monthly calendar validation; String, BigInteger, Double and Duration are unsupported here |
| ECB OBS_VALUE | Explicit String TextFormat (historical maxLength=30, isSequence=false); actual available values must still be positive finite ASCII numeric lexicals |
| Eurostat OBS_VALUE | Explicit Double TextFormat; actual available values must satisfy the numeric grammar and finite Double range, then the existing positive measure rule |
| Declared attribute | String, BigInteger or Double TextFormat; String or BigInteger EnumerationFormat on an Enumeration. Check actual lexical type, lengths and available inline membership. No time or measure semantics are inferred |

Every declared component requires its local representation: external concept
references cannot substitute for missing format evidence. All present minLength,
maxLength and isSequence fields are enforced; sequences, incompatible types and
measure/time Enumerations fail closed. Length is checked on the original XML
attribute value, before numeric normalization, so 103.20 has six characters.
An absent/empty/colon observation remains explicitly unavailable; numeric type
and length checks apply to available values, while schema-role checks apply even
if every observation is missing. A retained String measure is not an arbitrary
string or a duration. ECB DECIMALS BigInteger attributes remain legal and receive
integer lexical checks; BigInteger is not a global forbidden type.

As before, a references=none Codelist with no retained codes is a concrete
reference, not an inferred list or proof of its contents. Actual fixed dimension
codes are checked against the exact supported series; any retained inline list
must contain the actual code. The separate Eurostat UNIT original must still
prove I25 and its meaning. Undeclared data attributes remain opaque, preserved
provider metadata. There are no new requests to resolve concepts or codelists.

Tests repeat all six reviewer mutations for each provider: frequency BigInteger,
time BigInteger, measure BigInteger, measure maxLength=1, unresolved measure
Enumeration, and measure Enumeration with a complete codelist containing only 0.
Both synthetic fixtures and in-memory copies of SHA-verified historical
originals must reject at build/validate/Publication/materialization, serialization
restart and fresh-process exact/as-of Read. Enabled Acquisition uses an offline
exchange, retains HTTP200 before rejection and cannot requery after reopening.
Historical originals also traverse positive build/validate/InMemory Publication,
durable materialization and fresh-process Read without any byte substitution.
Default CI still skips host-only historical cases without explicit evidence opt-in.

This is an offline engineering correction only. Fresh professional review, full
Host QA, integration and exact-head Hosted CI, current official Source evidence,
real Garage/devPG qualification and final postflight remain pending. No T08
completion, production or release claim follows from these tests.
