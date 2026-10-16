# BLS Employment retained candidate R1

T03 is an unqualified, producer-local CES/CPS candidate. Its explicit bounded
[live acquirer](bls-employment-live-edge-r1.md) uses the existing MDF BLS Source
capability without an additional grant/verifier or per-attempt approval. It has no
active SourceBinding or expansion of `mdf.consumer.read.v1`. The qualified Python
baseline and dependencies remain unchanged. Synthetic tests establish engineering
behavior; this revision has no original Employment response, real Garage raw,
or real PostgreSQL Publication proof. `BLS_EMPLOYMENT.source_qualified` is false.

The fixed target is September 2026 with July/August overlap. A credential-free
request descriptor names POST `https://api.bls.gov/publicAPI/v2/timeseries/data/`,
2025–2026, exactly three series, `catalog=true` and `calculations=true`.
[The official API signature](https://www.bls.gov/developers/api_signature_v2.htm)
supports those options for registered users. Its examples do not establish the
exact Employment catalog labels or calculation response paths below. Those paths
remain candidate requirements until independently checked against qualified
original bytes; missing or differently shaped fields are rejected.

| Measure | Series | Required source field | Meaning |
| --- | --- | --- | --- |
| NFP monthly net change | CES0000000001 | calculations.net_changes.1 | CES total nonfarm payroll jobs, thousands, SA |
| Unemployment rate | LNS14000000 | value | CPS people, age 16+, both sexes/all races, percent, SA |
| AHE MoM | CES0500000003 | calculations.pct_changes.1 | CES all employees, total private, percent, SA |
| AHE YoY | CES0500000003 | calculations.pct_changes.12 | Same AHE population, percent, SA |

CES employment measures jobs, while CPS measures people; the surveys remain
separate in each projected observation. See [BLS CPS technical documentation](https://www.bls.gov/cps/documentation.htm).
AHE supporting values are dollars per hour, consistent with [official Table B-3](https://www.bls.gov/news.release/empsit.t19.htm).
Each measurement preserves its catalog, exact JSON pointers, current source value,
comparison levels, periods, units and footnotes. NFP is checked against the
same-response difference in levels. AHE percentages are preserved as returned,
without recalculation from rounded levels. There is no product-derived fallback.

One Version contains all twelve measurements across the three reference months;
`read_local_employment(reference=...)` validates that requested reference but returns
the complete snapshot and lineage. This is a deliberately bounded snapshot
contract, not per-month atomic commits or a rolling backfill service. A changed
raw digest permits a separate explicit job for the same target. The journal
reserves a target+raw-digest scope; identical bytes cannot register another ordinary
job in that journal. Versions are acquisition snapshots, without economic semantic
deduplication across differing response bytes. Repeated execution of one job is
idempotent. No automatic refresh or benchmark-history coverage is claimed.

The [BLS CES revision documentation](https://www.bls.gov/web/empsit/cesnaicsrev.htm)
describes two monthly revisions before annual benchmarking. CPS revisions have
their own seasonal methodology and are not labeled CES revisions. `P` footnotes
are preliminary context; absence of `P` does not assert finality. Scheduled and
actual release instants, provider revision and vintage are unknown in this
candidate. Obtained/requested time comes from the supplied original acquisition,
not the time of a retained run. Frozen, Publication decision and usable times
remain independent and cannot precede acquisition.
Employment publication checks that decision and usable times follow source
obtention and Version freeze. Its PostgreSQL adapter checks the final server
timestamp inside the existing publication transaction, before INSERT/COMMIT.
Local Read independently rejects legacy publications with inverted times; it
does not rewrite their history.

`EmploymentRetainedCandidate` carries `EmploymentEvidence` and its exact
`EmploymentPlan`. Build with `build_plan`, then explicitly submit an
`EmploymentJobSpec(expected_raw_sha256=...)` and run with `enabled=True`, a preflight
and that retained candidate. Provider budget is always zero. Raw creation, ledger
handoff and Publication each have a budget of one. A caller must qualify the raw
identity and acquisition metadata separately before nonproduction effects.
There is no secret loader, transport, Provider fallback, retry loop or scheduler.
Runtime files and any real raw remain in ignored `artifacts/`.

Materialization validates the entire plan before storage, creates/verifies exact
content-addressed raw under writer protection, persists canonical Version/content,
and publishes last. Unknown PUT with verified readback is recorded as
`unknown_reconciled_present`; it never claims this attempt created the object.
Raw or Version alone cannot be read as a Publication. The candidate reuses
`MacroJobJournal`, typed plan codec, protected observation and `RetainedRepair`.
Preflight failure/cancel, interrupted execution, unknown commit/publication,
explicit reconcile and a single retained repair successor retain separate history.
Reconcile never retries writes. Unknown attempts stay unknown after confirmed
presence/absence. Repair requires the predecessor's exact plan/raw and either a
safe pre-storage terminal or raw-verified confirmed-not-published reconciliation.
Unresolved or raw-absent outcomes cannot repair automatically.
After a pre-handoff raw read failure, a successful protection release is recorded
in the synthetic writer session before the attempt becomes terminal. A fresh
controller can then observe exact raw and publication identities and permit a
separate repair only when raw is verified and publication is confirmed absent.
A failed or unproven release remains unresolved. The original unknown attempt
and its history remain unchanged by repair.

Local exact reads rebuild the complete source/binding/run/acquisition/material/
Version/Publication graph from embedded original bytes. As-of considers only
Published and usable versions at the requested time, selects the latest original
`obtained_at`, and rejects ties. A later replay of older source bytes cannot
replace a newer acquisition merely because it was published later. Previous
versions remain exact-readable. Invalid visible lineage fails closed. Read creates
no clients, jobs, raw objects or writes.
Job readback verifies this attempt's exact Version/content/publication separately
from the as-of winner. An older retained acquisition may succeed while the as-of
selection continues to return a newer acquisition; ties remain rejected.

The PostgreSQL path only admits `mdf_macro_bls_employment_vertical_r1` on the existing
development host, with the existing Garage adapter, same-connection advisory
protection, idle writer and independent readback session requirements. Adding the
namespace to `canonical/postgres.py` performs no schema creation. Neither this
candidate nor its focused tests contact PostgreSQL or Garage. Real persistence,
original source qualification, lost connections and repair against those real
capabilities remain unproven and require independently reviewed, bounded effect
plans before execution. Owner-only single-host journal assumptions remain in force.
