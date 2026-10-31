# 美国就业连续核心 R1

本轮交付连续来源、数值语义、共享持久化适配与独立 CLI。公共 Web Host、
就业资产查询/采集页面、执行记录适配、Employment Situation 日历与周期策略、
worker 尚未接通；没有新增可用按钮，也没有启用策略。
旧 T03 固定快照及 `source_qualified` 声明保持原状，不作为本实现的运行授权。

## 来源与语义

root 于 2026-10-31T06:49:27.403127Z 完成 BLS v2 三序列 2025..2026
一次真实取样。材料在本次 transaction 的 `SOURCE_RESPONSE.json`、
`SOURCE_REVIEW.json`，没有复制进 Git。响应 SHA-256：
`d476ae40d0afbd1e2d25e1b7d4b259c8c00ae08425f7ad8d7a316818d0895793`。
各序列 21 行，最新参考月 2026-09；catalog 明确区分 CES、CPS 与季调状态。
实现端仅离线解析/重放该材料，核实 63 个 Decimal 观测，未增加真实请求。
逐点重放结果在 transaction 的 `SOURCE_PARSE_VERIFICATION.json`。

| series_id | 指标及单位 | 本地计算 |
| --- | --- | --- |
| CES0000000001 | CES 总非农，千岗位，SA | 月净变化为绝对差值，`net_change_1m_thousand_jobs` |
| LNS14000000 | CPS 16岁以上总体失业率，百分数，SA | 月变化为百分点，`change_1m_percentage_points` |
| CES0500000003 | CES 私营部门全体雇员平均时薪，USD/hour，SA | `mom_pct`/`yoy_pct` 为百分数；`change_1m_ratio`/`change_12m_ratio` 为比例 |

岗位不是人数；本轮没有新增就业人数序列。失业率不生成通胀式环比/同比。
官方 `calculations.net_changes`、`calculations.pct_changes` 和 `footnotes`
单独保留，包含负变化、preliminary 脚注和空脚注对象，不用官方变化替代本地推导。
官方计算可能使用不同精度。原值为 Decimal，序列缺值和比较基期缺值不补零。

## 连续范围与保存

复用 BLS 20年/10年有界批量、共享请求控制、退避与凭据屏蔽，以及
逐序列缺口补取、同截止点前12月缓冲、changed-only 修订和提交后可用时间。
当前查询优先本地，缺口上游补取不保存；带 `as_of` 的查询只读本地。
`revision=1` 表示首次本地观察，不表示官方首发。原始材料使用就业独立前缀。

固定 DEV schema 为 `mdf_employment_timeseries_dev_r1`。`batches`、`revisions`
与内部 `latest` 复用共享结构，内部 `index_value` 仅为数值存储列名。
对外 `employment_latest` 提供 `value`、`value_unit`、`survey`、`local_revision`、
官方百分数及 `official_net_changes`/单位；`employment_timeseries_rates` 提供
`total_nonfarm_thousand_jobs`、`unemployment_rate_percent`、
`average_hourly_earnings_usd_per_hour` 和各自适用的差值/时薪变化列。
本轮没有连接或执行实际 PostgreSQL DDL；SQL 投影尚需 root 在 DEV 验证。

## root 可显式执行的核心命令

使用已合格的 uv/Python 3.14.8 环境。`MDF_RUNTIME`、`MDF_SECRET` 由 root 指定
已有配置路径；程序不会改写配置或 secret。`prepare-schema` 才执行就业 schema DDL。

```sh
uv run --no-sync python scripts/employment_timeseries.py --runtime "$MDF_RUNTIME" --secret "$MDF_SECRET" prepare-schema
uv run --no-sync python scripts/employment_timeseries.py --runtime "$MDF_RUNTIME" --secret "$MDF_SECRET" --live-provider init --end 2026-09 --months 240
uv run --no-sync python scripts/employment_timeseries.py --runtime "$MDF_RUNTIME" --secret "$MDF_SECRET" query --start 2025-01 --end 2026-09 --local-only
uv run --no-sync python scripts/employment_timeseries.py --runtime "$MDF_RUNTIME" --secret "$MDF_SECRET" --live-provider update --start 2026-07 --end 2026-09
```

`--series` 可重复指定三个 series_id；没有 headline/core 或四口径参数。
初始化可改 `--months` 或显式 `--start`/`--end`；省略结束月时请求当前月份，
实际公布覆盖以返回结果为准。更新必须明确起止范围。
CLI 上游凭据读取要求显式 `--live-provider`，采用具名 BLS 就业 profile，
复用现有 BLS 密钥读取和请求控制，不借用 CPI/PPI 的 capability 开关。
公共 package 尚未加入 employment capability，不能用三族 Host 代替就业 Host。
没有可宣称已接通的新 Host 或 worker 启动命令。

## 查询表格排序

CPI/PPI/PCE 的共享范围结果、旧普通查询和通用本地读取表格按月份倒序。
同月保留既有系列/指标顺序。仅排序表格副本，不改变请求、响应数组、原始 HTTP
正文或执行列表。旧 PCE 对象式结果只含一个参考月，既有指标次序保持不变。
