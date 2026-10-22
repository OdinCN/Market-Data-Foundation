# I-3 有限 Host 验证与精确清理

代码支持已有 Owner 授权内的 DEV 操作。本轮进程没有注入三项凭据，实际只读 intake
在网络前停止；无需再申请相同授权。下列操作由具备正常凭据注入和网络能力的 Host
launcher 执行，不能通过此候选探测其它源、库、账户或 bucket。

## 前置 intake

从当前候选 checkout 运行 uv-managed Python 3.14.8。无需新增依赖；pyproject/uv.lock
未修改。本环境验证使用已存在的同基线环境：

```sh
export UV_CACHE_DIR=/tmp/mdf-i3-uv-cache
export UV_PROJECT_ENVIRONMENT=/srv/market-data-foundation/.venv
export PYTHONPATH=src
uv run --no-sync python scripts/mdf_i3_storage_probe.py --action intake \
  --receipt artifacts/i3/host-intake.json
```

正常独立 checkout 可使用 `uv sync --frozen` 后 `uv run`。本沙箱不能写公共 uv cache，
临时空 cache 的 offline sync 缺少 wheel，故使用已有合格环境；没有更改系统 Python。

Host 通过现有安全注入提供 `MDF_POSTGRES_PASSWORD`、`MDF_GARAGE_ACCESS_KEY`、
`MDF_GARAGE_SECRET_KEY`。不在命令行、文档、Git 或 stdout 填入值；脚本不读取
secret.toml、不打印 DSN/Authorization/原始异常。HTTP credential signing 沿用现有
Garage 内网风格；不宣称提供 transport TLS。

PG intake 检查当前 server/db/user/session user、SSL、CREATE 能力、非管理 role、
default ACL、候选 schema 是否已有。若同名存在，不运行 probe、不覆盖；先核实原 run。
Garage bucket 的独立凭据归属和 bucket read/write/delete ACL 由 Host 现有管理能力
只读核查，不从旧 success 或 sentinel 404 推断。Host 同时核对本次完整 prefix 不存在、
既有宏观/P4材料隔离、备份 UNKNOWN 仅适用于可丢弃 probe；将事实填入 ignored 文件：

```json
{
  "run_id": "i3-<本次32位小写hex>",
  "schema": "mdf_market_dataset_dev_r1",
  "bucket": "mdf-artifacts",
  "endpoint": "http://192.168.10.8:3900",
  "credential_scope": "MDF_DEDICATED",
  "bucket_read_write_delete": "VERIFIED",
  "probe_prefix_absent": "VERIFIED",
  "existing_materials_isolated": "VERIFIED",
  "disposable_probe_no_backup_dependency": true,
  "evidence_ref": "本次Host只读ACL/name检查的本地证据引用"
}
```

这是待填写形状，不是已经发生的证明。不能把示例保存后直接宣称检查完成。
脚本记录该 intake 文件 SHA256，并再次实时检查 PG；仅在同 run 的新 probe prefix
用负向条件写核实 Garage 的 If-None-Match 支持。若不支持、403、超时、网络沙箱拒绝
或 ACL 不足就 STOP；无 fallback、无自动重试。

## 探针与预算

```sh
uv run --no-sync python scripts/mdf_i3_storage_probe.py --action probe \
  --opt-in-dev-write --run-id i3-<本次32位小写hex> \
  --host-intake artifacts/i3/host-storage-intake.json \
  --receipt artifacts/i3/host-probe.json
```

预算：120秒全局 alarm、最多3个 PG connection、每语句5秒/锁1秒、最多96个 S3请求、
单材料8MiB、一个合成材料/Version/Publication、两次连接重开、零重试；触顶停止。
固定 endpoint，无互联网扫描/自动备源/长连接服务。所有 status/raw异常分类收据在
ignored `artifacts/`；每步和每个 S3 请求前写 UNKNOWN intent，响应后再记录观察。

步骤：新 sentinel 条件写资格 → 新 schema 原子 DDL → 合成 raw PUT/GET和 receipt
→ Version 提交后主动丢 ACK → 新 PG 连接原身份核实 → 显式 synthetic DEV admission
→ synthetic Publication → 新投影 generation READY → 再次新连接 exact/content/history
读回 → 仅删除该新run合成Version的projection_rows以注入单表故障 → 验证读取拒绝
→ suspend/rebuild为新generation → 再次精确读回 → 精确清理并确认不存在。
**连接重开不等于数据库/服务器重启或断电恢复。**

成功默认先删除本 run PG schema，确认不存在，再 DELETE 本实例记录的 Garage keys，
每 key GET=404。`--hold` 可保留独立 DEV 样本供 Host 复核，不安装 TTL或后台清理。
源市场数据获取和盘中 freshness 不属于此合成 probe；不能将结果标成 I-2/I-4 Source
PASS，更不能把 probe prefix 当正式 retention/admission。

## 失败恢复、持有期与清理

任何失败保留已发生的 UNKNOWN/OBSERVED 效果。不要重跑 probe并覆盖收据；不要产生
新 occurrence 来掩盖未决提交。`ledger.verify(operation, identity, payload_digest)`
在新连接上核实；只有原 receipt/digest 一致才可认定 COMMITTED。

SQL独立 writer API还提供 `suspend()` 和 `rebuild(new_generation, covered_months)`。
确定性测试实证了单表投影损坏的停止与恢复；Host合成probe包含上述有界注入和恢复，
但当前环境尚未真实执行，真实资格仍为UNKNOWN。canonical丢失必须从已验证备份恢复，
不能依赖 rebuild创造事实。全库 PITR与Garage恢复演练另需实际条件，不能借旧PASS。

先检查完整 effect ledger，再生成清理计划：

```sh
uv run --no-sync python scripts/mdf_i3_storage_cleanup.py \
  --original-receipt artifacts/i3/host-probe.json \
  --cleanup-receipt artifacts/i3/cleanup-plan.json
uv run --no-sync python scripts/mdf_i3_storage_cleanup.py \
  --original-receipt artifacts/i3/host-probe.json \
  --cleanup-receipt artifacts/i3/cleanup-result.json --apply-owned-cleanup
```

计划默认无网络/无远端写；apply 最多90秒、一个PG连接、最多16个精确key/32个S3请求，
无列表遍历。读取可信、未编辑原收据并记录SHA；核对DDL/run/bucket/schema，拒绝
其它run归属；PG只有本run且DDL/binding匹配才可删。DROP不用CASCADE，有额外依赖
就整体回滚。确认PG不存在后才处理原收据中本prefix的PUT intents，未知ACK也保留
在清理核实名单。仅本测试prefix可清理，不可复用此工具删正式/宏观/P4材料。

## I-1 / I-2 集成交接

直接使用 `PostgresMarketDataset.retain/commit_version/publish/verify` 和
`with ledger.read_snapshot(...) as view`。输入必须仍为现有D3类型，所有经济语义和
已知时钟由上游证据提供。本端口不提供 Provider downloader，不合成TDX单位、不把
I-2普通JSON自动变成合格Version。I-2若仅有SDK projection，显式level并留独立SHA；
接入D3前仍须具名decoder和完整framing/field证据。原生网络报文和source_projection
可分别持有，不能混用同一等级。正式资产前缀、完整D1 coverage manifest持久化、
D4 page生命周期/HTTP接线、同字节Host专业复核由集中集成继续完成。
