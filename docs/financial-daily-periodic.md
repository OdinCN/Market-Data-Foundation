# 金融条件日频 DEV 周期采集

`mdf_financial_conditions_worker.py` 使用独立状态目录和正常日频作业目录，必须显式 opt-in、允许采集并提供既有 45 日窗口文件。页面读取、部署与轮询均不启用策略或启动服务。周期页“自动采集”开启先检查既有 FRED 凭据绑定、固定单元、实际进程与心跳，再保存该单项意图；关闭只停止后续触发，在途作业收尾，人工采集独立。失败/未知独立核实，客户端不提供 unit、命令或路径。

MDF 维护检查采用 America/New_York（随 DST 切换）：政策上下限每日 09:10/14:45；EFFR/SOFR 工作日 09:15/14:45；财政部及 MDF 派生工作日 18:30；IG/HY 工作日 10:30/19:00。它们是维护时点，不是官方公布承诺。纽约联储通常于 08:00 发布 SOFR、09:00 发布 EFFR，可能于 14:30 同日修订；财政部利率依据约 15:30 的指示性收盘报价，并未据此假定网页实际可得时刻。政策按具体变更生效日投影。信用更新使用既有 FRED 显式绑定，不重复归档初始化；周末月末值在下次回看时保留。

依据：[纽约联储 SOFR](https://www.newyorkfed.org/markets/reference-rates/sofr/)、[参考利率说明](https://www.newyorkfed.org/markets/reference-rates/additional-information-about-reference-rates)、[财政部利率统计](https://home.treasury.gov/policy-issues/financing-the-government/interest-rate-statistics/)。没有将来源节假日或缺值补成观测。

每组取近 14 日，派生步骤在两条输入持久化后执行，保留同截止腿版本。每个组/维护时点的 UUID 固定；成功时点不再重复取数。部分失败按来源 retry_at 与指数退避中较晚者续跑同一作业，最多三次/时点；只重做未完成段，原件已留存则恢复提交不重抓。下一时点照常检查近期区间，原失败事实留存；长于 14 日的暂停缺口由人工指定区间恢复，不伪称全历史覆盖。人工/周期作业共用互斥锁，忙时不消耗上游重试次数。重启读取磁盘意图与任务，不重设时点身份。

接口为既有 `/api/r5/financial/timeseries/strategy` GET/POST，POST body 为 `{"enabled":true|false}`，明确异常恢复可加 `"recover":true`（只允许 enabled=true）。正常私有写准入复用现有 Host/Origin 规则；公开 Host 仍只读。GET `/jobs` 与 `/jobs/{UUID}` 可追踪 `origin=periodic`、schedule_slot、所选指标、逐段来源/原件/PG 确认；本地 `/summary`、`/query` 不发上游请求。心跳线程独立于阻塞采集，在线状态不代替采集成功。

到期以原窗口的绝对截止为准：启动 guard、运行时检查、systemd RuntimeMaxSec 以及原 expire timer 的实际停止集合共同约束新增单元；不重算窗口。
