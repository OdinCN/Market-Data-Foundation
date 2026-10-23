# I1 Job → I3 PG/Garage 有界 DEV 验收

`retain_page(..., physical=simple_intake(store, level="raw"))` 在原 D2 Journal
receipt 校验后、任何写入前冻结完整 Garage locator 与显式 physical level；返回的
Material 保留原 Job/shard/attempt/request、原始 bytes 摘要、行边界和时间/provenance。
不传 `physical` 仍使用原 memory 合同。物理提交的验证身份是 `(material, level)`，
不能用 ACK 反推或修改 Material。未改 DDL、依赖、D1–D4、Driver 或真实 Source admission。

脚本 `scripts/mdf_i1_i3_job_storage_dev.py` 仅供一次明确授权后的人工执行：

```bash
export PYTHONPATH=src:.
export UV_CACHE_DIR=/tmp/mdf-i1-i3-uv-cache
export UV_PROJECT_ENVIRONMENT=/srv/market-data-foundation/.venv
uv run --frozen --no-sync python -c \
  'from scripts.mdf_i1_i3_job_storage_dev import candidate_revision; print(candidate_revision())'
# 将上行冻结摘要填入参数；独立审查和聚焦门禁通过后，只执行一次：
uv run --frozen --no-sync python scripts/mdf_i1_i3_job_storage_dev.py \
  --opt-in-dev-write --expected-revision '<冻结候选摘要>' \
  --receipt artifacts/i1-i3-job-storage/receipt.json
```

冻结身份为基线加全部 tracked/untracked、非 ignored 候选文件的内容摘要，保存在外部
收据；每阶段及 HTTP 传输前检查候选/主线漂移。运行说明不承载最终状态或完成身份。
凭据复用 owner-local 配置的安全注入，不打印密码、连接串、HTTP 签名或原始异常。

合成 fixture 经过 D1 `plan_job`、D2 `DailyRunner`/`Journal` 及重开、I1
`retain_page`/`prepare_version`。一个 invented instrument、九个日期跨两个月，
两个明确开市日各产生一个 page；其余日期有显式关闭/成员资格/生命周期证据。
这是合成业务材料在真实 DEV PG/Garage 的读写验收，不是市场采集。

验证包括 PG rollback 后固定原 payload 恢复、原请求幂等、换 Material Key/bytes
在对象 IO 前拒绝、不完整覆盖和未准入发布拒绝、正常 Version/Publication、显式
projection 构建。独立冷进程重新连接 PG，恢复同一原 payload，再读取 SQL-backed
Version/Publication/lineage/content/selection；读取阶段启用默认只读事务并禁止 PUT。
冷读内部复用 D3 的有界投影验证结构，数据来源仍为该新 schema 中的实际 SQL 结果。

全父子共享最初 295 秒截止时间（授权最大 300 秒），父/子 Garage 上限 96/32 请求，
每个 adapter 仍限 120 秒。最多八个 page/object、4 MiB；当前固定 fixture 产生两个
对象。所有 GET/HEAD/PUT、失败与重试都记入原预算。无 resume/第二 run、Garage
DELETE、DROP、旧对象清理、timer 或服务启动；超时先终止回收子进程。

脚本仅新建一个 `mdf_i3_simple_i3_<32hex>` schema 和
`mdf-artifacts/market-datasets/dev-simplified/i3-<32hex>/` 前缀。所有材料保留，
receipt、冷进程 receipt、冻结 Plan/intent 和 Journal 位于 ignored `artifacts/`。
PG 专属身份/TLS、持久设置、schema/table/column ACL 以及可观察备份状态均为本次
preflight；没有恢复证明时 PITR 记录 UNKNOWN，不视为生产资格。

离线直接测试 `tests/test_i1_i3_job_storage.py` 使用 SQLite/wire 替身验证算法和脚本
控制流，不替代 PG、TLS、持久性或真实子进程证据。完成后同一候选须通过完整
`pytest` deterministic、repository hygiene、`ruff check`、`ruff format --check`；
源文件一旦变化，旧 DEV 收据不能证明新候选。不得自动追加真实复验。
