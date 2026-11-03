# 来源配置

宏观接入包包含美国 CPI、美国 PPI、美国 JOLTS、美国就业、美国零售销售、美国消费与收入、美国初请与续请失业金、美国 GDP 与增长、美国 PCE价格指数九族。来源页按本次选择标出所需来源，同时保留未启用能力的来源与资产关系。数值通路从现有 canonical 绑定投影，关系缺失时明确显示未装配。

初请与续请的当前数值提供者是 FRED，原始机构是 DOL/ETA，发布日历是 DOL 官方周报日历；CSV 备用数值通路未装配。BLS、BEA、Census 的数值、原始机构与日历分别显示，不将日历可用性当作数值来源资格。

FRED 与 BLS、BEA、Census 共用现有 write-only 替换、CAS 和私有不可变凭据文件。保存后 Host 与验证消费相同有效凭据加载器；当前引用损坏时不回退。替换失败或冲突后须回读，页面不会回显、恢复或自动重发输入的 Key。停用能力保留历史和凭据。

`POST /api/r5/packages/{package_id}/sources/{source_id}/verify` 接受仅包含 `expected_revision` 的 JSON。只有用户显式点击才请求上游，沿用 Host 写权限、受控 Host、同源门禁、能力准入与既有跨进程请求控制。每次检查至多一次实际交换，不自动重试、不采集、不写业务数据库。进程内检查互斥，同配置、同有效凭据的结果在 30 秒内复用。

配置存在、请求成功、认证确认、来源资格和采集准入是不同事实。FRED 的具名元数据成功可确认本次凭据认证；BEA 官方 GetDatasetList 的合法成功结果也可确认 UserID 认证。BEA 明确业务错误 1 表示认证拒绝，错误 7 表示限流；其他错误不推断为 Key 错误。BLS 与 Census 在本检查中保守保持认证未知，Census 当前官方接口同样要求 API key。HTTP 200 仍须通过业务响应检查。403、限流、超时、协议错误分别表达，失败不一律归因于 Key。验证不改变资格、采集权限、策略或已保存数据读取。

结果只保留本次服务进程内；重启后显示尚未验证。结果通过配置版本和有效凭据的私有进程内身份关联当前环境、包及来源；检查期间配置竞争拒绝发布结果，回读发现版本或凭据变化时撤销旧成功。HTTP 响应不含 Key、凭据身份、私有引用、请求 URL、提供方正文或原始异常。

上一段有界 DEV 核验中 FRED 认证确认，BLS 和 BEA 请求成功、当时认证投影未知；该历史观察保持原样。本段仅据官方合同修正 BEA 认证投影，未重新请求上游或回填认证状态。Census 触发既有 `census_retail_duplicate_http_header` 门禁，页面保留协议失败；没有放宽既有协议断言。这不表示 Census 当前请求成功，也不改变其已有数据的本地读取。

认证依据为本事务提供的 [BEA 官方 API 用户指南](https://apps.bea.gov/api/_pdf/bea_web_service_api_user_guide.pdf)（2026-04-20版，第1、4、5页）及 [Census 当前官方示例页](https://api.census.gov/data/timeseries/eits/marts/examples.html)。本段未联网重取文档。验证时间统一通过 `formatBeijingTime` 标明北京时间，不随浏览器时区改变。
