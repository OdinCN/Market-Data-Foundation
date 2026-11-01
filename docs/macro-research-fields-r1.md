# 五族研究字段扩展 R1

本轮在现有连续查询、两表版本存储、手动任务、预置策略、worker、执行回读与资产工作台中扩展字段。
不新增顶级导航或替代 API。五族发布材料/事件、证据和版本关联已由[发布记录合同](macro-release-records-r1.md)扩展。

| 族 | 本轮增量 | 原有默认与单位 |
| --- | --- | --- |
| CPI | shelter=`SAH1`、food=`SAF1`、energy=`SA0E`、core_goods=`SACL1E`、services_less_energy=`SASLE`，每项各 `CUSR0000` SA / `CUUR0000` NSA；共14条 | API省略篮子仍headline/sa；UI仍总体/核心×SA/NSA。短期SA与年度NSA分别列出原生序列，禁止拼接；所有累计ratio语义保留 |
| PPI | core_ex_trade=`WPSFD49116/WPUFD49116`；共6条 | core仍仅剔除食品、能源，含贸易服务。`provider_status`从原始脚注保留P/R声明；未注明为null，不据MDF revision或经过月份判最终值 |
| PCE价格 | `annualized_3m_ratio=(I[t]/I[t-3])^4−1`、`annualized_6m_ratio=(I[t]/I[t-6])^2−1` | 同截止未舍入T20804指数；仅最终HALF_UP舍入ratio四位。既有3/6月累计变化保留，UI默认仍原五列，新年化列可选。不是消费额/收入，亦非SAAR水平 |
| 就业 | 私营CES0500000001、就业人口比LNS12300000、CES10/20/30/40/50/55/60/65/70/80/90开头的十一主要行业；共18条 | 查询省略series及UI初选仍原五项；采集/策略覆盖18条。CES雇员水平与月净增为千岗位；EPOP为percent与百分点差。不把总量、私营聚合项和行业重复相加，不包含行业子项 |
| 零售 | ex_auto=`SM44X72−SM441`及1/3/6/12月变化；沿用同一六输入 | API省略basket仍总体/控制组，UI亦同。control公式不变，保留445；新增派生不额外请求Census。金额百万当期美元，ratio不年化，官方MPCSM是percent |

Root留存2026年BLS合格响应验证了全部25个新增候选，每条9个2026月值和catalog。
这是真实来源证据，非合成fixture。原始材料只在事务artifacts目录；代码只记录证据覆盖2026-01至09。
新增系列原生`start_month`仍未知，`coverage_basis`不是系列起点；CPI所选NSA行基期由Table1 header/行脚注支持1982-84=100，SA与未逐项证明的PPI基期仍保持未知。未声称20年都有值。
BEA消费/收入留存材料属于独立后续资产，本轮未将它们混入PCE价格profile。

## 查询合同

既有 `/api/r5/{family}/timeseries/query` 不变；历史按同一inclusive截止选择所有端点和零售操作数，GET不落库。
CPI省略`local_only`仍true，其余族仍false；UI current显式false、history显式true。北京时间输入序列化保留+08:00。

- CPI/PPI/PCE `basket`支持单项、逗号多选或all；原headline/core/overall别名保留。就业仍以series选择，拒绝basket/adjustment。
- `columns`为可选逗号字段。省略保留完整响应；指定后投影数值列并返回`selected_columns`，原始元数据/计算依据及通胀measure的value别名保留。就业不适用的统计量仍缺席。`calculation_missing`只列选中字段。
- 通胀字段：index_value、mom_pct、yoy_pct、change_1m_ratio、change_3m_ratio、change_6m_ratio、change_12m_ratio；PCE再加annualized_3m_ratio、annualized_6m_ratio。
- 就业字段：value、net_change_1m_thousand_jobs、change_1m_percentage_points、change_1m_hours、mom_pct、yoy_pct、change_1m_ratio、change_12m_ratio；最后四项仅时薪。value单位按series，不以百分比格式化岗位数。
- 零售basket支持headline/control/ex_auto/all及逗号多选；core是control别名。columns用index_value选择`*_level`，change_km_ratio选择`*_rates[k]`，official_mpcsm_percent选择official_headline_mpcsm。保留source_cells/input_versions作为审计依据。默认未选择ex_auto，不增加旧消费者的结果篮子。
- 零售`derivations.control/ex_auto`含identity、formula、unit、provider_official=false、same_cutoff及当前/比较月份的精确input_versions。已有source_cells及输入版本不删除，不用最终结果版本冒充操作数版本。

示例（不是本轮执行记录）：

```text
/api/r5/cpi/timeseries/query?start=2026-01&end=2026-09&basket=shelter,core_goods&adjustment=sa&columns=index_value,change_3m_ratio&local_only=true
/api/r5/ppi/timeseries/query?start=2026-09&end=2026-09&basket=core_ex_trade&adjustment=all&local_only=true
/api/r5/pce/timeseries/query?start=2026-09&end=2026-09&basket=all&columns=index_value,change_3m_ratio,annualized_3m_ratio,annualized_6m_ratio&local_only=true
/api/r5/employment/timeseries/query?start=2026-09&end=2026-09&series=CES0500000001,LNS12300000,CES4000000001&local_only=true
/api/r5/retail/timeseries/query?start=2026-09&end=2026-09&basket=ex_auto,control&columns=index_value,change_1m_ratio&query_mode=history&local_only=true&as_of_basis=available&as_of=2026-11-01T18%3A00%3A00%2B08%3A00
```

OpenAPI由真实router生成：既有retail.openapi.json以及macro-timeseries.openapi.json，生成的TS进入请求构造类型检查。
`python scripts/export_r5_openapi.py`及`node web/scripts/check-contract.mjs --write`只生成合同，不启动监听或数据库。
UI原文继续展示实际HTTP字节文本；列切换可复用已取得字段，新增未取得的投影列需要重新查询。

## Root迁移与权限

本CLI不执行DDL、Provider请求、数据库/S3/Redis连接、运行时或Git写入。
显式文件 `scripts/sql/macro-research-fields-r1.sql` 由下列打印命令生成，Root在owner事务中审查执行：

```sh
uv run --locked --no-sync --offline python -m market_data_foundation.historical.macro_research_migration > /tmp/macro-research-fields-r1.sql
```

整合迁移默认明确绑定 `mdf_retail_timeseries_dev_r1`，不使用零售存储生成器的候选 schema 默认值；函数可用具名 `retail_schema` 参数指定经核验的实际目标。

先保存约束、视图定义/列序、owner、grants及batches/revisions计数和指纹；对照Root已有prechange artifacts。
CPI/PPI只扩展具名series CHECK；就业扩展两个CHECK、替换employment_latest及rates视图；PCE rates尾部追加年化列；零售rates尾部追加ex_auto/变化和派生身份/公式。
不DROP TABLE/VIEW、不改旧revision、不重建latest、不调用全族init。CREATE OR REPLACE保留owner/grants；旧视图列序及类型保留，新列在末尾。
就业rates原失业率/参与率/工时/时薪列保留；net_change列对新CES岗位行有效，change_1m_percentage_points对新EPOP行有效，参与率仍有独立旧列。
历史名称`employment-five-series-r1.sql`与`five_series_migration_sql()`现在也采用全部18条准入，避免运行旧入口把准入降回五条；新安装使用整合迁移。

Root先BEGIN、执行、核对全部既有兼容视图及依赖、回读旧数据和列序，再ROLLBACK演练；核实后新事务COMMIT。
未知CHECK命名会失败，不能用忽略错误或删除数据处理。迁移重复执行不产生数值版本。
回退应用代码不要求删列/删系列或降低CHECK；保持扩展schema和已捕获数据，保留旧视图列可支持旧客户端。

Root核验每个实际schema及所有兼容视图：odin_dba有USAGE、已有表/视图SELECT，无写权限；会话default_transaction_read_only有效。
若缺授权，由实际对象owner分别执行（按当前DEV身份替换owner，不在应用GET中执行）：

```sql
GRANT USAGE ON SCHEMA mdf_cpi_timeseries_dev_r1 TO odin_dba;
GRANT SELECT ON ALL TABLES IN SCHEMA mdf_cpi_timeseries_dev_r1 TO odin_dba;
-- FOR ROLE必须是今后创建对象的实际owner；每个family schema分别设置。
ALTER DEFAULT PRIVILEGES FOR ROLE actual_object_owner
 IN SCHEMA mdf_cpi_timeseries_dev_r1 GRANT SELECT ON TABLES TO odin_dba;
```

同样核对ppi、pce、employment、retail及既有兼容schema；默认授权不追溯已有对象，现有授权不保证未来对象。
本迁移不自行新增授权、修改角色或所有权。

## 仅新增系列补史与运行装配

共享手动jobs现在对BLS三族均可选series；省略仍采集该profile全部输入。
UI继续默认采集全族，保留原可编辑月份/确认/简报/执行流程；子集补史使用API/CLI的明确series参数，不增加迁移专用选项。
提交init会只补缺口，保留同内容不增版本；initialize保留现有受控刷新语义。Root补史用init，不重跑全族初始化。
init/update均按实际尝试的series-month记录来源覆盖；`source_missing`中的未提供月份与显式null分开回读。
来源返回的每一点（含null和缓冲月份）必须真实持久化并可消费，丢写仍失败；已有数据完整且不需请求的init可成功。
来源未提供的早期月份不等于原生历史起点。旧失败执行记录不改写，也不因本次分类修复重跑20年请求。
现有worker仍按asset独立opt-in和官方日历运行；策略不增加复杂配置，读取profile全量系列。
升级后首次普通刷新将覆盖新增系列，深历史由Root显式子集补齐。

Root在现有获准DEV来源绑定下，使用既有CLI（先核对凭据/包授权；命令示例不执行）：

```sh
uv run --locked --no-sync --offline python -m market_data_foundation.historical.cpi_timeseries_cli init --start 2006-10 --end 2026-09 --series CUSR0000SAH1,CUUR0000SAH1,CUSR0000SAF1,CUUR0000SAF1,CUSR0000SA0E,CUUR0000SA0E,CUSR0000SACL1E,CUUR0000SACL1E,CUSR0000SASLE,CUUR0000SASLE
uv run --locked --no-sync --offline python -m market_data_foundation.historical.ppi_timeseries_cli init --start 2006-10 --end 2026-09 --series WPSFD49116,WPUFD49116
uv run --locked --no-sync --offline python scripts/employment_timeseries.py --live-provider init --start 2006-10 --end 2026-09 --series CES0500000001 --series LNS12300000 --series CES1000000001 --series CES2000000001 --series CES3000000001 --series CES4000000001 --series CES5000000001 --series CES5500000001 --series CES6000000001 --series CES6500000001 --series CES7000000001 --series CES8000000001 --series CES9000000001
```

CLI package目录/environment/runtime参数按Root实际DEV绑定填写，不使用猜测身份。
BLS共享20/10日历年有界请求、12月缓冲；新增系列数量均在单个BLS请求上限以内，不按月拆请求。
短历史/缺月/官方null保持真实缺口。PCE/零售纯派生无需新来源或重采已有原始历史。
Root再核对正常任务→持久化→available回读、同内容重复、worker选择/心跳/日历/执行范围和浏览器窄屏。
只有Root实际运行结果可以声明DEV数据库、来源、运行窗口及自然发布验证成立。
