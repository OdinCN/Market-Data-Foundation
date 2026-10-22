# I-4 Go 主入口与离线构建 Profile 候选 R1

本候选对应 Session `#16 MDF Product Control`、Board `Owner-local`，仅在
`mdf-i4-go-entry-builder-20261023-r1` 隔离工作树实施。产品基线为
`6ca0801efd8bbb7261831f73c3c06c9b5e5f5c3c`。这是源码及离线构建候选，
尚未通过独立 binary review、Driver pin/admission 或 activation。

## 变更与门禁

实际产品修改仅有：

- `drivers/tdx/main.go`：在原版本/IPC 入口之前调用现有 `I4Dispatch()`。
- `scripts/build_tdx_driver.py`：增加显式 `--i4-candidate`，记录 profile、tags、
  release、实际生成 binary 的路径/大小/SHA，并明确 `runtime_admission=not_reviewed`。
- `tests/test_tdx_driver_build.py`：覆盖错误 SDK/profile、目的地不可覆盖、
  默认/回滚 release、test/build tags 和离线环境。
- `tests/test_i4_builder_integration.py`：显式使用本机已构建收据，检查实际二进制。
- 本说明文件。

现有 Go I-4 实现、禁用路由、SDK、wait-preregister patch、Python runtime
信任表及依赖锁均未修改。

| 选择 | SDK | Go test / build tags | release 后缀 | `--i4-probe` |
| --- | --- | --- | --- | --- |
| 默认 | v1.0.0 | 无 | `+mdf-wait-preregister-r1` | 保持旧行为，退出 1 |
| 默认回滚 | v0.0.87 | 无 | `+mdf-wait-preregister-r1` | 保持旧行为，退出 1 |
| `--i4-candidate` | 仅 v1.0.0 | `mdf_i4` | `+mdf-wait-preregister-r1+i4-raw-r1` | 精确具名入口 |

候选完整 release 为：

```text
injoyai/tdx v1.0.0@2a9000681f5a9896ea98e4de8bc4904f1dce78ba+mdf-wait-preregister-r1+i4-raw-r1
```

I-4 profile 与 v0.0.87 的组合在 SDK/Go 执行及输出目录创建前由参数门禁拒绝。
原 SDK HEAD/tag、输入/派生 client、patch 字节及 Go 1.25.3 检查继续执行。
环境中的 `GOFLAGS` 被清空，不能隐式给默认 profile 注入 I-4 tag；
`GOPROXY=off`、`GOSUMDB=off`、`GOTOOLCHAIN=local` 等原离线约束保留。

Tagged binary 只对精确单参数 `--i4-probe` 调用现有 I-4 Host 入口；
`MDF_I4_ALLOW_LIVE` 必须精确等于 `1`，否则读取 stdin、拨号及发送之前退出 2，
stdout/stderr 为空。多余参数仍进入旧参数拒绝路径。默认及回滚构建的
`I4Dispatch()` 恒为 false，原 `--version`、K-frame/ACK IPC 及旧调用语义保留。
本次没有向可执行 binary 提供 live opt-in，也没有运行 `--live`。

## 离线复现

使用已存在的本机 Python 3.14.8 环境、Go 1.25.3、只读 SDK 与模块缓存。
不下载 module、SDK 或工具链。先选择全新的输出目录；既有 binary/收据不可覆盖。

```bash
export UV_PROJECT_ENVIRONMENT=/srv/market-data-foundation/.venv
export UV_CACHE_DIR=/tmp/mdf-i4-uv-cache
export GOMAXPROCS=2

uv run --offline --frozen --no-sync python scripts/build_tdx_driver.py \
  --sdk-checkout /var/tmp/mdf-tdx-sdk-v100-20261021 \
  --sdk-version v1.0.0 \
  --go /var/tmp/mdf-tdx-live-20261011/toolchain/go/bin/go \
  --module-cache /var/tmp/mdf-tdx-live-20261011/pkg/mod \
  --output artifacts/NEW-i4-offline/i4-candidate --race --i4-candidate
```

默认 v1.0.0 构建删除 `--i4-candidate`，输出命名为 `default-v100`。
回滚验证同样不提供该 flag，SDK checkout 使用本机既有
`/var/tmp/mdf-tdx-live-20261011/release-v0.0.87`，SDK version 为 `v0.0.87`，
输出命名为 `rollback-v0087`。回滚仅为离线兼容性对照，不作为 I-4 SDK 身份。
三种构建均先执行 SDK `./protocol` 和 driver `./...` 的
`go test -mod=readonly -count=1 -timeout=120s -race`，再执行
`go build -mod=readonly -trimpath -buildvcs=false`；只有 I-4 profile 在这两类
命令加入 `-tags=mdf_i4`。不运行可能拨号的 SDK 根测试。

实际 binary 测试仅在显式提供本机收据目录时运行，普通确定性测试自动跳过它：

```bash
MDF_I4_OFFLINE_BUILD_DIR=artifacts/NEW-i4-offline \
  uv run --offline --frozen --no-sync pytest -q \
  tests/test_tdx_driver_build.py tests/test_i4_builder_integration.py
uv run --offline --frozen --no-sync pytest -q
uv run --offline --frozen --no-sync pytest -q tests/test_i4_*.py \
  tests/test_tdx_driver_build.py tests/test_tdx_watchlist_runtime.py \
  tests/test_p5_go_tdx_watchlist_source_adapter.py
uv run --offline --frozen --no-sync ruff check .
uv run --offline --frozen --no-sync ruff format --check .
uv run --offline --frozen --no-sync python scripts/check_repository_hygiene.py \
  --include-untracked
git diff --check
```

## 本机证据与结果

本次实际输出位于被 Git 忽略的 `artifacts/i4-builder-offline-r1/`。
该目录属于产品源码之外的离线证据，未作为运行入口采用：

- `default-v100`、`rollback-v0087`、`i4-candidate`：各 profile 的生成 binary。
- 同名 `.build.json`：实际 profile/tags/release、SDK 与 Go 输入摘要、race
  测试结果以及生成 binary 的身份；SHA 仅是构建观察，不是可信 runtime pin。
- 同名 `.log`：各构建的协议/driver race 测试及构建结果；三种构建均通过。
- `pytest-builder-binaries.log`：构建器与实际二进制验证，49 passed。
  opt-in 负例覆盖缺失、空值、`0`、`true`、`01`、`1 `；stdin 保持打开且无输入，
  要求立即退出，验证门禁位于 plan 读取之前。已有 tagged Go 内存/mock 测试
  另覆盖构造、预算、响应关联、sink failure、deadline、压缩尾部和拒绝前零拨号。
- `pytest-related-regression.log`：I-4、构建器、旧 runtime 和 source adapter
  确定性回归，491 passed、30 skipped；跳过的 30 项本机 binary 检查已在上述
  显式收据测试中通过。
- `pytest-deterministic.log`：完整确定性 pytest 为 **INCOMPLETE_BLOCKED**。
  输出 1578 个成功进度标记后，在未修改的
  `tests/test_cpi_scheduling.py::test_http_acceptance_auth_strict_input_and_no_execution`
  停滞；中止该挂起进程，退出码 130，不宣称全量通过。
- `pytest-http-blocker.log`：该单例以 `-o faulthandler_timeout=10` 独立复现，
  35 秒外层 timeout 终止；栈显示 AnyIO worker queue 与 asyncio selector 等待。
  命令为 `timeout 35s ... uv run --offline --frozen --no-sync pytest -q
  tests/test_cpi_scheduling.py::test_http_acceptance_auth_strict_input_and_no_execution
  -o faulthandler_timeout=10`。未修改范围外 HTTP/AnyIO 代码或依赖来绕过该阻塞。
- `pytest-collection.log`：用于定位停滞测试的只收集输出，不执行测试；
  7475/7494 tests collected，19 deselected。
- `checks.log`：全库 Ruff、format、repository hygiene 与差异空白检查结果。
- `offline-evidence.json`：最终产品输入摘要、精确构建命令、退出码及各 binary/
  收据/日志的外部冻结摘要。产品文件不嵌入自身最终 SHA 或后续运行身份。

生成 binary 和收据在记录外部证据时设为只读；这是本机文件权限冻结，
不等同于不可篡改存储、独立 Reviewer 签署或 runtime admission。

## 保持的 STOP 与下一 Gate

`I3_PHYSICAL_STORAGE: STOP`

真实 TDX/ExHq Provider、PG/Garage/Storage、Driver pin、替换旧 binary、
Git commit/push/PR/merge、CI、timer、部署、activation/adoption 均未执行。
已有 runtime 哈希信任表、旧冻结 binary 和默认运行配置不变。

下一独立专业 review Gate：Reviewer 核验本候选源码、SDK/patch/toolchain 输入、
离线测试和外部冻结 binary 身份，并补完独立 Host 全量确定性回归；
只有另行批准的 pin/admission Gate 才能考虑
将精确新 release/SHA 加入 runtime 信任表。Provider/Storage 或 activation
仍需要各自明确授权；本候选不预先通过这些 Gate。
