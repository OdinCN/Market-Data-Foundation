# 宏观资格合同 R1

宏观资格复用 canonical Fixture、Binding、Evidence 和 observe，不加入证券资格的冻结能力集合。
`Capability` 保持原16项；Fuyao/TDX 的预算、矩阵、历史报告、fixture 与观察v2均保持原合同。

- 宏观能力来自 `MacroCapability`，当前只有 `TIMESERIES=macro.timeseries`。
- `provider_lab/macro-fixtures.toml` 使用 fixture格式v1及显式 `capability_scope="macro"`，
  由 `load_macro_fixtures` 加载；旧 `load_fixtures` 只接受无scope的证券文件，两者不能混读。
- 宏观观察使用 `schema_version=3`、`capability_scope="macro"`，由
  `provider_lab/macro-observation.schema.json` 验证。证券观察继续输出v2且不增加scope字段；
  `provider_lab/fixtures.toml` 和 `provider_lab/observation.schema.json` 保留冻结字节。
- live Binding 仍需显式 `allow_live=True`；capability不匹配在执行前拒绝。失败只留类型/分类，
  不留原始异常消息、凭据、运行状态或生产数据。

JOLTS fixture声明八个全国非农SA原生ID；声明和合成回归不表示来源已合格。
定义/方法及具名发布材料缺口继续保留，`source_qualified=false`。
旧段已产生的宏观v2观察不重新解释为证券资格成功；其传输原文、403事实和真实取得时间保留，
需要适配时另存有明确原记录引用的离线v3观察，不倒填业务可用时间，也不触发新上游请求。
