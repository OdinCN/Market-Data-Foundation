# 美国消费与收入：独立产品链与 Root DEV 交接

本资产为 `mdf.macro.bea.consumption-income@r1`，能力键 `consumption`，API 前缀
`/api/r5/consumption/timeseries`。PCE价格指数仍为原独立资产。
2026-11-01 已完成有界 DEV 迁移、240个月初始化、回读及真实 worker 联调。
目标范围有9条来源缺口，不能称完整二十年覆盖；未来自然发布及真实浏览器像素尚未验收。
实际效果与证据见本文末尾的 DEV 验证记录，以下命令是可复用运行手册，勿无条件重跑。

## 已核实的来源与数值

实现阶段先使用 Root 留存的两个 2026 年 API Results 投影，两次资格核验均 200；
其后由 Root 单独执行下文记录的有界实际采集，未让实现 CLI 接触凭据或发起来源请求。
严格匹配 TableName、LineNumber、SeriesCode、METRIC_NAME、CL_UNIT、UNIT_MULT，
拒绝未知的目标映射、单位变化、缺少 DataValue 和重复点。无关官方表行不扩充指标集合。

| 系列 | 表/行 | 指标 | 原生单位 | 2026-09 留存值 |
| --- | --- | --- | --- | --- |
| DPCERC | T20600/29 | 名义个人消费支出 | 百万美元，当期价，SAAR水平 | 22452780 |
| DPCERX | T20806/1 | 实际个人消费支出 | 百万2017链式美元，SAAR水平 | 16977581 |
| DGDSRX | T20806/2 | 实际商品消费 | 百万2017链式美元，SAAR水平 | 5780732 |
| DSERRX | T20806/13 | 实际服务消费 | 百万2017链式美元，SAAR水平 | 11219342 |
| A067RC | T20600/27 | 名义可支配个人收入 | 百万美元，当期价，SAAR水平 | 24289049 |
| A067RX | T20600/37 | 实际可支配个人收入 | 百万2017链式美元，SAAR水平 | 18366069 |
| A072RC | T20600/35 | 个人储蓄率 | 百分数，季调，不年化 | 3.8 |

金额 `UNIT_MULT=6` 已是百万美元，不据含 billions 的 LineDescription 再缩放。
储蓄率实际元数据为 `METRIC_NAME=Ratio, CL_UNIT=Level, UNIT_MULT=0`；3.8 即3.8%，
零、负值有效，来源空值与缺行各自保留。储蓄率只提供原值和1/12月百分点差，
不提供百分比增速或年化。金额使用原生 Decimal，衍生值用同 cutoff 的同系列端点；
API 累计比率最终保留4位、百分数最终保留2位，百分点差不预舍入。
SQL rates view 使用 PostgreSQL numeric 除法，最终比率4位/百分数2位，与API一致，不预舍入原金额。
SAAR 是水平的年度速率，不能冒充年化增速。实际商品与服务不可加总成实际PCE；不做CPI平减。

描述符明确七项名称、单位、scale、季调、年度速率语义、链基年及 provenance。
`start_month=null`：2026样本不证明原生起点。实际覆盖从 store.status、每次 source_missing/
source_unprovided/readback 返回。每表最多5个日历年，二十年跨21个日历年时可需5批、10个表请求。
两个表分别完成，不构成原子官方快照；数值投影只保留验证后的 Results，丢弃可回显 UserID 的整个 Request。
原完整 API 响应不得写入日志或 Git。

## 复用与隔离

复用 BEA transport/safe Results/shared request control、版本库与 Garage raw store、
cutoff/首次捕获、官方日历、材料/事件合同、manual jobs、worker 和共同工作台。
新增 profile/source/service/scheduler/host 是共享端口的明确参数化，不是另搭存储栈。

- schema：`mdf_consumption_timeseries_dev_r1`；锁：`78328741642028106`。
- raw：`consumption-timeseries/dev-r1/sha256/`，仍在现有 MDF Garage 能力内。
- 数值两表：`batches/revisions`，原生数值列 `value numeric`；视图 `latest/consumption_timeseries_rates`。
  不新增 BLS percent 列，不把金额/储蓄率叫 index_value。内部共享 SourcePoint 的历史字段名只在端口内使用。
- evidence：同 schema 下 `release_events/release_evidence/release_annotations`。
- 独立 calendar state、`consumption-strategy.json`、job目录和execution身份，禁止继承PCE状态文件。
- 显式将 PCE 和 consumption 的 `--bea-request-control-path` 指向同一绝对文件。
  数值来源的 reservation/spacing/429 cooldown 跨进程共享；不混入BLS额度。
- 同 control 文件父目录的 `bea-pio-calendar.json` 复用6小时内验证过的原始ICS，
  `bea-pio-material-acquisition.json` 协调材料任务，公共原始HTML字节缓存带SHA并持有进程锁。
  第二资产的retention.acquisition_method明确为shared_original_material_cache，不冒称新transport delivery。
  数值表返回不跨资产冒充相同系列数据。

BEA PIO canonical事件仍是 `bea:personal-income-and-outlays:YYYY-MM-DD`，同次可覆盖多月。
原材料 SHA 可共享；consumption 用 `official-consumption-income-v1` 解析身份，
PCE 原 `official-five-family-v3` evidence IDs/时间/旧cutoff保持不变。
同字节必须经过消费收入解析并导入本资产证据，不能拿PCE-only evidence ID 当消费收入已解析。

实现阶段的 Sep/Aug 聚焦HTML支持四类官方 MoM；其后 Root 取得的完整官方材料
进一步核实了单参考月储蓄率叙述。两次事件各保留9项公布/修订声明，含储蓄率实际值。
聚焦材料未含某段时仍保持missing，不能借完整材料成绩改写旧证据的覆盖范围。
不从舍入变化反推金额；HTML 未列的金额、实际商品/服务统计量明确 missing。
多参考月储蓄率叙述未明确分月时不猜月份。`hist.xlsx` 未当作 vintage old/new 修订表。
连续计算的 MoM 不替代 HTML 官方率，不凭相等绑定 continuous_version。
source_history/event/first_capture 路径沿共同合同；无稳定具名共识来源，surprise缺依据时为null和原因。

## 查询、任务和界面

当前：显式 `measure=native&local_only=false&query_mode=current&as_of_basis=observed`。
优先本地，缺口临时补齐，不持久化。只请求已结束参考月；past+future范围保留可用本地行，未来月missing。
204空响应表示无返回记录；有可用本地目标行的 transient gapfill失败返回PARTIAL及安全分类/HTTP状态/
retry_at和真实logical请求/attempt计数。权限、凭据、结构错误不掩盖。
历史：显式 `local_only=true&query_mode=history&as_of_basis=available&as_of=...`，
只读本地，同cutoff约束所有比较端点、首次捕获和事件证据，无latest回退。显式observed模式仍可用。

`series` CSV选择七个原生ID，省略为全部；`columns` 投影原值/同比环比/累计比率/储蓄率百分点差，
不适用的储蓄率增长字段不制造数值。`measure` 只接受native。
API help、OpenAPI、生成TS、共享frontend parser和可见表格一致；七指标选择、单位/季调/SAAR说明，
请求预览、cURL/复制、原始HTTP、北京时间cutoff及月倒序沿共同界面，不加顶栏。

新建采集选本资产；初始化建议最近240个月，可编辑，建议不代表已核实覆盖。
manual initialize/update实际执行、durable journal、重启中断状态、幂等request_id、提交后local-only readback、
partial source coverage均接入共同流程。已有五资产不重跑初始化。
右侧摘要跳到精确本资产执行行；执行列表自动刷新。预设官方日历策略只开始/暂停，worker另需显式启动。
实际worker使用本资产service、lease/heartbeat、due plan、历史复核/重试和BEA共享额度。
材料失败独立记录，不覆盖已提交数值成功。

## Root 才执行的 DEV 步骤

以下为 Root 运行手册；实现 CLI 不执行这些副作用。每次实际执行前核对真实DEV配置、现有五族状态、PG角色、Garage能力和备份。
所有变量为路径/身份，不在命令行放凭据。示例目录应换成Root批准的持久runtime/artifacts根；
`CFG/runtime.toml` 与 `CFG/secret.toml` 沿已有格式，PG必须为已资格化DEV能力，勿改系统Python。

```sh
export CFG=/absolute/approved/config
export PKG=/absolute/approved/package-store
export ENV_ID=approved-dev-environment
export STATE=/absolute/approved/artifacts/macro-runtime
export R=/srv/market-data-foundation-control/transactions/MDF-MACRO-RESEARCH-20261101-R1
```

1. 在已有宏观包配置启用 `consumption`，保留原五个能力；BEA凭据绑定给本资产，
   不借其他Provider。包保存仍走既有期望revision/权限控制。Root保存原配置快照。
2. 检查生成DDL与新namespace无同名旧表；由实际写入对象owner显式准备。
   若用psql，应 `-v ON_ERROR_STOP=1 --single-transaction -f scripts/sql/consumption-income-r1.sql`，
   使用既有安全连接身份，不把密码放URL。也可用CLI（无来源请求）：

```sh
uv run --locked --no-sync --offline python scripts/consumption_timeseries.py prepare-schema \
  --runtime "$CFG/runtime.toml" --secret "$CFG/secret.toml" \
  --package-store-directory "$PKG" --environment-id "$ENV_ID" \
  --bea-request-control-path "$STATE/bea/request-control.json"
```

DDL只新建本namespace两数值表/视图和三证据表；不迁移/清空其他资产。
`odin_dba` schema USAGE及全表/视图SELECT；撤本schema内现有DML和PUBLIC表权限，
为**执行DDL的对象owner**加未来表/视图SELECT默认授权。未来若换owner须对该owner再次设置默认权限。
审计同时检查global默认ACL及角色继承；若已有global默认DML授权，schema级REVOKE不能抵消，
Root须先处置该实际授权，不能凭生成SQL宣称未来对象已只读。
Root以 `scripts/sql/consumption-income-audit.sql` 回读表型、单位、owner、现有与未来授权。

3. 有界新资产初始化：先2026年已资格区间（2表请求，可能有retry），只本资产，勿重跑五族。

```sh
uv run --locked --no-sync --offline python scripts/consumption_timeseries.py init \
  --runtime "$CFG/runtime.toml" --secret "$CFG/secret.toml" \
  --package-store-directory "$PKG" --environment-id "$ENV_ID" \
  --bea-request-control-path "$STATE/bea/request-control.json" --start 2026-01 --end 2026-09
uv run --locked --no-sync --offline python scripts/consumption_timeseries.py query \
  --runtime "$CFG/runtime.toml" --secret "$CFG/secret.toml" \
  --start 2026-09 --end 2026-09 --local-only
```

uv的offline仅禁止依赖下载，不禁止命令中的显式来源请求！Root选择执行init才会采集。
之后用Web manual job做一月update/真实journal/readback，检查2 logical请求与实际attempts，
有界240月初始化由Root选择，先预估五年窗口/共享额度；本轮实际执行事实单独记录如下。

4. 导入已留存官方材料，无新增来源请求。`FILE`应是Root现有credential-free HTML/投影，
   选对material-format，不把聚焦投影叫原始HTTP。不可选BEA完整API response。

```sh
uv run --locked --no-sync --offline python scripts/consumption_timeseries.py import-material \
  --runtime "$CFG/runtime.toml" --secret "$CFG/secret.toml" \
  --file /absolute/retained/bea-pio-202609.html \
  --source-url https://www.bea.gov/news/2026/personal-income-and-outlays-september-2026 \
  --material-format official_selected_projection
uv run --locked --no-sync --offline python scripts/consumption_timeseries.py releases \
  --runtime "$CFG/runtime.toml" --secret "$CFG/secret.toml" \
  --read-mode source_history --source-version 2026-10-29 --reference-month 2026-09
```

另导入Aug文件和对应URL。回读PCE/消费收入相同event_id/rawsha、不相同evidence_id，
并用导入前/后的as_of验证晚证据不倒填。正常API query不隐式prepare。

5. 在Root已有web进程命令追加下列参数，保留五资产原参数和路径，**Root负责重启**：

```sh
--enable-consumption-timeseries --allow-consumption-local-read --allow-consumption-collect \
--consumption-schedule-path "$STATE/consumption/calendar.json" \
--consumption-job-directory "$STATE/consumption/jobs" --consumption-history-start 2006-11 \
--bea-request-control-path "$STATE/bea/request-control.json"
```

实际host脚本为 `uv run --locked --no-sync --offline python scripts/mdf_cpi_timeseries_web.py`，
已有必需参数：`--opt-in-timeseries-web --config-directory "$CFG" --package-store-directory "$PKG"`
`--environment-id "$ENV_ID" --pg-role ... --pce-schema-owner ... --pce-schema-oid ...`
`--schedule-path ... --job-directory ...`。这些pce命名的schema身份是既有host设置字段，
必须使用Root已核实的原值；不代表消费收入继承PCE表或状态。
消费收入实际read/write factory绑定新profile。固定DEV listener沿既有host，不新开端口/服务权限。

6. 策略在Web点击开始或PUT `{ "enabled": true }`；它只改本资产策略，未启动worker。
   Root以现有进程管理器启动以下实际worker，不在Web请求内模拟tick：

```sh
uv run --locked --no-sync --offline python scripts/mdf_cpi_timeseries_worker.py \
  --asset consumption --opt-in-consumption-worker --allow-collect \
  --config-directory "$CFG" --package-store-directory "$PKG" --environment-id "$ENV_ID" \
  --pg-role APPROVED_ROLE --pce-schema-owner EXISTING_VERIFIED_OWNER --pce-schema-oid EXISTING_VERIFIED_OID \
  --schedule-path "$STATE/cpi/calendar.json" --job-directory "$STATE/cpi/jobs" \
  --consumption-schedule-path "$STATE/consumption/calendar.json" \
  --consumption-job-directory "$STATE/consumption/jobs" --consumption-history-start 2006-11 \
  --bea-request-control-path "$STATE/bea/request-control.json" --poll-seconds 5
```

两个consumption路径必须独立绝对路径、互不嵌套，不得指向PCE或CPI；进程umask077。
无opt-in拒绝启动，无allow-collect无采集权限，暂停策略仍可观察worker心跳。
Root回读策略、schedule、executions，确认heartbeat/lease和本资产历史cursor。
自然发布应另验，不用手工tick成绩冒称未来事件成功。

7. 手动HTTP（同既有host origin/admitted LAN client，Root确认后操作）：

```sh
curl -fsS 'http://192.168.10.20:18761/api/r5/consumption/timeseries/query?start=2026-09&end=2027-02&series=DPCERC,A072RC&measure=native&local_only=false&query_mode=current&as_of_basis=observed'
curl -fsS 'http://192.168.10.20:18761/api/r5/consumption/timeseries/query?start=2026-09&end=2026-09&measure=native&local_only=true&query_mode=history&as_of_basis=available&as_of=2026-11-01T20%3A00%3A00%2B08%3A00'
curl -fsS -H 'Origin: http://192.168.10.20:18761' -H 'Content-Type: application/json' \
  -d '{"request_id":"ROOT_GENERATED_UUID","action":"update","start":"2026-09","end":"2026-09"}' \
  http://192.168.10.20:18761/api/r5/consumption/timeseries/jobs
```

历史cutoff应替换成实际提交后时间；初始历史早于本次捕获应为空，不能用latest补。
在浏览器验证七项切换/显示列/原始正文/复制、240月可编辑、执行摘要精确跳转、自动刷新、策略开始暂停。
新任务UUID每次生成；同UUID相同参数重放不再调用来源，改变参数应409。

8. Root正常环境检查（PG集成仅只读，须先完成上面准备/初始化/材料导入）：

```sh
MDF_CONSUMPTION_PG_READBACK=1 MDF_CONSUMPTION_RUNTIME="$CFG/runtime.toml" \
MDF_CONSUMPTION_SECRET="$CFG/secret.toml" uv run --locked --no-sync --offline pytest \
  -m postgres_integration tests/test_consumption_postgres_integration.py
uv run --locked --no-sync --offline pytest -q tests/test_consumption_http.py
uv run --locked --no-sync --offline pytest -q
```

## 回退与扩展点

首选暂停消费收入策略、停止该worker、撤新collect权限/enable flag，回退应用并保留新schema/raw/journal供审计。
不drop已有五族，不重写PCE证据，不删除失败日志。物理撤新namespace必须Root额外确认已备份并核对无下游依赖；
本实现不提供自动DROP。只读授权回退限新schema：先撤odin_dba的默认SELECT，再撤本schema表SELECT/USAGE，
不触碰其他schema授权。Root以audit确认实际owner及所有未来对象owner的default privileges。

新增扩展点：consumption_metadata/contract/source/service/schedule/CLI/host；profile allowlist、
shared store原生列/约束、query fields/serializer/OpenAPI、capability/source inventory、root host factories、
web/worker flags、共享BEA material cache，以及既有frontend profile/资产/采集/策略/执行路由。
本包未引入依赖，无uv.lock变更；不改Debian系统Python，不接QMT/ClickHouse/Redis。


## 2026-11-01 实际 DEV 验证记录

本节仅描述本轮有界开发环境效果，不把离线 fixture、HTTP、DOM 或运行心跳混为自然发布/浏览器验收。
原五族没有重跑初始化；迁移与新包采集前后，五族全部现有表内容散列一致。

| 验证 | 实际结果 |
| --- | --- |
| 迁移 | 新 schema 两数值表、两视图、三证据表；事务回滚演练准确；`odin_dba` 现有/未来对象只读授权已回读 |
| 初始化 | `85d3a422-825d-4ec4-8a46-e3be60edf4cc`；2006-10～2026-09并请求12月比较缓冲；10 logical requests/10 attempts；1,719条新观测，0修订 |
| 目标覆盖 | 1,671 / 1,680；三条实际消费序列各缺2006-10、11、12，明确来源未提供；额外比较缓冲缺36条 |
| 原始响应核对 | 留存的对应批次中，上述三系列没有2005/2006行；仅说明这次响应的覆盖，未推定原生最早年份 |
| 单月更新 | `078698c0-613f-4e36-a170-12bd8f776877`；2026-09，2数值请求/2 attempts，0新增/修订；回读成功；相同ID重放不再请求 |
| SQL/API | 10,737项原生值和衍生指标逐项一致；七项2026-09值匹配资格样本 |
| 材料 | 一次真实 BEA 9月发布HTML获取；8月原始HTML从已有留存离线导入；两个事件各9项声明；重复导入幂等 |
| 时点与证据 | 材料取得时间不倒填；导入前cutoff不见晚证据；首次捕获不冒充本次官方公布；无共识时surprise为null |
| BEA复用 | 同一原始材料可分资产解析；共享缓存不再HTTP；两资产不同解析/evidence身份；PCE证据不被改写 |
| 运行 | Web及六族真实worker恢复；新策略启动/暂停/恢复、进程重启后新心跳、两项任务恢复均通过；旧五族启用状态保留 |

安全证据保存在控制目录事务 `MDF-MACRO-RESEARCH-20261101-R1/artifacts/consumption/`，
包括迁移演练/提交、来源预算/实际任务、readback/evidence核对、runtime验收及HTTP/DOM记录。
该目录不提交凭据，完整BEA API Request回显从不落日志/Git。

剩余边界：暂无稳定具名共识、完整官方历史首发/修订覆盖或精确实际发布时刻；
历史材料使用自身日期粒度。BLS来源阻断不通过重试消耗额度。浏览器像素和未来自然发布留待对应验收。
