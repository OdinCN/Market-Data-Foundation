# I2 最后三个 P2：本轮实现回报

本轮依据 active `AGENTS.md`、事务 `BATCH_DIRECTIVE.md` / `SHARED_PORT_R1.md`、
`outputs/I2_CORRECTION_CODEX_RETURN_R1.md` 和新的独立
`outputs/REVIEW_I2_VERDICT_R1.md`，仅处理最后三个 P2。
这是实现侧离线回报，不是独立 Reviewer PASS、live 授权或 adoption。

## 精确写集

| 文件 | 本轮修改 |
| --- | --- |
| `src/market_data_foundation/qualification/tdx_market_collect.py` | 中断后停止/回收 child、有界非阻塞 stdout 排空；完整/partial/UNKNOWN 效果对账 |
| `scripts/mdf_i2_source.go` | 股票日 K 的逐字段消费长度验证 |
| `scripts/mdf_i2_source_test.go` | 真实变长尾部、截断和原 SDK 投影一致性测试 |
| `tests/test_i2_final_p2.py`（新） | 10 个确定性用例，包括真实本地 child/匿名管道的中断反例 |
| `docs/r5-i7-i2/FINAL_P2_CORRECTION.md`（新） | 本回报 |

运行材料仅在 ignored `artifacts/i2-final-p2-qa-20261022/`。没有修改旧回报、
review、STOP、candidate 收据、SDK/pin、Go 主入口或其他工作线。
已确认的 builder 冻结、日历授权、bytes 前置预算及 retained intake 均保留；
旧 SHA 对照记录见本轮 `final-readback.json`。未改变 Python 3.14.8 或依赖。

## 三项修正及实际反例

1. **未读 stdout 中断保留。** `SourceStopped` 在清理排空之后构造。
   首先 kill producer，最多等待回收 2 秒；随后设置 stdout 为非阻塞，
   在 0.5 秒排空窗口和原 16 MiB 总 stdout 上限内读取。遇 EOF、EAGAIN、
   时间或字节上限立即结束，不等待继承写端关闭。每块仍通过原回调
   flush/fsync；partial 文件继续 SHA/readback/fsync。回收超时仍排空，
   并保留超时分类，而不覆盖原中断。清理和最终 ledger 写入期间屏蔽再次
   SIGINT/SIGTERM，随后恢复原处理器。原 intent 不改写，最终 ledger 写完后传播中断。

   新测试启动的只是本地无网络 Python writer：写入 **404 字节有效握手 JSONL**，
   然后等待信号。selector 确认 pipe 可读后、父进程第一次 `os.read` 前触发
   KeyboardInterrupt 或实际 SIGTERM。断言 child 已被 kill/reap 才开始非阻塞读、
   progress/partial 两份均保留 404 字节并实际 fsync、最终 UNKNOWN ledger 和
   原 intent 均 fsync、无 quote 启动，整条路径小于 5 秒。
   另测试写端仍打开、缩小测试上限至 200 字节、回收超时三种情形。
   在保存的旧代码上，前两例明确失败于 `b'' != 404-byte JSONL`。

2. **observed 下界与 total effect 分开。** `actual_request_bytes` /
   `actual_response_bytes` 在无法证明完整退出时为字符串 `UNKNOWN`。
   `source_effect.total_effect` 明确为 `UNKNOWN`，而
   `observed_confirmed_lower_bound` 保留已验证记录对应的字节数。
   仅有握手并退出 -9 时，下界仍为 **13 / 17**，stage 为 `HANDSHAKE_ONLY`。
   `source_effect` 绑定原 run_id、plan_revision，逐项记录握手及 frozen requests
   的 index/request、完整或 partial 记录、已观察字节；缺失记录统一标为
   `UNCONFIRMED_EFFECT_UNKNOWN`，绝不推断下一请求未写入。
   未验证的 JSONL 尾部单独计数。中断路径同样对已保存完整行做对账。

   只有退出 0、所有预定记录完整且验证通过、stdout 无未核实尾部时才为 `EXACT` /
   `COMPLETE`，原精确计数保留。测试涵盖 handshake/-9、handshake/0、full/-9、
   full/0、partial/-9。完整退出仍精确为 **31 / 35**；partial 记录保留各自已观察值。
   旧代码负例直接比较原 actual 值，复现 `(13, 17)` 或 `(31, 35)` 被误作全局确定值。

3. **Kline 完整消费。** 只读核实锁定 SDK commit
   `2a9000681f5a9896ea98e4de8bc4904f1dce78ba` 的
   `protocol/model_kline.go` (`Decode`) 和 `protocol/types_price.go` (`GetPrice`)。
   KindStock 日 K 每行是 time[4]、四个以 `0x80` 表示 continuation 的变长价格字段、
   volume[4]、amount[4]。新增扫描器只确认逐字段边界及最终 offset 恰等于 payload
   长度；价格、日期、量额解码仍使用原 SDK，不猜固定行宽/变长字段最大宽度，
   不另行解释或修补价格。扫描受既有 payload 字节上限及 requested count 约束。

   Go mock/race 测试中，Count=1 后放两条不同长度的完整记录，并备妥下一请求的
   有效响应；新代码返回 `decode_invalid`，保留准确原帧，只写握手和 Kline 两次。
   另覆盖单字节尾部、Count=0 带记录、未终止价格（旧 SDK 可返回零而不消费），
   以及有效两条记录的每个截断前缀。1/2/3/4/6/20 字节价格字段的完整记录与原 SDK
   JSON 投影逐字节一致。隔离旧代码的 race 测试实际跑出四种错误 payload 全部继续
   请求，**status=0 / writes=3**，不是未执行的静态推测。

## 实际验证与最终材料

环境：既有 uv 管理的 Python 3.14.8；`UV_PROJECT_ENVIRONMENT=/srv/market-data-foundation/.venv`，
`UV_CACHE_DIR=/tmp/mdf-i2-uv-cache`，`uv run --offline --no-sync`。
Go 沿用未修改 builder 的 pinned archive、Go 1.25.3、`GOPROXY=off` / `GOSUMDB=off`。

| 检查 | 实际结果 |
| --- | --- |
| 三个 I2 测试文件 focused pytest | 61 passed，含本轮 10 个新用例 |
| Go `test -mod=readonly -race -count=1 -timeout=60s` 和 build | 通过，12 个顶层测试（含表驱动子例） |
| 保存的旧 Python + 本轮负例 | 7 failed（预期失败，逐项日志留存） |
| 保存的旧 Go + 本轮 mock/race 负例 | 4 个尾部/未终止子例 failed（预期失败） |
| 仓库 Ruff check / format check | 通过 |
| gofmt 检查 | 通过，无未格式化文件 |
| repository hygiene（含 untracked，含本回报） | 通过 |

完整命令、退出码、时间和日志 SHA 在 `checks.json`；最终文件大小/SHA、
Go 构建输入核对、旧文件保持不变的对照在 `final-readback.json`。
初次新 Go 测试曾把 JSON RawMessage 的 `null` 错当成零长度；修正测试断言后，
上述最终 Go race/build 通过。初次旧 Python 对账负例先碰到新字段缺失；
最终调整断言顺序，直接证明旧 actual 值错误。没有更改被测效果来迁就测试。
本轮没有重跑全仓 deterministic suite；未将上一轮 490 个兼容测试充作本轮执行。

新 candidate：`artifacts/i2-final-p2-qa-20261022/source`。
SHA256：`0e50715defd15641bb8f0ad748ca80f4b6516370a24fabbfe5e00f7e0239c17c`。
构建收据：相邻 `source.build.json`。这是本轮候选，不改历史 candidate 或 plan。

## 实效、未决项及交接

本轮实效仅本地代码/测试/文档、离线构建和 QA 材料；真实 Provider、ExHq、PG、
Garage 请求/写入均为零；无部署、timer、Git/PR 变更。构建验证器、pin 事实读取及
hygiene 按既有流程只读 Git 元数据/树，没有 staging、commit、branch 或 push。

中断后的 Provider 总效果仍可能 UNKNOWN；stdout 下界不是所有网络写入。
进程回收和非阻塞排空有明确时间/字节界限，但本地 fsync 依赖存储系统可响应；
不承诺故障内核或卡死存储上的硬实时完成。SIGKILL / power loss 无法保证最后动作
或最终 ledger，必须从原 intent 和已 fsync 的前缀审计，不能把缺失收据当零效果。
清理的 BYTE_LIMIT/TIME_LIMIT/回收失败等原因均留在 ledger，不能当作完整 drain。

live freshness、源事件时间/单位、日历权威、quote TCP response bytes 仍未获证明。
没有新 live run/plan。本轮仅提交同一候选供 Host **新 Reviewer** 按最终 bytes/SHA
独立核验；独立复审与同字节全量集成 QA 仍待完成。实现测试退出 0 不替代这些裁定。
