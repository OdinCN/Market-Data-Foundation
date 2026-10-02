# Market-Data-Foundation development constraints

- Use uv for Python environments and dependencies; commit `uv.lock` with dependency changes.
- Keep production data, credentials, private keys and runtime artifacts out of Git.
- Provider bindings are capability-specific. Never silently fall back to another provider.
- Keep live-provider tests separate from deterministic CI; mark them `live_provider`.
- `192.168.10.4` is the external MDF development PostgreSQL capability. GitHub-hosted CI must never require or access it or any private LAN service.
- QMT, ClickHouse and Redis realtime infrastructure is not materialized yet.
- Run repository hygiene, `ruff check`, `ruff format --check` and deterministic pytest before pushing.
