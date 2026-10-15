# Market-Data-Foundation

Market-Data-Foundation is the market-data foundation product. Its current state is
runtime baseline qualification, a reusable Provider Qualification harness and the
[TDX R1 implementation bake-off](docs/qualification/tdx-r1.md). R1 identifies
tdx-python 0.1.6 as an implementation candidate with explicit endpoint pinning,
redial disabled and capability/source limitations.
MDF Primary Provider binding is not yet authorized; no production adapter is bound.
The [nonproduction Go/TDX watchlist MVP](docs/p5/real-go-tdx-watchlist-mvp-r1.md)
adds an explicitly launched, bounded real source with local Ingress/Read and IPC ACK.
Its live verification is scheduled separately by PM/Host.
The [local latest Watchlist Read](docs/p5/watchlist-latest-read-r1.md) exposes
fully Go-ACKed snapshots through a Python API and local CLI, including while the
producer runs. Consumers need a readable private MDF-dev session directory;
this is not a network-published MBO Runtime/API or canonical Publication.

## Development

Use uv-managed Python 3.14.8 and uv 0.12.22. Debian's system Python remains separate.
The committed `uv.lock` fixes development
dependencies; uv manages the local environment.

```sh
uv sync --locked --group dev
uv build
uv run --locked python scripts/check_repository_hygiene.py
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
```

- `src/market_data_foundation/`: Python package namespace.
- `tests/`: deterministic tests; ordinary pytest excludes `live_provider` tests.
- `provider_lab/`: qualification fixtures, observation schema and usage contract.
- `docs/architecture/`: canonical home for future stable architecture records.

CI runs on pushes to `main`, pull requests and manual dispatch. It uses read-only
repository permissions and does not persist checkout credentials. It accesses no
market-data providers or private LAN services. The external MDF development
PostgreSQL capability at `192.168.10.4` is never a CI prerequisite. QMT, ClickHouse
and Redis realtime infrastructure has not been materialized.

Local runtime configuration lives in ignored `config/local/runtime.toml` and
`config/local/secret.toml`; local TLS material belongs in ignored `tls/` only when
required. CI never reads these files. See `provider_lab/README.md` for the harness
and `docs/qualification/q0-runtime.md` for tested runtime/candidate evidence.

`MDF_CI_READ_PAT` is reserved for future read-only access to other private
repositories. Same-repository CI does not require it. Never put its value, other
credentials, private keys or production data in this repository.
