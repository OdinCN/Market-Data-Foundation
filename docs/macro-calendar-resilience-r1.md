# 六族官方日历容错

适用 CPI、PPI、PCE、就业、零售销售、消费与收入的连续时序工作台。只切换同一官方机构的入口，不替换数据提供方，不改变查询参数默认值，不触发初始化或历史补采。

## 官方入口与适用范围

| 来源 / 资产 | 主入口 | 备用入口 | 约束 |
| --- | --- | --- | --- |
| BLS / CPI、PPI、就业 | [BLS ICS](https://www.bls.gov/schedule/news_release/bls.ics) | [CPI](https://www.bls.gov/schedule/news_release/cpi.htm)、[PPI](https://www.bls.gov/schedule/news_release/ppi.htm)、[就业](https://www.bls.gov/schedule/news_release/empSit.htm) | HTML 须明确参考月、日期、时间；已绑定 ICS 的 UID、SEQUENCE、取消与改期记录保留，备用日期不一致或未覆盖的目标暂停 |
| BEA / PCE、消费与收入 | [BEA ICS](https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics) | [完整日历](https://www.bea.gov/news/schedule/full) | 仅 Personal Income and Outlays，明确 Year 表头、参考月及 Eastern 时间；两族共享原始日历缓存，目标和执行记录独立 |
| Census / 零售 | [Advance 日历](https://www.census.gov/retail/release_schedule.html) | [年度经济指标日历](https://www.census.gov/economic-indicators/calendar-listview-2026.html) | 仅 Advance Monthly Sales，使用明确 Period Covered；第四季度最多再检查下一年，缺少合格表格不推算跨年日期 |

日历是计划证据。切换入口、到达计划时刻、成功抓取页面均不等于数据已发布或已入库。现有原始材料与发布事件关联规则保持有效。

## 刷新和缓存

每轮先请求主入口，只有合格失败才请求具名备用入口。BLS/BEA 每轮最多各两个入口；Census 第四季度最多三个入口。Census 仍使用原有共享限额文件，与数据请求共用预算；独立历史日历维护继续遵守同一预算。成功取得的年度页也写入历史日历缓存，避免在同一轮重复取得。

429、供应方限额/冷却、证书/TLS 或来源约束错误不通过改入口绕过。Retry-After 可推迟下次刷新；主入口恢复后自动回到主入口。备用读取失败时不重写上次获取时间。

缓存有效期为实际取得后 24 小时，边界到期即不可触发新的自动采集；时钟倒退不可用。有效缓存可继续支持已确认计划，过期只保留展示。BEA 跨族缓存读取保留真正取得时间，不能延长有效期。既有数值查询和本地历史读取不因日历故障而停用。

备用入口不能撤销主入口已确认的取消、TBA 或改期。冲突/未覆盖月份保留原计划证据并阻止执行，等待主入口重新确认；来源切换不重置目标阶段、请求编号、尝试次数或已完成任务。Census 年度历史记录不会自动变成历史采集积压。

## API 与工作台

现有 `/api/r5/{family}/timeseries/schedule` 的 `calendar` 对象增加：

- `primary_source`、`source`（Census 多页另含 `sources`）：主入口和本次采用入口。
- `refresh_mode`：`primary` / `fallback` / `cache` / `unavailable`；须同时读取 `calendar_status` 判断是否可用。
- `refresh_attempts`：本轮入口、accepted/failed、安全错误分类和 HTTP 状态；不含原始异常或响应正文。
- `primary_error`、`last_refresh_attempt`、`next_refresh_at`、`cache_valid_until`：主入口故障、刷新与有效期。
- `acquired_at` / `fetched_at`：继续表示成功获取时间，失败不能重置。

策略详情直接展示当前入口、缓存有效期、下次刷新；故障分类和 HTTP 状态放在可展开的刷新详情。旧状态文件在下一次显式/worker 刷新时补齐新元数据，GET 不迁移、不联网、不写状态。无需数据库 schema 迁移。

## 验证与真实缺口

离线测试覆盖六族切换、主入口恢复、有效缓存/到期、取消/TBA/改期保护、阶段身份保留、跨年和夏令时，以及 429 不追加备用请求。浏览器检查同时核对实际 schedule 响应和策略详情，不能仅凭接口 200 判断成功。

2026-11-02 DEV 有界来源核验：BLS 三个产品页和 BEA 完整日历可以解析并与 ICS 对照；Census 2026 年度表可解析 13 条 Advance 记录。2027 年度 URL 当次没有合格日历表，因此不声称备用入口覆盖 2026-12 的发布；保留已有该月证据，但备用模式下不据缺失页新增或恢复该月执行。BEA 备用页仅承诺页面明确年份的覆盖，不推算次年时刻。

本轮故障注入、DEV 来源采样和浏览器联调不能代替未来真实发布时的自然运行验收。
