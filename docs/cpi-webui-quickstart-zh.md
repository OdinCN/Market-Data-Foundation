# CPI WebUI 快速上手

开发与审查先读 [五族基线指南](macro-family-development-review.md)；下文保留 CPI 操作说明。

本基线已初步验收 CPI 查询、采集、预置周期策略、执行记录与 API 说明；兼容共用 WebUI 的既有 PCE 读取。
范围仅为 CPI 样本，不代表全部 MACRO 完成或生产部署；历史 live 验收不承诺当前服务可用。

从仓库根准备环境（uv 0.12.22、uv 管理的 Python 3.14.8，Node/npm 须满足 web 依赖）：

```sh
uv sync --locked --group dev
export PYTHONPATH="$PWD/src"
uv run --locked python scripts/mdf_cpi_timeseries_web.py --help
cd web
npm ci
npm run check
npm test
npm run build
```

以上只准备环境、查看参数与生成静态资源，不启动后端。DEV 启动需用户自备配置目录、package store、
环境 ID、数据库身份、绝对 schedule-path 和 job-directory；采集还需显式许可及有效 BLS 绑定。
入口为部署者提供的基址加 `/pce`；地址、端口和 Origin 是 DEV 部署配置。
当前启动器的监听/Origin 约束见 `src/market_data_foundation/web/pce_dev_host.py`，不能仅替换 URL 假定可迁移。
本文不指定固定内网或生产 endpoint，也不承诺有常驻 worker。

1. **数据资产 → CPI → 查询数据**：选择月份、篮子与季调，查看请求预览；显示指标只控制表格列。
2. **当前查询**：本地优先，真实缺口可临时请求上游，仅合并本次响应，不落业务库或原始材料。
3. **历史查询**：strict 仅按本地 available_at 截止，截止输入使用北京时间 +08:00；无证据则缺失，不请求上游补过去。
4. **四处图标复制**：请求预览 URL、cURL、本次请求 URL、原始响应；复制失败时选中文本后按 Ctrl+C（Mac：⌘C）。
5. **查询结果**：表格/原始响应切换不再请求；保留当次 URL、状态码、耗时和真实 HTTP 原文，错误响应也可查看。
6. **采集数据**：初始化预置 240 个月且可修改，按需显式更新；右侧简报可定位执行详情，查询成功不代表已回补本地。
7. **预置周期策略**：在策略列表启停、查看月份计划；需显式启用并由独立 worker 执行，状态页不能证明 worker 常驻。
8. **执行记录**：区分手动任务、周期阶段和失败原因；HTTP 成功不等于目标月可用或历史核对完成。
9. **API 说明**：查看参数默认值、类型、单位、null、当前/历史查询、缺口补取与示例；接口标为开发接口。
10. **状态提示**：“暂未刷新”表示状态读取尚未成功，不代表 CPI 数据过期；可点“重新核实”，资产页不承诺自动轮询。

时间点展示采用 Asia/Shanghai（+08:00），后端可存储/返回 UTC；月份 YYYY-MM 是参考月，不是时点。
初始化本地 revision=1 不等于官方首发；官方全部 vintage 查询仍 unsupported，真实未来发布事件仍待自然验证。

Web API 前缀为 `/api/r5/cpi/timeseries`：GET `status/query/schedule/strategy/jobs/executions`，
POST `jobs` 提交采集，PUT `strategy` 启停策略；均受 DEV 准入及能力绑定约束。
CLI 本机 API 前缀为 `/api/cpi`，采集使用 POST `init/update`，不能与 Web 路径混用。
用户短只读清单、参数和命令见 [CLI / 本机 API](cpi-timeseries-cli-zh.md#最小只读验收清单)，
查询与历史边界见 [发布历史设计](macro-publication-history-design.md)。
