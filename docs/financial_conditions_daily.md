# 金融条件日频数据层 r1

本模块只提供独立数据链，未接 Web、常驻 worker 或二十年初始化作业。
稳定数据集为 `mdf.macro.financial-conditions.daily@r1`，来源绑定为
`mdf.binding.fred.financial-conditions.daily@r1`。run/attempt 不构成数据集身份。

## 当前来源准入

2026-11-05 核查的 [FRED 条款](https://fred.stlouisfed.org/legal/) 中，API
Prohibitions (k)/(l) 对所述开发用途和存储、缓存、归档及数据库纳入设有限制。
当前既有绑定没有证明本持久化 DEV 用途已取得相应许可。十条序列统一保持
`fred_retention_permission_unconfirmed`，在凭据解析、HTTP 和采集写入之前拒绝。
API key、Owner 的开发授权与数据提供方的使用许可是不同事实；没有客户端绕过开关。
后续取得适用许可后，应更新本包固定来源审阅，逐条完成原生 metadata/小窗资格核验，
再启用既有数据接口。不得静默转到原始机构或其他分发者。

| 原生序列 | 原始机构/含义 | 日历与单位 |
|---|---|---|
| DFEDTARL / DFEDTARU | Federal Reserve Board，目标区间下/上限 | 每日七天；Percent，NSA；表示该日有效界限 |
| EFFR | NY Fed，无担保隔夜有效利率 | Daily；Percent，NSA |
| SOFR | NY Fed，有担保隔夜融资利率 | Daily；Percent，NSA |
| DGS2 / DGS10 | Federal Reserve Board，固定期限名义收益率 | Daily；Percent，NSA；不是逐券成交价 |
| DFII10 | Federal Reserve Board，10 年通胀挂钩实际收益率 | Daily；Percent，NSA；允许负值 |
| T10YIE | St. Louis Fed，10 年盈亏平衡通胀率 | Daily；Percent，NSA；不称纯通胀调查或独立风险因子 |
| BAMLC0A0CM / BAMLH0A0HYM2 | ICE，投资级/高收益 OAS | Daily Close；Percent，NSA；bp=Percent×100 |

ICE [投资级](https://fred.stlouisfed.org/series/BAMLC0A0CM) 与
[高收益](https://fred.stlouisfed.org/series/BAMLH0A0HYM2) 的公开说明还涉及复制/分发许可，
并注明自 2026 年 4 月起 FRED 仅包括三年观察。精确 API 起止尚未实测，不能承诺二十年。
ICE 月末可有周末观察，不能统一删掉周末。工作日假期日历未独立核实时，缺行保持
`not_returned_calendar_unconfirmed`，不凭缺行推定休市。

## 复用接口

- `financial_daily.py`：十项注册、日期、稳定定义/绑定和来源使用准入。
- `financial_daily_source.py`：复用已有精确 FRED HTTPS 与持久限流，原生 metadata/观测解析。
- `financial_daily_store.py`：显式迁移、PG 日日期修订、Garage 内容地址与原批次恢复。
- `financial_daily_service.py`：逐系列部分结算、纯本地查询、统一截止投影。
- `financial_daily_runtime.py`：既有 DEV PG/TLS、Garage、宏观包 FRED 凭据解析接口；不写配置。

调用 `FinancialDailyService.collect(start,end,selected=...)`，每条单独保存并结算；失败不抹去
其他已确认序列。只恢复失败的 series 可重新指定 `selected`；不把多次 FRED 请求称为原子快照。
存储提交未知但 batch 已留存时，调用 `recover(batch_id)` 从原 raw/metadata 回读确认，
不再次访问 FRED、不改原观察时间。原始回包先验证凭据安全，再交 raw sink；语义解析不触发重抓。

```bash
uv run --no-sync python scripts/mdf_financial_conditions.py registry
uv run --no-sync python scripts/mdf_financial_conditions.py migrate --opt-in-dev-write
uv run --no-sync python scripts/mdf_financial_conditions.py query \
  --start 2026-10-30 --end 2026-11-03 --series DFEDTARL SOFR DGS10
uv run --no-sync python scripts/mdf_financial_conditions.py collect \
  --start 2026-10-30 --end 2026-11-03 --opt-in-dev-write
```

最后一条在本版返回权限阻塞，**不会**取得数值。查询无来源请求或 DDL。
迁移写入 `mdf_financial_conditions_daily_dev_r1`，只创建新命名空间/对象，拒绝不认识的既有
命名空间，不清理历史、不重建全库。受控数据只允许进入 `test_financial_daily_*`，不能进入产品表。

## 时间、缺项与版本

- `observation_date` 是来源参考日。政策日有效不等于政策宣布或 MDF 当时可知。
- FRED `realtime_start/end` 保持日期精度；来源逐点发布时间/可得时间没有证据时为 null。
  metadata 的 last_updated 也不借作每个数值的发布时间。
- `observed_at` 是该数值第一次 MDF 观察，`available_at` 是提交返回后确认的可用时间。
  默认按后者选择截止版本；显式 `basis=observed` 仅描述 MDF 观察史，不是官方历史 vintage。
- 相同输入重放不增经济版本、不重置首次时间；仅数值或点号状态变化增版，原批次和原文可追。
  点号为 `provider_missing`，不能补零。源失败、尚未可得、未到本地截止、日历未知分别返回。
- 确认中断的当前值为 `commit_pending`，旧已确认值只作为 `last_confirmed` 独立提供；
  不把旧版本的时间/材料冒称当前未确认版本。

## 验证边界

确定性检查：`uv run --no-sync pytest -q tests/test_financial_daily.py`。
私网存储检查必须显式开启，默认云 CI 排除：

```bash
MDF_FINANCIAL_DAILY_DEV_OPT_IN=1 uv run --no-sync pytest -q \
  tests/test_financial_daily_dev.py --live-provider -m 'live_provider and postgres_integration'
```

该检查以受控回包及受控修订写真实 PG/Garage，使用独立测试 schema；不是官方来源实采。
本段不提供 workbench/常驻执行，不改变已有十族策略、进程、配置或 DEV 绝对截止。
