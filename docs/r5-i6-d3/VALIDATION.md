# D3 本地实现验证记录

绑定基线：`e7b64b382cbd8492b3e98785014270f2326ee2d3`。仅 D3 四个 `canonical/market_*` 新模块、`tests/test_r5_i6_d3_material_projection.py`、本线合同与验证说明；未改共享文件、依赖、HEAD，未执行 commit/push/部署。没有 Provider、PG、Garage、网络或 DDL 操作。

此记录是执行结果与限制，不是独立审查或专业验收结论。集成端仍需对最终候选做独立 review 和六线集中 Host QA。

## post-STOP Directed Correction R1（本轮）

本轮仅修正 `ao_recovery_review3.return.md` 指出的 P1：canonical 已保留 PUBLISH + WITHDRAW，投影 history 与 guard 同时被替换为旧值时，读口仍能返回已撤回内容。历史 STOP 和旧 reviewer/return 证据保持原样；本次修正不解除 STOP，不启动额外自动修正或自称独立验收。Host 后续对最终文件计算 bytes/SHA，再提交一次独立、revision-bound reviewer 复核。

本轮修改已有 D3 候选中的 `market_publication.py`、`market_persistence.py`、`market_projection.py`、专属测试和两份说明；`market_material.py` 无改动。增加独立于 `ProjectionIndex` 的 canonical `PublicationHead`（Version、count、head、完整历史摘要），在 Version/Publication 原子事务内建立或更新。读口 `histories/version_guards` 用固定数量主键点查绑定 canonical anchor，再按既有预算验证历史；`select/content` 继承此门，不再以可同时失真的 projected guard 为唯一依据。writer 也不能沿旧投影追加业务决定。restore/rebuild 从完整 canonical 图和原 receipts 校验重建锚点与投影，备份不信任派生快照。

新增 **33** 项确定性回归。原问题及多次发布后的相同投影回滚，在修复前的8项直接读口回归均真实失败（**8 failed**）；修复后验证 histories/select/content/version_guards 全部返回 `INDEX_INCONSISTENT`，canonical/receipts/投影完全不变，且没有原材料读回。另覆盖无全表扫描的点查约束、0/2/3/65事件的 rebuild/restore、缺尾部但仍有receipt时拒绝恢复、withdraw事务各故障点及ACK丢失原身份重放、锚点缺失/错误、writer拒绝旧投影，以及超限历史在访问/序列化前失败。既有64事件、累计2048事件边界与累计超限回归保留；超过预算的canonical完整历史仍由checkpoint保留，未截断。

从本工作树运行下列命令，使用已有 uv 管理 Python **3.14.8**，未安装或更改依赖：

```bash
env PYTHONDONTWRITEBYTECODE=1 UV_OFFLINE=1 GOPROXY=off \
  /srv/market-data-foundation-control/worktrees/r5-i5-cpi-scheduling-20261020/.venv/bin/python \
  -m pytest -q --capture=sys -p no:cacheprovider \
  tests/test_r5_i6_d3_material_projection.py \
  tests/test_r5_i6_market_contracts.py \
  tests/test_canonical_kernel.py \
  tests/test_repository_hygiene.py
/srv/market-data-foundation/.venv/bin/ruff check --no-cache .
/srv/market-data-foundation/.venv/bin/ruff format --check --no-cache .
env PYTHONDONTWRITEBYTECODE=1 /srv/market-data-foundation/.venv/bin/python \
  scripts/check_repository_hygiene.py --include-untracked
git diff --check
```

四文件确定性回归结果 **287 passed**，其中D3专属 **169** 项；包含D1、canonical kernel与卫生测试。全树Ruff、format（289文件）、含未跟踪候选的hygiene（326文件）、`git diff --check`及全部七个未跟踪候选的`git diff --no-index --check`通过。HEAD仍为绑定基线；未改branch、commit、push，未接触D1/shared SDK/IPC/其他lane、Provider/网络或PG/Garage/部署/adoption。

未运行全树pytest。真实存储durability、受保护canonical anchor存储和主键查询计划/成本/deadline，以及D2/D4/kernel集成仍未资格验证；独立Reviewer结论尚待Host办理。本轮只提供可复核候选和真实本地检查结果。以下章节均保留此前候选记录，不代替本轮结果。

## 本轮独立审查纠正：遗漏源行与历史预检

本轮修改 `market_material.py`、`market_projection.py`、专属测试与两份本线说明；其余产品模块保持原样。材料解码 port 新增 `enumerate_rows`，独立校验完整材料/页格式并枚举物理行，再与 receipt 全量对账；空 locators 不再意味着没有行情行。合成的无行情证据页改用显式 JSON evidence 格式，fixture decoder revision 为 `synthetic-json-r2`，不代表真实 TDX 解码或经济语义资格。

`histories` 对整批 guard 身份/count/change、每 Version 64 事件和累计 2048 事件完成预检并预留预算后，才访问历史；实际长度检查在摘要前，之后仍验证摘要、canonical 事件和状态转换。超限事实继续完整保留，不截断历史。

新增 21 项确定性回归，并加强原 65 事件用例：遗漏 duplicate/unique/范围外行（声明和未声明隐藏页两组）在 assessment 与伪造 assessment 的 Version 提交前均拒绝，canonical/projection/receipts 不变、Publication 不可产生；完整声明的重复日期仍拒绝。另覆盖所有 locators 省略、页身份/顺序、空页/尾部/未定位非法字节，以及 guard 身份/count/change、实际历史长度与 guard 不一致。历史反例直接禁止历史访问和 `content_id`，证明 65 事件及单批/跨调用累计超限在读取/序列化前失败；64 事件和累计 2048 边界成功。

使用下文现成 uv 管理的 Python 3.14.8 环境，设置 `PYTHONDONTWRITEBYTECODE=1 UV_OFFLINE=1 GOPROXY=off`，执行四文件定向回归并使用 `--capture=sys -p no:cacheprovider`：**254 passed in 28.97s**，其中 **136** 项 D3 专属测试。全树 Ruff、格式检查（289 文件）、含未跟踪文件的仓库卫生检查（326 文件）和差异检查通过。HEAD 保持绑定基线，七个未跟踪候选文件仍全部位于独占前缀；未执行 commit/push/分支切换或真实存储、Provider、网络操作。本轮未跑全树 pytest。

`MarketProjectionRead` 的签名和返回类型保持不变，D4 无需接口迁移；能力 decoder 的新增枚举方法是必须落实的集成缺口。本线仍无真实 durability、后端 deadline/查询成本或 D2/D4 接线资格；精确接缝见 [PORT.md](../contracts/r5-i6-d3/PORT.md)。下列既有结果均为此前候选记录，不代替本轮验证，也不构成独立复审或专业验收结论。

## generation 复用阻塞修正（保留记录）

本轮仅修改 `market_persistence.py`、专属测试、`PORT.md` 和本记录。`rebuild` 除检查当前 generation 外，还检查 restore/rebuild 两类 receipt 中已使用的名称，包括精确恢复导入的历史；拒绝时返回 `new_generation_required`，不改变事实、receipts、覆盖或当前 generation。原操作相同 identity/payload 的幂等重放仍返回原 receipt，不重新激活旧 generation。

新增 **8** 项确定性回归。修复前为 **2 failed、6 passed**，分别复现同 ledger 及经再次恢复导入后复用 restore generation；修复后 **8 passed**。测试验证拒绝前后旧 generation 均返回 `INDEX_GENERATION_CHANGED`、checkpoint 不变、当前内容和完整历史可读；覆盖 restore/rebuild 的原操作重放、导入后的重放，以及恢复拒用两类历史名称。

使用下文现成 uv Python 3.14.8 环境和四文件 pytest 命令，增加 `--capture=sys` 使用内存捕获，结果 **233 passed in 26.60s**，其中 **115** 项 D3 专属测试。全树 Ruff、格式、含未跟踪文件的仓库卫生检查（326 文件）及 `git diff --check` 通过。HEAD 保持绑定基线，未提交、推送或改分支，没有网络、Provider、PG/Garage 或 DDL 操作。

未运行全树 pytest；此记录不替代针对最终候选的独立复审。D4 的 `MarketProjectionRead` 签名和返回类型不变，继续每页重开视图并核对 generation。真实写适配器必须在原子边界保留并联合检查 restore/rebuild 恢复身份，不能分别去重。D2/kernel/Go 接线、真实 durability、查询成本和 deadline 限制继续见 [PORT.md](../contracts/r5-i6-d3/PORT.md)。下文保留此前验证，不作为本轮计数。

## review3 两项阻塞修正（保留记录）

本次改动限于 `market_persistence.py`、`market_projection.py`、专属测试及本线两份说明。历史 automatic STOP 收据、`review1..3.return.md` 保持只读原样；本次结果不解除 STOP，也不替代新的独立审查。

- P1：计划完成精确核对 Definition、instrument、Universe revision、Calendar revision 和 timezone。已发布不兼容候选不能证明 Publication 完成；发布校验失败后的材料轴 fallback 也只使用相同 scope 的 assessment。兼容的已撤回历史仍可证明材料读回，不能证明当前发布。日期范围继续按格点汇总，既有拆分范围正例保留。
- P2：`version_guards()`、`admission_guards()` 在读取 guard 前对共享 `versions_seen` 计数。与 candidates/versions/histories/content/lineage 累计同一 256 上限，重复 key 不加计数；UNKNOWN/denied admission 也计数，超限稳定返回 `READ_CAPACITY_EXCEEDED`。D4 port 的签名和返回类型未改变。

新增 **14** 项确定性用例，修复前运行新增子集为 **11 failed、3 passed**，修复后为 **14 passed**。三个 scope 字段分别验证原版本撤回后替换发布、无兼容版本时未发布材料 fallback、独立冻结 manifest 及审计无写入；guard 使用公共 writer 提交 257 个不同 Version，验证批量/逐个 256 成功、重复访问、257 拒绝及与元数据/历史/lineage 混合调用共享预算。

沿用下文现成 uv Python 3.14.8 环境及四文件 pytest 命令，结果 **225 passed in 29.66s**，其中 **107** 项 D3 专属测试；包括 D1、canonical kernel 和 repository hygiene 回归。全树 Ruff 检查、全树格式检查、含未跟踪文件的仓库卫生检查及 `git diff --check` 通过。HEAD 仍为绑定基线，未提交、推送或改分支；所有七个候选文件仍位于授权前缀。

本次未运行全树 pytest。真实后端 durability、超时/查询成本和 D2/D4/共享 kernel 接线仍未资格验证；精确接缝见 [PORT.md](../contracts/r5-i6-d3/PORT.md)。下列记录保留此前两轮结果，不作为本次测试计数。

## 上一轮独立审查纠正（保留记录）

本轮只更改 `market_material.py`、`market_persistence.py`、本线测试及两份本线说明；`market_projection.py`、`market_publication.py` 未修改。HEAD 保持绑定基线，无 Git 写操作、网络、Provider 或真实存储操作。

- P1：在跳过非观测行处理前解析每个显式 Material 引用。缺失引用无法形成 assessment、Version 或 Publication；全局材料读回轴不能 COMPLETE。有效无成交材料同样必须 canonical 留存、独立读回并包含在 lineage。
- P2：去除按存储 Version 个数决定完成的门。全局投影审计返回逐日唯一有效候选，汇总以这些候选和精确 manifest evidence 判定；无发布时仍单独核实材料轴。未发布 sibling 和撤回 predecessor 全量保留，双有效重叠继续为歧义。

修复前新增复现子集为 **3 failed**（NO_TRADE_CONFIRMED、MARKET_CLOSED 悬空引用，以及同单元 unpublished sibling）。本轮最终新增 **6** 项确定性用例，D3 合计 **93** 项。额外证明独立无成交材料的 canonical/读回/lineage 门、撤回再发布后的完整历史、修订 evidence 不借用历史证明、两个不同日期范围覆盖原单元、恢复原发布重新产生歧义。

沿用下列 Python 3.14.8 uv 环境和同一四文件 pytest 命令，最终结果 **211 passed**，含所有 D3 测试、定向 D1/kernel 回归与 repository hygiene 测试；四个依赖临时目录的卫生测试本次也执行成功。全树 Ruff、格式检查、含未跟踪文件的只读仓库卫生检查与 `git diff --check` 通过。未重跑全树 deterministic pytest，不声明全树 pytest 结论。下文 205/87 为此前纠正记录。

## 环境与可复现命令

当前 worktree 未提供 `.venv`。初次使用 `/srv/market-data-foundation/.venv`（Python 3.14.8）可以跑专属测试，但全树收集缺少 fastapi/httpx，出现六个收集错误；没有安装依赖。随后只读使用本机已有的 uv 环境：

`/srv/market-data-foundation-control/worktrees/r5-i5-cpi-scheduling-20261020/.venv/bin/python`

核对版本：Python 3.14.8、pytest 8.4.2、fastapi 0.119.1、httpx 0.28.1、pydantic 2.12.3、psycopg 3.3.6。没有修改该环境或系统 Python。

终稿专属与定向回归：

```bash
env PYTHONDONTWRITEBYTECODE=1 UV_OFFLINE=1 GOPROXY=off \
  /srv/market-data-foundation-control/worktrees/r5-i5-cpi-scheduling-20261020/.venv/bin/python \
  -m pytest -q -p no:cacheprovider \
  tests/test_r5_i6_d3_material_projection.py \
  tests/test_r5_i6_market_contracts.py \
  tests/test_canonical_kernel.py \
  tests/test_repository_hygiene.py
```

本次同范围纠正后的结果：**205 passed**。其中本线 **87** 项专属测试（原 59 项保留，新增 28 项）。执行时设置 `UV_OFFLINE=1`、`GOPROXY=off`；仍使用上述现成 Python 3.14.8 环境，未安装依赖。

独立 review 的两个 blocker 均先以回归复现：加入 receipt 的显式 requested_at 字段、尚未加入提交/汇总校验时，新增回归子集为 **19 failed、4 passed**。随后补齐 Version 提交和 PUBLISH/RESTORE 的取得时间门，以及全局逐格点重叠候选、canonical 全历史、投影和 guard 校验。新增反例覆盖缺失请求/取得时间、未来年份、取得晚于冻结、请求逆序；边界相等及 received_at UNKNOWN 的独立时钟正例；严格子区间已发布歧义；候选 tuple 删除、候选/目录/复合行一并删除、反向索引、元数据、历史、guard、与 canonical 不符的自洽历史截断、admission guard、schema、READY 和覆盖故障。汇总失败无修复副作用，新冻结 manifest 不可 COMPLETE，旧 manifest 保留原事实。

```bash
/srv/market-data-foundation/.venv/bin/ruff check --no-cache .
/srv/market-data-foundation/.venv/bin/ruff format --check --no-cache .
env PYTHONDONTWRITEBYTECODE=1 /srv/market-data-foundation/.venv/bin/python \
  scripts/check_repository_hygiene.py --include-untracked
git diff --check
git status --porcelain=v1 --untracked-files=all
git rev-parse HEAD
```

Ruff、格式、包含未跟踪文件的卫生检查通过；Git 检查用于确认新增文件均在独占前缀，HEAD 仍是绑定基线。新文件尚未提交，`git diff` 自身不能替代对未跟踪文件的检查。

此前额外启动过全树 deterministic pytest（默认排除 live_provider/postgres_integration，UV_OFFLINE=1、GOPROXY=off）。该次运行在推进约四分之一后因耗时中断，退出 130，**没有完整全树结论**。它启动于后续 guard/fixture/元数据收紧之前，不能用于当前候选验收；本次纠正以上述 205 项定向回归核对。中央集成仍须跑完整全树回归。

## 专属测试实际覆盖

- raw digest/length/独立读回、row digest、页身份、request lineage、instrument namespace 和 Source/Binding/Mapping/period 精确绑定。
- UNKNOWN known_at/effect、请求/取得时序缺失或不可能、冲突/UNKNOWN 单位、retained 输入拒用 synthetic decoder；raw 保留，不能升格为合格指标。
- 零成交、OHLC/非有限数/负值/float/缺值拒绝、重复 JSON 字段、重复源日期、未被 coverage 解释的源行、伪造 assessment 重放失败。
- 普通 Version 和 Publication 在 canonical 后/投影后/commit 前故障整体回滚；commit 后 ACK 丢失按原 identity 核实；UNKNOWN 核实与幂等重放零写入。
- 并发同身份重放不加计数；并发重叠 Version 全量保留；Publication occurrence 不可换 identity。
- 未发布 Version 仍占自然日格点；空闭市格点不丢；无覆盖月份不是零候选。
- 第 65 个候选、65 条 Publication 历史全部保留，读取超限；exact 走复合行点查，不枚举 sibling 候选。
- 完整历史与 predecessor/时间、withdraw/restore、当前 admission、材料失效；两个有效版本 exact 分别读回，as-of AMBIGUOUS；SUPERSEDE 显式禁用。
- 候选缺失/重复、目录计数、反向范围、长度声明、历史缺失、guard 错误、旧 generation/错误 schema/binding、不可用均拒绝读取，不触发修复。
- 累计格点/候选引用/Version/exact/历史事件/metadata/content/lineage 容量门。
- 新 generation 重建故障不半切换；备份恢复保留原时间、receipt 和身份，恢复前独立字节核对；缺事实/receipt/错误 binding/guard/重复身份/非空 ledger/复用 generation 不可 READY。
- 使用冻结 D1 真实 planner/classifier 的双月合成全局 manifest：部分 Publication、严格子区间歧义或投影/全历史/guard 校验失败不提升全局；独立 assessment Material 读回/幂等/恢复；后续成功不覆写旧 partial manifest。

## 未被这些结果证明的内容

实现是单进程内存事务/字节存储替身和内存备份值合同，未证明跨进程、断电、真实 PG 事务、索引查询计划/I/O、Garage retention 或正式灾备。没有 TDX native 解码器、真实资格/admission adapter、旧 canonical kernel 注册、D2/D4 真实接线、Consumer HTTP/cursor 或 UI。Python 私有字段不是数据库权限边界；故障测试使用私有状态注入，普通 Reader 不承诺检测 canonical 与投影被同时恶意篡改。

全局离线审计为了对照 canonical 完整性会扫描内存事实；该路径不是 D4 的选择性读取 port，不证明全 Job 审计成本或真实后端 deadline。新增 requested_at 必须由未来 D2 adapter 透传精确请求事实；旧 receipt 缺值仍可保留但不能通过 Version/发布门。具体端口与逐项接线缺口见 [PORT.md](../contracts/r5-i6-d3/PORT.md)。
