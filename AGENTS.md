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
- 新增或修改带页面操作的 API 时，按 `docs/runtime-family-ui.md` 的按钮流程验收规则，在 PR 中验证实际点击、请求次数、页面去向、回执身份及失败恢复。只读实页巡检不覆盖提交等写按钮。
- 采集提交复用 `useCollectionSubmit`，在产品 `collectionRegistry` / `MACRO_PROFILES` / `RuntimeFamily.collection` 登记能力；跑 `CollectionSubmitMatrix.test.tsx` 的自动全登记矩阵。新增协议补隔离 fixture，禁止为跳过用例而裁剪资产或完成证据检查。

- 新建采集与执行详情复用 `CollectionLifecycle` / `ExecutionBriefCard`：受理即锁原意图、原号只读跟踪，未知或在途不得重开；领域终态允许时仅当前卡片可“开始另一项采集”，清确认后显式提交新号。兼容服务编号不可伪造。
- 修改采集入口或能力时，跑 `CollectionSubmitMatrix.test.tsx` 的全登记行为矩阵（含生命周期与各入口身份），领域扩展放公共详情外壳内。按实际恢复能力显示操作，异步回执与路由动作绑定资产版本和原任务号。PR 的 Chromium 回归从目录登记逐资产操作构建产物，覆盖同页重开、执行详情、切换记录与结果返回。

- 简报已有可查看卡片时以卡片进入原资产/原编号详情，不重复渲染回查按钮；无记录的丢回执保留原号 GET 核实。执行详情自动跟踪，操作必须有真实去向或恢复效果。回归须直接断言状态、选中记录和去向，不以无效按钮后整页刷新代替。
