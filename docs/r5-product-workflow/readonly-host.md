# CPI/PCE 只读 Host 装配

这是面向现有 DEV PostgreSQL 的显式装配入口，不是发布或运行授权。`create_readonly_preview` 默认拒绝创建，必须传 `enabled=True`；数据读取权限另由受信 Host 明确提供，默认 unknown。新入口不构造 Journal、Worker、Garage 或来源传输器。

## 配置与数据边界

`ReadonlySettings` 显式接收配置目录、既有接入包存储目录、环境 ID 和 CPI/PCE 独立的 schema owner/OID。路径来自 Host，不来自浏览器；不沿用写端口运行器的开发机默认目录。PG endpoint/user/TLS 继续遵守 `load_pg_config` 的现有 DEV 合同及全局可选 sslmode/CA 配置，不新增 TLS 例外或改系统 Python。

每次连接先重读并核对配置，PG 会话启动设置 `default_transaction_read_only=on`。CPI/PCE 共用原有有界快照 machinery：显式 REPEATABLE READ READ ONLY、超时、端点/角色/SSL/schema 身份校验、结果数量及字节上限、退出后快照失效、回滚并关闭。复用现有 PG 用户，未更改其数据库权限或新增只读账号。

两个数据资产分别绑定自己的 schema，不能混用。历史投影只有从实际快照及域 Reader 获得证据后才显示持有和可读；PG读取或关闭失败保持 UNKNOWN；已确认存在的版本若只是不满足覆盖或质量要求，仍保留历史并标记不可读。PPI/TDX/KD 未装配 Reader，不能当成历史为空。停用能力不隐藏已确认持有的历史，也不阻断合格本地读取。

## HTTP 行为

- 复用 `/api/r5/packages` 和 CPI 普通查询、exact/as-of；复用原 PCE catalog/assets/read 实现。PCE 查询后仍不声明来源准入。
- 默认只保留 GET/HEAD。单独启用 `package_config_save=True` / `--allow-package-config-save` 时，仅增加同源 JSON `PUT /api/r5/packages/{macro,tdx,kd}`；应用和 TCP 适配器均独立限定路径、Origin、Content-Type、LAN peer 与正文长度。所有 Job、计划、Worker 和管理写请求仍拒绝。
- 默认关闭包保存和凭据替换。显式 DEV 配置模式允许原子保存能力集合与只写凭据，复用受信旧 Key、CAS 与未知回执保护。此模式不安装 Job、计划、管理写 Host 或来源传输器。
- 普通查询的来源绑定集合为空。本地不足时明确要求选择或报告具名绑定未装配，不接外部来源、不回退 provider、不创建 Job 或 Publication。
- Key 状态沿用受信配置及02已发布引用的解析规则；不回显明文，不把 configured 当认证通过。Worker 的历史运行入口本轮未改。

## 接入包存储

`LocalPackageStore` 只打开既有目录，不自动初始化或根据代码目录/持有历史种入启用集合。不存在或损坏时，接入包 API 明确不可用；CPI 合格本地查询仍独立可用。没有初始化真实存储时，不能宣称已具备可用的浏览器接入包界面。

真实 PG 验证可以使用明确标识的临时测试存储核对“启用集合为空，但实际历史仍可见”。该测试配置不等于当前环境部署配置，也不会持久启用任何来源。

## 显式 DEV 启动器

`scripts/mdf_readonly_preview.py` 复用现有受限18761预览适配器。所有路径、环境、PG角色及两个 schema 身份都必须通过具名参数提供；`--allow-cpi-local-read`、`--allow-pce-local-read` 分别控制读取许可，缺失时保持 unknown。启动需要 `--opt-in-readonly-preview`，不得借此重启旧18760写窗口或重跑既有回补。

入口未自动启动。开启实际预览窗口、初始化真实接入包配置、来源请求、采集或 Worker 等效果仍分别遵守当前授权边界。

## 产品入口

数据源管理与数据资产共同验收。只读窗口不能证明配置保存完成。两页正常路径不再显示内部合同修订选择或重复“技术详情”；代码合同目录位于平台管理的开发诊断。资产展示提供方（如 BLS），取数实现仅在本地未满足且后端明确要求选择时出现，未接入候选不当作可选来源。查询历史时点为可选条件，一次查询同时应用条件并读取；精确版本深链继续经过原 Reader，不混入来源当前值。
