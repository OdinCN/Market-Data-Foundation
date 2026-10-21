# TDX v1.0.0 隔离派生迁移 R1

范围是 Owner-local 有界工程候选和离线验证，源码待 AO 独立评审。
不授予真实 Provider 执行、Source admission、部署、主线交付或 D2–D4 权限。
历史 Source 的 `SDK_RELEASE`、历史交易数据和既有 v0.0.87 二进制保持原身份。

固定输入：

| SDK | tag target | 派生标识 |
| --- | --- | --- |
| `injoyai/tdx v1.0.0` | `2a9000681f5a9896ea98e4de8bc4904f1dce78ba` | `mdf-wait-preregister-r1` |
| 回滚 `injoyai/tdx v0.0.87` | `51c1ed51020fa43727300339dd92cbecf06a7d2e` | `mdf-wait-preregister-r1` |

两版原始 `client.go` SHA256 都是
`003444d252c22328d45563c4c85115a07d7ccf1476d0dd206551bbfe7d19658b`。
原 waiter patch 原样保留，SHA256 为
`4fea3baa0d53b4a9ba6e1e788c522345c5a8808200072c2487893cddea90eedc`；
派生 `client.go` SHA256 为
`5c14068d5ff48a77f969ecd5b68079be803b225815a9ad8d1ecf3a84d8ca8d28`。
注册 waiter 先于 Write；同一 mutex 串行保护注册、Reader Done 和超时／写失败清理，
回调使用一次性缓冲通道。没有更换 waiter 依赖。

离线构建示例（工作树根目录，输出必须是新路径）：

```bash
/srv/market-data-foundation/.venv/bin/python scripts/build_tdx_driver.py \
  --sdk-version v1.0.0 \
  --sdk-checkout /var/tmp/mdf-tdx-sdk-v100-20261021 \
  --go /var/tmp/mdf-tdx-live-20261011/toolchain/go/bin/go \
  --module-cache /var/tmp/mdf-tdx-live-20261011/pkg/mod \
  --output artifacts/tdx-sdk-v100-r1/tdx-driver-v100-review --race
```

构建核验 HEAD、tag target、tracked 修改、Go 1.25.3 及补丁输入／输出哈希。
仅复制 tracked SDK 文件到临时 overlay，原 checkout 和依赖缓存用于读取。
模块精确 require 所选版本，replace 只指向临时 overlay。
`GOPROXY=off`、`GOSUMDB=off`、`GOTOOLCHAIN=local`，忽略外部 Go workspace/flags。
执行 SDK 的离线 `./protocol` 测试和 driver 的 `net.Pipe`／mock 测试；
SDK 根包可能连接 Provider 的测试不执行。现存二进制、sidecar、输出软链接均拒绝覆盖。
受控构建产物可用 `--version` 辅助读取派生身份；不得对未经验证的可执行文件运行该选项。
每次构建的 `.build.json` 记录实际输入、执行结果及编译后的二进制哈希，日志留在忽略目录。
最终源码字节／SHA、运行身份和测试终态由 Host 完成后独立记录；本文不嵌入自身身份或验收结论。

验证及语义边界：

- 两版都覆盖提前回复（Reader 分派先于 Write 返回）、近超时回复、写失败清理，
  以及确定性的“先超时清理、再注册下一 waiter、再分派迟到／重复回复”。
- 合成逐笔 wire 同时走当日和历史解码；UTC、洛杉矶、上海宿主时区下验证序列化、
  UTC instant、两个交易日各 241 根分钟 K、9:30／15:00 边界及 10:23 聚合量价。
  v1.0.0 使用固定 UTC+8（本 fixture 日期等同 Asia/Shanghai），保留原交易日。
  v0.0.87 仍将逐笔当 UTC，分钟 K 使用宿主本地日期／时区；回滚会恢复该旧行为，
  洛杉矶宿主可出现前一日日期。旧记录不能被标为已修正。
- ETF／指数验证是 SDK 层合成协议回归：代码分类、Multiple、Decimal、LastPrice、
  逐笔价格缩放、CodeModel 转换、日／分钟 K 量额和指数涨跌家数。
  金额／价格保留 SDK 整数厘语义；没有据此授予 ETF／指数 Source 资格，
  Python quote volume／level 单位仍声明 UNKNOWN。
- Python 对两版执行分帧 IPC、多轮 ACK、冻结字节哈希和 receipt／final-report 重建；
  连接和 batch 必须匹配同一个明确 pin，混版不能产生 ACK。

版本切换与回滚是显式选择二进制路径和匹配的 parent policy，不执行 activate：

- v1.0.0 driver 对应 `SessionConfig.driver_sdk_release` 或 CLI
  `--driver-sdk-release` 值
  `injoyai/tdx v1.0.0@2a9000681f5a9896ea98e4de8bc4904f1dce78ba+mdf-wait-preregister-r1`。
- 回滚重用原二进制 `/srv/market-data-foundation/artifacts/tdx-mvp-build/tdx-driver`，
  选择旧 release
  `injoyai/tdx v0.0.87@51c1ed51020fa43727300339dd92cbecf06a7d2e+mdf-wait-preregister-r1`。
  新源码也可用 `--sdk-version v0.0.87` 加旧 checkout 构建至另一新路径作为对照；
  该重建物不冒充既有二进制的字节身份。
- 为兼容既有会话，Python 默认 policy 仍是旧 release；该 parent-only 字段不发送到
  Go K-frame，旧二进制保持原配置 schema。缺少该字段的历史 receipt／report 按旧 release
  重建，新版 receipt 必须保留明确 policy；删字段或换版会拒绝新版 frozen batch。
  既有冻结 batch 及 ACK 哈希不重写，也不会自动回退或混用两个版本。

离线回归不证明原二进制在真实 Provider 上可用；真实启动及任何运行替换仍需另行授权。

本轮 PREPROVIDER release gate 定向修正：Python 在任何 `--live` 子进程启动前，
用源码内固定的 SHA256 allowlist 将所选 release 绑定到精确执行字节：

| release | 唯一受信二进制 SHA256 |
| --- | --- |
| 原始 v0.0.87 回滚 | `9480165a514e97764bff2aace55d5fe56923b34724b51f947f8a17ce00ce159a` |
| 上轮独立构建 v1.0.0 pinned 派生版 | `13cfe86189ea2309bf1dc6d7c096c390021195b577f804d90f18f0c4c64a0a93` |

信任根是经 Owner 冻结、代码评审的以上 pin；CLI、config 和邻接 `.build.json`
均不能增加信任。sidecar 仅是构建记录，缺失不影响已固定字节，伪造或符号链接 sidecar
不能使未知／篡改二进制通过。重建的 v0.0.87 对照物不在此 allowlist，不能直接用于
live 回滚；应重用上述原始回滚二进制。新增产物必须先经独立审查并更新固定 pin。

Linux 启动路径逐级使用目录 fd 和 `O_NOFOLLOW`，拒绝父目录／文件符号链接、`..`、
非普通文件、无执行位和超界文件；不先执行目标的 `--version`。文件复制到匿名 memfd 后，
内核必须确认 WRITE／GROW／SHRINK／SEAL 全部 seals，再对不可变快照计算 SHA256。
随后复核原文件和重新遍历路径的 inode、元数据及字节；核验期间替换或改写会拒绝。
`Popen` 仅继承验证后的 fd，从 `/proc/self/fd/<fd>` 执行 sealed 快照。
核验完成至 exec 的竞争期间即使路径或源 inode 被替换／改写，未经信任的字节也不会执行。
内核、memfd sealing 或 fd 执行不可用时 fail-closed，没有回退到原路径的执行方式。

该边界依赖可信 Linux 内核、系统加载器、进程环境、Python／启动代码和固定 allowlist；不防御能修改这些信任根的
同权限／特权进程。IPC 内部仍独立核验 connection／batch release，旧默认 policy、
旧 receipt 及 Go K-frame schema／冻结 batch 字节不变。确定性反例在拦截所有子进程启动
的条件下验证双向错版、未知／篡改字节、伪造 metadata、路径替换及 sealing 失败；
两版合成 IPC／ACK 另经实际 sealed fd 子进程验证，覆盖核验后换路径／改写源 inode。
这些 fixture 的 pin 仅在测试内临时注入，不提供生产信任覆盖入口。
本修正不改变上一事务 completion、Return 或 AO review。
