# P6 in-process Operator WSGI / development PostgreSQL proof entry

The existing WSGI and web-entry tests use SQL/snapshot doubles. Earlier real
PostgreSQL proofs stop below these adapters. They do not establish the delivered
`OperatorWSGIGateway → OperatorWebEntry → PostgresLedger.operator_snapshot →
inspect_operator_status → render_operator_report → WSGI response` composition.

`tests/test_p6_operator_wsgi_postgres_integration.py` adds two opt-in cases for
the admitted CPI and raw daily bars Definitions, using existing synthetic plan
builders. No provider call or listener is made. The test host supplies an object
capability and exact-selection authorizer independently of environ. This is
test injection, not a real authentication implementation.

The owner run of the original candidate reached real PostgreSQL and failed at
the CPI concurrent-withdrawal hook with `operator_status_unavailable`. The hook
constructed a WITHDRAW request with empty evidence and grades. Delivered
`rules.validate_evidence_for_publication` applies the bound CPI mapping's evidence
requirements to WITHDRAW too, so it raises `publication_evidence_required` before
insertion. The inspection boundary classifies the callback exception and WebEntry
returns 503. Offline execution of the same synthetic plan reproduces that exact
ContractError; raw daily bars are unbound and admit the original empty-evidence
request. This explains the reported CPI failure without changing product rules.

The correction builds the withdrawal by replacing only the event fields of the
admitted publication request. It retains scope, evidence, grades, replayability
and completeness, and lets the new decision obtain its own usability instant.
Two deterministic regressions use the same corrected request builder with
InMemoryLedger and delivered publication/status logic: stripped CPI evidence is
rejected without insertion; both valid withdrawals become `version_withdrawn`.
Network/PG connects are forbidden in those regressions. They prove fixture
semantics only, not real SQL or snapshot isolation. Real PG cases keep their own
`live_provider` and `postgres_integration` markers and retain all prior assertions.

Each case creates a random 61-character schema under the already admitted
`mdf_p3_canonical_kernel_r1_w_` prefix. Before mutation, a read-only connection
checks the owner-configured `192.168.10.4 / market_data_foundation_dev` capability,
server version, CREATE privilege, target absence, and a dedicated empty database
(`public` only, no user relations). A nonempty/shared capability stops before
mutation. No existing namespace is reused or dropped. Only synthetic setup and
withdrawal writes occur in the new namespace; report requests use the delivered
REPEATABLE READ / READ ONLY scope. The attempted DELETE is a negative test of
server read-only enforcement and must fail without changing storage.

Assertions cover repeated successful HTML reads, expired scopes, fresh
authorization after revocation, request capability spoofing, exact Definition
and as-of denial without a scope, SQL-error rollback and recovery, an intervening
writer commit between the real as-of and binding inspections, stable publication
counts in the old snapshot and withdrawal in the next snapshot, transport failure
after cleanup, and connection loss at scope exit. The response starts once after
scope exit, contains one bytes block, has exact length/security headers, and never
uses WSGI write(). Controlled locators/receipts stay out of the response; failures
return only fixed classifications, with no partial HTML.

The fixture closes connections, removes only its own schema, then verifies
absence and unchanged catalog inventory through a fresh read-only connection.
That verification is part of the test; it is not independent owner QA. A cleanup
failure is FAIL and requires owner reconciliation before another run. Observations
under ignored `artifacts/MDF-P6-OPERATOR-WSGI-DEVPG-INTEGRATION-R1/devpg-*.json`
contain exception classes/classifications, never raw exception messages or
connection options. No observation is written to the control root.

Owner execution from the repository root, with the existing local runtime and
secret config (or `MDF_POSTGRES_PASSWORD` supplied privately):

```bash
MDF_RUN_WSGI_DEVPG_INTEGRATION=1 uv run --locked pytest \
  tests/test_p6_operator_wsgi_postgres_integration.py \
  -m 'live_provider and postgres_integration' --live-provider --tb=no --maxfail=1 -q
```

Both markers, the CLI live opt-in, and the task environment opt-in are required
for the two real PG cases. Default deterministic CI runs only the two offline
regressions in this file and never connects to the LAN.

Historical EO sandbox attempts stopped at connection preflight with
`OperationalError` (**NOT_EXECUTED**, no schema created); no PG socket errno was
established. Subsequently the owner ran the original candidate on its legitimate
host: repeated CPI HTML reads, authorization/spoof denial, real SQLSTATE 25006 and
rollback/recovery completed before the withdrawal failure. Observation
`devpg-mdf_p3_canonical_kernel_r1_w_d5c5287302da46e49470929cf3f7b121.json`
records **INCOMPLETE**, `cleanup_completed=true`, and fresh-connection absence/
inventory PASS. Owner reports a separate read-only connection independently
confirmed target absence, only `public`, and no objects. The original full host
deterministic result was 1530 passed / 16 deselected / 3 warnings, exit 0.

This correction has **not been rerun on real PG**. It does not convert the
original INCOMPLETE result or offline regression results into real SQL PASS.
Before rerun, bind the two corrected file hashes, confirm the prior schema remains
absent and the dedicated dev capability is still empty, and use the same opt-in
command above. Each case still creates its own fresh UUID namespace, cleans it,
and requires separate owner absence/inventory postflight. No production code,
privilege, provider access, other database, service or deployment effect is
authorized. If a new failure indicates a delivered product defect, STOP and
return it for separate disposition rather than weaken assertions or change code.

After a successful owner run, use a separate process/connection for independent
read-only postflight. Set `MDF_WSGI_PG_OBSERVATION` to each of that run's two exact
observation paths; do not select an older attempt by glob or newest-file guessing:

```bash
PYTHONPATH=.:src:tests uv run --locked python - <<'PY'
import json
import os
from pathlib import Path
from tests.test_p6_operator_wsgi_postgres_integration import _connect, _inventory
from market_data_foundation.canonical.postgres import PostgresLedger

try:
    proof = json.loads(Path(os.environ['MDF_WSGI_PG_OBSERVATION']).read_text())
    assert proof['sql_proof'] == 'PASS'
    assert proof['cases'][-1] == 'composition_complete'
    assert proof['fresh_connection_absence_and_inventory'] == 'PASS'
    with _connect(read_only=True) as observer:
        assert observer.execute('SHOW transaction_read_only').fetchone() == ('on',)
        assert not PostgresLedger(observer, proof['schema']).namespace_exists()
        actual = json.loads(json.dumps(_inventory(observer)))
        assert actual == proof['preflight']
    print('OWNER_READ_ONLY_POSTFLIGHT_PASS', proof['schema'])
except BaseException as error:
    print('OWNER_READ_ONLY_POSTFLIGHT_FAIL', type(error).__name__)
    raise SystemExit(1) from None
PY
```

Retain the exact candidate hashes, owner test results and both external postflight
results for fresh independent professional review. This candidate makes no
changes to production code, dependencies, public wire, admitted Definitions,
SourceBinding activation, or standing controls. Physical IO/TOAST/CPU/memory/
locks/latency/SLA and B-END remain STOP / NOT PROVEN. Owner host QA (including the
separate sandbox TDX ptrace EPERM rerun), independent professional review, and
subsequently authorized normal Git delivery/exact-head CI remain separate gates.
