# A-EX-DIR 集成交接

实现仅位于 `src/market_data_foundation/candidates/tdx_exhq_directory/`，专属测试为 `tests/test_r5_i6_a_ex_dir.py`。详细字段/边界合同见 [OFFLINE_DIRECTORY_V1.md](../contracts/r5-i6-a-ex-dir/OFFLINE_DIRECTORY_V1.md)。不修改 D1/kernel、SDK、Go bridge、Source catalog、依赖或全局配置。

单一集成事务需要闭合的具体端口：

1. **Go → Python payload receipt**：当前 Go CLI 没有该通道。本模块要求 ExMarkets/ExCount/ExInstruments 的已解压原始响应 payload bytes，而上游三个方法的 decoded slice/int 已丢失 declared count、截断和短帧信息，不能拿 JSON 列表反推合格材料。未来 `drivers/tdx/exhq.go` / `main.go` / IPC 应在解码前截取 payload，保留精确请求 operation/start:uint32/count:uint16、捕获序号、Source/Binding/mapping revision、upstream commit、实际 derived binary SHA、材料 SHA/length/locator，以及独立时钟；经可信请求关联后构造 `CaptureContext` 与 `OfflineReceipt`。网络 framing/zlib 完整性与解压预算在 bridge 层验证，不能把响应前 4 bytes 当未经证明的 start 回显。本 lane 不实现或调用该 bridge。
2. **D2/D3 MaterialReceipt**：`OfflineReceipt.material` / `DirectorySnapshot.materials` 输出能力 `security.universe`、精确 producer operation、请求、版本和 byte lineage；`RowLocator` 给出材料 identity、raw hash、64-byte 行位置。它们不是 canonical Material key。集成人仍须补正式 Job/Shard/Attempt、endpoint/预算/请求身份验证、D3 材料独立读回/保留/Assessment 接线，不能仅凭 locator 入 canonical 或 Publication。观察器的成功不能替代上述门。
3. **D1 Universe/SourceMapping**：消费 `snapshot.directory_hash`、`market_table_hash`、`mapping_hash` 和 `DirectoryMapping` 的精确源三元组/行证据；不得把 source-scoped instrument_id 当具备历史稳定性的 canonical instrument ID。现有 D1 `AssetClass` 只有 A_SHARE/ETF/INDEX/SECTOR_INDEX，SourceMapping 需要有效日期区间，Universe/Calendar 需要资格和具名 revision。扩展 HK/期货/外盘类别、外部合约经济字段与映射区间必须独立补证、集中演进合同；当前代码不伪造 DateWindow、CalendarRevision 或 Source 权利，不能直接产出合格 D1 UniverseRevision。
4. **扩展能力与注册**：HK/FUT/GLOBAL 仅留 UNKNOWN entry，后续消费者须校验具体目录 hash 与关联状态；即使某条关联 exact，币种/手数/乘数/到期/日历/使用权仍需各市场证据。Source catalog/Definition 注册不在本 lane。实际 derived driver 字节变动、信任 pin、dial/握手/heartbeat/redial 计量及任意真实 Provider 执行另由有权集成处理；没有本笔 7727 探测或连接授权。

确定性验证命令（工作树无 `.venv`，复用已有 uv 管理的 Python 3.14.8 环境）：

```bash
PYTHONDONTWRITEBYTECODE=1 /srv/market-data-foundation/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_r5_i6_a_ex_dir.py
/srv/market-data-foundation/.venv/bin/ruff check --no-cache src/market_data_foundation/candidates/tdx_exhq_directory tests/test_r5_i6_a_ex_dir.py
/srv/market-data-foundation/.venv/bin/ruff format --no-cache --check src/market_data_foundation/candidates/tdx_exhq_directory tests/test_r5_i6_a_ex_dir.py
PYTHONDONTWRITEBYTECODE=1 /srv/market-data-foundation/.venv/bin/python scripts/check_repository_hygiene.py --include-untracked
```

专属用例由代码生成 synthetic bytes，覆盖 pinned AST 字段、9-byte 代码/前导零、精确跨市场/类别身份、GBK 错误、declared count、全部短帧长度、截断/尾部、重复/改名重复、分页缺口/重叠/顺序/短页/空页、count 漂移、零目录、预算、材料摘要/SDK/时钟/上下文混拼、UNKNOWN 经济字段、hash 对保留字节敏感、既有 observation 合同和零 socket 效果。没有真实市场 retained 样本，因此不能宣称真实服务目录、经济语义或 Source 资格已闭合。实际测试终态由外部运行收据记录，本文不自授专业 PASS。
