# I-3 候选回执 R1

Session #15 / Owner-local / MDF-DEV；基线
`e8faac58b8d97594109ed2dc63ea110afb36cfce`。交付独立物理存储候选、DDL、有限Host
探针及清理工具。**本线工程测试通过；真实PG/Garage资格受凭据缺失阻断，非真实PASS。**
未提交/推送/合并；等待集中同字节Host QA与专业复核。

## 实际文件清单

| 文件 | 实现 |
| --- | --- |
| `src/market_data_foundation/storage/market_dataset_codec.py` | D1/D3闭合类型、UTC、版本化编码、长度门、拒绝任意执行/未知类型 |
| `src/market_data_foundation/storage/market_dataset_garage.py` | 固定MDF bucket/endpoint、仅本run prefix、签名条件PUT、负向条件能力验证、精确GET/SHA、请求/时间上限、精确清理 |
| `src/market_data_foundation/storage/market_dataset_postgres.py` | 固定DEV连接/intake、DDL、材料/Version/Publication原子写、独立heads/receipts、显式重建/停止及安全schema回滚 |
| `src/market_data_foundation/storage/market_dataset_reader.py` | 精确Key/日期SQL、显式只读repeatable-read生命周期、D3有界view校验 |
| `src/market_data_foundation/storage/market_dataset_probe.py` | 可重复、明确invented的一日合成fixture，不是市场数据证据 |
| `db/market_dataset/001_market_dataset.sql` | 15张必要表、FK/unique/check、候选与历史复合/反向索引 |
| `scripts/mdf_i3_storage_probe.py` | 只读intake / 显式DEV probe、冻结代码SHA与预算、丢ACK、新连接核实、单表故障恢复与读回、脱敏收据 |
| `scripts/mdf_i3_storage_cleanup.py` | 默认本地计划；核对原收据/owner/DDL后仅清理自身PG对象和精确S3 PUT intents |
| `tests/test_i3_market_dataset.py` | 55项确定性SQL事务/存储transport/失败恢复/Host编排边界测试 |
| `docs/market-four-stream-i3/PHYSICAL_MODEL_R1.md` | 物理模型、表/索引、时间/身份、权限/持有/恢复/集成缺口 |
| `docs/market-four-stream-i3/HOST_RUNBOOK_R1.md` | Host实际执行、预算、凭据注入、失败核实和清理命令 |
| `docs/market-four-stream-i3/RETURN_R1.md` | 本回执 |

pyproject.toml/uv.lock无变更：复用已有psycopg及标准库，无新依赖。FastAPI已声明，
未误改依赖。未写I-1/I-2/I-4或共享D1/D2/D3/D4、Go主入口、旧allowlist、Driver pin。

推荐数据库/schema为原 `market_data_foundation_dev.mdf_market_dataset_dev_r1`。
推荐Garage沿用 `mdf-artifacts/market-datasets/v1/`；本候选能力严格限于其中
`probes/i3-<uuidhex>/`。canonical/projection各表及索引详见物理模型。
DDL revision=`market.dataset.physical.r1`，文件SHA256：
`b3214dea21480a21fc90e78a1d6e21325b807941775a24a7777e6b5840306aaa`。

## 实际命令和门禁

Python为已有uv管理3.14.8，uv 0.12.22。命令统一使用：

```sh
export UV_CACHE_DIR=/tmp/mdf-i3-uv-cache
export UV_PROJECT_ENVIRONMENT=/srv/market-data-foundation/.venv
uv run --no-sync pytest -q tests/test_i3_market_dataset.py
uv run --no-sync pytest -q tests/test_i3_market_dataset.py tests/test_r5_i6_* tests/test_canonical_kernel.py
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync python scripts/check_repository_hygiene.py --include-untracked
git diff --check
```

- 最终I-3专项：**55 passed in 2.83s**。最后一项在SQLite事务double+内存S3 transport
  上执行完整Host脚本：原身份丢ACK核实、新连接编排、单表损坏拒读、重建、精确清理。
- D1/D2/D3/D4、目录、clocks、canonical kernel与当时42项I-3合并回归：
  **1085 passed in 166.60s**。之后新增Host入口/ACL/异常脱敏/完整编排测试已包含在
  最终55项专项中；没有把两次重叠测试相加当作不同用例数。
- 全仓Ruff、format、含untracked hygiene、diff whitespace检查通过；最终检查另有
  ignored artifact日志。没有新增运行依赖、系统Python修改或CI私网访问要求。
- 完整默认deterministic pytest确实尝试过，前1578项输出通过后，在既有
  `test_cpi_scheduling.py::test_http_acceptance_auth_strict_input_and_no_execution`
  停滞，主动中止exit130；**完整套件未完成，不能标PASS**。
  单例 `timeout 30s ... pytest -q <上述node> -o faulthandler_timeout=8` 重现exit124；
  堆栈为既有HTTP测试的asyncio select和AnyIO worker queue等待，没有到达此线存储代码。
  正常Host仍须执行完整门禁，不以历史R5-I6 PASS替代。

## 真实effect ledger与资格

最终入口实际执行：

```sh
PYTHONPATH=src uv run --no-sync python scripts/mdf_i3_storage_probe.py \
  --action intake --receipt artifacts/i3/candidate-intake.json
```

结果exit2，`BLOCKED / CREDENTIALS_NOT_INJECTED`。当前进程的三项凭据均未注入，
脚本在网络前停止；未读取其它项目secret、不绕过沙箱。run为
`i3-461ee4ac673a469eae74e06fbd99e45f`（精确实际run以原收据为准）。

| 区域 | 本次实际效果 / 资格 |
| --- | --- |
| PG连接/TLS/角色/ACL | NOT_ATTEMPTED；当前真实权限UNKNOWN，Owner提供的能力锚点未被当作本轮PASS |
| PG schema/DDL/行写/commit/readback | 0次真实效果；未创建schema/数据库/role，未修改已有P4/宏观材料 |
| Garage HEAD/GET/PUT/DELETE | 0次真实请求；bucket当前ACL、条件创建、持有/读回均UNKNOWN |
| 清理 | NOT_REQUIRED，本次没有远端新对象；有界精确清理工具已实现并在double编排验证 |
| 单表失败恢复 | 确定性double PASS；Host脚本具备真实执行步骤，当前真实资格UNKNOWN |
| 存储重启/PITR | 代码支持新PG连接读回编排；未执行真实服务重启/断电/PITR，均UNKNOWN |
| Source/市场 | 无Provider请求、无真实下载。A股/ETF/指数/HK/期货语义与freshness分别UNKNOWN |
| 本地 | 专属源码/DDL/文档/测试与ignored artifacts；无生产、交易、部署、activation/adoption效果 |

## 原始证据与同字节交接

运行材料全部留在git-ignored `artifacts/i3/`。原收据只含分类、目标、身份和SHA，
没有凭据值、DSN、raw异常或真实价格。`candidate-intake.json`冻结本次源码/脚本/DDL的
逐文件SHA256。主要证据：

| artifact | SHA256 / 结果 |
| --- | --- |
| `candidate-intake.json` | `b5e5eeab275fae5aecfcca41066490b6ba13413ccefd877b439e4df9b24518d5`；BLOCKED |
| `i3-focused-final.txt` | `ce3c39bf3a2084a870eabe6380b14678d920a1d5f237132daeaa65cbff4b3cba`；55 passed |
| `contract-regression.txt` | `572f6ff31fb384093b3fafcffab353de62e5e33ebcfe7c7c2cc88f145095d6e7`；1085 passed |
| `sandbox-http-diagnostic.txt` | `8c55af866a52746161e8542b20cff25eda28a8cad0dcccaeb172b93df00a0644`；8秒stack、30秒exit124 |
| `deterministic-pytest.txt` | 完整套件未完成的原始输出，非PASS证据 |
| `test-collection.txt` | 当时6963 deterministic / 19 excluded的collection定位证据；不是执行结果 |

下个gate：Host按runbook完成当前ACL/name/凭据隔离intake并执行固定预算probe；
核对同字节candidate、真实write/GET/原身份/rebuild/cleanup收据；再做独立专业复核、
集中完整deterministic QA和条件Git/Hosted CI。D4生命周期接线、全Job manifest持久化、
PG代表规模EXPLAIN/I/O、备份/正式retention及任何production adoption仍需独立完成。
本回执不授予timer、长期运行、交易、生产部署或Phase/Release权限。
