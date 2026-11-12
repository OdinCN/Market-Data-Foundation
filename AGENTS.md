# Market-Data-Foundation development constraints

- Use uv for Python environments and dependencies; commit `uv.lock` with dependency changes.
- The qualified product baseline is uv-managed Python 3.14.8. Do not change Debian's system Python. Requalify target dependencies before changing the baseline.
- Keep production data, credentials, private keys and runtime artifacts out of Git.
- Provider bindings are capability-specific. Never silently fall back to another provider.
- Keep live-provider tests separate from deterministic CI; mark them `live_provider`.
- Use the qualification capability/fixture/observation contract. Live execution requires explicit opt-in; observations contain exception classifications, never credential values or raw exception messages. Keep raw responses and observations under ignored `artifacts/`.
- `192.168.10.4` is the external MDF development PostgreSQL capability. GitHub-hosted CI must never require or access it or any private LAN service.
- QMT, ClickHouse and Redis realtime infrastructure is not materialized yet.
- Run repository hygiene, `ruff check`, `ruff format --check` and deterministic pytest before pushing.

## 数据族工作台

- 新增或修改数据族时，接入已有资产目录、查询和采集工作台，沿用周期任务与执行记录入口。族专用字段放在对应表单或详情中。接入位置和已有回归见 `docs/runtime-family-ui.md`。
- DEV 交付前，在交给用户的同一地址从目录进入，验证改动族及受影响的既有族。用已知本地样本查到实际数值和表格行，查看 API 说明、采集目标、周期详情和已有执行详情。使用 `web/scripts/check-live-workbench.mjs` 的可配置用例复验。基准端口只作对照。独立记录尚未验证的路径和真实业务失败。
