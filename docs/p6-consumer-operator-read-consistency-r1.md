# P6 Consumer / Operator bounded cross-surface proof

This candidate adds tests only, against the delivered read surfaces. Existing
consumer tests prove exact/as-of reads, Operator tests prove metadata/report and
WSGI behavior, and the preceding devPG proof exercises the WSGI SQL composition.
Existing as-of/status tests already compare some consumer content failures, and
report/web tests check v1 wire stability. The added property is the joint oracle
over both Definitions and all lifecycle boundaries through actual WSGI
inspection/report, including successful exact reads during as-of ambiguity.

The new deterministic file builds both admitted Definitions in one synthetic
canonical ledger, using existing offline plan builders and the accepted kernel.
CPI preserves its required evidence, grades and replay claims on every event;
daily bars remain unbound. No provider request is made. Each Definition has an
original and replacement Version. A Version's `supersedes` reference alone does
not withdraw the original: the replacement PUBLISH becomes effective before the
original SUPERSEDE, deliberately creating an ambiguous interval.

For the exact same Definition and microsecond-precision UTC `as_of`, each probe
executes consumer exact-version/as-of reads and existing v1 wire serialization,
then the real in-process `OperatorWSGIGateway → OperatorWebEntry →
inspect_operator_status → render_operator_report` chain. Inspection is observed
without substituting results. Independent expected Version/Publication keys and
states are checked, and returned HTML must equal the real captured status's
rendered report. Storage and consumer wire bytes must remain unchanged. Operator
content reads are forbidden, and successful consumer binding keys must match the
Operator's exact stored binding association.

| Falsifiable invariant / counterexample | Expected result |
| --- | --- |
| Pending clocks, including equality and one microsecond before boundaries | No premature selection; pending later events preserve the previous effective key |
| PUBLISH / RESTORE, unique eligible Version with intact content/lineage | Consumer exact/as-of succeed with the same Version and effective Publication shown by Operator |
| WITHDRAW / SUPERSEDE | Exact read returns the matching terminal classification; zero eligible Versions gives `as_of_no_version` |
| Two eligible Versions | Both exact reads succeed; consumer as-of and Operator cardinality report `as_of_ambiguous`, with no arbitrary choice |
| Empty / unpublished graph | No selection; unpublished exact reads fail unavailable-as-of |
| Missing / corrupt / unavailable content | Consumer returns `content_missing` / `content_digest_mismatch` / `storage_unavailable`; Operator still reports one publication candidate and unknown integrity/availability |
| Missing lineage / malformed history / metadata storage failure / absent admitted Definition | Matching typed read/status failures; WSGI deliberately collapses these to fixed `503 operator_status_unavailable`, with no partial report |

Publication eligibility is metadata eligibility. It does not establish content
readability, integrity, current availability, activation, freshness or external
admission. Status validates all candidate metadata, including future/terminal
records; this is not a universal equivalence with every consumer short-circuit or
error precedence. Assertions cover only the documented fixtures.

The opt-in SQL file reuses the exact existing dev capability connector and runs
the same lifecycle/ambiguity oracle for both Definitions. It also removes and
corrupts synthetic content to prove the non-equivalence with real SQL. Before
mutation it requires an empty dedicated database (`public` only, no user objects)
and absence of its random task-owned schema under the existing allowed canonical
prefix. Writes occur only in that schema, inside transactions. Consumer scopes
are REPEATABLE READ / READ ONLY; Operator uses the delivered snapshot manager.
The fixture closes connections, drops only its owned schema, and checks fresh
read-only absence and inventory equality. This internal check requires a separate
Owner postflight. It proves no concurrent-writer or physical resource guarantee.

Ordinary CI stays offline; both live/PG markers, the CLI opt-in and the task
environment opt-in are required. Owner entry, from the repository root with the
existing local dev capability configuration (credentials supplied privately):

```bash
MDF_RUN_CONSUMER_OPERATOR_DEVPG=1 uv run --locked --offline pytest \
  tests/test_p6_consumer_operator_consistency_postgres_integration.py \
  -m 'live_provider and postgres_integration' --live-provider \
  --tb=line --show-capture=no --maxfail=1 -q
```

EO's sandbox attempt stopped at preflight with `OperationalError`:
**NOT_EXECUTED**, `created=false`, no SQL proof cases. Observations and logs are
under ignored `artifacts/MDF-P6-CONSUMER-OPERATOR-READ-CONSISTENCY-R1/`; they retain
exception classes, never raw messages or credentials. No network bypass is
authorized. Offline results are not a real PG PASS.

Owner subsequently ran the original candidate on devPG. Observation
`devpg-mdf_p3_canonical_kernel_r1_c_7947a6a7a64b473bb84aca5818e86d76.json`
records **INCOMPLETE**, `created=true`, no completed cases, cleanup and fresh
absence/inventory PASS. Owner also reports independent read-only absence with
only `public` and no objects. The failure at the original probe's line 248 alone
does not distinguish admission, scope setup or inspection failure.

An offline control now proves a candidate request-format defect: the original
probe passes a UTC datetime, but the same instant represented as `+08:00` reaches
that exact assertion with zero failures/statuses. Actual WSGI response is
`400 invalid_as_of`, with no context/authorization/snapshot calls. The delivered
gateway requires canonical UTC `Z`; `PostgresLedger.server_now()` returns the
driver's datetime unchanged, and pinned Psycopg timestamptz loaders use the
connection TimeZone. Replacing only `+00:00` with `Z` does not normalize offsets.
The correction uses `astimezone(UTC)` only for request serialization, preserving
the instant, graph and full oracle. Both Definitions now have positive lifecycle
regressions with `+08:00` and `-05:30`, plus original-format rejection controls.

Normal outer Psycopg transaction exit commits and restores transaction count
zero/IDLE; it is not itself evidence of leaked ambient work. New fixture checks
require IDLE/count zero before and after Consumer and before Operator, without
rollback or autocommit workarounds. Every probe requires exactly one actual
inspection; any mismatch now reports only HTTP status, counts and typed failure
classifications. The Owner run did not record its offset or HTTP classification,
so the offline defect is **not yet a confirmed attribution of that host failure**.
Real SQL remains **NOT_PASS** until Owner reruns this corrected, hash-bound
candidate on the same isolated capability. No sandbox PG attempt is repeated.

After Owner execution, set `MDF_CONSISTENCY_PG_OBSERVATION` to that run's exact
observation path, then use a separate process/connection for independent postflight:

```bash
PYTHONPATH=.:src:tests uv run --locked python - <<'PY'
import json
import os
from pathlib import Path
from tests.test_p6_operator_wsgi_postgres_integration import _connect, _inventory
from market_data_foundation.canonical.postgres import PostgresLedger

try:
    proof = json.loads(Path(os.environ['MDF_CONSISTENCY_PG_OBSERVATION']).read_text())
    assert proof['sql_proof'] == 'PASS'
    assert proof['cases'][-1] == 'cross_surface_complete'
    assert proof['cleanup_completed'] is True
    assert proof['fresh_connection_absence_and_inventory'] == 'PASS'
    with _connect(read_only=True) as observer:
        assert observer.execute('SHOW transaction_read_only').fetchone() == ('on',)
        assert not PostgresLedger(observer, proof['schema']).namespace_exists()
        assert json.loads(json.dumps(_inventory(observer))) == proof['preflight']
    print('OWNER_READ_ONLY_POSTFLIGHT_PASS', proof['schema'])
except BaseException as error:
    print('OWNER_READ_ONLY_POSTFLIGHT_FAIL', type(error).__name__)
    raise SystemExit(1) from None
PY
```

Stop the PG branch if capability isolation, cleanup or postflight fails; reconcile
before another run. Stop and return for disposition if integration needs public
wire, admission, real authentication, provider, deployment, production mutation
or production code changes. `B-END-TO-END-READ-CAPABILITY` and physical
IO/TOAST/CPU/memory/locks/latency/SLA remain STOP / NOT PROVEN. Owner full host QA,
independent review bound to the three candidate file hashes, and conditionally
authorized normal delivery/exact-head CI are subsequent gates. EO leaves this
candidate uncommitted and makes no professional PASS or whole-P6 claim.
