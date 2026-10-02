# Q0 runtime qualification

Observed 2026-10-03 (Asia/Shanghai), from clean repository baseline
`508592779ea4592d9236d5ab185ff61672d8e2f4`, Linux x86_64 MDF Dev host.

## Selected runtime

Use uv-managed CPython **3.14.8** with uv **0.12.22**. CPython **3.13.16** was
independently qualified as the fallback candidate. Both passed project lock/sync,
sdist/wheel build, installed import, deterministic pytest, ruff lint/format and
PostgreSQL client compatibility. The original Python 3.11 project restriction
was widened only in ignored project copies for the comparison. Debian's
`/usr/bin/python3` remains 3.11.2; no OS Python package or symlink was changed.

The canonical `.python-version` pins 3.14.8 and `requires-python` admits the 3.14
series. CI uses that file to install its runtime. Python 3.13 is qualification
evidence, not a second silently selected production baseline.

## Current implementation census and offline smoke

Versions were fresh-read from PyPI's JSON endpoints, not inferred from classifiers.
Each candidate was installed separately under each tested runtime, followed by
`uv pip check`, actual imports and the offline operation below.

| Implementation | Version | Python 3.14.8 | Python 3.13.16 | Offline operation |
| --- | --- | --- | --- | --- |
| [tdx-python](https://pypi.org/project/tdx-python/0.1.6/) | 0.1.6 | PASS | PASS | Native Go string-slice allocation and typed synthetic CodePage conversion |
| [pytdxdata](https://pypi.org/project/pytdxdata/0.6.1/) | 0.6.1 | PASS | PASS | SecurityBar datetime, public API and interval enums |
| [mootdx-plus](https://pypi.org/project/mootdx-plus/0.12.0/) | 0.12.0 | PASS | PASS | Reader/Quotes import, symbol normalization and synthetic pandas/NumPy conversion |

Sources: [tdx-python upstream](https://github.com/lyj0309/tdx-py),
[pytdxdata upstream](https://github.com/openbot-coder/pytdxdata),
and the mootdx-plus published wheel linked from PyPI. The latter's published
metadata points to [mootdx/mootdx](https://github.com/mootdx/mootdx).
Original [mootdx 0.11.7](https://pypi.org/project/mootdx/0.11.7/) was census-only
legacy reference and was not installed: it shares the `mootdx` import namespace
with mootdx-plus and is not part of the target set.

Current candidates were also installed together with project development tools
and `psycopg[binary]==3.3.6` in each ignored project copy. Both dependency checks
and all offline smokes passed. Resolved support included tdxpy 0.2.7, pandas 3.0.6,
NumPy 2.5.3 and httpx 0.28.1. No compiler was needed for these installs; the
tdx-python CPython-specific Linux wheel loaded its native binding on both runtimes.
No missing binary/runtime support or dependency conflicts were observed for the
tested target set. Optional SDK extras were not qualified.

Python socket connection/DNS hooks were blocked during offline smokes. No live
provider API was invoked; native Go calls were confined to local allocation and
conversion. mootdx-plus's implicit configuration path was redirected into its
isolated lab for smoke execution. Raw install logs, wheel census/hashes and smoke
records stay under ignored `artifacts/q0/`.

These results select the runtime, not a canonical TDX implementation. API
semantics already differ: the tested pytdxdata daily enum is 4 while tdx-python
documents daily category 9. Endpoint routing, units, coverage, pagination and
fallback behavior still require a capability-specific live bake-off.

## PostgreSQL and local boundary

psycopg 3.3.6 installed and authenticated successfully under each runtime using
the existing TOML PostgreSQL keys. Read-only sessions reported database/user
`market_data_foundation_dev`, client `192.168.10.20` and TLS 1.3. The same runtime
credential was explicitly rejected by pg_hba when connecting to `postgres`.
Only SELECTs were issued; no schema/data/privilege or server/network change was made.

`config/local/` and empty `tls/` remain root-owned 0700 boundaries; both local TOML
files remain 0600 and untracked. No client TLS files are currently required.
Credential names/presence only were checked for `fuyao.api_key`,
`tickflow.api_key`, `baostock.api_key` and `RQDATA.license_key`; all were present.
No provider secret was authenticated or exposed. Presence does not establish that
a provider requires that key, accepts it, or grants a particular capability.

## Harness scope

Tracked harness code uses only Python's standard library. JSON Schema validation,
including RFC 3339 timestamp validation, is a development gate. Fixtures use
synthetic/test definitions; unit tests exercise normalized success/failure,
exception-message suppression, single-call behavior, capability matching,
explicit live opt-in and ordinary CI exclusion. No provider adapters or live
provider tests are implemented in Q0. Full Provider Qualification is not started.
