# A-EX-DIR 离线目录合同 v1

绑定 Market-Data-Foundation / #15 MDF Product Control / Owner-local；产品基线 `e7b64b382cbd8492b3e98785014270f2326ee2d3`。模块为 `market_data_foundation.candidates.tdx_exhq_directory`。所有入口只消费调用方明确提供的 synthetic/retained 内存 bytes；不打开 locator，不连接 Provider，不持久化材料或 observation。

字段证据固定为 upstream `v1.0.0` / `2a9000681f5a9896ea98e4de8bc4904f1dce78ba` 的 `protocol/model_ex.go`，并对照批次的 `UPSTREAM_V100_FIELD_STRUCT_INDEX_R1.json` 与 `UPSTREAM_V100_FIELD_SEMANTICS_GAP_AUDIT_R1.md`。解码 revision 为 `mdf.exhq.directory.decode.v1`；内容 schema 为 `mdf.exhq.directory.snapshot.v1`。它们描述 SDK 源字段和本地核验规则，不代表数据使用权或真实市场资格。

| 操作 | 输入与上游字段证据 | 离线核验 |
| --- | --- | --- |
| ExMarkets / 0x23F4 | `model_ex.go:30–35,176–198`；Market:uint16、Category:uint8、Name:string、ShortName:string（JSON `shortName`）；payload 开头 uint16 declared count，随后每条 64 bytes | category@0、name@1:33、market@33（wire uint8 扩为 uint16）、shortName@34:36；严格检查全部 declared slots。仅 category=0 且 market=0 的 slot 按 upstream 跳过，但记录 slot index，不能拿输出行数替换 declared count |
| ExCount / 0x23F0 | `model_ex.go:205–211`；SDK 返回 int，源值来自 payload[19:23] little-endian uint32 | 少于 23 bytes 拒绝，不能把 upstream 的短帧 0 当真实零。23 bytes 以后的内容无已证语义，保留在材料摘要中 |
| ExInstruments / 0x23F5 | `model_ex.go:38–44,215–246`；Category:uint8、Market:uint8、Code/Name/Desc:string；请求 start:uint32、count:uint16 | 请求 data=`<IH`；响应 declared count 位于 [4:6]，header 后每条 64 bytes；category@0、market@1、code@5:14（9 bytes）、name@14:31、desc@31:40；其余 bytes 不解释但纳入原始摘要 |

`decode_markets` / `decode_instruments` 接收的是**已解压响应 payload**，不是网络响应帧或上游 JSON 投影。两者要求长度恰为 header + declared_count × 64，拒绝截断和未声明尾部；不能继承 upstream `break` 后部分成功。短帧、None/可变 bytes、超过请求的 declared count、重复精确身份均拒绝。GBK 取首个 NUL 前的内容；保留代码前导零、大小写、空格，不转数字、不修补长度。无效 GBK 明确拒绝，避免上游忽略编码错误后形成损坏身份。NUL 后内容与保留字节仍受 raw hash 约束。

`PageRequest` 拒绝 bool/浮点/字符串和 uint 溢出，额外拒绝 start+count 超出 uint32 地址空间。原生 count=0 可表达；目录组装要求每页 count>0，避免不前进。响应前 4 bytes 不被猜测为 start 回显。

`build_directory(markets, count_before, pages, count_after)` 要求显式且相同 capture context 的 ExCount 前后材料；两端数量相等，按给定顺序从 start=0 连续推进；每页 declared count 恰为 min(request.count, remaining)。非末页短页、空页、缺页、交叠、乱序、重复品种和总数以后多余页均拒绝。真实零目录必须有有效零 count 材料且没有页。ExCount 是全部品种数量，不能把筛选某个 market 后的子集冒充该全局分页结果。

容量门为单 payload ≤4 MiB、累计材料 ≤16 MiB、品种 ≤100000、品种页 ≤1024；超限拒绝，不截断。这些是离线输入边界，不是传输、解压或生产后端性能承诺。调用方在构造 bytes 前的资源成本不由此适配层控制。

`OfflineReceipt` 校验调用方传入的 SHA256、固定 SDK commit、operation/request 形状、独立 obtained_at/received_at/known_at 的已知时钟先后关系。时间须带时区，None 保留 UNKNOWN；snapshot known_at 仅当全部材料 known_at 已知时取最大值。材料 locator 必须唯一且只作为引用值；SDK commit 声明不认证材料来源，producer derived binary SHA 缺失时保持 None。

`DirectorySnapshot` 保留有序原始字段、物理行序号、跳过市场 slots、材料 receipt、逐行 digest/byte offset 及映射。`directory_hash` 使用 canonical JSON（键排序、数组保序、UTC 时间、紧凑编码），涵盖 schema、所有 receipt 身份和 raw digest、页请求、上下文、时钟、成员及 UNKNOWN。`market_table_hash` 是 ExMarkets 原始 payload SHA256；`mapping_hash` 包含精确关联和行证据。相同经济显示值但 raw/保留字节、材料来源、mapping revision 或页顺不同不能共享目录身份。

关联只按响应表内精确 `(market, category)`，品种身份按 `(market, category, code)`。市场表外/类别不匹配记录保留 `UNKNOWN_MARKET_CATEGORY` 与空 market evidence，列入 `unresolved_associations`，不被静态枚举补齐。`STATIC_MARKET_REFERENCES` 只是明确标识的 upstream 枚举摘录，不是服务全集；30 的 SHF/INE 同值仍保留两条符号引用，无法据此识别真实 venue。31/48 不归并、不猜方向；category=2/3 在 QuoteList 的解码说明不成为目录到港股/期货产品的自动路由。

`tdx.exhq.source_identity.v1` 的 instrument_id 是 source+原始三元组的确定性摘要，不是交易所的跨历史 canonical instrument ID；不同 source 不互认，代码跨时间复用仍未解决。映射有效区间、上市/终止、历史成员、currency/lot/multiplier/expiry/calendar/unit 均 UNKNOWN。当前目录不扩展为历史全集。前后 count 一致也不能排除同数量换码、服务器同量漂移或跨页非原子快照；这里只声明 `bounded_count_and_pages_consistent`。

`observe_directory` 使用既有 `Capability.SECURITY_UNIVERSE`、`Fixture` 和 harness `observe`，固定 offline Binding，要求显式 observed_at，fixture 不可要求历史窗口。fixture symbols 是原样 `market:category:code`；缺失 exact symbol 返回 incomplete，页请求不得超出 fixture.page_size。observation 的 success/complete 只指本次有界结构与请求一致性，exhausted 保持 None，Source/使用权/经济资格均 UNKNOWN；异常仅经既有分类器输出类别、模块、类型，不输出原始异常文本或 payload。函数返回内存字典；如未来保留材料/observations，必须由调用方放在忽略的 `artifacts/`。

未获资格的 `EXTENSION_ENTRIES` 为 `tdx.exhq.hk.offline.v1`、`tdx.exhq.futures.offline.v1`、`tdx.exhq.global.offline.v1`。这些是后续集成端口名字，不包含行情实现、CalendarRevision、Source rights 或 live 资格。
