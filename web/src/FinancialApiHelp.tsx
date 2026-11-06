import { CodeBlock, CompactTable, RequestPreview } from "./CpiQueryDetails";
import {
  FINANCIAL_PREFIX as prefix,
  financialQuerySuffix,
} from "./financialQuery";
import example from "./financial-query-example.json";

export type FinancialHelp = {
  endpoints?: Record<string, string>;
  notes?: string[];
};
const metrics = [
  ["DFEDTARL", "政策目标下限", "percent；按具体生效日投影，含周末"],
  ["DFEDTARU", "政策目标上限", "percent；按具体生效日投影，含周末"],
  ["EFFR", "有效联邦基金利率", "percent；纽约联储观测日"],
  ["SOFR", "担保隔夜融资利率", "percent；2018-04-02前无本来源历史"],
  ["DGS2", "2年名义国债收益率", "percent；财政部观测日"],
  ["DGS10", "10年名义国债收益率", "percent；财政部观测日"],
  ["DFII10", "10年实际国债收益率", "percent；财政部观测日"],
  ["T10YIE", "MDF通胀补偿", "percent；同日名义减实际收益率"],
  ["BAMLC0A0CM", "ICE投资级OAS", "percent；另有basis_points；保留月末周末观测"],
  [
    "BAMLH0A0HYM2",
    "ICE高收益OAS",
    "percent；另有basis_points；保留月末周末观测",
  ],
];

export function FinancialApiHelp({
  help,
  start,
  end,
  series,
  cutoff,
  offset,
}: {
  help: FinancialHelp | null;
  start: string;
  end: string;
  series: string;
  cutoff: string;
  offset: number;
}) {
  let path: string | null = null;
  try {
    path =
      prefix + financialQuerySuffix({ start, end, series, cutoff, offset });
  } catch {
    /* Invalid draft: do not invent a valid request. */
  }
  return (
    <section
      aria-label="金融日频 API说明"
      className="cpi-api-help financial-api-help"
    >
      <h4>GET {prefix}/query</h4>
      <p>
        按观测日和 MDF 确认可用截止读取本地版本。GET
        不采集、不写库、不建表，上游请求数为0。日期、指标、截止和分页与当前查询表单共用同一请求生成器。
      </p>
      {path ? (
        <RequestPreview request={{ method: "GET", path }} />
      ) : (
        <p role="status">
          当前截止时间格式无效，请回到查询数据核对；未生成或执行请求。
        </p>
      )}
      <CompactTable
        label="查询参数"
        headers={["参数", "必填 / 默认", "类型、含义与边界"]}
        rows={[
          ["start", "是", "YYYY-MM-DD观测日，包含起日；必须为真实日期。"],
          [
            "end",
            "是",
            "YYYY-MM-DD，包含止日；start≤end，日期差最多366×30日。查询未来日仍可能返回缺项，不生成未来值。",
          ],
          [
            "series",
            "否 / 十项",
            "逗号分隔的精确代码；至少一项、无重复、不加空格。当前表单选择一个指标，API可选多项。",
          ],
          [
            "as_of",
            "否 / 服务读取时刻",
            "含时区ISO8601的MDF确认可用截止。表单北京时间转UTC；留空则请求不带此参数。不是官方历史首发时刻。",
          ],
          ["offset", "否 / 0", "非负整数；日历行偏移，超出total返回空rows。"],
          [
            "limit",
            "否 / 200",
            "整数1..1000；当前表单使用200。按所选序列顺序、序列内日期升序分页。",
          ],
        ]}
      />
      <p>
        只支持以上六个参数；重复参数和未知参数会被拒绝。不支持月频的basket、measure、columns或单位转换、来源切换参数。total为包含端点的日历天数×所选序列数，包含缺值行，不能当作成功观测数。
      </p>
      <details>
        <summary>十条序列、类型与单位</summary>
        <CompactTable
          label="指标与单位"
          headers={["series", "指标", "单位与日历"]}
          rows={metrics}
        />
        <p>
          value为十进制字符串或null，0是有效值。信用basis_points=value×100；其他八项该字段为null。非观测日和短历史不补零、不前向填值；政策投影有独立生效日依据。
        </p>
      </details>
      <details>
        <summary>返回字段、类型与单位</summary>
        <CompactTable
          label="返回字段"
          headers={["字段", "类型 / nullable", "含义"]}
          rows={[
            [
              "status / rows",
              "string / object[]",
              "成功为OK；rows按日历行返回。",
            ],
            ["as_of", "string", "本次实际采用的含时区MDF可用截止。"],
            [
              "total / offset / next_offset",
              "integer / integer / integer|null",
              "本地日历行总量、当前偏移及下一页；末页next_offset=null。",
            ],
            [
              "provider_requests",
              "integer；恒0",
              "本次本地查询不产生来源请求。",
            ],
            [
              "coverage",
              "object",
              "同截止、完整请求区间的覆盖；series数组始终含十项，不受查询series过滤。",
            ],
            [
              "coverage.as_of / provider_requests / series[]",
              "string / integer / object[]",
              "截止、0请求及逐序列覆盖：series_id、requested_start/end、returned_days、valid_rows、first/last、explicit_missing、calendar_days_without_row、gap_explanation。first/last可null。",
            ],
            [
              "rows[].dataset_id / definition_revision",
              "string / string",
              "稳定数据集mdf.macro.financial-conditions.daily及定义r1。",
            ],
            [
              "series_id / observation_date / state",
              "string / string / string",
              "精确序列、YYYY-MM-DD观测日、该日事实分类；不是发布时间。",
            ],
            [
              "value / unit / basis_points",
              "decimal string|null / string / decimal string|null",
              "unit=percent；信用额外返回bp。缺值是null，禁止补零。",
            ],
            [
              "revision / batch_id",
              "integer|null / string|null",
              "本地修订序号与留存批次；不证明官方vintage。",
            ],
            [
              "observed_at / available_at",
              "ISO string|null / ISO string|null",
              "MDF首次观察与确认可用时间，二者独立。早期截止不借后来数据。",
            ],
            [
              "source_publication_at / source_available_at / policy_announcement_at",
              "null（当前实现）",
              "未取得具名历史时点证据，不推造。",
            ],
            [
              "policy_effective_date",
              "date string|null",
              "政策目标的具体生效日；不等于每日观察或MDF可用时刻。",
            ],
            [
              "origin / identity",
              "string|null / string|null",
              "原机构、政策投影、credit_archive、fred或mdf_derived等来源身份。受控样本会标controlled_fixture。",
            ],
            [
              "original_agency / distributor / source_url / attribution",
              "string",
              "原发布机构或指数提供者、实际分发者、出处及所需引用。",
            ],
            [
              "source_binding / source_request / provider_extra",
              "object / object|null / object|null",
              "具名绑定、实际请求依据、来源特有信息；派生输入版本见绑定与原件。",
            ],
            [
              "is_copyrighted",
              "boolean|null",
              "已取得FRED元数据的copyright识别；未知保留null。",
            ],
            [
              "raw_locator / metadata_locator",
              "string|null / string|null",
              "已留存原件与元数据内容定位。",
            ],
            [
              "coverage_evidence / last_confirmed",
              "object|null / object|null",
              "缺项关联留存依据；提交待确认时可附上次确认值，当前value仍为null。",
            ],
            [
              "latest_request_state / latest_request_classification",
              "string|null / string|null",
              "最近请求状态与失败分类；已持有数值不自动证明本次请求成功。",
            ],
          ]}
        />
      </details>
      <details>
        <summary>缺值、状态与历史截止</summary>
        <CompactTable
          label="日频状态"
          headers={["state", "解释 / 下一步"]}
          rows={[
            [
              "observed / provider_missing",
              "有本地有值版本 / 来源明确点号缺值；后者value=null。",
            ],
            [
              "non_observation_day",
              "有依据的非观测日，不补零；信用保留有据月末周末。",
            ],
            [
              "not_known_at_cutoff / not_collected",
              "该截止尚无已知批次 / 所选日期尚未采集；检查截止或显式人工补取。",
            ],
            [
              "outside_provider_range / not_yet_available_from_provider",
              "有据来源区间之外 / 尚未可得；按真实短历史或后续来源公布处理。",
            ],
            [
              "provider_field_absent / not_returned_calendar_unconfirmed",
              "原件缺少该字段 / 没有返回且日历未确认，不能直接认作周末。",
            ],
            [
              "request_failed / request_outcome_unknown",
              "请求失败 / 结果未知；看最新请求分类和现有作业，先核实再恢复。",
            ],
            [
              "commit_pending / derived_inputs_unavailable",
              "持久确认待恢复 / 同日同截止派生输入不足；当前不输出未经确认的值。",
            ],
            [
              "source_permission_required",
              "该具名通路缺少所需许可；不静默换源。",
            ],
          ]}
        />
        <p>
          原文公布时间、MDF观察、MDF确认可用、政策生效各自独立。后来回补不能证明历史初值或历史首发；已有数值、执行成功、无新增和自然发布捕获也分别结算。
        </p>
      </details>
      <details>
        <summary>来源与MDF计算</summary>
        <p>
          政策目标上下限来自联储理事会具体政策生效记录；EFFR/SOFR来自纽约联储；DGS2/DGS10/DFII10来自美国财政部。以上FRED代码仅为指标映射。
        </p>
        <p>
          MDF派生T10YIE=DGS10−DFII10，采用同观测日、同一截止可得的两条财政部输入并保留两腿版本；它是收益率差形成的通胀补偿，不是纯调查预期或FRED实采。
        </p>
        <p>
          IG/HY使用Owner固定的PStoclet
          GitHub信用归档初始化，FRED显式补尾与增量，指数提供者为ICE。逐点实际origin、分发者与引用以返回绑定及留存原件为准。历史归档不证明历史首发。
        </p>
        {help?.notes?.map((note) => (
          <p key={note}>{note}</p>
        ))}
      </details>
      <details>
        <summary>成功示例（合成合同样本）</summary>
        <p>
          以下由隔离产品路由及受控数据生成，含有值和来源点号缺值；不是现场数值、原始发布或当前筛选结果。复制仅复制文本。
        </p>
        <CodeBlock
          text={JSON.stringify(example, null, 2)}
          copyLabel="复制成功示例JSON"
        />
      </details>
      <details>
        <summary>失败示例与诊断</summary>
        <CompactTable
          label="失败诊断"
          headers={["HTTP / classification", "含义与下一步"]}
          rows={[
            [
              "400 financial_query_invalid / financial_date_invalid",
              "检查必填日期、真实日期、重复/未知参数、整数格式、起止顺序、日期差≤366×30、offset≥0和limit=1..1000。当前query统一将区间/分页越界及未装配读能力归为financial_query_invalid。",
            ],
            [
              "400 financial_series_invalid / financial_instant_invalid",
              "检查精确代码、无重复和含时区的as_of。",
            ],
            [
              "403 financial_capability_not_enabled / operator_not_authorized",
              "其他读入口未准入 / 敏感写不在已准入私网Host、Origin或能力内。query未装配读能力时使用上列400分类。",
            ],
            [
              "404 financial_job_not_found / 409 financial_request_id_conflict",
              "核对原任务UUID / 同UUID只能对应原请求。",
            ],
            [
              "503 financial_local_unavailable / financial_worker_awaiting_heartbeat",
              "本地存储或服务未核实 / 执行器未读回在线；查看真实状态，不宣称恢复成功。",
            ],
          ]}
        />
        <p>合成失败示例（无输入或凭据回显）：</p>
        <CodeBlock
          text={JSON.stringify(
            {
              status: "UNKNOWN",
              classification: "financial_query_invalid",
            },
            null,
            2,
          )}
          copyLabel="复制失败示例JSON"
        />
        <p>
          网络失败可能没有HTTP状态或正文。写入/启动结果未知时，先GET原作业或strategy核实，不自动重复启动或另建作业。复制示例不执行请求。
        </p>
      </details>
      <details>
        <summary>采集、恢复与周期接口</summary>
        <CompactTable
          label="采集与周期接口"
          headers={["方法 / 路径", "真实合同"]}
          rows={[
            [
              `GET ${prefix}/status`,
              "十条指标与来源绑定、观测日时区、/status.defaults的可编辑20日历年默认窗口。默认止日不保证当日已公布。",
            ],
            [
              `GET ${prefix}/summary`,
              "本地已持有覆盖与最后确认可用时刻；不触发采集。",
            ],
            [
              `GET ${prefix}/jobs`,
              "已有作业，provider_requests=0；任务内累计来源请求另读。",
            ],
            [
              `GET ${prefix}/jobs/{request_id}`,
              "精确原任务UUID；可查manual/periodic、范围、步骤、来源与持久覆盖。",
            ],
            [
              `POST ${prefix}/jobs`,
              "仅request_id/action/start/end四字段。action=init|update，覆盖十项；不接受series。规范小写UUID，同ID同请求幂等返回原任务，改范围/action则409。起止日可编辑，包含端点的日历天数≤366×30，结束日不得晚于北京时间当前日。",
            ],
            [
              `POST ${prefix}/jobs/resume`,
              "仅request_id；恢复同一任务未完成段，保留已确认数值、原件与首次可用时间。不是新初始化或全量历史重抓。",
            ],
            [
              `GET ${prefix}/strategy`,
              "真实策略、分组下次检查、worker/control与最近成功；开启意图、在线状态与数据成功分别读取。",
            ],
            [
              `POST ${prefix}/strategy`,
              "{enabled:true}启用并核实必要执行器；{enabled:false}暂停后续自动触发，已有任务正常收尾；{enabled:true,recover:true}恢复并回读。使用现有受控DEV单元、原到期guard；不接受任意命令。",
            ],
            [
              `GET ${prefix}/api-help`,
              "读取当前服务接口合同与来源/历史截止提示，不执行采集。",
            ],
          ]}
        />
        <p>
          POST仅正常已准入私网写入口可执行，公开Host保持只读。以下是合成请求体示例，不是本次要提交的任务；复制不会执行写操作。
        </p>
        <CodeBlock
          text={JSON.stringify(
            {
              request_id: "00000000-0000-4000-8000-000000000001",
              action: "update",
              start: "2026-10-30",
              end: "2026-11-03",
            },
            null,
            2,
          )}
          copyLabel="复制采集请求体示例"
        />
        <CodeBlock
          text={JSON.stringify(
            { request_id: "00000000-0000-4000-8000-000000000001" },
            null,
            2,
          )}
          copyLabel="复制恢复请求体示例"
        />
        <CodeBlock
          text={JSON.stringify({ enabled: true, recover: true }, null, 2)}
          copyLabel="复制周期恢复请求体示例"
        />
      </details>
    </section>
  );
}
