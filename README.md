# Market-Data-Foundation

Market-Data-Foundation is the market-data foundation product. Its current state is
repository and CI bootstrap, preparing for Provider Qualification.
Provider Qualification has **not started**; no provider adapters or infrastructure
are implemented here.

## Development

Use Python 3.11 and uv 0.12.22. The committed `uv.lock` fixes development
dependencies; uv manages the local environment.

```sh
uv sync --locked --group dev
uv run --locked python scripts/check_repository_hygiene.py
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
```

- `src/market_data_foundation/`: Python package namespace.
- `tests/`: deterministic tests; ordinary pytest excludes `live_provider` tests.
- `provider_lab/`: reserved for future Provider Qualification.
- `docs/architecture/`: canonical home for future stable architecture records.

CI runs on pushes to `main`, pull requests and manual dispatch. It uses read-only
repository permissions and does not persist checkout credentials. It accesses no
market-data providers or private LAN services. The external MDF development
PostgreSQL capability at `192.168.10.4` is never a CI prerequisite. QMT, ClickHouse
and Redis realtime infrastructure has not been materialized.

`MDF_CI_READ_PAT` is reserved for future read-only access to other private
repositories. Same-repository CI does not require it. Never put its value, other
credentials, private keys or production data in this repository.
