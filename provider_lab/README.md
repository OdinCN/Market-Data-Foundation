# Provider qualification harness

Full Provider Qualification has not started. This directory contains provider-neutral
requests and the observation contract; it selects no provider or implementation.

- `fixtures.toml`: versioned fixtures with canonical capability IDs, `code.MIC`
  symbols, inclusive local date ranges, small pagination requests and requested
  semantics. These are test definitions, not market responses.
- `observation.schema.json`: strict JSON Schema 2020-12 for normalized evidence.
- `market_data_foundation.qualification`: fixture loading, an explicit capability
  binding and a single-executor observation runner. Runtime code uses the stdlib.

Capabilities cover security universe/metadata, quote snapshot, daily/intraday bars,
intraday trades, corporate actions, financial statements and trading calendar.
Each future adapter must explicitly map symbols, interval enums and units. The
fixture's requested semantics must not be mistaken for measured provider semantics.

## Deterministic example

Run from the repository root with `uv run --locked python`. This synthetic example
contacts no provider and reports an incomplete result:

```python
import json
from pathlib import Path
from market_data_foundation.qualification import (
    Binding,
    Evidence,
    load_fixtures,
    observe,
)

fixture = load_fixtures(Path("provider_lab/fixtures.toml"))[3]
binding = Binding("synthetic", "example-executor", "1", fixture.capability, "offline")
record = observe(
    binding, fixture, lambda _: Evidence(False, missing_fields=("volume",))
)
output = Path("artifacts/qualification")
output.mkdir(parents=True, exist_ok=True)
(output / "synthetic-observation.json").write_text(json.dumps(record, indent=2) + "\n")
```

The executor receives one fixture and returns `Evidence`, containing normalized
counts, returned symbols, actual date coverage, measured units/semantics,
pagination and endpoint identity where observable. It must not print credentials
or raw responses. Put raw responses and exported observations under ignored
`artifacts/`; never put them beside the tracked fixtures.

`success` describes one fixture's checks, not product adoption or overall provider
qualification. Incomplete coverage may be recorded with `success=False` and no
exception. Unknown completeness/pagination exhaustion is `null`, unknown units or
semantics are empty maps, and an unobservable endpoint has an explicit reason.
On exceptions, the runner preserves the binding identity and known attempted
endpoint, classifies the original exception by module/type/category, and records
failure after one call. It neither stores exception messages nor tries another
provider. The binding's endpoint describes the attempted target; successful
evidence must report the actual connected endpoint, particularly for SDKs that
route internally. Do not assert an actual server identity when it is not visible.

## Live execution boundary

Live adapters are future work. A live binding requires `mode="live"` and
`observe(..., allow_live=True)`. Future live tests must have
`@pytest.mark.live_provider` and need both explicit CLI opt-in and marker selection:

```sh
uv run --locked pytest --live-provider -m live_provider
```

Ordinary pytest and GitHub CI exclude this marker. Selecting it without
`--live-provider` skips the test. Live-test modules must not contact providers or
read runtime secrets during import/collection. Deterministic tests use synthetic
executors and require no credentials, LAN services or SDK installations.

## Local runtime and credentials

Runtime configuration stays in ignored `config/local/runtime.toml`, secrets in
ignored `config/local/secret.toml`, and any required local TLS files in ignored
`tls/`. Preserve the exact configured key names; credential presence is not an
authentication or entitlement PASS. Q0 inspected only names/presence of
`fuyao.api_key`, `tickflow.api_key`, `baostock.api_key` and `RQDATA.license_key`.
It used only the PostgreSQL runtime credential for read-only authentication checks.

Runtime/candidate evidence is in `docs/qualification/q0-runtime.md`. Candidate SDKs
remain in isolated ignored lab environments and are not project dependencies.
